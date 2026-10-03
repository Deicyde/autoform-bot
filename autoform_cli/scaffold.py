"""Write a blueprint vault deterministically instead of describing one.

Setup used to instruct an agent, in prose, to create ``blueprint/`` with a
landing page, ``roadmap/``, ``coverage/``, and ``sources/``, and to imitate the
bundled example. Agents improvise: a real project came back with chapter pages
as siblings of their directories rather than as ``<chapter>/README.md``, which
parses cleanly and publishes a book with no chapters at all. The structure is
fixed, so the tool writes it.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .provenance import normalize_git_source

_TEMPLATES = Path(__file__).resolve().parent / "templates"

#: Template paths whose leading dot is dropped on disk so packaging tools and
#: ignore rules do not swallow them.
_DOTTED = {
    "gitignore": ".gitignore",
    "blueprint/gitignore": "blueprint/.gitignore",
    "github": ".github",
}

DEFAULT_AUTOFORM_SOURCE = "https://github.com/facebookresearch/autoform-bot.git"
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_TEMPLATE_PLACEHOLDER = re.compile(r"\{\{(?P<name>[A-Z][A-Z0-9_]*)\}\}")
_TemplateSnapshot = tuple[tuple[str, bytes, int], ...]
_MAX_TEMPLATE_ENTRIES = 1_024
_MAX_TEMPLATE_FILE_BYTES = 4 * 1024 * 1024
_MAX_TEMPLATE_TOTAL_BYTES = 16 * 1024 * 1024
_MAX_TEMPLATE_DEPTH = 32


def _normalize_autoform_source(source: str, *, allow_github_scp: bool = False) -> str | None:
    """Compatibility wrapper for explicit workflow-source validation."""

    return normalize_git_source(source, allow_github_scp=allow_github_scp)


def plugin_pin() -> tuple[str, str]:
    """Return recorded all-or-nothing provenance for legacy callers."""

    from .provenance import plugin_pin as recorded_plugin_pin

    return recorded_plugin_pin()


def _filesystem_template_snapshot(root: Path) -> _TemplateSnapshot:
    """Capture one bounded, link-free snapshot of the packaged templates."""

    entries: list[tuple[str, bytes, int]] = []
    entry_count = 0
    byte_count = 0

    def visit(directory: Path, prefix: str, depth: int) -> None:
        nonlocal entry_count, byte_count
        if depth > _MAX_TEMPLATE_DEPTH:
            raise ScaffoldError(["local Autoform template tree is too deep"])
        children = sorted(os.scandir(directory), key=lambda entry: entry.name)
        entry_count += len(children)
        if entry_count > _MAX_TEMPLATE_ENTRIES:
            raise ScaffoldError(["local Autoform template tree is too large"])
        for child in children:
            name = child.name
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                raise ScaffoldError(["local Autoform template tree is invalid"])
            relative = f"{prefix}/{name}" if prefix else name
            before = child.stat(follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                raise ScaffoldError(["local Autoform template tree contains a link"])
            if stat.S_ISDIR(before.st_mode):
                if name != "__pycache__":
                    visit(Path(child.path), relative, depth + 1)
                continue
            if not stat.S_ISREG(before.st_mode):
                raise ScaffoldError(["local Autoform template tree contains a special file"])
            if name.endswith(".pyc"):
                continue
            if before.st_size > _MAX_TEMPLATE_FILE_BYTES:
                raise ScaffoldError(["local Autoform template tree is too large"])
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(child.path, flags)
            try:
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino, opened.st_size) != (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                ):
                    raise ScaffoldError(["local Autoform template tree changed"])
                with os.fdopen(descriptor, "rb", closefd=False) as source:
                    content = source.read(_MAX_TEMPLATE_FILE_BYTES + 1)
                if len(content) != opened.st_size or len(content) > _MAX_TEMPLATE_FILE_BYTES:
                    raise ScaffoldError(["local Autoform template tree changed"])
            finally:
                os.close(descriptor)
            byte_count += len(content)
            if byte_count > _MAX_TEMPLATE_TOTAL_BYTES:
                raise ScaffoldError(["local Autoform template tree is too large"])
            entries.append((relative, content, stat.S_IMODE(opened.st_mode)))

    try:
        before = root.lstat()
        if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise ScaffoldError(["local Autoform template tree is invalid"])
        visit(root, "", 0)
        after = root.lstat()
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            raise ScaffoldError(["local Autoform template tree changed"])
    except ScaffoldError:
        raise
    except (MemoryError, OSError) as error:
        raise ScaffoldError(["local Autoform templates cannot be read safely"]) from error
    return tuple(entries)


class ScaffoldError(ValueError):
    """The project could not be scaffolded safely."""

    def __init__(self, issues: list[str] | tuple[str, ...]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(self.issues))


@dataclass(frozen=True, slots=True)
class ScaffoldResult:
    """What a scaffold run wrote, and what it left alone."""

    project: str
    written: tuple[str, ...]
    skipped: tuple[str, ...]
    unpinned: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "project": self.project,
            "written": list(self.written),
            "skipped": list(self.skipped),
            "unpinned": self.unpinned,
        }


def _destination(relative: str) -> str:
    for template_prefix, real_prefix in _DOTTED.items():
        if relative == template_prefix:
            return real_prefix
        if relative.startswith(f"{template_prefix}/"):
            return real_prefix + relative[len(template_prefix) :]
    return relative


def _yaml_scalar(value: str) -> str:
    """Serialize *value* as a quoted YAML scalar.

    JSON strings are valid YAML double-quoted scalars. Using the standard JSON
    serializer preserves the value while escaping line breaks, tabs, nulls,
    quotes, backslashes, and every other control character that could otherwise
    alter the generated document.
    """
    return json.dumps(value, ensure_ascii=False)


def _render(text: str, substitutions: dict[str, str]) -> str:
    """Substitute tokens from the original template exactly once.

    Replacement values are user-controlled in several templates. A sequential
    series of ``str.replace`` calls can reinterpret token-shaped text inside an
    earlier value, corrupting YAML and Markdown or exposing another generated
    value. A single regex pass never scans replacement content again.
    """

    return _TEMPLATE_PLACEHOLDER.sub(
        lambda match: substitutions.get(match.group("name"), match.group(0)),
        text,
    )


def _atomic_write(destination: Path, content: bytes, *, mode: int) -> None:
    """Replace *destination* from a same-directory temporary file.

    Replacing rather than truncating is essential when an existing destination
    has hard links: ``--force`` must not modify another path to the old inode.
    """

    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(mode)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _within(path: Path, root: Path) -> bool:
    """True when *path* resolves to somewhere at or beneath *root*.

    Resolution follows symlinks, so this is what confines the scaffold: it is
    not enough to reject a symlinked project root, because a link one level
    down -- `project/blueprint` pointing elsewhere -- redirects the whole vault
    out of the project, and `--force` would then overwrite files there.
    """
    try:
        path.resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def scaffold_project(
    target: str | Path,
    *,
    title: str,
    repository_url: str = "",
    autoform_source: str = "",
    autoform_ref: str = "",
    force: bool = False,
) -> ScaffoldResult:
    """Write the blueprint vault, site config, and CI into *target*.

    Existing files are never overwritten unless *force* is set; they come back
    in ``skipped`` so a repair run reports exactly what it left in place.
    """

    requested = Path(target).expanduser()
    issues: list[str] = []
    if not title.strip():
        issues.append("project title must not be empty")
    # Checked before resolve(), which would collapse the link and hide it.
    if requested.is_symlink():
        issues.append(f"refusing to scaffold into a symlink: {requested}")
    root = requested.resolve()
    if root.exists() and not root.is_dir():
        issues.append(f"target exists and is not a directory: {root}")
    # A branch name or an abbreviated sha is the same silent failure this
    # gate exists to prevent, just supplied by hand: CI would reinstall a
    # different Autoform later and break a project that was passing.
    # Git treats a sha case-insensitively and always prints lowercase, so an
    # uppercase one pasted from a web UI is valid input, not a mistake.
    given_ref = autoform_ref.strip().lower()
    if given_ref and not _FULL_SHA.fullmatch(given_ref):
        issues.append(
            f"--autoform-ref must be a full 40-character commit sha, not {given_ref!r}; "
            "branches and abbreviated shas do not stay put"
        )
    given_source = ""
    if autoform_source:
        normalized = _normalize_autoform_source(autoform_source)
        if normalized is None:
            issues.append(
                "--autoform-source must be a safe credential-free HTTPS Git URL ending in .git"
            )
        else:
            given_source = normalized
    if issues:
        raise ScaffoldError(issues)

    template_snapshot = _filesystem_template_snapshot(_TEMPLATES)
    pinned_source, pinned_ref = ("", "") if given_source or given_ref else plugin_pin()
    safe_pinned_source = _normalize_autoform_source(pinned_source, allow_github_scp=True)
    if safe_pinned_source is None or not _FULL_SHA.fullmatch(pinned_ref.lower()):
        pinned_source, pinned_ref = "", ""
    else:
        pinned_source, pinned_ref = safe_pinned_source, pinned_ref.lower()
    source = given_source or pinned_source or DEFAULT_AUTOFORM_SOURCE
    # A ref identifies a commit in one repository. Naming a different source
    # while inheriting this checkout's HEAD produces `git+other.git@our-sha`,
    # which does not resolve there, so an explicit source carries its own ref
    # or none at all.
    ref = given_ref or ("" if given_source else pinned_ref)
    # CI installs Autoform from a Git ref. Where Autoform lives is a fixed fact
    # worth defaulting; which commit is not, and a guessed one publishes a
    # project whose first CI step fails for a reason no file in it explains. So
    # the ref alone decides: without one the workflows are skipped and reported.
    unpinned = not ref
    substitutions = {
        "PROJECT_TITLE_YAML": _yaml_scalar(title.strip()),
        "REPO_URL_YAML": _yaml_scalar(repository_url.strip()),
        "PROJECT_TITLE": title.strip(),
        "REPO_URL": repository_url.strip(),
        "AUTOFORM_SOURCE": source,
        "AUTOFORM_REF": ref,
        "AUTOFORM_SOURCE_YAML": _yaml_scalar(source),
        "AUTOFORM_REF_YAML": _yaml_scalar(ref),
    }

    written: list[str] = []
    skipped: list[str] = []
    for relative, template_content, template_mode in template_snapshot:
        if unpinned and relative.startswith("github/"):
            skipped.append(_destination(relative))
            continue
        destination = root / _destination(relative)
        # Confine every write, not just the root. Reject links outright before
        # checking whether the destination should be skipped: `exists()` is
        # false for a dangling symlink, but opening that path still follows the
        # link and can create a file outside the project.
        probe = root
        for part in Path(_destination(relative)).parts:
            probe = probe / part
            if probe.is_symlink() or (probe.exists() and not _within(probe, root)):
                raise ScaffoldError(
                    [f"refusing to write outside the project through a link: {probe}"]
                )
        if destination.exists() and not force:
            skipped.append(_destination(relative))
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if Path(relative).suffix in {".js", ".html"} or relative.endswith("gitignore"):
            content = template_content
        else:
            rendered = _render(template_content.decode("utf-8"), substitutions)
            content = rendered.encode("utf-8")
        _atomic_write(destination, content, mode=template_mode)
        written.append(_destination(relative))

    return ScaffoldResult(title.strip(), tuple(written), tuple(skipped), unpinned)


__all__ = [
    "DEFAULT_AUTOFORM_SOURCE",
    "ScaffoldError",
    "ScaffoldResult",
    "plugin_pin",
    "scaffold_project",
]
