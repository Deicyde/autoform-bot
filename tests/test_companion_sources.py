from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from autoform_cli.__main__ import main
from autoform_cli.companion_sources import CompanionImportError, import_annotations, write_annotation_catalog
from autoform_cli.graph import load_graph
from autoform_cli.render import render_site
from autoform_cli.scaffold import scaffold_project


@pytest.fixture
def reference():
    package = pytest.importorskip("formal_companion_annotations")
    if package.__version__ != "2.0.0a2":
        pytest.skip("requires the optional FCA 2.0.0a2 reference checker")
    return importlib.import_module("formal_companion_annotations.discovery")


@pytest.fixture
def shards(tmp_path: Path):
    root = tmp_path / "shards"
    nested = root / "nested"
    nested.mkdir(parents=True)
    records = [
        {"id": "same", "source": {"type": "pdf", "uri": "../book.pdf"},
         "location": {"page": 2, "rect": [0.1, 0.2, 0.3, 0.4]}, "target": "https://example.invalid/formal#one"},
        {"id": "same", "source": {"type": "latex", "uri": "../book.tex"},
         "location": {"label": "thm:one"}, "target": "urn:lean:Test.one"},
        {"source": {"type": "html", "uri": "https://example.invalid/book"},
         "location": {"quote": {"exact": "Some statement."}}, "target": "../formal.html#two"},
        {"source": {"type": "generic", "uri": "../image.svg"},
         "location": {}, "target": "urn:lean:Test.three",
         "extensions": {"custom": {"large": 2**100, "values": [3, 2, 1]}}},
    ]
    (nested / "links.jsonl").write_text("\n".join(json.dumps(record) for record in records + [records[0]]) + "\n")
    return root, records + [records[0]]


def archive(path: Path):
    return [json.loads(line) for line in (path / "records.jsonl").read_text().splitlines()]


def test_catalog_retains_every_occurrence_origin_location_and_extension(reference, shards, tmp_path):
    root, records = shards
    output = tmp_path / "catalog"
    report = import_annotations([root], output=output)
    assert report["annotations"] == 5
    saved = archive(output)
    assert [row["annotation"] for row in saved] == records
    assert [row["origin"]["line"] for row in saved] == [1, 2, 3, 4, 5]
    assert {row["origin"]["base_uri"] for row in saved} == {(root / "nested/links.jsonl").as_uri()}
    assert [p.name for p in sorted(output.glob("[0-9]*.md"))] == [f"{n:06}.md" for n in range(1, 6)]
    assert "generic location remains opaque" in (output / "000004.md").read_text()
    assert "whole informal source" not in (output / "000004.md").read_text()
    assert report["proof_verification"] == "not-performed"
    other = tmp_path / "again"
    import_annotations([root], output=other)
    assert {p.name: p.read_bytes() for p in output.iterdir()} == {p.name: p.read_bytes() for p in other.iterdir()}


def test_captured_collection_does_not_reread_or_reresolve_sources(reference, shards, tmp_path):
    root, records = shards
    captured = reference.collect([root])
    original_path = root / "nested/links.jsonl"
    moved = tmp_path / "moved"
    root.rename(moved)
    root.symlink_to(moved, target_is_directory=True)
    (moved / "nested/links.jsonl").write_text("malformed replacement")
    output = tmp_path / "catalog"
    write_annotation_catalog(captured, output=output)
    assert [row["annotation"] for row in archive(output)] == records
    assert archive(output)[0]["origin"]["base_uri"] == original_path.as_uri()


def test_captured_annotations_are_revalidated_before_any_output(reference, shards, tmp_path):
    root, _ = shards
    captured = reference.collect([root])
    captured.records[0].annotation["location"]["rect"] = [0.9, 0.2, 0.3, 0.4]
    output = tmp_path / "catalog"
    with pytest.raises(CompanionImportError, match="reference validation"):
        write_annotation_catalog(captured, output=output)
    assert not output.exists()
    assert not list(tmp_path.glob(".autoform-companion-sources-*"))


def test_invalid_selected_shard_aborts_all_and_explicit_filters_work(reference, shards, tmp_path):
    root, _ = shards
    (root / "package.json").write_text('{"name":"unrelated"}')
    output = tmp_path / "catalog"
    with pytest.raises(CompanionImportError, match="reference validation"):
        import_annotations([root], output=output)
    assert not output.exists()
    assert import_annotations([root], output=output, include=["*.jsonl"])["annotations"] == 5


def test_untrusted_markup_and_unsafe_uris_are_inert(reference, tmp_path):
    from autoform_cli.markdown import site_converter
    text = '```\n<script>alert(1)</script>\n```\n[x](javascript:alert(1))'
    record = {"id": text,
              "source": {"type": "generic", "uri": "javascript:alert(1)"},
              "target": "data:text/html,<script>alert(2)</script>",
              "location": {"custom": text}, "extensions": {"untrusted": text}}
    shard = tmp_path / "input.json"
    shard.write_text(json.dumps(record))
    output = tmp_path / "catalog"
    import_annotations([shard], output=output)
    page = (output / "000001.md").read_text()
    rendered = site_converter().convert(page)
    assert "<script>" not in rendered
    assert "href=\"javascript:" not in rendered
    assert "href=\"data:" not in rendered
    assert "&lt;script&gt;" in rendered
    assert archive(output)[0]["annotation"] == record


def test_whole_source_is_distinct_from_explicit_generic_location(reference, tmp_path):
    shard = tmp_path / "input.json"
    shard.write_text(json.dumps({"source": {"type": "generic", "uri": "urn:source"}, "target": "urn:formal"}))
    output = tmp_path / "catalog"
    import_annotations([shard], output=output)
    assert "selects the whole informal source" in (output / "000001.md").read_text()


def test_project_catalog_is_readable_by_existing_graph_and_renderer(reference, shards, tmp_path, capsys):
    root, _ = shards
    project = tmp_path / "consumer"
    scaffold_project(project, title="Independent consumer")
    graph_before = load_graph(project / "blueprint")
    authored_before = {p.relative_to(project): p.read_bytes() for p in project.rglob("*") if p.is_file()}
    output = project / "blueprint/sources/companion snapshot"
    assert main(["import-annotations", str(root), "--project", str(project), "--output", str(output), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["blueprint_relative_catalog"] == "sources/companion%20snapshot/README.md"
    assert graph_before == load_graph(project / "blueprint")
    assert all((project / path).read_bytes() == data for path, data in authored_before.items())
    assert main(["check", str(project / "blueprint")]) == 0
    rendered = tmp_path / "site-src"
    render_site(project / "blueprint", rendered, lean_root=project)
    assert (rendered / "sources/companion snapshot/README.md").is_file()
    assert "Companion evidence 1" in (rendered / "sources/companion snapshot/000001.md").read_text()


def test_project_scope_and_existing_destination_are_not_overwritten(reference, shards, tmp_path):
    root, _ = shards
    project = tmp_path / "consumer"
    scaffold_project(project, title="Independent consumer")
    with pytest.raises(CompanionImportError, match="under blueprint/sources"):
        import_annotations([root], output=tmp_path / "outside", project=project)
    output = project / "blueprint/sources/companion"
    import_annotations([root], output=output, project=project)
    before = {p: p.read_bytes() for p in output.iterdir()}
    with pytest.raises(CompanionImportError, match="already exists"):
        import_annotations([root], output=output, project=project)
    assert before == {p: p.read_bytes() for p in output.iterdir()}


def test_missing_optional_checker_never_bypasses_validation(tmp_path, monkeypatch, capsys):
    import autoform_cli.companion_sources as module
    original = module.importlib.import_module

    def missing(name):
        if name.startswith("formal_companion_annotations"):
            raise ImportError("not installed")
        return original(name)

    monkeypatch.setattr(module.importlib, "import_module", missing)
    output = tmp_path / "catalog"
    assert main(["import-annotations", str(tmp_path), "--output", str(output)]) == 2
    assert "optional formal-companion-annotations 2.0.0a2" in capsys.readouterr().err
    assert not output.exists()


def test_project_catalog_refuses_symlink_ancestors(reference, shards, tmp_path):
    root, _ = shards
    project = tmp_path / "consumer"
    scaffold_project(project, title="Independent consumer")
    sources = project / "blueprint/sources"
    (sources / "real").mkdir()
    (sources / "alias").symlink_to(sources / "real", target_is_directory=True)
    with pytest.raises(CompanionImportError, match="symbolic link"):
        import_annotations([root], output=sources / "alias/catalog", project=project)
    assert not (sources / "real/catalog").exists()


def test_old_top_level_version_is_rejected_before_writing_catalog(reference, shards, tmp_path):
    _root, records = shards
    old = {"version": 2, **records[0]}
    shard = tmp_path / "old-versioned.json"
    shard.write_text(json.dumps(old))
    output = tmp_path / "catalog"
    with pytest.raises(CompanionImportError, match="reference validation"):
        import_annotations([shard], output=output)
    assert not output.exists()


def test_absent_target_target_object_and_status_are_shown_as_data(reference, tmp_path):
    library = {"type": "repository", "url": "https://example.invalid/lean", "commit": "0" * 40}
    records = [
        {"source": {"type": "generic", "uri": "urn:source:1"}, "kind": "remark",
         "status": {"value": "not_formalized", "pin": library}},
        {"source": {"type": "generic", "uri": "urn:source:2"}, "status": "formalized",
         "target": {"uri": "https://example.invalid/formal#two", "pin": {**library, "declaration": "Test.two"}}},
    ]
    shard = tmp_path / "links.jsonl"
    shard.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    output = tmp_path / "catalog"
    assert import_annotations([shard], output=output)["annotations"] == 2
    first = (output / "000001.md").read_text()
    assert "no formal counterpart" in first
    assert "Open formal target" not in first
    assert "not_formalized" in first and "not Autoform proof" in first
    second = (output / "000002.md").read_text()
    assert "[Open formal target](<https://example.invalid/formal#two>)" in second
    assert "Producer status: <code>&quot;formalized&quot;</code>" in second
    assert [row["annotation"] for row in archive(output)] == records
