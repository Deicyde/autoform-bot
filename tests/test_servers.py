"""Contracts for Autoform's domain-oriented server boundary."""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest


def make_lake_project(tmp_path, name: str = "lean-project"):
    project = tmp_path / name
    project.mkdir()
    (project / "lakefile.toml").write_text('[package]\nname = "Test"\n')
    return project


def required_tool_parameters(server):
    tools = asyncio.run(server.list_tools())
    return {tool.name: set(tool.parameters["required"]) for tool in tools}


class TestProjectResolution:
    """Lean roots are explicit and never inferred from the server cwd."""

    def test_accepts_absolute_lake_project(self, tmp_path):
        from servers import resolve_lean_project_dir

        project = make_lake_project(tmp_path)
        assert resolve_lean_project_dir(str(project)) == project.resolve()

    @pytest.mark.parametrize("value", ["", ".", "relative/project"])
    def test_rejects_missing_or_relative_project(self, value):
        from servers import resolve_lean_project_dir

        with pytest.raises(ValueError, match="project_dir"):
            resolve_lean_project_dir(value)

    def test_rejects_directory_without_lake_metadata(self, tmp_path):
        from servers import resolve_lean_project_dir

        with pytest.raises(ValueError, match="not a Lake project"):
            resolve_lean_project_dir(str(tmp_path))

    def test_resolves_relative_file_from_project(self, tmp_path):
        from servers import resolve_lean_file

        project = make_lake_project(tmp_path)
        (project / "Autoform").mkdir()
        (project / "Autoform" / "Main.lean").write_text("example : True := by trivial\n")
        root, lean_file = resolve_lean_file(str(project), "Autoform/Main.lean")
        assert root == project.resolve()
        assert lean_file == project.resolve() / "Autoform/Main.lean"

    def test_rejects_file_outside_declared_project(self, tmp_path):
        from servers import resolve_lean_file

        project = make_lake_project(tmp_path)
        outside = tmp_path / "Outside.lean"
        outside.write_text("example : True := by trivial\n")

        with pytest.raises(ValueError, match="inside project_dir"):
            resolve_lean_file(str(project), str(outside))

    @pytest.mark.parametrize("name", ["Missing.lean", "README.md"])
    def test_rejects_missing_or_non_lean_file(self, tmp_path, name):
        from servers import resolve_lean_file

        project = make_lake_project(tmp_path)
        if name.endswith(".md"):
            (project / name).write_text("not Lean\n")

        with pytest.raises(ValueError, match="file_path"):
            resolve_lean_file(str(project), name)

    def test_lake_environment_ignores_ambient_toolchain_overrides(self, monkeypatch):
        from servers import clean_lake_environment

        poisoned = {
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
        for name in poisoned:
            monkeypatch.setenv(name, "poisoned")
        monkeypatch.setenv("AUTOFORM_SENTINEL", "kept")

        environment = clean_lake_environment()

        assert poisoned.isdisjoint(environment)
        assert environment["AUTOFORM_SENTINEL"] == "kept"

    def test_lake_environment_uses_the_effective_project_toolchain(
        self, tmp_path, monkeypatch
    ):
        from servers import clean_lake_environment

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "lean-toolchain").write_text("leanprover/lean4:v4.32.0\n")
        project = make_lake_project(workspace)
        monkeypatch.setenv("ELAN_TOOLCHAIN", "leanprover/lean4:v4.34.0")

        environment = clean_lake_environment(project)

        assert environment["ELAN_TOOLCHAIN"] == "leanprover/lean4:v4.32.0"

    def test_lake_environment_does_not_trust_lean_sysroot_to_filter_path(
        self, tmp_path, monkeypatch
    ):
        from servers import clean_lake_environment

        monkeypatch.setenv("ELAN_HOME", str(tmp_path / "missing-elan"))
        monkeypatch.setenv("LEAN_SYSROOT", "/")
        monkeypatch.setenv("PATH", os.pathsep.join(("/usr/bin", "/bin")))

        environment = clean_lake_environment()

        assert "LEAN_SYSROOT" not in environment
        assert environment["PATH"] == os.pathsep.join(("/usr/bin", "/bin"))

    @pytest.mark.skipif(os.name != "posix", reason="executable probe uses POSIX scripts")
    def test_lake_environment_routes_bare_lake_through_the_elan_proxy(
        self, tmp_path, monkeypatch
    ):
        elan_home = tmp_path / "elan"
        proxy_bin = elan_home / "bin"
        wrong_bin = elan_home / "toolchains" / "wrong" / "bin"
        project_bin = tmp_path / "project" / ".lake" / "build" / "bin"
        for directory in (proxy_bin, wrong_bin, project_bin):
            directory.mkdir(parents=True)
        for executable in (proxy_bin / "elan", proxy_bin / "lake"):
            executable.write_text("#!/bin/sh\nprintf 'proxy:%s' \"$ELAN_TOOLCHAIN\"\n")
            executable.chmod(0o755)
        for executable in (wrong_bin / "lake", project_bin / "lake"):
            executable.write_text("#!/bin/sh\nprintf 'wrong'\n")
            executable.chmod(0o755)

        monkeypatch.setenv("ELAN_HOME", str(elan_home))
        project = tmp_path / "project"
        (project / "lean-toolchain").write_text("leanprover/lean4:v4.32.0\n")
        monkeypatch.setenv(
            "PATH",
            os.pathsep.join((str(wrong_bin), str(project_bin), str(proxy_bin))),
        )

        from servers import clean_lake_environment

        environment = clean_lake_environment(project)

        assert environment["PATH"].split(os.pathsep) == [str(proxy_bin)]
        completed = subprocess.run(
            ["lake"],
            check=True,
            capture_output=True,
            env=environment,
            text=True,
        )
        assert completed.stdout == "proxy:leanprover/lean4:v4.32.0"

    def test_project_fingerprint_tracks_root_and_config_identity(self, tmp_path):
        from servers import lean_project_fingerprint

        project = make_lake_project(tmp_path)
        original = lean_project_fingerprint(project)
        displaced = tmp_path / "displaced"
        project.rename(displaced)
        replacement = make_lake_project(tmp_path)

        assert lean_project_fingerprint(replacement).root != original.root

        before_config_replacement = lean_project_fingerprint(replacement)
        lakefile = replacement / "lakefile.toml"
        metadata = lakefile.stat()
        replacement_lakefile = replacement / "replacement.toml"
        replacement_lakefile.write_text(lakefile.read_text())
        replacement_lakefile.replace(lakefile)
        lakefile.chmod(metadata.st_mode)
        lakefile_stat = lakefile.stat()
        # Preserve the old size and timestamp so identity, not coarse metadata,
        # is what detects the replacement.
        os.utime(lakefile, ns=(lakefile_stat.st_atime_ns, metadata.st_mtime_ns))

        assert lean_project_fingerprint(replacement) != before_config_replacement

    def test_project_fingerprint_tracks_the_effective_ancestor_toolchain(
        self, tmp_path
    ):
        from servers import lean_project_fingerprint

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        toolchain = workspace / "lean-toolchain"
        toolchain.write_text("leanprover/lean4:v4.31.0\n")
        project = make_lake_project(workspace)
        inherited = lean_project_fingerprint(project)

        toolchain.write_text("leanprover/lean4:v4.34.0\n")
        changed_parent = lean_project_fingerprint(project)
        (project / "lean-toolchain").write_text("leanprover/lean4:v4.32.0\n")

        assert changed_parent != inherited
        assert lean_project_fingerprint(project) != changed_parent

    def test_only_initial_manifest_materialization_is_an_accepted_transition(
        self, tmp_path
    ):
        from servers import (
            is_initial_manifest_materialization,
            lean_project_fingerprint,
        )

        project = make_lake_project(tmp_path)
        before = lean_project_fingerprint(project)
        manifest = project / "lake-manifest.json"
        manifest.write_text('{"version": "1.1.0"}\n')
        materialized = lean_project_fingerprint(project)

        assert is_initial_manifest_materialization(before, materialized) is True
        assert is_initial_manifest_materialization(materialized, materialized) is False

        manifest.unlink()
        manifest.mkdir()
        assert (
            is_initial_manifest_materialization(
                before, lean_project_fingerprint(project)
            )
            is False
        )

        manifest.rmdir()
        manifest.write_text('{"version": "1.1.0"}\n')
        (project / "lakefile.toml").write_text('[package]\nname = "Changed"\n')

        assert (
            is_initial_manifest_materialization(
                before, lean_project_fingerprint(project)
            )
            is False
        )


# ---------------------------------------------------------------------------
# REPL server
# ---------------------------------------------------------------------------


class TestReplServer:
    """Contracts for the persistent Lean REPL server."""

    def test_import_server(self):
        from servers.repl import server  # noqa: F401

    def test_import_core(self):
        from servers.repl import core  # noqa: F401

    def test_import_pool(self):
        from servers.repl import pool  # noqa: F401

    def test_create_server(self):
        from servers.repl.server import create_repl_server

        server = create_repl_server(object())
        assert server is not None
        assert server.name == "autoform-repl"

    def test_tools_require_project_dir(self):
        from servers.repl.server import create_repl_server

        required = required_tool_parameters(create_repl_server(object()))
        assert required["run_lean_code"] == {"project_dir", "code"}
        assert required["get_repl_status"] == {"project_dir"}
        assert set(required) == {"run_lean_code", "get_repl_status"}

    def test_project_router_reuses_and_separates_pools(self, tmp_path):
        from servers.repl.projects import LeanReplProjects

        class FakePool:
            def __init__(self, root):
                self.root = root
                self.closed = False

            def shutdown(self):
                self.closed = True

        created = []

        def factory(root):
            pool = FakePool(root)
            created.append(pool)
            return pool

        first = make_lake_project(tmp_path, "first")
        second = make_lake_project(tmp_path, "second")
        projects = LeanReplProjects(factory)

        assert projects.get(str(first)) is projects.get(str(first))
        assert projects.get(str(first)) is not projects.get(str(second))
        assert [pool.root for pool in created] == [first.resolve(), second.resolve()]

        projects.shutdown()
        assert all(pool.closed for pool in created)


# ---------------------------------------------------------------------------
# LSP backend
# ---------------------------------------------------------------------------


class TestLspServer:
    """Contracts for the Lean language-server service."""

    def test_import_server(self):
        from servers.lsp import server  # noqa: F401

    def test_create_server(self):
        from servers.lsp.server import create_lsp_server

        server = create_lsp_server(object())
        assert server is not None
        assert server.name == "autoform-lsp"

    def test_tools_require_project_dir(self):
        from servers.lsp.server import create_lsp_server

        required = required_tool_parameters(create_lsp_server(object()))
        assert required["lean_diagnostic_messages"] == {"project_dir", "file_path"}
        assert required["lean_hover"] == {"project_dir", "file_path", "line", "character"}
        assert set(required) == {"lean_diagnostic_messages", "lean_hover"}

    def test_project_router_reuses_and_separates_sessions(self, tmp_path):
        from servers.lsp.server import LeanLspProjects

        class FakeSession:
            def __init__(self, root):
                self.root = root
                self.closed = False

            def close(self):
                self.closed = True

        created = []

        def factory(root):
            session = FakeSession(root)
            created.append(session)
            return session

        first = make_lake_project(tmp_path, "first")
        second = make_lake_project(tmp_path, "second")
        projects = LeanLspProjects(factory)

        assert projects.get(str(first)) is projects.get(str(first))
        assert projects.get(str(first)) is not projects.get(str(second))
        assert [session.root for session in created] == [first.resolve(), second.resolve()]

        projects.close()
        assert all(session.closed for session in created)
