"""Native FCA attachments, independent of the Markdown execution graph.

Config selects authored shards or losslessly retained captures. Nothing here
adds Lean targets, proves a claim, resolves a selector, or fetches a resource.
"""
from __future__ import annotations

import hashlib
import html
import importlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

from .companion_sources import CompanionImportError, REFERENCE_VERSION, _json, _reference_api, _target_reference
from .skeleton import SkeletonError, _rename_no_replace

CONFIG_NAME = ".autoform-companions.json"
INDEX_NAME = "companions.md"
_BUILTIN_PINS = frozenset({"website", "book", "paper", "repository"})
_PARTS = re.compile(
    r"^(?:(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*):)?(?://(?P<authority>[^/?#]*))?"
    r"(?P<path>[^?#]*)(?:\?(?P<query>[^#]*))?(?:#(?P<fragment>.*))?$"
)


def _codec():
    _reference_api()
    return importlib.import_module("formal_companion_annotations.companion")


def _validate_carrier_reference(reference: str, label: str) -> None:
    # Reuse the core reference lexical contract; the parser below only splits
    # components and deliberately does not supply lexical validation itself.
    value = {"source": {"type": "generic", "uri": reference}, "target": "urn:autoform:reference-check"}
    if _codec().check(value).problems:
        raise CompanionImportError(f"{label} must be a valid URI/IRI reference without whitespace or controls")


def _validated_json(value) -> str:
    codec = _codec()
    issues = codec.check(value).problems
    if issues:
        raise CompanionImportError("annotation failed reference validation: " + "; ".join(map(str, issues)))
    return _json(value)


@dataclass(frozen=True)
class RetainedRecord:
    """Immutable JSON capture; accessing its value returns a detached object."""

    annotation_json: str
    origin_json: str

    @property
    def base_uri(self) -> str:
        return self.origin["base_uri"]

    @property
    def annotation(self) -> dict:
        return json.loads(self.annotation_json)

    @property
    def origin(self) -> dict:
        return json.loads(self.origin_json)

    def entry(self) -> dict:
        return {"annotation": self.annotation, "origin": self.origin}


@dataclass(frozen=True)
class RetainedAnnotations:
    records: tuple[RetainedRecord, ...] = ()

    @property
    def revision(self) -> str:
        return hashlib.sha256(_json([record.entry() for record in self.records]).encode()).hexdigest()


def _record(value, origin) -> RetainedRecord:
    codec = _codec()
    # Validate all metadata as JSON too; unknown transport metadata is retained.
    codec.dumps(origin)
    if not isinstance(origin, dict) or not {"pointer", "base_uri"}.issubset(origin):
        raise CompanionImportError("retained origin requires pointer and base_uri")
    pointer, line, base = origin["pointer"], origin.get("line"), origin["base_uri"]
    if "path" in origin and (not isinstance(origin["path"], str) or not origin["path"]):
        raise CompanionImportError("retained origin path must be a nonempty string when supplied")
    if (not isinstance(pointer, str) or (pointer and not pointer.startswith("/"))
            or re.search(r"~(?![01])", pointer)):
        raise CompanionImportError("retained origin pointer must be a JSON pointer")
    if line is not None and (type(line) is not int or line < 1):
        raise CompanionImportError("retained origin line must be a positive integer or null")
    match = _PARTS.fullmatch(base) if isinstance(base, str) else None
    if match is None or match["scheme"] is None:
        raise CompanionImportError("retained base_uri must be an explicit absolute carrier URI")
    _validate_carrier_reference(base, "retained base_uri")
    return RetainedRecord(_validated_json(value), _json(origin))


def capture_retained_records(entries) -> RetainedAnnotations:
    """Validate and detach operational entries, including non-filesystem origins.

    The original carrier base is explicit; optional path and line plus unknown
    origin metadata survive without becoming annotation fields.
    """
    records = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"annotation", "origin"}:
            raise CompanionImportError("retained entry requires annotation and origin")
        records.append(_record(entry["annotation"], entry["origin"]))
    return RetainedAnnotations(tuple(records))


def capture_annotations(collection) -> RetainedAnnotations:
    """Check and detach a captured FCA Collection or an existing retained capture.

    The supplied collection is checked again; a mutable caller cannot bypass the
    reference validator. Files and original paths are never reread or resolved.
    """
    codec = _codec()
    if isinstance(collection, RetainedAnnotations):
        return RetainedAnnotations(tuple(
            _record(codec.loads(r.annotation_json), codec.loads(r.origin_json)) for r in collection.records
        ))
    if collection.problems:
        raise CompanionImportError("companion inputs failed reference validation: " + "; ".join(
            f"{p.code} {p.path}: {p.message}" for p in collection.problems
        ))
    records = []
    for record in collection.records:
        if not isinstance(record.path, Path) or not record.path.is_absolute():
            raise CompanionImportError("captured record origin must be an absolute carrier path")
        records.append(_record(record.annotation, {
            "path": str(record.path), "pointer": record.pointer, "line": record.line,
            "base_uri": record.path.as_uri(),
        }))
    return RetainedAnnotations(tuple(records))


def read_retained_annotations(path: str | Path) -> RetainedAnnotations:
    """Read Autoform's operational archive, which is deliberately not an FCA shard."""
    codec = _codec()
    records = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").split("\n"), 1):
        if not line.strip():
            continue
        try:
            entry = codec.loads(line)
            if not isinstance(entry, dict) or set(entry) != {"annotation", "origin"}:
                raise CompanionImportError("archive entry requires annotation and origin")
            records.append(_record(entry["annotation"], entry["origin"]))
        except ValueError as error:
            raise CompanionImportError(f"retained archive line {number}: {error}") from error
    return RetainedAnnotations(tuple(records))


def _write_output(files: dict[str, str], output: str | Path) -> Path:
    destination = Path(output).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise CompanionImportError("output already exists; choose a new directory")
    if not destination.parent.is_dir():
        raise CompanionImportError("output parent directory does not exist")
    stage = Path(tempfile.mkdtemp(prefix=".autoform-companions-", dir=destination.parent))
    try:
        for name, text in files.items():
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        try:
            _rename_no_replace(stage, destination)
        except SkeletonError as error:
            raise CompanionImportError("cannot publish companion output: " + "; ".join(error.issues)) from error
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return destination


def _report(snapshot: RetainedAnnotations) -> dict:
    return {"annotations": len(snapshot.records), "revision": snapshot.revision,
            "reference_version": REFERENCE_VERSION, "archive": "records.jsonl",
            "selectors_resolved": False, "proof_verification": "not-performed"}


def _archive(snapshot: RetainedAnnotations) -> str:
    return "".join(_json(record.entry()) + "\n" for record in snapshot.records)


def write_retained_annotations(collection, *, output: str | Path) -> dict:
    """Atomically retain every original value and occurrence, with carrier bases."""
    snapshot = capture_annotations(collection)
    report = _report(snapshot)
    _write_output({"records.jsonl": _archive(snapshot), "report.json": _json(report, pretty=True) + "\n"}, output)
    return report


def _remove_dots(path: str) -> str:
    # RFC 3986 section 5.2.4; preserve repeated slashes and encoded dot segments.
    result = ""
    while path:
        if path.startswith("../"):
            path = path[3:]
        elif path.startswith("./"):
            path = path[2:]
        elif path.startswith("/./") or path == "/.":
            path = "/" + path[3:]
        elif path.startswith("/../") or path == "/..":
            path = "/" + path[4:]
            result = result.rsplit("/", 1)[0] if "/" in result else ""
        elif path in {".", ".."}:
            path = ""
        else:
            end = path.find("/", 1 if path.startswith("/") else 0)
            if end < 0:
                result, path = result + path, ""
            else:
                result, path = result + path[:end], path[end:]
    return result


def resolve_reference(base: str, reference: str) -> str:
    """Resolve a core URI reference without urljoin's lexical normalization.

    Absolute references are retained exactly. Opaque extension values are not
    interpreted as URIs. Resolution does not assert that an endpoint exists.
    """
    match, base_match = _PARTS.fullmatch(reference), _PARTS.fullmatch(base)
    if match is None or base_match is None or base_match["scheme"] is None:
        raise CompanionImportError("cannot resolve URI reference against its original carrier")
    if match["scheme"] is not None:
        return reference
    parts, parent = match.groupdict(), base_match.groupdict()
    if parent["authority"] is None and not parent["path"].startswith("/"):
        raise CompanionImportError("relative reference has an opaque carrier URI")
    authority = parts["authority"] if parts["authority"] is not None else parent["authority"]
    if parts["authority"] is not None:
        path, query = _remove_dots(parts["path"]), parts["query"]
    elif not parts["path"]:
        path = parent["path"]
        query = parts["query"] if parts["query"] is not None else parent["query"]
    else:
        prefix = parent["path"].rsplit("/", 1)[0] + "/" if "/" in parent["path"] else ""
        if parent["authority"] is not None and not parent["path"]:
            prefix = "/"
        path = _remove_dots(parts["path"] if parts["path"].startswith("/") else prefix + parts["path"])
        query = parts["query"]
    result = parent["scheme"] + ":" + ("//" + authority if authority is not None else "") + path
    if query is not None:
        result += "?" + query
    if parts["fragment"] is not None:
        result += "#" + parts["fragment"]
    # Python rejects malformed bracket authorities. Do not emit an invented URL.
    try:
        urlsplit(result)
    except ValueError as error:
        raise CompanionImportError("cannot resolve malformed relative URI authority") from error
    return result


def _reference_slots(value: dict) -> list[tuple[dict, str, str]]:
    """Every core URI reference a record may carry, as (owner, key, JSON pointer).

    A generic pin defines its own keys, so its ``url`` stays opaque.
    """
    target, status = value.get("target"), value.get("status")
    slots = [(value["source"], "uri", "/source/uri")]
    slots.append((target, "uri", "/target/uri") if isinstance(target, dict) else (value, "target", "/target"))
    for owner, pointer in ((value["source"], "/source"), (target, "/target"), (status, "/status")):
        pin = owner.get("pin") if isinstance(owner, dict) else None
        if isinstance(pin, dict) and pin.get("type") in _BUILTIN_PINS:
            slots.append((pin, "url", pointer + "/pin/url"))
    slots.append((value, "$schema", "/$schema"))
    return slots


def export_retained_annotations(collection, *, output: str | Path) -> dict:
    """Export portable core records and retain originals alongside a rebase log.

    Only source.uri, target or target.uri, the url of a built-in pin and
    $schema have core URI-reference semantics. IDs, locations and unknown
    extensions are never rewritten. Each occurrence stays separate, even when
    content or IDs repeat.
    """
    snapshot = capture_annotations(collection)
    portable, changes = [], []
    for index, record in enumerate(snapshot.records, 1):
        value = record.annotation
        for owner, key, pointer in _reference_slots(value):
            if key not in owner:
                continue
            original = owner[key]
            resolved = resolve_reference(record.base_uri, original)
            if original != resolved:
                owner[key] = resolved
                changes.append({"occurrence": index, "pointer": pointer, "from": original, "to": resolved})
        portable.append(_validated_json(value))
    report = {**_report(snapshot), "shards": "annotations/links.jsonl", "rebased": changes,
              "extension_references": "opaque; original carrier bases remain in records.jsonl"}
    _write_output({"annotations/links.jsonl": "".join(text + "\n" for text in portable),
                   "records.jsonl": _archive(snapshot), "report.json": _json(report, pretty=True) + "\n"}, output)
    return report


@dataclass(frozen=True)
class CompanionProject:
    snapshot: RetainedAnnotations
    bindings: tuple[dict, ...] = ()
    configured: bool = False

    @property
    def revision(self) -> str:
        return hashlib.sha256(_json([self.snapshot.revision, self.bindings]).encode()).hexdigest()


def load_project_companions(project: str | Path, *, config: str | Path | None = None) -> CompanionProject:
    """Read application config without changing the roadmap or implicit discovery.

    Relative selections are based at the config file; roots may be anywhere.
    No configuration means no dependency on the optional reference package.
    """
    project = Path(project).expanduser().resolve()
    path = Path(config).expanduser().absolute() if config is not None else project / CONFIG_NAME
    if config is None and not path.exists() and not path.is_symlink():
        return CompanionProject(RetainedAnnotations())
    codec = _codec()
    try:
        data = codec.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CompanionImportError(f"cannot read companion configuration: {error}") from error
    allowed = {"roots", "archives", "include", "exclude", "bindings"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise CompanionImportError("companion configuration accepts only roots, archives, include, exclude and bindings")
    for key in ("roots", "archives", "include", "exclude"):
        if key in data and (not isinstance(data[key], list) or any(not isinstance(p, str) or not p for p in data[key])):
            raise CompanionImportError(f"companion {key} must be an array of nonempty strings")
    bindings = data.get("bindings", [])
    if not isinstance(bindings, list):
        raise CompanionImportError("companion bindings must be an array")
    seen = set()
    for binding in bindings:
        if (not isinstance(binding, dict) or set(binding) - {"source", "article", "article_id", "sha256"}
                or not {"source", "sha256"}.issubset(binding)
                or ("article" in binding) == ("article_id" in binding)
                or any(not isinstance(v, str) or not v for v in binding.values())
                or re.fullmatch(r"[0-9a-f]{64}", binding["sha256"]) is None):
            raise CompanionImportError("each binding requires source, sha256, and exactly one of article or article_id")
        match = _PARTS.fullmatch(binding["source"])
        if match is None or match["scheme"] is None:
            raise CompanionImportError("binding source must be an explicit absolute URI")
        _validate_carrier_reference(binding["source"], "binding source")
        if binding["source"] in seen:
            raise CompanionImportError("one source URI cannot have multiple article bindings")
        seen.add(binding["source"])
        if "article" in binding:
            article = Path(binding["article"])
            if article.is_absolute() or ".." in article.parts:
                raise CompanionImportError("binding article must be a confined project-relative Markdown path")
    def selection(name):
        return [path.parent / Path(p).expanduser() for p in data.get(name, [])]
    records = []
    if data.get("roots"):
        collect, _check = _reference_api()
        records.extend(capture_annotations(collect(selection("roots"), include=data.get("include"),
                                                   exclude=data.get("exclude", []))).records)
    for archive in selection("archives"):
        records.extend(read_retained_annotations(archive).records)
    return CompanionProject(RetainedAnnotations(tuple(records)), tuple(bindings), configured=True)


@dataclass(frozen=True)
class Attachment:
    occurrence: int
    source: str
    article: str | None
    state: str
    record: RetainedRecord
    article_path: Path | None = None
    article_sha256: str | None = None


def index_attachments(companions: CompanionProject, graph, *, project: str | Path) -> tuple[Attachment, ...]:
    """Join explicit source identities to article revisions, never to proof state.

    Explicit Markdown paths may identify narrative pages outside the roadmap.
    These remain documents: they are not inserted into the execution graph.
    """
    project = Path(project).resolve()
    blueprint = graph.blueprint_dir.resolve()
    by_path = {node.path.resolve(): node for node in graph.nodes.values()}
    by_id = {node.article_id: node for node in graph.nodes.values() if node.article_id}
    bindings = {binding["source"]: binding for binding in companions.bindings}
    documents = {}
    for binding in companions.bindings:
        if "article" not in binding:
            continue
        requested = project / binding["article"]
        path = requested.resolve()
        if not path.is_relative_to(blueprint) or path.suffix.lower() != ".md":
            raise CompanionImportError("bound article must be Markdown inside the project's blueprint")
        if any(p.is_symlink() for p in (requested, *requested.parents) if p.is_relative_to(project)):
            raise CompanionImportError("bound article path contains a symbolic link")
        if path not in by_path and path not in documents and path.is_file():
            relative = path.relative_to(blueprint)
            if any(part.startswith(".") for part in relative.parts):
                raise CompanionImportError("bound article cannot be a hidden document")
            documents[path] = ("document:" + relative.as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
    attachments = []
    for index, record in enumerate(companions.snapshot.records, 1):
        value = record.annotation
        try:
            source = resolve_reference(record.base_uri, value["source"]["uri"])
        except CompanionImportError:
            attachments.append(Attachment(index, value["source"]["uri"], None, "unresolved-source", record))
            continue
        binding = bindings.get(source)
        article, article_path, article_sha = None, None, None
        state = "external-source"
        if binding:
            path = (project / binding["article"]).resolve() if "article" in binding else None
            node = by_path.get(path) if path is not None else by_id.get(binding["article_id"])
            if node is not None:
                article, article_path, article_sha = node.id, node.path, node.source_sha256
            elif path in documents:
                article, article_sha = documents[path]
                article_path = path
            if article is None:
                state = "missing-article"
            elif value["source"]["type"] not in {"generic", "html"}:
                article, article_path, article_sha, state = None, None, None, "different-representation"
            else:
                # The digest checks the local article bound by the author. It
                # does not verify remote content or a generated HTML selector.
                state = "bound-revision" if article_sha == binding["sha256"] else "historical-revision"
        attachments.append(Attachment(index, source, article, state, record, article_path, article_sha))
    return tuple(attachments)


def inspect_project_companions(project: str | Path, *, config: str | Path | None = None) -> dict:
    from .graph import load_graph
    from .runtime import resolve_runtime_paths
    paths = resolve_runtime_paths(project)
    companions = load_project_companions(paths.project_root, config=config)
    attachments = index_attachments(companions, load_graph(paths.blueprint_dir), project=paths.project_root)
    return {**_report(companions.snapshot), "configured": companions.configured,
            "attachment_revision": companions.revision, "attachments": [
                {"occurrence": a.occurrence, "source": a.source, "article": a.article, "state": a.state,
                 "annotation": a.record.annotation, "origin": a.record.origin} for a in attachments]}


def _html_endpoint(label: str, reference: str) -> str:
    escaped = html.escape(reference, quote=True)
    text = f"<strong>{label}:</strong> <code>{escaped}</code>"
    try:
        parsed = urlsplit(reference)
        safe = (parsed.scheme.lower() in {"http", "https"} and parsed.hostname
                and parsed.username is None and parsed.password is None)
    except ValueError:
        safe = False
    if safe:
        href = html.escape(quote(reference, safe="/:?#[]@!$&*+,;=%~-._"), quote=True)
        text += f' <a href="{href}" rel="noopener noreferrer">Open {label.lower()}</a>'
    return "<p>" + text + "</p>"


def render_attachment(attachment: Attachment) -> str:
    """Inert, escaped source evidence, without local origin metadata in the site."""
    value = attachment.record.annotation
    state = {
        "bound-revision": "The explicit binding matches this article's recorded bytes; endpoint and selector unverified.",
        "historical-revision": "Historical association: this article differs from the bound revision. Locator not applied.",
        "missing-article": "The explicitly bound article is missing; this association has not been retargeted.",
        "different-representation": "The source is a different representation; no wiki-article attachment was inferred.",
        "unresolved-source": "The source reference could not be resolved against its original carrier.",
        "external-source": "Independent informal source; no wiki-article binding was supplied.",
    }[attachment.state]
    if "location" in value:
        location = "<p>Supplied location, unresolved (generic locations remain opaque):</p><pre><code>" + html.escape(
            _json(value["location"], pretty=True)) + "</code></pre>"
    else:
        location = "<p>No location supplied: whole informal source.</p>"
    # Display exact authored fields; only show resolved HTTP(S) destinations as
    # links. Local carrier paths are deliberately kept in the private archive.
    endpoints = []
    target = _target_reference(value)
    references = [("Informal source", value["source"]["uri"])]
    if target is not None:
        references.append(("Formal target", target))
    for label, reference in references:
        endpoint = _html_endpoint(label, reference)
        match = _PARTS.fullmatch(reference)
        if match is None or match["scheme"] is None:
            endpoint += "<p>Relative reference; original carrier retained in the companion archive.</p>"
            try:
                resolved = resolve_reference(attachment.record.base_uri, reference)
                if urlsplit(resolved).scheme.lower() in {"http", "https"}:
                    endpoint += _html_endpoint("Resolved " + label.lower(), resolved)
            except (CompanionImportError, ValueError):
                pass
        endpoints.append(endpoint)
    if target is None:
        endpoints.append("<p><strong>Formal target:</strong> none supplied. This record describes an "
                         "informal passage with no formal counterpart.</p>")
    if "status" in value:
        status = html.escape(_json(value["status"]), quote=True)
        endpoints.append(f"<p><strong>Producer status:</strong> <code>{status}</code> "
                         "(producer data, not Autoform proof or review state)</p>")
    return (f'<details class="autoform-companion" data-companion-occurrence="{attachment.occurrence}" '
            f'data-companion-state="{attachment.state}"><summary>Companion association '
            f'{attachment.occurrence}</summary><p>{state}</p>' + "".join(endpoints) + location
            + "<p>This association is source evidence, not Autoform proof or review state.</p>"
            + "<details><summary>Complete original annotation</summary><pre><code>"
            + html.escape(_json(value, pretty=True)) + "</code></pre></details></details>")


def render_article_attachment_blocks(attachments) -> dict[str, str]:
    """Group once so attachment rendering stays linear in the occurrence count."""
    groups: dict[str, list[Attachment]] = {}
    for attachment in attachments:
        if attachment.article is not None:
            groups.setdefault(attachment.article, []).append(attachment)
    return {article: '<section class="autoform-companions"><h3>Companion associations</h3>' + "\n".join(
        render_attachment(a) for a in selected) + "</section>" for article, selected in groups.items()}


def render_companion_index(attachments, *, article_links: dict[str, str]) -> str:
    groups: dict[tuple[str, str], list[Attachment]] = {}
    for attachment in attachments:
        # Unresolved relative references from different carriers are not one
        # informal resource. Their private origin participates only in grouping.
        origin = attachment.record.base_uri if attachment.state == "unresolved-source" else ""
        groups.setdefault((attachment.source, origin), []).append(attachment)
    lines = ["# Companion sources", "Independent informal resources and their retained formal associations. "
             "These links do not establish proof, equivalence, or selector resolution."]
    for index, occurrences in enumerate(groups.values(), 1):
        lines.append(f"## Informal resource {index}")
        source_reference = occurrences[0].record.annotation["source"]["uri"]
        lines.append(_html_endpoint("Informal source", source_reference))
        # Relative file carriers remain private; expose only authored text or a
        # navigable HTTP(S) identity outside the individual disclosures.
        if source_reference != occurrences[0].source:
            try:
                if urlsplit(occurrences[0].source).scheme.lower() in {"http", "https"}:
                    lines.append(_html_endpoint("Resolved informal source", occurrences[0].source))
            except ValueError:
                pass
        for attachment in occurrences:
            if attachment.article in article_links:
                href = html.escape(article_links[attachment.article], quote=True)
                lines.append(f'<p><a href="{href}">View on the original wiki article</a></p>')
            lines.append(render_attachment(attachment))
    return "\n\n".join(lines) + "\n"
