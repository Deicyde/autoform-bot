"""Export committed blueprint associations as independent FCA v2 annotations.

This is a derived interchange view. Markdown remains authoritative, and a
lexical source link says nothing about proof completion or correspondence.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .lean import _scan, _without_lean_comments, snapshot_project_sources
from .runtime import load_runtime_graph, resolve_runtime_paths
from .skeleton import SkeletonError, _rename_no_replace

_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REPOSITORY = re.compile(r"(?:https://)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\Z")
EXTENSION = "org.autoform"


class AnnotationExportError(ValueError):
    """A snapshot cannot be exported without inventing evidence."""


@dataclass(frozen=True)
class AnnotationExport:
    annotations: tuple[dict, ...]
    diagnostics: tuple[dict, ...]
    repo: str
    commit: str

    def report(self) -> dict:
        """Operational report, deliberately separate from annotation shards."""
        return {
            "complete": not self.diagnostics,
            "annotations": len(self.annotations),
            "repo": self.repo,
            "commit": self.commit,
            "diagnostics": list(self.diagnostics),
        }


def _git(root: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "--no-replace-objects", "--literal-pathspecs", "-C", str(root), *arguments],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
            env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AnnotationExportError("cannot read the local Git snapshot") from error
    if result.returncode:
        raise AnnotationExportError("the requested commit or file is not available in local Git")
    return result.stdout


def _committed(root: Path, commit: str, path: str, content: bytes) -> None:
    record = _git(root, "ls-tree", "-z", "--full-tree", commit, "--", path)
    parts = record.removesuffix(b"\0").split(b"\t")
    header = parts[0].split() if len(parts) == 2 else []
    if (len(header) != 3 or header[0] not in (b"100644", b"100755") or header[1] != b"blob"
            or parts[1] != path.encode("utf-8")):
        raise AnnotationExportError(f"{path}: not a regular file in the requested commit")
    algorithm = hashlib.sha1 if len(commit) == 40 else hashlib.sha256
    blob_id = algorithm(b"blob " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest()
    if header[2].decode("ascii") != blob_id:
        raise AnnotationExportError(f"{path}: working content differs from the requested commit")


def _plain_public_declaration(content: bytes, declaration) -> bool:
    # The shared lexical index has no privacy/attribute elaboration. Refuse
    # modifier/attribute continuation lines instead of guessing private names.
    lines = _without_lean_comments(content.decode("utf-8")).splitlines()
    prefix = lines[declaration.line - 1].split(declaration.keyword, 1)[0]
    if re.search(r"\bprivate\b", prefix):
        return False
    preceding = next((line.strip() for line in reversed(lines[:declaration.line - 1]) if line.strip()), "")
    modifiers = r"(?:private|protected|noncomputable|partial|unsafe|scoped|local)"
    return not (
        re.fullmatch(rf"{modifiers}(?:\s+{modifiers})*", preceding)
        or preceding.startswith("@[") or preceding.endswith("]")
    )


def export_annotations(project_or_blueprint: str | Path, *, repo: str, commit: str) -> AnnotationExport:
    """Return one FCA v2 record per committed article/local Lean association.

    GitHub is the only supported host. The caller explicitly supplies repository
    identity; availability or ownership of that remote is not checked. Sources
    are retained before hashing/link generation. External declarations require
    a future pinned library resolver and are reported as omissions now.
    """
    match = _REPOSITORY.fullmatch(repo)
    if not match or any(part in (".", "..") for part in match.groups()):
        raise AnnotationExportError("repo must be an explicit GitHub owner/repository URL without a trailing slash or .git")
    owner, repository = match.groups()
    if repository.endswith(".git"):
        raise AnnotationExportError("repo must omit the .git suffix")
    repo = f"https://github.com/{owner}/{repository}"
    if not _COMMIT.fullmatch(commit):
        raise AnnotationExportError("commit must be a full lowercase Git object ID")
    paths = resolve_runtime_paths(project_or_blueprint)
    runtime = load_runtime_graph(project_or_blueprint)
    root = Path(_git(paths.project_root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if _git(root, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip() != commit:
        raise AnnotationExportError("commit must identify a commit directly, not a tag object")
    project_prefix = paths.project_root.relative_to(root)
    snapshot = snapshot_project_sources(paths.project_root)
    lean_bytes = dict(snapshot.source_files)
    name_counts = Counter(
        declaration.name
        for source_path, content in snapshot.source_files
        for declaration in _scan(content.decode("utf-8", errors="replace"), source_path)
    )
    annotations, diagnostics = [], []

    def omitted(node, code: str, *, declaration: str | None = None) -> None:
        diagnostic = {"code": code, "article": node.article_path}
        if declaration is not None:
            diagnostic["declaration"] = declaration
        diagnostics.append(diagnostic)

    for node in runtime.nodes:
        # Check every article: container assertions also affect derived status.
        path = (project_prefix / node.article_path).as_posix()
        supplied = root / path
        if any(part.is_symlink() for part in (supplied, *supplied.parents)):
            raise AnnotationExportError(f"{path}: article path contains a symbolic link")
        content = supplied.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != node.source_sha256:
            raise AnnotationExportError(f"{path}: article changed while the export was being read")
        _committed(root, commit, path, content)
        if (node.lean_targets or node.mathlib_declarations) and node.article_id is None:
            omitted(node, "MISSING_ARTICLE_ID")
            continue
        for name in dict.fromkeys(target.declaration for target in node.lean_targets):
            declaration = snapshot.index.find(name)
            if name_counts[name] > 1:
                omitted(node, "AMBIGUOUS_DECLARATION", declaration=name)
                continue
            if declaration is None:
                omitted(node, "DECLARATION_NOT_FOUND", declaration=name)
                continue
            lean_path = (project_prefix / declaration.path).as_posix()
            lean_content = lean_bytes[declaration.path]
            if not _plain_public_declaration(lean_content, declaration):
                omitted(node, "DECLARATION_REQUIRES_ELABORATED_INDEX", declaration=name)
                continue
            _committed(root, commit, lean_path, lean_content)
            source_uri = f"https://raw.githubusercontent.com/{owner}/{repository}/{commit}/{quote(path, safe='/')}"
            target_uri = f"{repo}/blob/{commit}/{quote(lean_path, safe='/')}#L{declaration.line}"
            identity = json.dumps([repo, commit, node.article_id, name], ensure_ascii=True, separators=(",", ":"))
            annotations.append({
                "version": 2,
                "id": "urn:autoform:annotation:" + hashlib.sha256(identity.encode("ascii")).hexdigest(),
                "source": {
                    "type": "generic", "uri": source_uri,
                    "extensions": {EXTENSION: {"media_type": "text/markdown", "sha256": digest}},
                },
                # No location means the whole authored Markdown article. We do
                # not invent PDF/page coordinates for citations inside it.
                "target": target_uri,
                "extensions": {EXTENSION: {
                    "article_id": node.article_id, "article_path": path, "title": node.title,
                    "repo": repo, "commit": commit,
                    "declaration": {
                        "name": name, "kind": declaration.keyword, "path": lean_path,
                        "line": declaration.line, "sha256": hashlib.sha256(lean_content).hexdigest(),
                    },
                    "workflow_assertions": {**node.assertions.as_dict(), "mathlib": node.mathlib},
                    "derived_status": node.status.as_dict(),
                    "resolution": "lexical-source-index", "verification": "not-performed",
                }},
            })
        for name in dict.fromkeys(node.mathlib_declarations):
            omitted(node, "EXTERNAL_DECLARATION_REQUIRES_PINNED_INDEX", declaration=name)
    if not annotations and not diagnostics:
        diagnostics.append({"code": "NO_ASSOCIATIONS"})
    return AnnotationExport(tuple(annotations), tuple(diagnostics), repo, commit)


def write_annotation_shards(result: AnnotationExport, output: str | Path) -> Path:
    """Atomically publish a new directory; never replace an existing output.

    ``annotations/`` contains only atomic JSON records. ``report.json`` is an
    operational report outside that scan root, not another companion format.
    """
    destination = Path(output).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise AnnotationExportError("output already exists; choose a new directory")
    if not destination.parent.is_dir():
        raise AnnotationExportError("output parent directory does not exist")
    stage = Path(tempfile.mkdtemp(prefix=".autoform-annotations-", dir=destination.parent))
    try:
        shards = stage / "annotations"
        shards.mkdir()
        for index, annotation in enumerate(result.annotations, start=1):
            # Carrier names never depend on opaque application IDs.
            filename = f"{index:06d}.json"
            (shards / filename).write_text(json.dumps(annotation, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        (stage / "report.json").write_text(json.dumps(result.report(), ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        try:
            _rename_no_replace(stage, destination)
        except SkeletonError as error:
            raise AnnotationExportError("cannot publish annotation output: " + "; ".join(error.issues)) from error
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return destination
