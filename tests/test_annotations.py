"""Interchange exercises an independent Git consumer, never the product tree."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from autoform_cli.__main__ import main
from autoform_cli.annotations import AnnotationExportError, export_annotations, write_annotation_shards
from autoform_cli.graph import GraphValidationError
from autoform_cli.runtime import RuntimeProjectionError

REPO = "https://github.com/example/independent-consumer"
ARTICLE_ID = "af_000000000000000000000001"


def git(project: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(project), *args], capture_output=True, check=True, text=True,
    ).stdout.strip()


def article(project: Path, name: str = "result.md", *, lean: str = "Consumer.result", extra: str = "") -> Path:
    path = project / "blueprint" / "roadmap" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\narticle_id: {ARTICLE_ID}\ndeclaration: theorem\nlean: {lean}\n{extra}---\n\n"
        "# Independent result\n\nEvery test proposition holds.\n", encoding="utf-8",
    )
    return path


def commit(project: Path) -> str:
    git(project, "add", "blueprint", "Consumer.lean")
    git(project, "commit", "-qm", "Independent fixture")
    return git(project, "rev-parse", "HEAD")


@pytest.fixture
def consumer(tmp_path: Path) -> tuple[Path, str]:
    project = tmp_path / "independent consumer"
    project.mkdir()
    git(project, "init", "-q")
    git(project, "config", "user.name", "Fixture")
    git(project, "config", "user.email", "fixture@example.invalid")
    article(project)
    # Container identities are not mandatory when there is no association.
    (project / "blueprint/roadmap/README.md").write_text("# Roadmap\n", encoding="utf-8")
    (project / "Consumer.lean").write_text(
        "namespace Consumer\ntheorem result : True := by trivial\nend Consumer\n", encoding="utf-8",
    )
    return project, commit(project)


def test_committed_atomic_source_and_target_are_independent_records(consumer, tmp_path: Path) -> None:
    project, revision = consumer
    result = export_annotations(project, repo=REPO, commit=revision)
    assert not result.diagnostics
    assert result == export_annotations(project / "blueprint", repo=REPO, commit=revision)
    assert len(result.annotations) == 1
    record = result.annotations[0]
    assert set(record) == {"version", "id", "source", "target", "extensions"}
    assert record["version"] == 2
    assert record["source"]["type"] == "generic"
    assert record["source"]["uri"].endswith(f"/{revision}/blueprint/roadmap/result.md")
    assert record["target"] == f"{REPO}/blob/{revision}/Consumer.lean#L2"
    extension = record["extensions"]["org.autoform"]
    assert extension["article_id"] == ARTICLE_ID
    assert extension["verification"] == "not-performed"
    assert extension["declaration"]["name"] == "Consumer.result"
    assert not extension["workflow_assertions"]["proof_formalized"]
    output = write_annotation_shards(result, tmp_path / "export")
    assert json.loads((output / "report.json").read_text()) == result.report()
    shard = next((output / "annotations").glob("*.json"))
    assert json.loads(shard.read_text()) == record
    again = write_annotation_shards(result, tmp_path / "export-again")
    assert {p.relative_to(output): p.read_bytes() for p in output.rglob("*.json")} == {
        p.relative_to(again): p.read_bytes() for p in again.rglob("*.json")
    }


def test_multiple_declarations_split_and_repeated_names_do_not_duplicate(consumer) -> None:
    project, _ = consumer
    article(project, lean="Consumer.result Consumer.other Consumer.result")
    (project / "Consumer.lean").write_text(
        "namespace Consumer\ntheorem result : True := by trivial\ntheorem other : True := by trivial\nend Consumer\n",
        encoding="utf-8",
    )
    result = export_annotations(project, repo=REPO, commit=commit(project))
    assert len(result.annotations) == 2
    assert len({record["id"] for record in result.annotations}) == 2
    assert {record["target"].split("#")[-1] for record in result.annotations} == {"L2", "L3"}


@pytest.mark.parametrize("file", ["Consumer.lean", "blueprint/roadmap/result.md", "blueprint/roadmap/README.md"])
def test_changed_committed_input_aborts(consumer, file) -> None:
    project, revision = consumer
    with (project / file).open("a") as stream:
        stream.write("\n-- different\n")
    with pytest.raises(AnnotationExportError, match="differs from the requested commit"):
        export_annotations(project, repo=REPO, commit=revision)


def test_byte_snapshot_is_used_after_indexing(consumer, monkeypatch) -> None:
    import autoform_cli.annotations as module
    project, revision = consumer
    original = module.snapshot_project_sources

    def capture(root):
        snapshot = original(root)
        (project / "Consumer.lean").write_text("-- replaced after capture\n", encoding="utf-8")
        return snapshot

    monkeypatch.setattr(module, "snapshot_project_sources", capture)
    record = export_annotations(project, repo=REPO, commit=revision).annotations[0]
    assert record["target"].endswith("#L2")
    assert record["extensions"]["org.autoform"]["declaration"]["name"] == "Consumer.result"


def test_article_changed_after_runtime_capture_fails(consumer, monkeypatch) -> None:
    import autoform_cli.annotations as module
    project, revision = consumer
    original = module.load_runtime_graph

    def capture(root):
        runtime = original(root)
        article(project, lean="Consumer.other")
        return runtime

    monkeypatch.setattr(module, "load_runtime_graph", capture)
    with pytest.raises(AnnotationExportError, match="changed while"):
        export_annotations(project, repo=REPO, commit=revision)


def test_ambiguous_and_missing_names_do_not_become_links(consumer) -> None:
    project, _ = consumer
    article(project, lean="Consumer.result Consumer.missing")
    (project / "Other.lean").write_text("namespace Consumer\ntheorem result : True := by trivial\n", encoding="utf-8")
    git(project, "add", "Other.lean")
    result = export_annotations(project, repo=REPO, commit=commit(project))
    assert not result.annotations
    assert [d["code"] for d in result.diagnostics] == ["AMBIGUOUS_DECLARATION", "DECLARATION_NOT_FOUND"]


@pytest.mark.parametrize("modifier", ["private ", "private\n", "private\n@[simp]\n", "@[simp]\n"])
def test_private_and_complex_modifier_forms_are_not_guessed(consumer, modifier) -> None:
    project, _ = consumer
    (project / "Consumer.lean").write_text(
        f"namespace Consumer\n{modifier}theorem result : True := by trivial\nend Consumer\n", encoding="utf-8",
    )
    result = export_annotations(project, repo=REPO, commit=commit(project))
    assert not result.annotations
    assert result.diagnostics[0]["code"] == "DECLARATION_REQUIRES_ELABORATED_INDEX"


def test_external_mathlib_targets_stay_omissions(consumer) -> None:
    project, _ = consumer
    article(project, extra="mathlib: true\nmathlib_declaration: Nat.add_comm\n")
    result = export_annotations(project, repo=REPO, commit=commit(project))
    assert len(result.annotations) == 1
    assert result.diagnostics == ({
        "code": "EXTERNAL_DECLARATION_REQUIRES_PINNED_INDEX",
        "article": "blueprint/roadmap/result.md", "declaration": "Nat.add_comm",
    },)


def test_missing_article_identity_does_not_mint_unstable_id(consumer) -> None:
    project, _ = consumer
    path = project / "blueprint/roadmap/result.md"
    path.write_text(path.read_text().replace(f"article_id: {ARTICLE_ID}\n", ""))
    result = export_annotations(project, repo=REPO, commit=commit(project))
    assert not result.annotations
    assert result.diagnostics[0]["code"] == "MISSING_ARTICLE_ID"


def test_escaping_citation_and_symlink_are_refused(consumer, tmp_path: Path) -> None:
    project, _ = consumer
    path = project / "blueprint/roadmap/result.md"
    with path.open("a") as stream:
        stream.write("\n## Sources\n\n- [escape](../../outside.md)\n")
    revision = commit(project)
    with pytest.raises((GraphValidationError, RuntimeProjectionError)):
        export_annotations(project, repo=REPO, commit=revision)
    path.unlink()
    external = tmp_path / "outside.md"
    external.write_text("# outside\n")
    path.symlink_to(external)
    with pytest.raises(RuntimeProjectionError, match="symbolic link"):
        export_annotations(project, repo=REPO, commit=revision)


def test_paths_escape_uri_delimiters(consumer) -> None:
    project, _ = consumer
    path = project / "Consumer.lean"
    new_path = project / "Result #1?.lean"
    path.rename(new_path)
    git(project, "add", "Consumer.lean", new_path.name)
    git(project, "commit", "-qm", "URI path")
    revision = git(project, "rev-parse", "HEAD")
    record = export_annotations(project, repo=REPO, commit=revision).annotations[0]
    parsed = urlsplit(record["target"])
    assert parsed.fragment == "L2"
    assert not parsed.query
    assert unquote(parsed.path).endswith("/Result #1?.lean")


@pytest.mark.parametrize("repo", ["../escape", "https://github.com/a/b/tree/main", "https://gitlab.com/a/b", "github.com/../b"])
def test_unsupported_repository_identity_is_refused(consumer, repo) -> None:
    project, revision = consumer
    with pytest.raises(AnnotationExportError, match="repo"):
        export_annotations(project, repo=repo, commit=revision)


@pytest.mark.parametrize("revision", ["HEAD", "main", "deadbeef", "0" * 40])
def test_nonexistent_or_noncommit_revision_is_refused(consumer, revision) -> None:
    project, _ = consumer
    with pytest.raises(AnnotationExportError):
        export_annotations(project, repo=REPO, commit=revision)


def test_cli_preserves_existing_output_and_writes_partial_report(consumer, tmp_path: Path, capsys) -> None:
    project, revision = consumer
    output = tmp_path / "export"
    args = ["export-annotations", str(project), "--repo", REPO, "--commit", revision, "--output", str(output), "--json"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["complete"]
    before = {p: p.read_bytes() for p in output.rglob("*.json")}
    assert main(args) == 2
    assert "already exists" in capsys.readouterr().err
    assert before == {p: p.read_bytes() for p in output.rglob("*.json")}
    article(project, lean="Consumer.absent")
    revision = commit(project)
    args[args.index("--commit") + 1] = revision
    args[args.index("--output") + 1] = str(tmp_path / "partial")
    assert main(args) == 1
    assert not json.loads(capsys.readouterr().out)["complete"]
    assert (tmp_path / "partial/report.json").is_file()


def test_atomic_write_failure_cleans_stage_and_keeps_existing_race(consumer, tmp_path: Path, monkeypatch) -> None:
    import autoform_cli.annotations as module
    project, revision = consumer
    result = export_annotations(project, repo=REPO, commit=revision)
    destination = tmp_path / "companion"
    original = module._rename_no_replace

    def concurrent_writer(source, target):
        target.mkdir()
        (target / "owned.txt").write_text("concurrent result")
        original(source, target)

    monkeypatch.setattr(module, "_rename_no_replace", concurrent_writer)
    with pytest.raises(OSError):
        write_annotation_shards(result, destination)
    assert (destination / "owned.txt").read_text() == "concurrent result"
    assert not list(tmp_path.glob(".autoform-annotations-*"))


def test_nested_project_urls_are_relative_to_repository_root(tmp_path: Path) -> None:
    repository = tmp_path / "monorepo"
    project = repository / "packages/consumer"
    project.mkdir(parents=True)
    git(repository, "init", "-q")
    git(repository, "config", "user.name", "Fixture")
    git(repository, "config", "user.email", "fixture@example.invalid")
    article(project)
    (project / "Consumer.lean").write_text(
        "namespace Consumer\ntheorem result : True := by trivial\n", encoding="utf-8",
    )
    revision = commit(project)
    record = export_annotations(project, repo=REPO, commit=revision).annotations[0]
    assert record["source"]["uri"].endswith(f"/{revision}/packages/consumer/blueprint/roadmap/result.md")
    assert record["target"].endswith(f"/{revision}/packages/consumer/Consumer.lean#L2")


def test_no_associations_is_explicit_and_output_has_no_fake_record(consumer, tmp_path: Path) -> None:
    project, _ = consumer
    (project / "blueprint/roadmap/result.md").write_text("# Unlinked article\n", encoding="utf-8")
    result = export_annotations(project, repo=REPO, commit=commit(project))
    assert result.annotations == ()
    assert result.diagnostics == ({"code": "NO_ASSOCIATIONS"},)
    output = write_annotation_shards(result, tmp_path / "empty-companion")
    assert not list((output / "annotations").iterdir())
    assert not json.loads((output / "report.json").read_text())["complete"]


def test_writer_preserves_repeated_opaque_ids_without_using_them_as_paths(consumer, tmp_path: Path) -> None:
    from dataclasses import replace
    project, revision = consumer
    result = export_annotations(project, repo=REPO, commit=revision)
    record = {**result.annotations[0], "id": "../../outside"}
    result = replace(result, annotations=(record, record))
    output = write_annotation_shards(result, tmp_path / "companion")
    shards = sorted((output / "annotations").glob("*.json"))
    assert len(shards) == 2
    assert [json.loads(path.read_text())["id"] for path in shards] == ["../../outside", "../../outside"]
    assert not (tmp_path / "outside.json").exists()


def test_public_declaration_after_private_helper_remains_linkable(consumer) -> None:
    project, _ = consumer
    (project / "Consumer.lean").write_text(
        "namespace Consumer\nprivate theorem helper : True := by trivial\n"
        "theorem result : True := by trivial\nend Consumer\n", encoding="utf-8",
    )
    result = export_annotations(project, repo=REPO, commit=commit(project))
    assert not result.diagnostics
    assert result.annotations[0]["target"].endswith("#L3")
