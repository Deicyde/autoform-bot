"""Sharing, lifecycle, and resource-boundary tests for the Lean runtime."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from servers import lean_client, lean_runtime
from servers.lean_client import (
    INSTALL_ID,
    INSTALL_PATH_ID,
    PROTOCOL_VERSION,
    LeanRuntimeClient,
    LeanRuntimeOutcomeUnknown,
    LeanRuntimeProtocolError,
    LeanRuntimeUnavailable,
)
from servers.lean_runtime import (
    LeanRuntimeConfig,
    LeanRuntimeServices,
    ProjectResourceCache,
    ProjectResourceBusyError,
)


def make_lake_project(tmp_path, name: str):
    project = tmp_path / name
    project.mkdir()
    (project / "lakefile.toml").write_text(f'[package]\nname = "{name}"\n')
    return project


def test_runtime_identity_tracks_all_packaged_server_modules():
    assert lean_client._RUNTIME_FILES == tuple(
        sorted((lean_client.PACKAGE_ROOT / "servers").rglob("*.py"))
    )


def test_runtime_behavior_dependency_closure_contains_active_lsp_stack():
    assert lean_client._RUNTIME_ROOT_DISTRIBUTIONS == (
        "leanclient",
        "packaging",
        "psutil",
    )
    assert {
        "anyio",
        "leanclient",
        "orjson",
        "packaging",
        "psutil",
        "tqdm",
        "watchfiles",
    } <= set(lean_client._RUNTIME_DISTRIBUTIONS)


def test_runtime_behavior_dependency_closure_ignores_optional_extras(monkeypatch):
    requirements = {
        "root-package": (
            "watchfiles>=1",
            "pytest>=7; extra == 'dev'",
            "typing-extensions; python_version < '0'",
        ),
        "watchfiles": ("anyio>=3",),
        "anyio": (),
    }

    class FakeDistribution:
        def __init__(self, name):
            self.requires = requirements[name]

    monkeypatch.setattr(
        lean_client.metadata, "distribution", lambda name: FakeDistribution(name)
    )

    assert lean_client._runtime_distribution_names(("Root_Package",)) == (
        "anyio",
        "root-package",
        "watchfiles",
    )


@pytest.mark.parametrize("distribution", lean_client._RUNTIME_DISTRIBUTIONS)
def test_runtime_build_identity_tracks_behavior_dependency_versions(
    monkeypatch, distribution
):
    versions = {name: "1.0" for name in lean_client._RUNTIME_DISTRIBUTIONS}

    class FakeDistribution:
        def __init__(self, name):
            self.version = versions[name]

        def read_text(self, filename):
            return None

    monkeypatch.setattr(
        lean_client.metadata, "distribution", lambda name: FakeDistribution(name)
    )

    original = lean_client._build_id()
    versions[distribution] += ".changed"

    assert lean_client._build_id() != original


def test_runtime_build_identity_tracks_exact_dependency_record(monkeypatch):
    record = ["sha256=old"]

    class FakeDistribution:
        version = "0.13.2"

        def read_text(self, filename):
            return record[0] if filename == "RECORD" else None

    monkeypatch.setattr(
        lean_client.metadata, "distribution", lambda name: FakeDistribution()
    )

    original = lean_client._build_id()
    record[0] = "sha256=new"

    assert lean_client._build_id() != original


def test_runtime_build_generation_tracks_dependency_install_metadata(
    tmp_path, monkeypatch
):
    record = tmp_path / "RECORD"
    record.write_text("installed")

    class FakeDistribution:
        files = (Path("package.dist-info/RECORD"),)

        def locate_file(self, path):
            return record

    monkeypatch.setattr(lean_client, "_RUNTIME_GENERATION_FILES", ())
    monkeypatch.setattr(
        lean_client.metadata, "distribution", lambda name: FakeDistribution()
    )

    assert lean_client._build_generation() == record.stat().st_mtime_ns


def test_default_runtime_ownership_paths_are_protocol_independent(
    runtime_dir, monkeypatch
):
    monkeypatch.setenv("AUTOFORM_RUNTIME_DIR", str(runtime_dir))

    paths = lean_client.default_runtime_paths()

    assert paths.lock.name == f"lean-{INSTALL_PATH_ID}.lock"
    assert paths.lifetime_lock.name == f"lean-{INSTALL_PATH_ID}.lifetime.lock"
    assert lean_client.LEGACY_LOCK_PROTOCOL_VERSIONS == (1,)
    assert paths.compatibility_lifetime_locks == (
        runtime_dir / f"lean-v1-{INSTALL_PATH_ID}.lifetime.lock",
    )
    assert f"lean-v{PROTOCOL_VERSION}-" in paths.socket.name


def runtime_config(**overrides):
    values = {
        "max_projects": 2,
        "idle_seconds": 1800.0,
        "total_repl_workers": 2,
        "repl_workers_per_project": 1,
        "repl_project_limit": 2,
        "repl_command": ("lake", "exe", "repl"),
        "lsp_command": ("lake", "serve"),
        "lsp_timeout": 60.0,
        "max_lsp_request_seconds": 600.0,
        "repl_request_timeout": 30.0,
        "max_repl_request_seconds": 240.0,
        "rpc_read_timeout": 1.0,
        "max_connections": 8,
        "response_timeout": 900.0,
    }
    values.update(overrides)
    return LeanRuntimeConfig(**values)


class FakePool:
    def __init__(self, root):
        self.root = root
        self.capacity = 1
        self._shutdown = False
        self.calls = []

    def run(self, code, **kwargs):
        self.calls.append((code, kwargs))
        return {"messages": []}

    def get_memory_usage(self):
        return 0.25

    def is_usable(self):
        return not self._shutdown

    def shutdown(self):
        self._shutdown = True


class FakeLsp:
    def __init__(self, root):
        self.root = root
        self.closed = False

    def close(self):
        self.closed = True

    def abort(self):
        self.closed = True

    def is_alive(self):
        return not self.closed


def test_runtime_reuses_one_project_pool_and_status_stays_lazy(tmp_path):
    project = make_lake_project(tmp_path, "shared")
    pools = []

    def create_pool(root):
        pool = FakePool(root)
        pools.append(pool)
        return pool

    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=create_pool,
        lsp_factory=FakeLsp,
        start_sweepers=False,
    )
    try:
        cold = services.dispatch("repl.status", {"project_dir": str(project)})
        assert cold["state"] == "cold"
        assert pools == []

        first = services.dispatch(
            "repl.run",
            {"project_dir": str(project), "code": "#check Nat", "timeout": None},
        )
        second = services.dispatch(
            "repl.run",
            {"project_dir": str(project), "code": "#check Int", "timeout": 3},
        )

        assert first == second == "Compiles successfully"
        assert len(pools) == 1
        assert [call[0] for call in pools[0].calls] == ["#check Nat", "#check Int"]
        assert 0 < pools[0].calls[0][1]["timeout"] <= 30.0
        assert 0 < pools[0].calls[1][1]["timeout"] <= 3.0
        warm = services.dispatch("repl.status", {"project_dir": str(project)})
        assert warm["state"] == "warm"
        assert warm["memory_usage_gb"] == 0.25
    finally:
        services.close()

    assert pools[0]._shutdown is True


def test_status_reports_an_active_poisoned_pool_as_retiring(tmp_path):
    project = make_lake_project(tmp_path, "retiring-status")
    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=FakePool,
        lsp_factory=FakeLsp,
        start_sweepers=False,
    )
    try:
        with services.repl_projects.lease(str(project)) as pool:
            assert pool is not None
            pool._shutdown = True

            status = services.dispatch(
                "repl.status",
                {"project_dir": str(project)},
            )

            assert status["state"] == "retiring"
            assert status["shutdown"] is True
    finally:
        services.close()


def test_cache_inspection_does_not_validate_or_retire_state(tmp_path):
    project = make_lake_project(tmp_path, "inspection")
    validations = 0

    def is_valid(resource):
        nonlocal validations
        validations += 1
        return True

    cache = ProjectResourceCache(
        lambda root: root,
        lambda resource: None,
        max_entries=1,
        idle_seconds=1800,
        is_valid=is_valid,
        start_sweeper=False,
    )
    with cache.lease(str(project)):
        pass
    before = validations

    with cache.inspect(str(project)) as resource:
        assert resource == project.resolve()

    assert validations == before
    assert cache.state(str(project)) == "warm"
    cache.close()


@pytest.mark.parametrize("insert_before_interrupt", [False, True])
def test_interrupted_cache_inspection_releases_its_lease_token(
    tmp_path,
    insert_before_interrupt,
):
    project = make_lake_project(tmp_path, "interrupted-inspection")
    cache = ProjectResourceCache(
        lambda root: root,
        lambda resource: None,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(project)):
        pass
    entry = cache._entries[project.resolve()]

    class InterruptingSet(set):
        def add(self, value):
            if insert_before_interrupt:
                super().add(value)
            raise KeyboardInterrupt("after inspection admission")

    entry.active = InterruptingSet()

    with pytest.raises(KeyboardInterrupt, match="after inspection admission"):
        with cache.inspect(str(project)):
            pass

    assert entry.active == set()
    cache.close()


def test_failed_validation_poisons_a_resource_with_another_active_lease(tmp_path):
    project = make_lake_project(tmp_path, "concurrent-validation")
    created = []
    closed = []
    validation_error = [False]

    def factory(root):
        resource = object()
        created.append(resource)
        return resource

    def is_valid(resource):
        if validation_error[0]:
            validation_error[0] = False
            raise RuntimeError("validation failed")
        return True

    cache = ProjectResourceCache(
        factory,
        closed.append,
        max_entries=1,
        idle_seconds=1800,
        is_valid=is_valid,
        start_sweeper=False,
    )
    first = cache.lease(str(project))
    second = cache.lease(str(project))
    first_resource = first.__enter__()
    assert second.__enter__() is first_resource

    validation_error[0] = True
    with pytest.raises(RuntimeError, match="validation failed"):
        first.__exit__(None, None, None)
    second.__exit__(None, None, None)

    assert closed == [first_resource]
    with cache.lease(str(project)) as replacement:
        assert replacement is not first_resource
    assert len(created) == 2
    cache.close()


def test_shared_runtime_disables_ambiguous_repl_retries(tmp_path, monkeypatch):
    from servers import lean_runtime

    project = make_lake_project(tmp_path, "at-most-once")
    configs = []

    class CapturingPool(FakePool):
        def __init__(self, config):
            configs.append(config)
            super().__init__(config.cwd)

    monkeypatch.setattr(lean_runtime, "LeanReplPool", CapturingPool)
    services = LeanRuntimeServices(
        runtime_config(),
        lsp_factory=FakeLsp,
        start_sweepers=False,
    )
    try:
        services.dispatch(
            "repl.run",
            {"project_dir": str(project), "code": "#check Nat", "timeout": 1},
        )
        assert configs[0].max_retries == 0
    finally:
        services.close()


def test_lru_limit_never_evicts_an_active_project(tmp_path):
    first = make_lake_project(tmp_path, "first")
    second = make_lake_project(tmp_path, "second")
    closed = []
    second_attempted = threading.Event()
    second_created = threading.Event()

    def factory(root):
        if root == second.resolve():
            second_created.set()
        return root

    cache = ProjectResourceCache(
        factory,
        closed.append,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    def use_second():
        second_attempted.set()
        with cache.lease(str(second)) as resource:
            assert resource == second.resolve()

    with cache.lease(str(first)) as resource:
        assert resource == first.resolve()
        thread = threading.Thread(target=use_second)
        thread.start()
        assert second_attempted.wait(timeout=1)
        assert not second_created.wait(timeout=0.1)
        assert closed == []

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert second_created.is_set()
    assert closed == [first.resolve()]
    cache.close()
    assert closed == [first.resolve(), second.resolve()]


def test_root_replacement_invalidates_a_warm_project_resource(tmp_path):
    project = make_lake_project(tmp_path, "replace-root")
    created = []
    closed = []

    def factory(root):
        resource = object()
        created.append(resource)
        return resource

    cache = ProjectResourceCache(
        factory,
        closed.append,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(project)) as first:
        pass

    moved = project.with_name("replaced-root")
    project.rename(moved)
    project.mkdir()
    (project / "lakefile.toml").write_bytes((moved / "lakefile.toml").read_bytes())

    with cache.lease(str(project)) as second:
        assert second is not first

    assert created == [first, second]
    assert closed == [first]
    cache.close()
    assert closed == [first, second]


def test_root_replacement_during_startup_discards_the_resource(tmp_path):
    project = make_lake_project(tmp_path, "replace-during-startup")
    moved = project.with_name("startup-original")
    resource = object()
    closed = []

    def factory(root):
        root.rename(moved)
        root.mkdir()
        (root / "lakefile.toml").write_bytes(
            (moved / "lakefile.toml").read_bytes()
        )
        return resource

    cache = ProjectResourceCache(
        factory,
        closed.append,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    with pytest.raises(ProjectResourceBusyError, match="changed during startup"):
        with cache.lease(str(project)):
            pytest.fail("a resource bound to the replaced root must not be leased")

    cache.close()
    assert closed == [resource]
    assert cache.state(str(project)) == "cold"


def test_factory_created_derived_directory_does_not_invalidate_startup(tmp_path):
    project = make_lake_project(tmp_path, "derived-during-startup")
    resource = object()
    closed = []

    def factory(root):
        (root / ".lake").mkdir()
        return resource

    cache = ProjectResourceCache(
        factory,
        closed.append,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    with cache.lease(str(project)) as leased:
        assert leased is resource

    assert closed == []
    cache.close()
    assert closed == [resource]


def test_project_slot_admission_stops_before_the_response_budget(tmp_path):
    first = make_lake_project(tmp_path, "busy-first")
    second = make_lake_project(tmp_path, "busy-second")
    created = []
    cache = ProjectResourceCache(
        lambda root: created.append(root) or root,
        lambda resource: None,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    with cache.lease(str(first)):
        started = time.monotonic()
        with pytest.raises(ProjectResourceBusyError, match="response budget"):
            with cache.lease(
                str(second),
                acquisition_timeout=0.05,
                creation_budget=0.02,
            ):
                pytest.fail("a busy project slot must not be admitted late")
        assert time.monotonic() - started < 0.5

    assert created == [first.resolve()]
    cache.close()


def test_project_startup_that_misses_its_budget_is_reused(tmp_path):
    project = make_lake_project(tmp_path, "slow-startup")
    clock = {"now": 0.0}
    closed = []

    def slow_factory(root):
        clock["now"] = 11.0
        return root

    cache = ProjectResourceCache(
        slow_factory,
        closed.append,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
        clock=lambda: clock["now"],
    )

    with pytest.raises(ProjectResourceBusyError, match="startup exceeded"):
        with cache.lease(
            str(project),
            acquisition_timeout=10.0,
            creation_budget=1.0,
        ):
            pytest.fail("late project startup must never execute a tool request")

    assert closed == []
    assert cache.state(str(project)) == "warm"
    with cache.lease(str(project)) as resource:
        assert resource == project.resolve()
    cache.close()
    assert closed == [project.resolve()]


def test_settlement_fingerprint_work_cannot_extend_the_startup_deadline(
    tmp_path, monkeypatch
):
    from servers import lean_runtime as lean_runtime_module

    project = make_lake_project(tmp_path, "settlement-deadline")
    real_fingerprint = lean_runtime_module.lean_project_fingerprint
    clock = {"now": 0.0}
    fingerprint_calls = 0
    entered = False

    def fingerprint(root):
        nonlocal fingerprint_calls
        fingerprint_calls += 1
        result = real_fingerprint(root)
        if fingerprint_calls == 3:
            clock["now"] = 11.0
        return result

    monkeypatch.setattr(lean_runtime_module, "lean_project_fingerprint", fingerprint)
    cache = ProjectResourceCache(
        lambda root: root,
        lambda resource: None,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
        clock=lambda: clock["now"],
    )

    with pytest.raises(ProjectResourceBusyError, match="startup exceeded"):
        with cache.lease(str(project), acquisition_timeout=10) as resource:
            entered = True
            assert resource == project.resolve()

    assert entered is False
    assert cache.state(str(project)) == "warm"
    cache.close()


def test_warm_resource_validation_cannot_extend_the_acquisition_deadline(
    tmp_path, monkeypatch
):
    from servers import lean_runtime as lean_runtime_module

    project = make_lake_project(tmp_path, "warm-validation-deadline")
    real_fingerprint = lean_runtime_module.lean_project_fingerprint
    clock = {"now": 0.0}
    cache = ProjectResourceCache(
        lambda root: root,
        lambda resource: None,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
        clock=lambda: clock["now"],
    )
    with cache.lease(str(project)):
        pass

    fingerprint_calls = 0

    def delayed_fingerprint(root):
        nonlocal fingerprint_calls
        fingerprint_calls += 1
        result = real_fingerprint(root)
        if fingerprint_calls == 2:
            clock["now"] = 11.0
        return result

    monkeypatch.setattr(
        lean_runtime_module,
        "lean_project_fingerprint",
        delayed_fingerprint,
    )
    entered = False
    with pytest.raises(ProjectResourceBusyError, match="validation exceeded"):
        with cache.lease(str(project), deadline=10.0):
            entered = True

    assert entered is False
    assert cache.stats()["resident"][0]["active"] == 0
    cache.close()


def test_warm_resource_replacement_cleanup_honors_the_acquisition_deadline(
    tmp_path, monkeypatch
):
    from servers import lean_runtime as lean_runtime_module

    project = make_lake_project(tmp_path, "warm-replacement-deadline")
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()

    def close_resource(resource):
        cleanup_started.set()
        release_cleanup.wait(timeout=2)

    cache = ProjectResourceCache(
        lambda root: root,
        close_resource,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(project)):
        pass
    stable = cache._entries[project.resolve()].fingerprint
    fingerprints = iter((stable, object()))
    monkeypatch.setattr(
        lean_runtime_module,
        "lean_project_fingerprint",
        lambda root: next(fingerprints),
    )

    started = time.monotonic()
    with pytest.raises(ProjectResourceBusyError, match="response budget"):
        with cache.lease(str(project), acquisition_timeout=0.05):
            pytest.fail("stale warm resource must not reach the request")
    assert time.monotonic() - started < 0.5
    assert cleanup_started.is_set()
    assert cache.stats()["retiring"] == [str(project.resolve())]

    release_cleanup.set()
    cache.close()


def test_blocking_project_startup_returns_at_the_acquisition_deadline(tmp_path):
    project = make_lake_project(tmp_path, "blocking-startup")
    startup_started = threading.Event()
    release_startup = threading.Event()
    closed = []

    def factory(root):
        startup_started.set()
        release_startup.wait(timeout=2)
        return root

    cache = ProjectResourceCache(
        factory,
        closed.append,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    started = time.monotonic()
    with pytest.raises(ProjectResourceBusyError, match="startup exceeded"):
        with cache.lease(
            str(project),
            acquisition_timeout=0.05,
            creation_budget=0,
        ):
            pytest.fail("late project startup must not reach the request")
    assert time.monotonic() - started < 0.5
    assert startup_started.is_set()
    assert cache.state(str(project)) == "warming"

    release_startup.set()
    deadline = time.monotonic() + 2
    while cache.state(str(project)) != "warm" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cache.state(str(project)) == "warm"
    assert closed == []
    with cache.lease(str(project), acquisition_timeout=0.1) as resource:
        assert resource == project.resolve()
    cache.close()
    assert closed == [project.resolve()]


def test_deadline_factory_receives_the_absolute_acquisition_deadline(tmp_path):
    project = make_lake_project(tmp_path, "deadline-factory")
    deadlines = []

    def deadline_factory(root, deadline):
        deadlines.append(deadline)
        return root

    cache = ProjectResourceCache(
        lambda root: pytest.fail("one-argument factory used for a timed lease"),
        lambda resource: None,
        max_entries=1,
        idle_seconds=1800,
        deadline_factory=deadline_factory,
        start_sweeper=False,
    )
    deadline = time.monotonic() + 1

    with cache.lease(str(project), deadline=deadline) as resource:
        assert resource == project.resolve()

    assert deadlines == [deadline]
    cache.close()


def test_factory_timeout_is_not_misreported_as_an_acquisition_timeout(tmp_path):
    project = make_lake_project(tmp_path, "factory-timeout")

    def factory(root):
        raise TimeoutError("factory timed out internally")

    cache = ProjectResourceCache(
        factory,
        lambda resource: None,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    with pytest.raises(TimeoutError, match="factory timed out internally"):
        with cache.lease(
            str(project),
            acquisition_timeout=1,
            creation_budget=0,
        ):
            pytest.fail("failed startup must not reach the request")
    assert cache.state(str(project)) == "cold"
    cache.close()


def test_inflight_factory_remains_owned_when_acquiring_caller_is_interrupted(
    tmp_path, monkeypatch
):
    from servers import lean_runtime as lean_runtime_module

    project = make_lake_project(tmp_path, "interrupted-startup")
    startup_started = threading.Event()
    release_startup = threading.Event()

    class InterruptingFuture(lean_runtime_module.Future):
        def result(self, timeout=None):
            if timeout is not None:
                assert startup_started.wait(timeout=1)
                raise KeyboardInterrupt("request cancelled")
            return super().result(timeout=timeout)

    def factory(root):
        startup_started.set()
        release_startup.wait(timeout=2)
        return root

    monkeypatch.setattr(lean_runtime_module, "Future", InterruptingFuture)
    cache = ProjectResourceCache(
        factory,
        lambda resource: None,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    with pytest.raises(KeyboardInterrupt, match="request cancelled"):
        with cache.lease(str(project), acquisition_timeout=1):
            pytest.fail("interrupted startup must not reach the request")
    assert cache.state(str(project)) == "warming"

    release_startup.set()
    deadline = time.monotonic() + 2
    while cache.state(str(project)) != "warm" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cache.state(str(project)) == "warm"
    cache.close()


def test_completed_factory_result_remains_owned_when_caller_is_interrupted(
    tmp_path, monkeypatch
):
    from servers import lean_runtime as lean_runtime_module

    project = make_lake_project(tmp_path, "interrupted-completed-startup")
    created = []
    closed = []

    class InterruptAfterCompletionFuture(lean_runtime_module.Future):
        def result(self, timeout=None):
            result = super().result(timeout=timeout)
            if timeout is not None:
                raise KeyboardInterrupt("request cancelled after completion")
            return result

    def factory(root):
        created.append(root)
        return root

    monkeypatch.setattr(
        lean_runtime_module,
        "Future",
        InterruptAfterCompletionFuture,
    )
    cache = ProjectResourceCache(
        factory,
        closed.append,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    with pytest.raises(KeyboardInterrupt, match="after completion"):
        with cache.lease(str(project), acquisition_timeout=1):
            pytest.fail("interrupted startup must not reach the request")

    deadline = time.monotonic() + 2
    while cache.state(str(project)) != "warm" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cache.state(str(project)) == "warm"
    assert created == [project.resolve()]
    assert closed == []
    cache.close()
    assert closed == [project.resolve()]


@pytest.mark.parametrize("launch", [False, True])
def test_interrupted_factory_thread_start_never_runs_an_unowned_factory(
    tmp_path, monkeypatch, launch
):
    from servers import lean_runtime as lean_runtime_module

    project = make_lake_project(tmp_path, f"interrupted-thread-start-{launch}")
    created = []
    worker_exited = threading.Event()
    real_thread = threading.Thread

    class InterruptedStart:
        ident = None

        def __init__(self, thread):
            self.thread = thread

        def start(self):
            if launch:
                self.thread.start()
            raise KeyboardInterrupt("thread start interrupted")

    def thread_factory(*args, **kwargs):
        if kwargs.get("name") != "autoform-project-startup":
            return real_thread(*args, **kwargs)
        target = kwargs["target"]

        def run():
            try:
                target()
            finally:
                worker_exited.set()

        kwargs["target"] = run
        return InterruptedStart(real_thread(*args, **kwargs))

    monkeypatch.setattr(lean_runtime_module.threading, "Thread", thread_factory)
    cache = ProjectResourceCache(
        lambda root: created.append(root) or root,
        lambda resource: None,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    with pytest.raises(KeyboardInterrupt, match="thread start interrupted"):
        with cache.lease(str(project), acquisition_timeout=1):
            pytest.fail("interrupted thread start must not reach the request")

    if launch:
        assert worker_exited.wait(timeout=1)
    assert created == []
    assert cache.state(str(project)) == "cold"
    cache.close()


def test_completed_stale_factory_never_closes_on_the_request_thread(
    tmp_path, monkeypatch
):
    from servers import lean_runtime as lean_runtime_module

    project = make_lake_project(tmp_path, "stale-inline-startup-finalizer")
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    cleanup_finished = threading.Event()
    real_future = lean_runtime_module.Future

    class BoundaryFuture(real_future):
        def result(self, timeout=None):
            result = super().result(timeout=timeout)
            if timeout is not None:
                raise lean_runtime_module.FutureTimeoutError
            return result

    def factory(root):
        (root / "lakefile.toml").write_text('[package]\nname = "Changed"\n')
        return root

    def close_resource(resource):
        cleanup_started.set()
        release_cleanup.wait(timeout=2)
        cleanup_finished.set()

    monkeypatch.setattr(lean_runtime_module, "Future", BoundaryFuture)
    cache = ProjectResourceCache(
        factory,
        close_resource,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    started = time.monotonic()
    with pytest.raises(ProjectResourceBusyError, match="startup exceeded"):
        with cache.lease(str(project), acquisition_timeout=1):
            pytest.fail("boundary-timeout startup must not reach the request")
    assert time.monotonic() - started < 1.5
    assert cleanup_started.wait(timeout=1)
    assert not cleanup_finished.is_set()
    assert cache.stats()["retiring"] == [str(project.resolve())]

    release_cleanup.set()
    assert cleanup_finished.wait(timeout=2)
    cache.close()


def test_cache_close_waits_for_inflight_startup_cleanup(tmp_path):
    project = make_lake_project(tmp_path, "startup-close")
    startup_started = threading.Event()
    release_startup = threading.Event()
    cleanup_finished = threading.Event()
    caller_errors = []

    def factory(root):
        startup_started.set()
        release_startup.wait(timeout=2)
        return root

    cache = ProjectResourceCache(
        factory,
        lambda resource: cleanup_finished.set(),
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    def acquire():
        try:
            with cache.lease(str(project)):
                pytest.fail("startup completed after cache shutdown")
        except RuntimeError as error:
            caller_errors.append(error)

    caller = threading.Thread(target=acquire)
    caller.start()
    assert startup_started.wait(timeout=1)

    cache_closed = threading.Event()
    closer = threading.Thread(target=lambda: (cache.close(), cache_closed.set()))
    closer.start()
    try:
        assert not cache_closed.wait(timeout=0.1)
        release_startup.set()
    finally:
        caller.join(timeout=2)
        closer.join(timeout=2)

    assert not caller.is_alive()
    assert not closer.is_alive()
    assert cache_closed.is_set()
    assert cleanup_finished.is_set()
    assert len(caller_errors) == 1
    assert "closed during startup" in str(caller_errors[0])


def test_project_change_during_startup_discards_the_new_resource(tmp_path):
    project = make_lake_project(tmp_path, "changed-startup")
    resource = object()
    closed = []

    def factory(root):
        (root / "lakefile.toml").write_text('[package]\nname = "Changed"\n')
        return resource

    cache = ProjectResourceCache(
        factory,
        closed.append,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    with pytest.raises(ProjectResourceBusyError, match="changed during startup"):
        with cache.lease(str(project)):
            pytest.fail("changed project must not publish its resource")

    cache.close()
    assert closed == [resource]
    assert cache.state(str(project)) == "cold"


def test_project_change_between_settlement_and_claim_retires_idle_resource(
    tmp_path, monkeypatch
):
    from servers import lean_runtime as lean_runtime_module

    project = make_lake_project(tmp_path, "changed-before-claim")
    resource = object()
    closed = []
    stable = lean_runtime_module.lean_project_fingerprint(project.resolve())
    fingerprints = iter((stable, stable, stable, object()))
    monkeypatch.setattr(
        lean_runtime_module,
        "lean_project_fingerprint",
        lambda root: next(fingerprints),
    )
    cache = ProjectResourceCache(
        lambda root: resource,
        closed.append,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    with pytest.raises(ProjectResourceBusyError, match="changed during startup"):
        with cache.lease(str(project)):
            pytest.fail("changed project must not reach the request")

    assert closed == [resource]
    assert cache.state(str(project)) == "cold"
    cache.close()


def test_required_project_fingerprint_rejects_pre_start_change(tmp_path):
    from servers import lean_project_fingerprint

    project = make_lake_project(tmp_path, "required-fingerprint")
    expected = lean_project_fingerprint(project.resolve())
    (project / "lakefile.toml").write_text('[package]\nname = "Changed"\n')
    created = []
    cache = ProjectResourceCache(
        lambda root: created.append(root) or root,
        lambda resource: None,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    with pytest.raises(ProjectResourceBusyError, match="changed after validation"):
        with cache.lease(str(project), required_fingerprint=expected):
            pytest.fail("changed project must not reach its factory")

    assert created == []
    cache.close()


def test_idle_ttl_never_closes_an_active_resource(tmp_path):
    project = make_lake_project(tmp_path, "idle")
    clock = {"now": 0.0}
    closed = []
    cache = ProjectResourceCache(
        lambda root: root,
        closed.append,
        max_entries=1,
        idle_seconds=10,
        start_sweeper=False,
        clock=lambda: clock["now"],
    )

    with cache.lease(str(project)):
        clock["now"] = 20
        assert cache.evict_idle() == 0
        assert closed == []

    clock["now"] = 31
    assert cache.evict_idle() == 1
    assert closed == [project.resolve()]
    cache.close()


def test_failed_retirement_blocks_replacement_without_losing_ownership(tmp_path):
    first = make_lake_project(tmp_path, "retiring-first")
    second = make_lake_project(tmp_path, "retiring-second")
    created = []
    allow_close = False

    def factory(root):
        created.append(root)
        return root

    def close(resource):
        if not allow_close:
            raise RuntimeError("cleanup failed")

    cache = ProjectResourceCache(
        factory,
        close,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(first)):
        pass

    with pytest.raises(ProjectResourceBusyError, match="failed to retire"):
        with cache.lease(str(second)):
            pytest.fail("replacement must wait for confirmed cleanup")

    assert created == [first.resolve()]
    assert cache.state(str(first)) == "retiring"
    assert cache.stats()["retiring"] == [str(first.resolve())]

    allow_close = True
    with cache.lease(str(second)) as resource:
        assert resource == second.resolve()
    assert created == [first.resolve(), second.resolve()]
    cache.close()


def test_blocking_victim_retirement_returns_at_the_acquisition_deadline(tmp_path):
    first = make_lake_project(tmp_path, "cleanup-first")
    second = make_lake_project(tmp_path, "cleanup-second")
    created = []
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    cleanup_finished = threading.Event()

    def factory(root):
        created.append(root)
        return root

    def close_resource(resource):
        if resource == first.resolve():
            cleanup_started.set()
            release_cleanup.wait(timeout=2)
            cleanup_finished.set()

    cache = ProjectResourceCache(
        factory,
        close_resource,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(first)):
        pass

    started = time.monotonic()
    with pytest.raises(ProjectResourceBusyError, match="retiring a displaced"):
        with cache.lease(str(second), deadline=time.monotonic() + 0.05):
            pytest.fail("replacement must not start while its victim is alive")
    assert time.monotonic() - started < 0.5
    assert cleanup_started.is_set()
    assert created == [first.resolve()]
    assert cache.stats()["retiring"] == [str(first.resolve())]

    release_cleanup.set()
    assert cleanup_finished.wait(timeout=2)
    with cache.lease(str(second), acquisition_timeout=1) as resource:
        assert resource == second.resolve()
    assert created == [first.resolve(), second.resolve()]
    cache.close()


def test_timed_out_victim_retirement_failure_stays_quarantined(tmp_path):
    first = make_lake_project(tmp_path, "quarantine-first")
    second = make_lake_project(tmp_path, "quarantine-second")
    created = []
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    cleanup_failed = threading.Event()
    allow_close = False

    def factory(root):
        created.append(root)
        return root

    def close_resource(resource):
        nonlocal allow_close
        if resource == first.resolve() and not allow_close:
            cleanup_started.set()
            release_cleanup.wait(timeout=2)
            cleanup_failed.set()
            raise RuntimeError("cleanup failed")

    cache = ProjectResourceCache(
        factory,
        close_resource,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(first)):
        pass

    with pytest.raises(ProjectResourceBusyError, match="retiring a displaced"):
        with cache.lease(str(second), deadline=time.monotonic() + 0.05):
            pytest.fail("replacement must not start while cleanup is unresolved")
    assert cleanup_started.is_set()
    release_cleanup.set()
    assert cleanup_failed.wait(timeout=2)

    deadline = time.monotonic() + 2
    while cache.stats()["retiring"] != [str(first.resolve())]:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert created == [first.resolve()]

    allow_close = True
    with cache.lease(str(second), acquisition_timeout=1) as resource:
        assert resource == second.resolve()
    assert created == [first.resolve(), second.resolve()]
    cache.close()


def test_retirement_thread_construction_failure_keeps_victim_quarantined(
    tmp_path, monkeypatch
):
    from servers import lean_runtime as lean_runtime_module

    first = make_lake_project(tmp_path, "thread-failure-first")
    second = make_lake_project(tmp_path, "thread-failure-second")
    created = []
    real_thread = threading.Thread

    def factory(root):
        created.append(root)
        return root

    cache = ProjectResourceCache(
        factory,
        lambda resource: None,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(first)):
        pass

    def fail_retirement_thread(*args, **kwargs):
        if kwargs.get("name") == "autoform-project-retirement":
            raise RuntimeError("cannot construct retirement worker")
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(
        lean_runtime_module.threading,
        "Thread",
        fail_retirement_thread,
    )
    with pytest.raises(ProjectResourceBusyError, match="failed to retire"):
        with cache.lease(str(second), deadline=time.monotonic() + 1):
            pytest.fail("replacement must not start without a retirement owner")

    assert created == [first.resolve()]
    assert cache.stats()["retiring"] == [str(first.resolve())]
    assert first.resolve() not in cache._retiring_active

    monkeypatch.setattr(lean_runtime_module.threading, "Thread", real_thread)
    with cache.lease(str(second)) as resource:
        assert resource == second.resolve()
    cache.close()


def test_interrupted_retirement_wait_keeps_victim_owned(tmp_path, monkeypatch):
    from servers import lean_runtime as lean_runtime_module

    first = make_lake_project(tmp_path, "interrupted-retirement-first")
    second = make_lake_project(tmp_path, "interrupted-retirement-second")
    created = []
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    real_future = lean_runtime_module.Future
    interrupt_next_wait = True

    class InterruptingFuture(real_future):
        def result(self, timeout=None):
            nonlocal interrupt_next_wait
            if timeout is not None and interrupt_next_wait:
                interrupt_next_wait = False
                assert cleanup_started.wait(timeout=1)
                raise KeyboardInterrupt("retirement wait interrupted")
            return super().result(timeout=timeout)

    def factory(root):
        created.append(root)
        return root

    def close_resource(resource):
        if resource == first.resolve():
            cleanup_started.set()
            release_cleanup.wait(timeout=2)

    cache = ProjectResourceCache(
        factory,
        close_resource,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(first)):
        pass

    monkeypatch.setattr(lean_runtime_module, "Future", InterruptingFuture)
    with pytest.raises(KeyboardInterrupt, match="retirement wait interrupted"):
        with cache.lease(str(second), deadline=time.monotonic() + 1):
            pytest.fail("interrupted retirement must not start a replacement")
    assert created == [first.resolve()]
    assert cache.stats()["retiring"] == [str(first.resolve())]

    release_cleanup.set()
    deadline = time.monotonic() + 2
    while cache.stats()["retiring"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cache.stats()["retiring"] == []
    with cache.lease(str(second)) as resource:
        assert resource == second.resolve()
    cache.close()


@pytest.mark.parametrize("launch", [False, True])
def test_interrupted_retirement_thread_start_keeps_quarantine_retryable(
    tmp_path, monkeypatch, launch
):
    from servers import lean_runtime as lean_runtime_module

    first = make_lake_project(tmp_path, f"retire-start-first-{launch}")
    second = make_lake_project(tmp_path, f"retire-start-second-{launch}")
    close_calls = []
    worker_exited = threading.Event()
    real_thread = threading.Thread

    class InterruptedStart:
        ident = None

        def __init__(self, thread):
            self.thread = thread

        def start(self):
            if launch:
                self.thread.start()
            raise KeyboardInterrupt("retirement start interrupted")

    def thread_factory(*args, **kwargs):
        if kwargs.get("name") != "autoform-project-retirement":
            return real_thread(*args, **kwargs)
        target = kwargs["target"]

        def run():
            try:
                target()
            finally:
                worker_exited.set()

        kwargs["target"] = run
        return InterruptedStart(real_thread(*args, **kwargs))

    cache = ProjectResourceCache(
        lambda root: root,
        close_calls.append,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(first)):
        pass

    monkeypatch.setattr(lean_runtime_module.threading, "Thread", thread_factory)
    with pytest.raises(KeyboardInterrupt, match="retirement start interrupted"):
        with cache.lease(str(second), deadline=time.monotonic() + 1):
            pytest.fail("interrupted retirement start must not create replacement")
    if launch:
        assert worker_exited.wait(timeout=1)
    assert close_calls == []
    assert cache.stats()["retiring"] == [str(first.resolve())]
    assert first.resolve() not in cache._retiring_active

    monkeypatch.setattr(lean_runtime_module.threading, "Thread", real_thread)
    with cache.lease(str(second)) as resource:
        assert resource == second.resolve()
    cache.close()


def test_cache_close_during_victim_retirement_blocks_replacement_startup(tmp_path):
    first = make_lake_project(tmp_path, "closing-first")
    second = make_lake_project(tmp_path, "closing-second")
    created = []
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    replacement_started = threading.Event()
    errors = []

    def factory(root):
        created.append(root)
        if root == second.resolve():
            replacement_started.set()
        return root

    def close_resource(resource):
        if resource == first.resolve():
            cleanup_started.set()
            release_cleanup.wait(timeout=2)

    cache = ProjectResourceCache(
        factory,
        close_resource,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(first)):
        pass

    def acquire_replacement():
        try:
            with cache.lease(str(second), deadline=time.monotonic() + 2):
                pytest.fail("replacement reached the caller during shutdown")
        except BaseException as error:
            errors.append(error)

    acquisition = threading.Thread(target=acquire_replacement)
    acquisition.start()
    assert cleanup_started.wait(timeout=1)

    cache_closed = threading.Event()
    closer = threading.Thread(target=lambda: (cache.close(), cache_closed.set()))
    closer.start()
    try:
        assert not cache_closed.wait(timeout=0.1)
        release_cleanup.set()
    finally:
        acquisition.join(timeout=2)
        closer.join(timeout=2)

    assert not acquisition.is_alive()
    assert not closer.is_alive()
    assert cache_closed.is_set()
    assert not replacement_started.is_set()
    assert created == [first.resolve()]
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert "cache is closed" in str(errors[0])


def test_concurrent_replacement_has_only_one_retirement_owner(tmp_path):
    first = make_lake_project(tmp_path, "single-closer-first")
    second = make_lake_project(tmp_path, "single-closer-second")
    close_started = threading.Event()
    release_close = threading.Event()
    concurrent_close = threading.Event()
    close_active = 0
    first_close_calls = 0

    def close(resource):
        nonlocal close_active, first_close_calls
        if resource != first.resolve():
            return
        first_close_calls += 1
        close_active += 1
        if close_active > 1:
            concurrent_close.set()
        close_started.set()
        release_close.wait(timeout=2)
        close_active -= 1

    cache = ProjectResourceCache(
        lambda root: root,
        close,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(first)):
        pass

    errors = []

    def replace():
        try:
            with cache.lease(str(second)):
                pass
        except BaseException as error:
            errors.append(error)

    callers = [threading.Thread(target=replace) for _ in range(2)]
    callers[0].start()
    assert close_started.wait(timeout=1)
    callers[1].start()
    assert not concurrent_close.wait(timeout=0.1)
    release_close.set()
    for caller in callers:
        caller.join(timeout=2)

    assert all(not caller.is_alive() for caller in callers)
    assert errors == []
    assert first_close_calls == 1
    cache.close()


def test_cache_close_retains_failed_resources_for_a_later_retry(tmp_path):
    project = make_lake_project(tmp_path, "close-retry")
    close_calls = 0

    def close(resource):
        nonlocal close_calls
        close_calls += 1
        if close_calls == 1:
            raise RuntimeError("cleanup failed")

    cache = ProjectResourceCache(
        lambda root: root,
        close,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(project)):
        pass

    with pytest.raises(RuntimeError, match="failed to retire 1"):
        cache.close()

    assert cache.stats()["retiring"] == [str(project.resolve())]
    cache.close()
    assert close_calls == 2
    assert cache.stats()["retiring"] == []


def test_lease_preserves_operation_cancellation_when_release_also_fails(tmp_path):
    project = make_lake_project(tmp_path, "release-cancellation")

    def is_valid(resource):
        raise asyncio.CancelledError("release")

    cache = ProjectResourceCache(
        lambda root: root,
        lambda resource: None,
        max_entries=1,
        idle_seconds=1800,
        is_valid=is_valid,
        start_sweeper=False,
    )

    with pytest.raises(KeyboardInterrupt, match="operation") as raised:
        with cache.lease(str(project)):
            raise KeyboardInterrupt("operation")

    if hasattr(raised.value, "add_note"):
        assert raised.value.__notes__ == [
            "Lean project resource release also failed: release"
        ]
    cache.close()


def test_services_attempt_lsp_cleanup_after_repl_cleanup_failure(monkeypatch):
    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=FakePool,
        lsp_factory=FakeLsp,
        start_sweepers=False,
    )
    lsp_closed = []
    monkeypatch.setattr(
        services.repl_projects,
        "close",
        lambda: (_ for _ in ()).throw(RuntimeError("REPL cleanup failed")),
    )
    monkeypatch.setattr(
        services.lsp_projects,
        "close",
        lambda: lsp_closed.append(True),
    )

    with pytest.raises(RuntimeError, match="REPL cleanup failed"):
        services.close()

    assert lsp_closed == [True]


def test_terminal_cleanup_retries_without_releasing_ownership(monkeypatch):
    from servers import lean_runtime

    close_calls = 0
    delays = []

    class Services:
        def close(self):
            nonlocal close_calls
            close_calls += 1
            if close_calls < 3:
                raise RuntimeError("cleanup failed")

    monkeypatch.setattr(lean_runtime.time, "sleep", delays.append)

    lean_runtime._close_services_until_clean(Services())

    assert close_calls == 3
    assert delays == [
        lean_runtime.TERMINAL_CLEANUP_RETRY_SECONDS,
        lean_runtime.TERMINAL_CLEANUP_RETRY_SECONDS * 2,
    ]


def test_stdio_mcp_adapters_delegate_without_owning_lean_state():
    from servers.lsp.server import create_lsp_server
    from servers.repl.server import create_repl_server

    class FakeRuntime:
        def __init__(self):
            self.calls = []

        def request(self, method, params):
            self.calls.append((method, params))
            return "delegated"

    repl_runtime = FakeRuntime()
    repl = create_repl_server(repl_runtime)
    asyncio.run(
        repl.call_tool(
            "run_lean_code",
            {"project_dir": "/lean", "code": "#check Nat", "timeout": None},
        )
    )
    asyncio.run(repl.call_tool("get_repl_status", {"project_dir": "/lean"}))
    assert repl_runtime.calls == [
        (
            "repl.run",
            {"project_dir": "/lean", "code": "#check Nat", "timeout": None},
        ),
        ("repl.status", {"project_dir": "/lean"}),
    ]

    lsp_runtime = FakeRuntime()
    lsp = create_lsp_server(lsp_runtime)
    asyncio.run(
        lsp.call_tool(
            "lean_hover",
            {
                "project_dir": "/lean",
                "file_path": "Main.lean",
                "line": 0,
                "character": 3,
            },
        )
    )
    asyncio.run(
        lsp.call_tool(
            "lean_diagnostic_messages",
            {"project_dir": "/lean", "file_path": "Main.lean"},
        )
    )
    assert lsp_runtime.calls == [
        (
            "lsp.hover",
            {
                "project_dir": "/lean",
                "file_path": "Main.lean",
                "line": 0,
                "character": 3,
            },
        ),
        (
            "lsp.diagnostics",
            {"project_dir": "/lean", "file_path": "Main.lean"},
        ),
    ]


def test_lsp_diagnostic_formatting_remains_stable():
    from servers.lsp.server import format_lsp_diagnostics

    assert format_lsp_diagnostics([]).startswith("No diagnostics")
    formatted = format_lsp_diagnostics(
        [
            {
                "severity": 1,
                "message": "unknown identifier",
                "range": {"start": {"line": 2, "character": 4}},
            }
        ]
    )
    assert formatted == (
        "Diagnostics: 1 error(s), 0 warning(s)\n"
        "3:4: error: unknown identifier"
    )


def test_startup_times_out_while_previous_runtime_retains_lifetime_lock(
    runtime_dir,
    monkeypatch,
):
    import fcntl

    socket_path = runtime_dir / "retiring.sock"
    client = LeanRuntimeClient(socket_path=socket_path, startup_timeout=0.01)
    lock_calls = 0

    def flock(fd, operation):
        nonlocal lock_calls
        lock_calls += 1
        if lock_calls == 1:
            return
        raise BlockingIOError

    monkeypatch.setattr(fcntl, "flock", flock)

    with pytest.raises(LeanRuntimeUnavailable, match="still be cleaning up"):
        client.ensure_running()


def test_startup_does_not_acquire_a_free_lock_after_its_deadline(
    runtime_dir,
    monkeypatch,
):
    from servers import lean_client

    client = LeanRuntimeClient(
        socket_path=runtime_dir / "expired.sock",
        startup_timeout=1,
    )
    now = [100.0]
    lock_calls = []

    def unavailable_ping(*, autostart=False, deadline=None):
        now[0] = 102.0
        raise LeanRuntimeUnavailable("not listening")

    monkeypatch.setattr(lean_client.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(client, "ping", unavailable_ping)
    monkeypatch.setattr(
        "fcntl.flock",
        lambda *args: lock_calls.append(args),
    )

    with pytest.raises(LeanRuntimeUnavailable, match="startup coordination"):
        client.ensure_running()

    assert lock_calls == []


def test_previous_build_shutdown_uses_the_startup_deadline(runtime_dir, monkeypatch):
    from servers import lean_client

    current_socket = runtime_dir / f"lean-v{PROTOCOL_VERSION}-{INSTALL_PATH_ID}-new.sock"
    old_socket = runtime_dir / f"lean-v{PROTOCOL_VERSION}-{INSTALL_PATH_ID}-old.sock"
    old_socket.touch()
    client = LeanRuntimeClient(socket_path=current_socket)
    client._uses_default_paths = True
    calls = []

    class PreviousClient:
        def __init__(self, *, socket_path, **kwargs):
            assert socket_path == old_socket

        def _request_once(
            self,
            method,
            params,
            *,
            deadline,
            response_timeout,
            protocol_version,
        ):
            calls.append((method, deadline, protocol_version))
            return {
                "protocol": protocol_version,
                "install_id": INSTALL_ID,
                "build_generation": 0,
            }

        def _stop_protocol(self, protocol_version, *, deadline):
            calls.append(("stop", deadline, protocol_version))
            return {"pid": 7}

        def _wait_for_lifetime(self, path, *, deadline):
            calls.append(("lifetime", deadline, path))

    monkeypatch.setattr(lean_client, "LeanRuntimeClient", PreviousClient)
    monkeypatch.setattr(
        client,
        "_wait_for_lifetimes",
        lambda *, deadline: calls.append(("lifetimes", deadline)),
    )

    assert client._stop_previous_builds(deadline=123.0) == [7]
    assert calls == [
        ("daemon.ping", 123.0, PROTOCOL_VERSION),
        ("stop", 123.0, PROTOCOL_VERSION),
        ("lifetimes", 123.0),
    ]


def test_stale_wrapper_cannot_replace_a_newer_runtime_build(runtime_dir, monkeypatch):
    current_socket = runtime_dir / f"lean-v{PROTOCOL_VERSION}-{INSTALL_PATH_ID}-old.sock"
    newer_socket = runtime_dir / f"lean-v{PROTOCOL_VERSION}-{INSTALL_PATH_ID}-new.sock"
    newer_socket.touch()
    client = LeanRuntimeClient(socket_path=current_socket)
    client._uses_default_paths = True

    class NewerClient:
        def __init__(self, *, socket_path, **kwargs):
            assert socket_path == newer_socket

        def _request_once(self, method, params, **kwargs):
            return {
                "protocol": PROTOCOL_VERSION,
                "install_id": f"{INSTALL_PATH_ID}-new",
                "build_generation": lean_client.BUILD_GENERATION + 1,
            }

        def _stop_protocol(self, protocol_version, *, deadline):
            pytest.fail("a stale wrapper must not stop a newer runtime")

    monkeypatch.setattr(lean_client, "LeanRuntimeClient", NewerClient)

    with pytest.raises(LeanRuntimeProtocolError, match="newer Autoform runtime"):
        client._stop_previous_builds(deadline=time.monotonic() + 1)


def test_failed_start_never_hard_kills_a_daemon_that_may_own_work(runtime_dir):
    client = LeanRuntimeClient(socket_path=runtime_dir / "failed-start.sock")

    class Process:
        returncode = None
        terminate_calls = 0
        kill_calls = 0

        def poll(self):
            return None

        def terminate(self):
            self.terminate_calls += 1

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("runtime", timeout)

        def kill(self):
            self.kill_calls += 1

    process = Process()
    client._terminate_failed_start(process)

    assert process.terminate_calls == 1
    assert process.kill_calls == 0


@pytest.mark.daemon
def test_concurrent_clients_boot_one_daemon_that_outlives_each_client(runtime_dir, monkeypatch):
    socket_path = runtime_dir / "lean.sock"
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "1")
    monkeypatch.setenv("AUTOFORM_MAX_LEAN_PROJECTS", "1")
    clients = [
        LeanRuntimeClient(socket_path=socket_path, startup_timeout=15),
        LeanRuntimeClient(socket_path=socket_path, startup_timeout=15),
    ]
    barrier = threading.Barrier(3)
    pids = []
    errors = []

    def start(client):
        barrier.wait()
        try:
            pids.append(client.ensure_running()["pid"])
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=start, args=(client,)) for client in clients]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=20)

    try:
        assert errors == []
        assert all(not thread.is_alive() for thread in threads)
        assert len(pids) == 2
        assert len(set(pids)) == 1

        # Clients own no process handle or shutdown hook. Losing the client that
        # happened to bootstrap the daemon cannot stop shared Lean state.
        del clients[0]
        assert clients[0].ping()["pid"] == pids[0]
    finally:
        try:
            clients[-1].stop()
        except LeanRuntimeUnavailable:
            pass

    deadline = time.monotonic() + 5
    while socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.025)
    assert not socket_path.exists()


@pytest.mark.daemon
def test_daemon_outlives_the_separate_process_that_started_it(
    tmp_path,
    runtime_dir,
    repo_root,
    monkeypatch,
):
    socket_path = runtime_dir / "owner.sock"
    project = make_lake_project(tmp_path, "cold")
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "1")
    monkeypatch.setenv("AUTOFORM_MAX_LEAN_PROJECTS", "1")
    helper = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from servers.lean_client import LeanRuntimeClient; "
                "print(LeanRuntimeClient(socket_path=sys.argv[1]).ensure_running()['pid'])"
            ),
            str(socket_path),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert helper.returncode == 0, helper.stderr
    owner_pid = int(helper.stdout.strip())

    client = LeanRuntimeClient(socket_path=socket_path)
    try:
        assert client.ping()["pid"] == owner_pid
        status = client.request("daemon.status", autostart=False)
        assert status["repl_projects"]["resident"] == []
        assert status["lsp_projects"]["resident"] == []

        repl_status = client.request(
            "repl.status",
            {"project_dir": str(project)},
            autostart=False,
        )
        assert repl_status["state"] == "cold"
        assert client.request("daemon.status", autostart=False)["repl_projects"][
            "resident"
        ] == []
    finally:
        client.stop()


@pytest.mark.daemon
def test_stop_then_immediate_start_is_serialized(runtime_dir, monkeypatch):
    socket_path = runtime_dir / "restart.sock"
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "1")
    client = LeanRuntimeClient(socket_path=socket_path, startup_timeout=15)
    first_pid = client.ensure_running()["pid"]
    client.stop()
    second_pid = client.ensure_running()["pid"]
    try:
        assert second_pid != first_pid
        assert client.ping()["pid"] == second_pid
    finally:
        client.stop()


def test_stop_waits_for_cleanup_after_runtime_socket_disappears(
    runtime_dir, monkeypatch
):
    import fcntl

    monkeypatch.setenv("AUTOFORM_RUNTIME_DIR", str(runtime_dir))
    client = LeanRuntimeClient(autostart=False, response_timeout=2)
    lifetime_fd = os.open(
        client.paths.lifetime_lock, os.O_CREAT | os.O_RDWR, 0o600
    )
    fcntl.flock(lifetime_fd, fcntl.LOCK_EX)
    errors = []

    def stop():
        try:
            client.stop()
        except BaseException as error:
            errors.append(error)

    stopping = threading.Thread(target=stop)
    stopping.start()
    try:
        stopping.join(timeout=0.1)
        assert stopping.is_alive()
    finally:
        os.close(lifetime_fd)
        stopping.join(timeout=2)

    assert not stopping.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], LeanRuntimeUnavailable)


@pytest.mark.daemon
def test_new_build_replaces_previous_runtime_at_same_install_path(runtime_dir, monkeypatch):
    import fcntl

    monkeypatch.setenv("AUTOFORM_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "1")
    old_socket = runtime_dir / f"lean-v{PROTOCOL_VERSION}-{INSTALL_PATH_ID}-old.sock"
    old_client = LeanRuntimeClient(socket_path=old_socket, startup_timeout=15)
    old_client.paths = replace(
        old_client.paths,
        lifetime_lock=runtime_dir / f"lean-v1-{INSTALL_PATH_ID}.lifetime.lock",
    )
    old_pid = old_client.ensure_running()["pid"]

    current = LeanRuntimeClient(startup_timeout=15)
    try:
        current_pid = current.ensure_running()["pid"]
        assert current_pid != old_pid
        assert not old_socket.exists()
        lifetime_fd = os.open(current.paths.lifetime_lock, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lifetime_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(lifetime_fd)
    finally:
        current.stop()


@pytest.mark.daemon
def test_new_lock_scheme_retires_pr13_runtime_before_owning_runtime(
    runtime_dir,
    repo_root,
    monkeypatch,
    request,
):
    monkeypatch.setenv("AUTOFORM_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "1")
    current = LeanRuntimeClient(startup_timeout=15)
    old_protocol = 1
    old_socket = runtime_dir / f"lean-v1-{INSTALL_PATH_ID}-old.sock"
    stale_socket = runtime_dir / f"lean-v1-{INSTALL_PATH_ID}-000.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(stale_socket))
    stale.close()
    old_lifetime_lock = runtime_dir / f"lean-v1-{INSTALL_PATH_ID}.lifetime.lock"
    draining = runtime_dir / "old-draining"
    release = runtime_dir / "release-old"
    old_script = """
import fcntl
import json
import os
import socket
import sys
import time
from pathlib import Path

socket_path = Path(sys.argv[1])
lifetime_path = Path(sys.argv[2])
draining_path = Path(sys.argv[3])
release_path = Path(sys.argv[4])
protocol = int(sys.argv[5])
lifetime_fd = os.open(lifetime_path, os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(lifetime_fd, fcntl.LOCK_EX)
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(str(socket_path))
server.listen()
try:
    while True:
        connection, _ = server.accept()
        with connection:
            request = json.loads(connection.makefile("rb").readline())
            assert request["v"] == protocol
            stopping = request["method"] == "daemon.shutdown"
            result = (
                {"stopping": True, "pid": os.getpid()}
                if stopping
                else {
                    "pid": os.getpid(),
                    "protocol": protocol,
                    "install_id": sys.argv[6],
                    "build_generation": 0,
                }
            )
            connection.sendall(
                json.dumps(
                    {"v": protocol, "id": request["id"], "ok": True, "result": result}
                ).encode()
                + b"\\n"
            )
        if stopping:
            server.close()
            socket_path.unlink()
            draining_path.touch()
            while not release_path.exists():
                time.sleep(0.01)
            break
finally:
    server.close()
    socket_path.unlink(missing_ok=True)
    os.close(lifetime_fd)
"""
    old_process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            old_script,
            str(old_socket),
            str(old_lifetime_lock),
            str(draining),
            str(release),
            str(old_protocol),
            f"{INSTALL_PATH_ID}-old",
        ],
        cwd=repo_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    def stop_old_process():
        release.touch(exist_ok=True)
        if old_process.poll() is None:
            old_process.terminate()
            old_process.wait(timeout=5)

    request.addfinalizer(stop_old_process)
    old_client = LeanRuntimeClient(socket_path=old_socket, startup_timeout=15)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if old_process.poll() is not None:
            pytest.fail(f"old protocol runtime exited with {old_process.returncode}")
        try:
            old_status = old_client._request_once(
                "daemon.ping",
                {},
                protocol_version=old_protocol,
            )
            break
        except LeanRuntimeUnavailable:
            time.sleep(0.025)
    else:
        pytest.fail("old protocol runtime did not become ready")

    statuses = []
    errors = []

    def start_current():
        try:
            statuses.append(current.ensure_running())
        except BaseException as error:
            errors.append(error)

    starter = threading.Thread(target=start_current)
    try:
        starter.start()
        deadline = time.monotonic() + 10
        while not draining.exists() and time.monotonic() < deadline:
            time.sleep(0.025)
        assert draining.exists()
        starter.join(timeout=0.1)
        assert starter.is_alive()
        assert old_process.poll() is None
        assert not current.paths.socket.exists()

        release.touch()
        starter.join(timeout=20)
        assert not starter.is_alive()
        assert errors == []
        assert len(statuses) == 1
        current_status = statuses[0]
        assert current_status["protocol"] == PROTOCOL_VERSION
        assert current_status["pid"] != old_status["pid"]
        assert old_process.wait(timeout=5) == 0
        assert not stale_socket.exists()

        import fcntl

        lifetime_fd = os.open(
            current.paths.lifetime_lock, os.O_CREAT | os.O_RDWR, 0o600
        )
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lifetime_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(lifetime_fd)
    finally:
        release.touch(exist_ok=True)
        starter.join(timeout=5)
        try:
            current.stop()
        except LeanRuntimeUnavailable:
            pass
        if old_process.poll() is None:
            old_process.terminate()
            old_process.wait(timeout=5)


@pytest.mark.daemon
def test_new_lock_scheme_removes_stale_pr13_socket(runtime_dir, monkeypatch):
    monkeypatch.setenv("AUTOFORM_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "1")
    stale_path = runtime_dir / f"lean-v1-{INSTALL_PATH_ID}-stale.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(stale_path))
    stale.close()

    current = LeanRuntimeClient(startup_timeout=15)
    try:
        current.ensure_running()
        assert not stale_path.exists()
    finally:
        current.stop()


@pytest.mark.daemon
def test_new_runtime_blocks_pr13_client_lifetime_ownership(runtime_dir, monkeypatch):
    import fcntl

    monkeypatch.setenv("AUTOFORM_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "1")
    current = LeanRuntimeClient(startup_timeout=15)
    current.ensure_running()
    old_bootstrap = os.open(
        runtime_dir / f"lean-v1-{INSTALL_PATH_ID}.lock",
        os.O_CREAT | os.O_RDWR,
        0o600,
    )
    old_lifetime = os.open(
        runtime_dir / f"lean-v1-{INSTALL_PATH_ID}.lifetime.lock",
        os.O_CREAT | os.O_RDWR,
        0o600,
    )
    try:
        fcntl.flock(old_bootstrap, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            fcntl.flock(old_lifetime, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(old_lifetime)
        os.close(old_bootstrap)
        current.stop()


def test_newer_protocol_socket_fails_closed(runtime_dir, monkeypatch):
    monkeypatch.setenv("AUTOFORM_RUNTIME_DIR", str(runtime_dir))
    newer_path = runtime_dir / f"lean-v{PROTOCOL_VERSION + 1}-{INSTALL_PATH_ID}-new.sock"
    newer_path.touch()
    current = LeanRuntimeClient(startup_timeout=0.1)

    with pytest.raises(LeanRuntimeProtocolError, match="newer than supported"):
        current.ensure_running()

    assert newer_path.is_file()


def test_unsupported_older_protocol_socket_fails_closed(runtime_dir, monkeypatch):
    monkeypatch.setenv("AUTOFORM_RUNTIME_DIR", str(runtime_dir))
    older_path = runtime_dir / f"lean-v0-{INSTALL_PATH_ID}-old.sock"
    older_path.touch()
    current = LeanRuntimeClient(startup_timeout=0.1)

    with pytest.raises(LeanRuntimeProtocolError, match="older than"):
        current.ensure_running()

    assert older_path.is_file()


@pytest.mark.daemon
def test_default_cli_stop_finds_a_previous_build(runtime_dir, monkeypatch, capsys):
    from servers import lean_runtime

    monkeypatch.setenv("AUTOFORM_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "1")
    old_socket = runtime_dir / f"lean-v{PROTOCOL_VERSION}-{INSTALL_PATH_ID}-old.sock"
    old_client = LeanRuntimeClient(socket_path=old_socket, startup_timeout=15)
    old_client.ensure_running()

    lean_runtime.main(["stop"])

    result = capsys.readouterr().out
    assert "stopped_previous" in result
    assert not old_socket.exists()


@pytest.mark.daemon
def test_silent_connection_cannot_block_graceful_stop(runtime_dir, monkeypatch):
    socket_path = runtime_dir / "silent.sock"
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "1")
    monkeypatch.setenv("AUTOFORM_RUNTIME_READ_TIMEOUT", "0.2")
    client = LeanRuntimeClient(socket_path=socket_path, startup_timeout=15)
    client.ensure_running()
    silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    silent.connect(str(socket_path))
    silent.sendall(b'{"v":1')
    errors = []

    def stop():
        try:
            client.stop()
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=stop)
    thread.start()
    thread.join(timeout=3)
    try:
        assert not thread.is_alive()
        assert errors == []
        assert not socket_path.exists()
    finally:
        silent.close()
        if thread.is_alive():
            thread.join(timeout=3)


def test_server_close_waits_for_an_admitted_request(runtime_dir):
    entered = threading.Event()
    release = threading.Event()

    class BlockingServices:
        config = SimpleNamespace(max_connections=2, rpc_read_timeout=1.0)

        def dispatch(self, method, params):
            entered.set()
            assert release.wait(timeout=2)
            return {"finished": True}

    socket_path = runtime_dir / "drain.sock"
    server = lean_runtime.LeanRuntimeServer(socket_path, BlockingServices())
    serving = threading.Thread(target=server.serve_forever)
    serving.start()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2)
    client.connect(str(socket_path))
    client.sendall(b'{"v":1,"id":"slow","method":"repl.run","params":{}}\n')
    assert entered.wait(timeout=1)

    server.shutdown()
    closing = threading.Thread(target=server.server_close)
    closing.start()
    try:
        closing.join(timeout=0.05)
        assert closing.is_alive()
        release.set()
        response = b""
        while not response.endswith(b"\n"):
            response += client.recv(4096)
        assert json.loads(response) == {
            "v": PROTOCOL_VERSION,
            "id": "slow",
            "ok": True,
            "result": {"finished": True},
        }
        closing.join(timeout=1)
        serving.join(timeout=1)
        assert not closing.is_alive()
        assert not serving.is_alive()
    finally:
        release.set()
        client.close()
        server.server_close()
        serving.join(timeout=1)


def test_connected_send_failure_is_never_retried(runtime_dir, monkeypatch):
    from servers import lean_client

    class FailingSocket:
        def settimeout(self, timeout):
            pass

        def connect(self, path):
            pass

        def sendall(self, payload):
            raise OSError("uncertain delivery")

        def close(self):
            pass

    client = LeanRuntimeClient(socket_path=runtime_dir / "fake.sock")
    monkeypatch.setattr(lean_client.socket, "socket", lambda *args: FailingSocket())
    monkeypatch.setattr(
        client,
        "ensure_running",
        lambda: pytest.fail("an ambiguously dispatched request must not be retried"),
    )

    with pytest.raises(LeanRuntimeOutcomeUnknown, match="must not be replayed"):
        client.request("repl.run", {"project_dir": "/lean", "code": "#check Nat"})


def test_post_dispatch_timeout_is_explicitly_outcome_unknown(runtime_dir, monkeypatch):
    from servers import lean_client

    class TimingOutSocket:
        def settimeout(self, timeout):
            pass

        def connect(self, path):
            pass

        def sendall(self, payload):
            pass

        def recv(self, size):
            raise socket.timeout

        def close(self):
            pass

    client = LeanRuntimeClient(socket_path=runtime_dir / "fake.sock")
    monkeypatch.setattr(lean_client.socket, "socket", lambda *args: TimingOutSocket())
    monkeypatch.setattr(
        client,
        "ensure_running",
        lambda **kwargs: pytest.fail("a dispatched request must not be retried"),
    )

    with pytest.raises(LeanRuntimeOutcomeUnknown, match="must not be replayed"):
        client.request("repl.run", {"project_dir": "/lean", "code": "#check Nat"})


@pytest.mark.parametrize("response", [b"", b"{\n"])
def test_post_dispatch_invalid_response_is_explicitly_outcome_unknown(
    runtime_dir,
    monkeypatch,
    response,
):
    from servers import lean_client

    class InvalidResponseSocket:
        def settimeout(self, timeout):
            pass

        def connect(self, path):
            pass

        def sendall(self, payload):
            pass

        def recv(self, size):
            return response

        def close(self):
            pass

    client = LeanRuntimeClient(socket_path=runtime_dir / "fake.sock")
    monkeypatch.setattr(
        lean_client.socket,
        "socket",
        lambda *args: InvalidResponseSocket(),
    )

    with pytest.raises(LeanRuntimeOutcomeUnknown, match="must not be replayed"):
        client.request("repl.run", {"project_dir": "/lean", "code": "#check Nat"})


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        ("LEAN_NUM_REPLS", "-1", "nonnegative integer"),
        ("LEAN_REPL_CMD", "   ", "must not be empty"),
        ("AUTOFORM_LEAN_IDLE_SECONDS", "nan", "finite nonnegative"),
        ("LEAN_LSP_TIMEOUT", "601", "cannot exceed"),
        ("AUTOFORM_RUNTIME_RESPONSE_TIMEOUT", "100", "too small"),
    ],
)
def test_invalid_node_configuration_fails_fast(monkeypatch, name, value, match):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=match):
        LeanRuntimeConfig.from_environment()


def test_per_project_workers_cannot_exceed_node_budget(monkeypatch):
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "1")
    monkeypatch.setenv("AUTOFORM_REPL_WORKERS_PER_PROJECT", "2")
    with pytest.raises(ValueError, match="cannot exceed"):
        LeanRuntimeConfig.from_environment()


def test_response_budget_does_not_scale_with_cold_repl_pool_size(monkeypatch):
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "3")
    monkeypatch.setenv("AUTOFORM_REPL_WORKERS_PER_PROJECT", "3")
    monkeypatch.setenv("AUTOFORM_RUNTIME_RESPONSE_TIMEOUT", "900")

    config = LeanRuntimeConfig.from_environment()

    assert config.repl_workers_per_project == 3
    assert config.response_timeout == 900


def test_response_budget_must_leave_room_for_repl_cleanup(monkeypatch):
    monkeypatch.setenv("AUTOFORM_RUNTIME_RESPONSE_TIMEOUT", "272")
    monkeypatch.setenv("LEAN_LSP_TIMEOUT", "1")
    monkeypatch.setenv("AUTOFORM_MAX_LSP_REQUEST_SECONDS", "1")

    with pytest.raises(ValueError, match="REPL request and cleanup limits"):
        LeanRuntimeConfig.from_environment()


def test_response_budget_reserves_lsp_cleanup_but_not_a_second_startup_budget(
    monkeypatch,
):
    boundary = (
        lean_runtime.DEFAULT_MAX_LSP_REQUEST_SECONDS
        + lean_runtime.LSP_CLOSE_BUDGET
        + lean_runtime.RUNTIME_SAFETY_SECONDS
    )
    monkeypatch.setenv("AUTOFORM_RUNTIME_RESPONSE_TIMEOUT", str(boundary))

    assert LeanRuntimeConfig.from_environment().response_timeout == boundary

    monkeypatch.setenv("AUTOFORM_RUNTIME_RESPONSE_TIMEOUT", str(boundary - 1))
    with pytest.raises(ValueError, match="AUTOFORM_MAX_LSP_REQUEST_SECONDS"):
        LeanRuntimeConfig.from_environment()


@pytest.mark.parametrize("timeout", [-1, 0, True, float("nan"), float("inf"), 241])
def test_invalid_repl_timeout_never_warms_a_pool(tmp_path, timeout):
    project = make_lake_project(tmp_path, "timeout")
    pools = []
    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=lambda root: pools.append(FakePool(root)) or pools[-1],
        lsp_factory=FakeLsp,
        start_sweepers=False,
    )
    try:
        with pytest.raises(ValueError, match="timeout"):
            services.dispatch(
                "repl.run",
                {"project_dir": str(project), "code": "#check Nat", "timeout": timeout},
            )
        assert pools == []
    finally:
        services.close()


@pytest.mark.parametrize("method", ["lsp.diagnostics", "lsp.hover"])
def test_lsp_dispatch_shares_one_deadline_with_start_and_operation(
    tmp_path,
    monkeypatch,
    method,
):
    project = make_lake_project(tmp_path, f"deadline-{method.rsplit('.', 1)[1]}")
    source = project / "Main.lean"
    source.write_text("#check Nat\n")
    events = []

    class Session:
        def __init__(self, config):
            self.config = config
            self.closed = False

        def start(self, *, deadline=None):
            events.append(("start", deadline))

        def get_diagnostics(self, file_path, *, deadline=None):
            events.append(("diagnostics", deadline))
            return []

        def hover(self, file_path, line, character, *, deadline=None):
            events.append(("hover", deadline))
            return "Nat : Type"

        def is_alive(self):
            return not self.closed

        def close(self):
            self.closed = True

        def abort(self):
            self.closed = True

    monkeypatch.setattr(lean_runtime, "LeanLspSession", Session)
    timeout = 2.0
    services = LeanRuntimeServices(
        runtime_config(lsp_timeout=timeout),
        repl_factory=FakePool,
        start_sweepers=False,
    )
    params = {"project_dir": str(project), "file_path": str(source)}
    if method == "lsp.hover":
        params.update({"line": 0, "character": 0})
    before = time.monotonic()
    try:
        services.dispatch(method, params)
        after = time.monotonic()
    finally:
        services.close()

    assert [event[0] for event in events] == [
        "start",
        method.rsplit(".", 1)[1],
    ]
    assert events[0][1] == events[1][1]
    assert before + timeout <= events[0][1] <= after + timeout


def test_failed_lsp_session_is_replaced_on_the_next_call(tmp_path):
    from servers.lsp.server import LspProtocolError

    project = make_lake_project(tmp_path, "lsp-restart")
    source = project / "Main.lean"
    source.write_text("#check Nat\n")
    sessions = []

    class Session(FakeLsp):
        def __init__(self, root):
            super().__init__(root)
            self.number = len(sessions) + 1
            self.alive = True
            sessions.append(self)

        def is_alive(self):
            return self.alive and not self.closed

        def get_diagnostics(self, file_path, *, deadline=None):
            if self.number == 1:
                self.alive = False
                raise LspProtocolError("broken shared stream")
            return []

    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=FakePool,
        lsp_factory=Session,
        start_sweepers=False,
    )
    try:
        params = {"project_dir": str(project), "file_path": "Main.lean"}
        with pytest.raises(LspProtocolError, match="broken shared stream"):
            services.dispatch("lsp.diagnostics", params)
        assert services.dispatch("lsp.diagnostics", params).startswith("No diagnostics")
        assert len(sessions) == 2
        assert sessions[0].closed is True
    finally:
        services.close()


@pytest.mark.parametrize("method", ["lsp.diagnostics", "lsp.hover"])
@pytest.mark.parametrize("changed_file", ["lakefile.toml", "Main.lean"])
def test_lsp_result_is_rejected_when_request_inputs_change(
    tmp_path, method, changed_file
):
    project = make_lake_project(tmp_path, f"changed-{method.rsplit('.', 1)[1]}")
    source = project / "Main.lean"
    source.write_text("#check Nat\n")
    sessions = []

    class Session(FakeLsp):
        def __init__(self, root):
            super().__init__(root)
            self.number = len(sessions) + 1
            sessions.append(self)

        def change_input(self):
            if self.number != 1:
                return
            target = project / changed_file
            original_mtime = target.stat().st_mtime_ns
            replacement = project / f"{changed_file}.replacement"
            replacement.write_text(target.read_text() + "-- changed\n")
            os.utime(replacement, ns=(original_mtime, original_mtime))
            replacement.replace(target)

        def get_diagnostics(self, file_path, *, deadline=None):
            self.change_input()
            return []

        def hover(self, file_path, line, character, *, deadline=None):
            self.change_input()
            return "Nat : Type"

    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=FakePool,
        lsp_factory=Session,
        start_sweepers=False,
    )
    params = {"project_dir": str(project), "file_path": "Main.lean"}
    if method == "lsp.hover":
        params.update({"line": 0, "character": 7})
    try:
        with pytest.raises(ProjectResourceBusyError, match="changed during LSP request"):
            services.dispatch(method, params)
        assert sessions[0].closed is True

        result = services.dispatch(method, params)
        assert result in {"No diagnostics — file compiles cleanly.", "Nat : Type"}
        assert len(sessions) == 2
    finally:
        services.close()
