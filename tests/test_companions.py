from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys
from dataclasses import replace

import pytest

from autoform_cli import companions
from autoform_cli.__main__ import main
from autoform_cli.companion_sources import CompanionImportError
from autoform_cli.graph import load_graph
from autoform_cli.markdown import site_converter
from autoform_cli.render import PublicationError, render_site
from autoform_cli.runtime import load_runtime_graph
from autoform_cli.scaffold import scaffold_project


@pytest.fixture(autouse=True)
def reference():
    package = pytest.importorskip("formal_companion_annotations")
    if package.__version__ != "2.0.0a2":
        pytest.skip("requires the optional FCA 2.0.0a2 reference checker")
    return importlib.import_module("formal_companion_annotations.discovery")


@pytest.fixture
def consumer(tmp_path):
    project = tmp_path / "independent-consumer"
    scaffold_project(project, title="Native attachment consumer")
    article = project / "blueprint/roadmap/result.md"
    article.write_text("---\narticle_id: af_0123456789abcdef01234567\ndeclaration: theorem\n---\n\n# Result\n\n"
                       "Independent mathematical prose.\n\n## Depends on\n\nNo prerequisites.\n")
    chapter = project / "blueprint/roadmap/README.md"
    chapter.write_text("---\narticle_id: af_1123456789abcdef01234567\n---\n\n# Roadmap\n\n- [Result](result.md)\n")
    return project, article


def annotation(uri="https://example.invalid/wiki/result", target="https://example.invalid/lean#result", **extra):
    return {"source": {"type": "generic", "uri": uri}, "target": target, **extra}


def entry(value=None, *, base="https://example.invalid/companions/shard.json"):
    return {"annotation": value or annotation(), "origin": {"base_uri": base, "pointer": "/items/0", "line": 7}}


def capture(values):
    return companions.capture_retained_records(values)


def configure(project, article, tmp_path, entries, *, article_id=False):
    snapshot = capture(entries)
    root = tmp_path / "retained"
    companions.write_retained_annotations(snapshot, output=root)
    binding = {"source": entries[0]["annotation"]["source"]["uri"],
               "sha256": hashlib.sha256(article.read_bytes()).hexdigest()}
    binding["article_id" if article_id else "article"] = (
        "af_0123456789abcdef01234567" if article_id else article.relative_to(project).as_posix())
    config = {"archives": [str(root / "records.jsonl")], "bindings": [binding]}
    (project / companions.CONFIG_NAME).write_text(json.dumps(config))
    return config


def test_native_capture_retains_all_modes_duplicates_unknown_values_and_origins(tmp_path):
    values = [
        entry({"source": {"type": "pdf", "uri": "../book.pdf"}, "target": "#theorem", "id": "repeat",
               "location": {"page": 2, "rect": [0.1, 0.2, 0.3, 0.4]}}),
        entry({"source": {"type": "latex", "uri": "../book.tex"}, "target": "urn:lean:Result",
               "location": {"lines": [3, 9]}, "extensions": {"future": {"count": 2**100}}}),
        entry({"source": {"type": "html", "uri": "../article.html"}, "target": "//formal.invalid/Result?",
               "location": {"fragment": "literal#id"}}),
        entry(annotation("../article.md", "../Lean.lean#L2", location={}, id="repeat",
                         extensions={"future": {"uri": "../opaque", "x": [True, None, 2**100]}})),
    ]
    values.append(values[0])
    snapshot = capture(values)
    out = tmp_path / "archive"
    report = companions.write_retained_annotations(snapshot, output=out)
    saved = companions.read_retained_annotations(out / "records.jsonl")
    assert [r.entry() for r in saved.records] == values
    assert report["annotations"] == 5
    assert report["revision"] == saved.revision
    portable = tmp_path / "portable"
    report = companions.export_retained_annotations(saved, output=portable)
    reexport = [json.loads(line) for line in (portable / report["shards"]).read_text().splitlines()]
    assert len(reexport) == 5
    assert reexport[0] == reexport[4]
    assert reexport[0]["source"]["uri"] == "https://example.invalid/book.pdf"
    assert reexport[0]["target"] == "https://example.invalid/companions/shard.json#theorem"
    assert reexport[3]["location"] == {}
    assert reexport[3]["extensions"] == values[3]["annotation"]["extensions"]
    assert [r.entry() for r in companions.read_retained_annotations(portable / "records.jsonl").records] == values
    checker = importlib.import_module("formal_companion_annotations.companion")
    assert all(not checker.check(v).problems for v in reexport)


def test_capture_detaches_mutable_values_and_rechecks_forged_snapshots(tmp_path):
    values = [entry(annotation(extensions={"extra": [1]}))]
    snapshot = capture(values)
    values[0]["annotation"]["extensions"]["extra"].append(9)
    snapshot.records[0].annotation["extensions"]["extra"].append(3)
    assert snapshot.records[0].annotation["extensions"]["extra"] == [1]
    forged = replace(snapshot.records[0], annotation_json='{"version":2,"source":{},"target":"urn:x"}')
    with pytest.raises(CompanionImportError, match="reference validation"):
        companions.write_retained_annotations(companions.RetainedAnnotations((forged,)), output=tmp_path / "bad")
    assert not (tmp_path / "bad").exists()


def test_captured_files_survive_source_replacement_and_archive_relocation(reference, tmp_path):
    shard = tmp_path / "shard.json"
    value = annotation("./article.md", "./target#x")
    shard.write_text(json.dumps(value))
    snapshot = companions.capture_annotations(reference.collect([shard]))
    shard.unlink()
    shard.symlink_to(tmp_path / "nonexistent")
    companions.write_retained_annotations(snapshot, output=tmp_path / "old")
    (tmp_path / "old").rename(tmp_path / "relocated")
    retained = companions.read_retained_annotations(tmp_path / "relocated/records.jsonl")
    assert retained.records[0].origin["base_uri"] == shard.as_uri()
    assert retained.records[0].annotation == value
    companions.export_retained_annotations(retained, output=tmp_path / "export")
    portable = json.loads((tmp_path / "export/annotations/links.jsonl").read_text())
    assert portable["source"]["uri"] == (tmp_path / "article.md").as_uri()


@pytest.mark.parametrize("reference,expected", [
    ("../x", "https://Example.invalid/a/x"),
    ("?", "https://Example.invalid/a/b/shard.json?"),
    ("#", "https://Example.invalid/a/b/shard.json?old#"),
    ("//Host.invalid/a//b/../c?#", "https://Host.invalid/a//c?#"),
    ("./%2e%2e/é.md", "https://Example.invalid/a/b/%2e%2e/é.md"),
    ("HTTPS://HOST.invalid/a/../b?#", "HTTPS://HOST.invalid/a/../b?#"),
    ("https:opaque", "https:opaque"),
    ("urn:lean:thing", "urn:lean:thing"),
])
def test_reference_resolution_preserves_lexical_identity(reference, expected):
    assert companions.resolve_reference("https://Example.invalid/a/b/shard.json?old", reference) == expected


def test_opaque_base_and_malformed_relative_authority_fail_before_output(tmp_path):
    for index, values in enumerate(([entry(annotation("relative"), base="urn:carrier:opaque")],
                                    [entry(annotation("//[bad/x"))])):
        with pytest.raises(CompanionImportError):
            companions.export_retained_annotations(capture(values), output=tmp_path / str(index))
        assert not (tmp_path / str(index)).exists()


@pytest.mark.parametrize("origin", [
    {"base_uri": "relative", "pointer": ""},
    {"base_uri": "https://example.invalid/shard", "pointer": "bad"},
    {"base_uri": "https://example.invalid/shard", "pointer": "", "line": True},
    {"base_uri": "https://example.invalid/shard", "pointer": "", "line": 0},
])
def test_malformed_origin_is_rejected(origin):
    with pytest.raises(CompanionImportError):
        capture([{"annotation": annotation(), "origin": origin}])


def test_archive_strict_json_and_atomic_no_overwrite(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"annotation":{},"annotation":{},"origin":{}}\n')
    with pytest.raises(CompanionImportError, match="duplicate key"):
        companions.read_retained_annotations(bad)
    out = tmp_path / "retained"
    snapshot = capture([entry()])
    companions.write_retained_annotations(snapshot, output=out)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    with pytest.raises(CompanionImportError, match="already exists"):
        companions.write_retained_annotations(capture([entry(annotation(target="urn:different"))]), output=out)
    assert before == {p.name: p.read_bytes() for p in out.iterdir()}
    again = tmp_path / "same"
    companions.write_retained_annotations(snapshot, output=again)
    assert before == {p.name: p.read_bytes() for p in again.iterdir()}


def test_original_article_receives_attachments_without_workflow_changes(consumer, tmp_path):
    project, article = consumer
    values = [entry(annotation(location={"opaque": "selection"}, extensions={"proof": "formalized"})), entry()]
    configure(project, article, tmp_path, values)
    graph, runtime = load_graph(project / "blueprint"), load_runtime_graph(project)
    authored = {p: p.read_bytes() for p in (project / "blueprint").rglob("*.md")}
    output = tmp_path / "site"
    render_site(project / "blueprint", output, lean_root=project)
    assert not (output / "roadmap/result.md").exists()  # actual article is embedded in its chapter
    chapter = (output / "roadmap/README.md").read_text()
    assert 'data-companion-state="bound-revision"' in chapter
    assert chapter.count('class="autoform-companion"') == 2
    assert "Independent mathematical prose." in chapter
    assert "generic locations remain opaque" in chapter
    index = (output / "companions.md").read_text()
    assert "roadmap/index.html#result" in index
    assert index.count("## Informal resource") == 1
    assert str(tmp_path) not in chapter + index
    assert "Companion sources" in (output / "SUMMARY.md").read_text()
    assert json.loads((output / "publication.json").read_text())["companions"]["annotations"] == 2
    assert authored == {p: p.read_bytes() for p in authored}
    assert graph == load_graph(project / "blueprint")
    assert runtime == load_runtime_graph(project)
    rendered = site_converter().convert(chapter)
    assert 'class="autoform-companion"' in rendered


def test_article_id_follows_unchanged_rename_but_revision_change_is_historical(consumer, tmp_path):
    project, article = consumer
    configure(project, article, tmp_path, [entry()], article_id=True)
    renamed = article.with_name("renamed.md")
    article.rename(renamed)
    chapter = project / "blueprint/roadmap/README.md"
    chapter.write_text(chapter.read_text().replace("result.md", "renamed.md"))
    report = companions.inspect_project_companions(project)
    assert report["attachments"][0]["article"] == "renamed"
    assert report["attachments"][0]["state"] == "bound-revision"
    renamed.write_text(renamed.read_text().replace("Independent", "Revised"))
    report = companions.inspect_project_companions(project)
    assert report["attachments"][0]["state"] == "historical-revision"
    render_site(project / "blueprint", tmp_path / "site", lean_root=project)
    assert "Historical association" in (tmp_path / "site/roadmap/README.md").read_text()


def test_missing_binding_never_matches_by_title_or_fca_article_id(consumer, tmp_path):
    project, article = consumer
    values = [entry(annotation(extensions={"org.autoform": {"article_id": "af_0123456789abcdef01234567"}}))]
    config = configure(project, article, tmp_path, values)
    config["bindings"][0]["source"] = "https://example.invalid/different-revision"
    (project / companions.CONFIG_NAME).write_text(json.dumps(config))
    report = companions.inspect_project_companions(project)
    assert report["attachments"][0]["article"] is None
    assert report["attachments"][0]["state"] == "external-source"


def test_pdf_is_not_a_markdown_article_even_with_binding(consumer, tmp_path):
    project, article = consumer
    value = {"source": {"type": "pdf", "uri": "https://example.invalid/paper.pdf"},
             "target": "urn:lean:Result", "location": {"page": 1}}
    configure(project, article, tmp_path, [entry(value)])
    report = companions.inspect_project_companions(project)
    assert report["attachments"][0]["state"] == "different-representation"
    assert report["attachments"][0]["article"] is None


@pytest.mark.parametrize("change", [
    lambda c: c["bindings"][0].pop("sha256"),
    lambda c: c["bindings"][0].update(article="../escape.md"),
    lambda c: c["bindings"].append(c["bindings"][0]),
    lambda c: c.update(typo_roots=[]),
    lambda c: c.update(roots="not a list"),
])
def test_config_rejects_ambiguous_or_unsafe_bindings_before_render_cleanup(consumer, tmp_path, change):
    project, article = consumer
    config = configure(project, article, tmp_path, [entry()])
    output = tmp_path / "site"
    render_site(project / "blueprint", output, lean_root=project)
    before = {p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()}
    change(config)
    (project / companions.CONFIG_NAME).write_text(json.dumps(config))
    with pytest.raises(PublicationError):
        render_site(project / "blueprint", output, lean_root=project)
    assert before == {p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()}


def test_config_selects_arbitrary_roots_relative_to_config_and_filters(consumer, tmp_path):
    project, _ = consumer
    elsewhere = tmp_path / "arbitrary-roots"
    (elsewhere / "deep").mkdir(parents=True)
    (elsewhere / "deep/links.jsonl").write_text(json.dumps(annotation()) + "\n")
    (elsewhere / "package.json").write_text('{"not":"an annotation"}')
    config = tmp_path / "selected.json"
    config.write_text(json.dumps({"roots": ["arbitrary-roots"], "include": ["*.jsonl"]}))
    selected = companions.load_project_companions(project, config=config)
    assert len(selected.snapshot.records) == 1
    assert selected.snapshot.records[0].origin["line"] == 1


def test_untrusted_html_scripts_and_producer_status_remain_inert(consumer, tmp_path):
    project, article = consumer
    malicious = '</code></pre></details><script>alert(1)</script>\n[x](javascript:alert(1))'
    value = annotation("javascript:alert(1)", "data:text/html,<script>alert(2)</script>",
                       location={"evil": malicious}, id=malicious, extensions={"status": "proved", "html": malicious})
    configure(project, article, tmp_path, [entry(value)])
    render_site(project / "blueprint", tmp_path / "site", lean_root=project)
    page = (tmp_path / "site/roadmap/README.md").read_text()
    rendered = site_converter().convert(page)
    assert "<script>" not in rendered
    assert 'href="javascript:' not in rendered
    assert 'href="data:' not in rendered
    assert "&lt;script&gt;" in rendered
    assert next(n for n in load_runtime_graph(project).nodes if n.id == "result").assertions.proof_formalized is False


def test_relative_http_links_are_resolved_without_publishing_private_origin():
    attachment = companions.Attachment(1, "https://example.invalid/article", None, "external-source",
                                      capture([entry(annotation("../article", "../formal#d"))]).records[0])
    rendered = site_converter().convert(companions.render_attachment(attachment))
    assert 'href="https://example.invalid/article"' in rendered
    assert 'href="https://example.invalid/formal#d"' in rendered
    assert '"pointer"' not in rendered


def test_article_mutation_during_render_never_completes_publication(consumer, tmp_path, monkeypatch):
    import autoform_cli.render as renderer
    project, article = consumer
    configure(project, article, tmp_path, [entry()])
    original = renderer._render_environment
    def mutate(*args, **kwargs):
        result = original(*args, **kwargs)
        article.write_text(article.read_text() + "\nChanged mid-render.\n")
        return result
    monkeypatch.setattr(renderer, "_render_environment", mutate)
    with pytest.raises(PublicationError, match="article changed"):
        render_site(project / "blueprint", tmp_path / "site", lean_root=project)
    assert json.loads((tmp_path / "site/publication.json").read_text())["complete"] is False


def test_native_cli_import_inspect_export_and_no_implicit_catalog(consumer, tmp_path, capsys):
    project, article = consumer
    shard = tmp_path / "links.jsonl"
    value = annotation(location={})
    shard.write_text(json.dumps(value) + "\n" + json.dumps(value) + "\n")
    output = tmp_path / "retained"
    assert main(["companions", "import", str(shard), "--output", str(output), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["annotations"] == 2
    assert not (output / "README.md").exists()
    (project / companions.CONFIG_NAME).write_text(json.dumps({"archives": [str(output / "records.jsonl")]}))
    assert main(["companions", "inspect", str(project), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["attachments"][0]["annotation"] == value
    assert main(["companions", "export", str(project), "--output", str(tmp_path / "portable"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["annotations"] == 2
    assert "Independent mathematical prose." in article.read_text()


def test_missing_checker_is_actionable_only_when_native_feature_selected(consumer, tmp_path, monkeypatch, capsys):
    import autoform_cli.companion_sources as module
    project, _ = consumer
    original = module.importlib.import_module
    def missing(name):
        if name.startswith("formal_companion_annotations"):
            raise ImportError("not installed")
        return original(name)
    monkeypatch.setattr(module.importlib, "import_module", missing)
    assert companions.load_project_companions(project).configured is False
    render_site(project / "blueprint", tmp_path / "site", lean_root=project)
    (project / companions.CONFIG_NAME).write_text('{"roots":[]}')
    assert main(["companions", "inspect", str(project)]) == 2
    assert "optional formal-companion-annotations" in capsys.readouterr().err


def test_narrative_documents_attach_without_becoming_workflow_nodes(consumer, tmp_path):
    project, _ = consumer
    article = project / "blueprint/sources/independent-essay.md"
    article.write_text("# An independent essay\n\nExposition without any execution target.\n")
    before = load_runtime_graph(project)
    configure(project, article, tmp_path, [entry()])
    output = tmp_path / "site"
    render_site(project / "blueprint", output, lean_root=project,
                repository_url="https://github.com/example/consumer", ref="abcd")
    assert "Companion associations" in (output / "sources/independent-essay.md").read_text()
    assert "sources/independent-essay.html" in (output / "companions.md").read_text()
    assert load_runtime_graph(project) == before
    assert companions.inspect_project_companions(project)["attachments"][0]["article"] == (
        "document:sources/independent-essay.md")


def test_native_index_never_replaces_existing_authored_document(consumer, tmp_path):
    project, article = consumer
    authored = project / "blueprint/companions.md"
    authored.write_text("# Authored companion discussion\n")
    # No native config means ordinary publication behavior is unchanged.
    render_site(project / "blueprint", tmp_path / "plain", lean_root=project)
    assert (tmp_path / "plain/companions.md").read_text().startswith("# Authored")
    configure(project, article, tmp_path, [entry()])
    with pytest.raises(PublicationError, match="overwrite authored"):
        render_site(project / "blueprint", tmp_path / "native", lean_root=project)
    assert not (tmp_path / "native").exists()
    assert authored.read_text() == "# Authored companion discussion\n"


def test_stable_current_symlink_does_not_rebase_retained_origins(consumer, tmp_path):
    project, _ = consumer
    root = tmp_path / "workspace"
    root.mkdir()
    original = entry(annotation("article", "formal"), base="https://origin.invalid/captured/shard.json")
    companions.write_retained_annotations(capture([original]), output=root / "generation-1")
    (root / "current").symlink_to(root / "generation-1", target_is_directory=True)
    (project / companions.CONFIG_NAME).write_text(json.dumps({"archives": [str(root / "current/records.jsonl")]}))
    selected = companions.load_project_companions(project)
    assert selected.snapshot.records[0].entry() == original
    report = companions.inspect_project_companions(project)
    assert report["attachments"][0]["source"] == "https://origin.invalid/captured/article"


def test_html_selection_is_retained_without_guessing_a_new_html_source(consumer, tmp_path):
    project, article = consumer
    value = {"source": {"type": "html", "uri": "https://example.invalid/published/chapter"},
             "target": "urn:lean:Result", "location": {"fragment": "result"}}
    configure(project, article, tmp_path, [entry(value)])
    output = tmp_path / "site"
    render_site(project / "blueprint", output, lean_root=project)
    report = companions.inspect_project_companions(project)
    assert report["attachments"][0]["annotation"] == value
    assert report["selectors_resolved"] is False
    assert "endpoint and selector unverified" in (output / "roadmap/README.md").read_text()
    assert "roadmap/index.html#result" in (output / "companions.md").read_text()


@pytest.mark.parametrize("invalid", ["https://example.invalid/a b", "https://example.invalid/a\n", "https://e.invalid/a\0"])
def test_carrier_and_binding_references_use_core_lexical_validation(consumer, tmp_path, invalid):
    with pytest.raises(CompanionImportError, match="URI/IRI reference"):
        capture([entry(base=invalid)])
    project, article = consumer
    config = configure(project, article, tmp_path, [entry()])
    config["bindings"][0]["source"] = invalid
    (project / companions.CONFIG_NAME).write_text(json.dumps(config))
    with pytest.raises(CompanionImportError, match="URI/IRI reference"):
        companions.load_project_companions(project)


def test_archives_keep_foreign_origin_paths_and_unicode_json_values(tmp_path):
    value = entry(annotation(location={"separator": "before\u2028after"}))
    value["origin"]["path"] = "C:\\original\\shard.json"
    value["origin"]["custom"] = {"uninterpreted": "./relative"}
    archive = tmp_path / "foreign.jsonl"
    archive.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")
    snapshot = companions.read_retained_annotations(archive)
    assert snapshot.records[0].entry() == value


def test_native_attachment_views_build_with_real_mkdocs_and_correct_chapter_anchor(consumer, tmp_path):
    pytest.importorskip("mkdocs")
    project, article = consumer
    configure(project, article, tmp_path, [entry(annotation(location={"quoted": "<tag>"}))])
    render_site(project / "blueprint", project / "site-src", lean_root=project)
    result = subprocess.run([sys.executable, "-m", "mkdocs", "build", "--strict", "--config-file",
                             str(project / "mkdocs.yml")], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    chapter = (project / "site/roadmap/index.html").read_text()
    index = (project / "site/companions.html").read_text()
    assert 'id="result"' in chapter
    assert 'class="autoform-companion"' in chapter
    assert 'href="roadmap/index.html#result"' in index
    assert "&lt;tag&gt;" in chapter


def test_absent_target_target_object_and_status_object_are_retained_rendered_and_rebased(consumer, tmp_path):
    project, article = consumer
    source = "https://example.invalid/wiki/result"
    library = {"type": "repository", "url": "../lean", "commit": "0" * 40}
    absent = {"source": {"type": "generic", "uri": source,
                         "pin": {"type": "website", "url": "../wiki", "revision": "7"}},
              "status": {"value": "not_formalized", "pin": library},
              "kind": "proposition", "label": "Unlinked passage", "note": "No formal counterpart yet."}
    linked = {"source": {"type": "generic", "uri": source, "pin": {"type": "generic", "url": "../opaque"}},
              "target": {"uri": "../Result.lean#L2",
                         "pin": {**library, "path": "Result.lean", "line": 2, "declaration": "Result"},
                         "extensions": {"org.example": {"reviewed": False}}},
              "status": "partial"}
    configure(project, article, tmp_path, [entry(absent), entry(linked)])
    report = companions.inspect_project_companions(project)
    assert [a["annotation"] for a in report["attachments"]] == [absent, linked]
    assert {a["state"] for a in report["attachments"]} == {"bound-revision"}
    output = tmp_path / "site"
    render_site(project / "blueprint", output, lean_root=project)
    chapter = (output / "roadmap/README.md").read_text()
    assert chapter.count('class="autoform-companion"') == 2
    assert "no formal counterpart" in chapter
    assert "not_formalized" in chapter and "partial" in chapter
    rendered = site_converter().convert(chapter)
    assert 'href="https://example.invalid/Result.lean#L2"' in rendered
    assert 'href="https://example.invalid/wiki"' not in rendered  # pins are cited, never navigated
    portable = tmp_path / "portable"
    report = companions.export_retained_annotations(companions.load_project_companions(project).snapshot,
                                                    output=portable)
    rows = [json.loads(line) for line in (portable / report["shards"]).read_text().splitlines()]
    assert "target" not in rows[0]
    assert rows[0]["source"]["pin"]["url"] == "https://example.invalid/wiki"
    assert rows[0]["status"]["pin"]["url"] == "https://example.invalid/lean"
    assert rows[1]["source"]["pin"] == {"type": "generic", "url": "../opaque"}
    assert rows[1]["target"]["uri"] == "https://example.invalid/Result.lean#L2"
    assert rows[1]["target"]["pin"]["url"] == "https://example.invalid/lean"
    assert rows[1]["target"]["extensions"] == {"org.example": {"reviewed": False}}
    assert rows[1]["status"] == "partial"
    assert [(c["occurrence"], c["pointer"]) for c in report["rebased"]] == [
        (1, "/source/pin/url"), (1, "/status/pin/url"), (2, "/target/uri"), (2, "/target/pin/url")]
    checker = importlib.import_module("formal_companion_annotations.companion")
    assert all(not checker.check(row).problems for row in rows)
