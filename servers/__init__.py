"""Shared path validation for Autoform's LSP and REPL servers.

Lean tools always name an absolute Lake project and never infer one from the
server process's working directory.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

LAKE_PROJECT_MARKERS = ("lakefile.lean", "lakefile.toml", "lake-manifest.json")
LEAN_PROJECT_CONFIG_FILES = (
    "lake-manifest.json",
    "lakefile.toml",
    "lakefile.lean",
)
_SCRUBBED_LAKE_ENVIRONMENT = frozenset(
    {
        "ELAN",
        "ELAN_TOOLCHAIN",
        "LAKE",
        "LAKE_ARTIFACT_CACHE",
        "LAKE_CACHE_ARTIFACT_ENDPOINT",
        "LAKE_CACHE_DIR",
        "LAKE_CACHE_KEY",
        "LAKE_CACHE_REVISION_ENDPOINT",
        "LAKE_CACHE_SERVICE",
        "LAKE_CONFIG",
        "LAKE_HOME",
        "LAKE_NO_CACHE",
        "LAKE_OVERRIDE_LEAN",
        "LAKE_PKG_URL_MAP",
        "LAKE_RESTORE_ARTIFACTS",
        "LEAN",
        "LEAN_AR",
        "LEAN_CC",
        "LEAN_GITHASH",
        "LEAN_PATH",
        "LEAN_SRC_PATH",
        "LEAN_SYSROOT",
        "LEAN_WORKER_PATH",
        "PYTHONPATH",
        "RESERVOIR_API_BASE_URL",
        "RESERVOIR_API_URL",
    }
)


def _effective_lean_toolchain(project_dir: Path) -> Path | None:
    for directory in (project_dir, *project_dir.parents):
        toolchain = directory / "lean-toolchain"
        try:
            toolchain.lstat()
        except FileNotFoundError:
            continue
        return toolchain
    return None


def clean_lake_environment(project_dir: str | Path | None = None) -> dict[str, str]:
    """Return the host environment without ambient Lean/Lake path overrides."""
    environment = os.environ.copy()
    original_path = environment.get("PATH")
    for name in _SCRUBBED_LAKE_ENVIRONMENT:
        environment.pop(name, None)
    if original_path is not None:
        elan_home = Path(
            environment.get("ELAN_HOME", str(Path.home() / ".elan"))
        ).expanduser().resolve()
        toolchains = elan_home / "toolchains"
        filtered: list[str] = []
        for entry in original_path.split(os.pathsep):
            candidate = Path(entry or ".").expanduser()
            if not entry or not candidate.is_absolute():
                continue
            inside_toolchain = candidate.is_relative_to(toolchains)
            parts = candidate.parts
            lake_build_bin = (
                ".lake" in parts and "build" in parts and parts[-1:] == ("bin",)
            )
            if inside_toolchain or lake_build_bin:
                continue
            if entry not in filtered:
                filtered.append(entry)

        elan = elan_home / "bin" / "elan"
        lake = elan_home / "bin" / "lake"
        proxy_dir: str | None = None
        if elan.is_file() and lake.is_file():
            proxy_dir = str(elan.parent)
        else:
            discovered_elan = shutil.which("elan", path=os.pathsep.join(filtered))
            if discovered_elan is not None:
                discovered_dir = str(Path(discovered_elan).parent)
                if (Path(discovered_dir) / "lake").is_file():
                    proxy_dir = discovered_dir
        if proxy_dir is not None:
            filtered = [entry for entry in filtered if entry != proxy_dir]
            filtered.insert(0, proxy_dir)
        environment["PATH"] = os.pathsep.join(filtered)
    if project_dir is not None:
        toolchain_file = _effective_lean_toolchain(Path(project_dir).resolve())
        if toolchain_file is not None:
            toolchain = toolchain_file.read_text(encoding="utf-8").strip()
            if not toolchain:
                raise ValueError(f"Lean toolchain file is empty: {toolchain_file}")
            environment["ELAN_TOOLCHAIN"] = toolchain
    return environment


@dataclass(frozen=True, slots=True)
class ProjectFingerprint:
    """Filesystem identity of a project root and its Lean configuration."""

    root: tuple[int, int, int]
    files: tuple[tuple[str, int, int, int, int, int, int], ...]


def lean_project_fingerprint(project_dir: Path) -> ProjectFingerprint:
    """Return the project metadata that makes resident Lean state stale."""
    root = project_dir.stat()
    fingerprint: list[tuple[str, int, int, int, int, int, int]] = []
    toolchain = _effective_lean_toolchain(project_dir)
    if toolchain is not None:
        info = toolchain.stat()
        fingerprint.append(
            (
                str(toolchain),
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
        )
    for name in LEAN_PROJECT_CONFIG_FILES:
        path = project_dir / name
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        fingerprint.append(
            (
                name,
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
        )
    return ProjectFingerprint(
        root=(root.st_dev, root.st_ino, root.st_mode),
        files=tuple(fingerprint),
    )


def is_initial_manifest_materialization(
    before: ProjectFingerprint,
    after: ProjectFingerprint,
) -> bool:
    """Return whether only a regular, previously absent manifest appeared."""
    manifest = "lake-manifest.json"
    created = [item for item in after.files if item[0] == manifest]
    return (
        before.root == after.root
        and all(item[0] != manifest for item in before.files)
        and len(created) == 1
        and stat.S_ISREG(created[0][3])
        and before.files
        == tuple(item for item in after.files if item[0] != manifest)
    )


def resolve_lean_project_dir(project_dir: str) -> Path:
    """Return a validated, absolute Lake project directory."""
    if not isinstance(project_dir, str) or not project_dir.strip():
        raise ValueError("project_dir is required and must be an absolute Lake project path")

    path = Path(project_dir).expanduser()
    if not path.is_absolute():
        raise ValueError(f"project_dir must be absolute, got {project_dir!r}")

    try:
        path = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"project_dir does not exist: {project_dir}") from exc

    if not path.is_dir():
        raise ValueError(f"project_dir is not a directory: {path}")
    if not any((path / marker).is_file() for marker in LAKE_PROJECT_MARKERS):
        markers = ", ".join(LAKE_PROJECT_MARKERS)
        raise ValueError(f"project_dir is not a Lake project: {path} (expected one of: {markers})")
    return path


def resolve_lean_file(project_dir: str, file_path: str) -> tuple[Path, Path]:
    """Resolve an existing in-project Lean file without using cwd."""
    root = resolve_lean_project_dir(project_dir)
    if not isinstance(file_path, str) or not file_path.strip():
        raise ValueError("file_path is required")
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"file_path must stay inside project_dir: {path}") from exc
    if path.suffix != ".lean":
        raise ValueError(f"file_path must name a .lean file: {path}")
    if not path.is_file():
        raise ValueError(f"file_path does not exist: {path}")
    return root, path
