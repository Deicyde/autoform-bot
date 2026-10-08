"""Read FCA records into Autoform's existing Markdown source-evidence surface.

The generated catalog is a derived, local view. It never authors roadmap nodes,
resolves selectors, runs Lean, or turns producer metadata into verified facts.
"""
from __future__ import annotations

import html
import importlib
import json
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit

from .runtime import resolve_runtime_paths
from .skeleton import SkeletonError, _rename_no_replace

REFERENCE_VERSION = "2.0.0a2"


class CompanionImportError(ValueError):
    """The catalog cannot be created without losing source evidence."""


def _reference_api():
    try:
        package = importlib.import_module("formal_companion_annotations")
        if getattr(package, "__version__", None) != REFERENCE_VERSION:
            raise ImportError("incompatible companion draft")
        reader = importlib.import_module("formal_companion_annotations.discovery").collect
        checker = importlib.import_module("formal_companion_annotations.companion").check
        return reader, checker
    except ImportError as error:
        raise CompanionImportError(
            "Companion import requires the optional formal-companion-annotations "
            "2.0.0a2 reference package in this same Python environment (Python 3.11+). "
            "Install the local draft wheel alongside Autoform; see the companion source "
            "import section of autoform_cli/README.md. Validation is never bypassed."
        ) from error


def _json(value: object, *, pretty: bool = False) -> str:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2 if pretty else None)


def _code(value: object) -> str:
    """Display arbitrary imported text as text even in permissive Markdown."""
    return "<code>" + html.escape(_json(value), quote=True) + "</code>"


def _json_block(value: object) -> str:
    text = _json(value, pretty=True)
    longest = max((len(match.group()) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return fence + "json\n" + text + "\n" + fence


def _target_reference(annotation: dict) -> str | None:
    """The formal destination reference, or None for a record with no formal counterpart."""
    target = annotation.get("target")
    return target["uri"] if isinstance(target, dict) else target


def _endpoint(label: str, reference: str) -> str:
    # Core acceptance is not permission to navigate. Only absolute HTTP(S)
    # endpoints become clickable; opaque, relative, file, and executable URIs
    # stay visible as evidence alongside their original carrier base.
    lines = [f"{label}: {_code(reference)}"]
    try:
        parsed = urlsplit(reference)
        safe = parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)
        safe = safe and parsed.username is None and parsed.password is None
        safe = safe and parsed.hostname is not None
    except ValueError:
        safe = False
    if safe:
        destination = quote(reference, safe="/:?#[]@!$&*+,;=%~-._")
        lines.append(f"[Open {label.lower()}](<{destination}>)")
    return "\n\n".join(lines)


def _page(index: int, annotation: dict, origin: dict) -> str:
    target = _target_reference(annotation)
    lines = [
        f"# Companion evidence {index}",
        "Imported association for source review. This page does not assert a proof, "
        "equivalence, human review, or that either endpoint or selector resolves.",
        _endpoint("Informal source", annotation["source"]["uri"]),
        "Source mode: " + _code(annotation["source"]["type"]),
        _endpoint("Formal target", target) if target is not None else
        "Formal target: none supplied. This record describes an informal passage with no formal counterpart.",
        "Relative references resolve against this original carrier URI: " + _code(origin["base_uri"]),
    ]
    if "status" in annotation:
        # A producer fact, shown as written. Pins inside it are citations, not checks.
        lines.append("Producer status: " + _code(annotation["status"])
                     + " (producer data, not Autoform proof or review state)")
    lines.append("## Location")
    if "location" in annotation:
        lines.extend([
            "The producer supplied this location. Its meaning is retained exactly; "
            "resolution has not been attempted. A generic location remains opaque.",
            _json_block(annotation["location"]),
        ])
    else:
        lines.append("No location was supplied: this annotation selects the whole informal source.")
    lines.extend([
        "## Original occurrence", _json_block(origin),
        "## Complete annotation", _json_block(annotation),
        "[Back to companion source catalog](README.md)",
    ])
    return "\n\n".join(lines) + "\n"


def import_annotations(
    paths,
    *,
    output: str | Path,
    include=None,
    exclude=(),
    project: str | Path | None = None,
) -> dict:
    """Validate all shards, then atomically create a new Markdown source catalog.

    Every occurrence gets its own page; IDs never become paths or dedupe keys.
    ``records.jsonl`` is an operational archive of unchanged annotation values
    and their origins, not a shard carrier: its records deliberately retain the
    original carrier bases. No existing authored file is modified.
    """
    collect, _check = _reference_api()
    collection = collect(paths, include=include, exclude=exclude)
    return write_annotation_catalog(collection, output=output, project=project)


def write_annotation_catalog(collection, *, output: str | Path, project: str | Path | None = None) -> dict:
    """Render an already captured FCA collection, checking every annotation again.

    Coordinators can pass one captured collection to several consumers without
    rereading mutable shards. Origins use the collector's resolved absolute path
    directly; no filesystem re-resolution can silently change a captured base.
    """
    _collect, check = _reference_api()
    result = collection
    if result.problems:
        issues = "; ".join(f"{problem.code} {problem.path}: {problem.message}" for problem in result.problems)
        raise CompanionImportError("companion inputs failed reference validation: " + issues)
    for record in result.records:
        findings = check(record.annotation).problems
        if findings:
            raise CompanionImportError("captured annotation failed reference validation: " + "; ".join(
                f"{finding.code} {finding.path}: {finding.message}" for finding in findings
            ))
        if not isinstance(record.path, Path) or not record.path.is_absolute():
            raise CompanionImportError("captured record origin must be an absolute carrier path")
    if not result.records:
        raise CompanionImportError("captured collection contains no annotations")
    destination = Path(output).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise CompanionImportError("output already exists; choose a new catalog directory")
    if not destination.parent.is_dir():
        raise CompanionImportError("output parent directory does not exist")
    citation = None
    if project is not None:
        project_paths = resolve_runtime_paths(project)
        sources = project_paths.blueprint_dir / "sources"
        # Resolve before comparing, so symlinks and '..' cannot redirect a
        # selected project catalog outside its existing source-evidence tree.
        resolved = destination.resolve()
        if not resolved.is_relative_to(sources.resolve()) or resolved == sources.resolve():
            raise CompanionImportError("project catalog output must be a new directory under blueprint/sources")
        for ancestor in destination.parents:
            if ancestor == project_paths.blueprint_dir:
                break
            if ancestor.is_symlink():
                raise CompanionImportError("project source catalog path contains a symbolic link")
        citation = resolved.relative_to(project_paths.blueprint_dir).as_posix() + "/README.md"
    report = {
        "annotations": len(result.records), "files": len(result.files),
        "reference_version": REFERENCE_VERSION,
        "catalog": "README.md", "archive": "records.jsonl",
        "selectors_resolved": False, "proof_verification": "not-performed",
    }
    if citation is not None:
        report["blueprint_relative_catalog"] = quote(citation, safe="/")
    pages = []
    archive = []
    for index, record in enumerate(result.records, start=1):
        origin = {"path": str(record.path), "pointer": record.pointer,
                  "line": record.line, "base_uri": record.path.as_uri()}
        archive.append(_json({"annotation": record.annotation, "origin": origin}))
        pages.append((f"{index:06d}.md", _page(index, record.annotation, origin)))
    catalog = [
        "# Companion sources",
        "Imported informal–formal associations, retained as source evidence. "
        "Review the original passages before using them in a roadmap. "
        "Producer statuses and extensions are data, not Autoform proof or review state.",
        "Each occurrence has its own page, including repeated IDs and repeated annotations. "
        "Selectors are displayed without attempting to resolve them. "
        "Relative endpoints keep the original carrier base shown on their page.",
        "The records.jsonl archive stores unchanged annotation values with their original "
        "path, pointer, line and base URI. It is an operational transport archive, "
        "not a directory of relocated FCA shards. JSON whitespace is reformatted; "
        "extension values, large integers and occurrence multiplicity are preserved.",
        "\n".join(f"- [Evidence {index}]({filename})" for index, (filename, _) in enumerate(pages, start=1)),
    ]
    stage = Path(tempfile.mkdtemp(prefix=".autoform-companion-sources-", dir=destination.parent))
    try:
        (stage / "README.md").write_text("\n\n".join(catalog) + "\n", encoding="utf-8")
        (stage / "records.jsonl").write_text("\n".join(archive) + "\n", encoding="utf-8")
        (stage / "report.json").write_text(_json(report, pretty=True) + "\n", encoding="utf-8")
        for filename, text in pages:
            (stage / filename).write_text(text, encoding="utf-8")
        try:
            _rename_no_replace(stage, destination)
        except SkeletonError as error:
            raise CompanionImportError("cannot publish source catalog: " + "; ".join(error.issues)) from error
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return report
