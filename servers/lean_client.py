"""Client and bootstrap logic for Autoform's node-local Lean runtime.

The two public MCP processes are intentionally short-lived stdio adapters.  A
small detached process owns the expensive Lean REPL pools and LSP sessions and
is reached through a private Unix-domain socket.
"""

from __future__ import annotations

import errno
import hashlib
from importlib import metadata
import json
import math
import os
import socket
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

logger = getLogger(__name__)

PROTOCOL_VERSION = 1
# Keep literal versions here: removing one lets that released client start a
# second daemon because it cannot see the protocol-independent lifetime lock.
LEGACY_LOCK_PROTOCOL_VERSIONS = (1,)
# Add a version only when this client can encode that version's shutdown RPC.
SUPPORTED_PREVIOUS_PROTOCOL_VERSIONS: tuple[int, ...] = ()
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
DEFAULT_CONNECT_TIMEOUT = 2.0
DEFAULT_RESPONSE_TIMEOUT = 900.0
DEFAULT_STARTUP_TIMEOUT = 15.0
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PATH_ID = hashlib.sha256(os.fsencode(PACKAGE_ROOT)).hexdigest()[:10]
_RUNTIME_FILES = tuple(sorted((PACKAGE_ROOT / "servers").rglob("*.py")))
_RUNTIME_GENERATION_FILES = (*_RUNTIME_FILES, Path(sys.executable).resolve())
_RUNTIME_ROOT_DISTRIBUTIONS = ("leanclient", "packaging", "psutil")
_DISTRIBUTION_IDENTITY_FILES = ("METADATA", "RECORD", "direct_url.json")


def _runtime_distribution_names(
    roots: tuple[str, ...] = _RUNTIME_ROOT_DISTRIBUTIONS,
) -> tuple[str, ...]:
    """Find the installed non-extra dependency closure used by the runtime."""
    pending = list(roots)
    discovered: set[str] = set()
    while pending:
        requirement = Requirement(pending.pop())
        name = canonicalize_name(requirement.name)
        if name in discovered:
            continue
        discovered.add(name)
        try:
            requirements = metadata.distribution(name).requires or ()
        except metadata.PackageNotFoundError:
            continue
        for dependency in requirements:
            requirement = Requirement(dependency)
            if requirement.marker is not None and not requirement.marker.evaluate(
                {"extra": ""}
            ):
                continue
            pending.append(requirement.name)
    return tuple(sorted(discovered))


_RUNTIME_DISTRIBUTIONS = _runtime_distribution_names()


def _distribution_identity(name: str) -> tuple[str, ...]:
    """Return stable installed metadata for a runtime dependency."""
    try:
        distribution = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return ("<missing>",)
    identity = [distribution.version]
    for filename in _DISTRIBUTION_IDENTITY_FILES:
        contents = distribution.read_text(filename)
        if contents is not None:
            identity.extend((filename, contents))
    return tuple(identity)


def _distribution_generation(name: str) -> int:
    """Return the newest install-metadata timestamp for one dependency."""
    try:
        distribution = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return 0
    mtimes = []
    for path in distribution.files or ():
        if path.name not in _DISTRIBUTION_IDENTITY_FILES:
            continue
        try:
            mtimes.append(Path(distribution.locate_file(path)).stat().st_mtime_ns)
        except OSError:
            continue
    return max(mtimes, default=0)


def _build_id() -> str:
    """Fingerprint code that can change persistent runtime behavior."""
    digest = hashlib.sha256()
    interpreter = (
        sys.implementation.name,
        str(sys.implementation.cache_tag),
        ".".join(str(component) for component in sys.version_info[:3]),
        sys.version,
    )
    for value in interpreter:
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    for path in _RUNTIME_FILES:
        digest.update(os.fsencode(path.relative_to(PACKAGE_ROOT)))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(os.fsencode(path))
        digest.update(b"\0")
    for distribution in _RUNTIME_DISTRIBUTIONS:
        digest.update(distribution.encode("utf-8"))
        digest.update(b"\0")
        for value in _distribution_identity(distribution):
            digest.update(value.encode("utf-8"))
            digest.update(b"\0")
    return digest.hexdigest()[:10]


def _build_generation() -> int:
    """Order in-place builds so an older live wrapper cannot replace a newer one."""
    mtimes: list[int] = []
    for path in _RUNTIME_GENERATION_FILES:
        try:
            mtimes.append(path.stat().st_mtime_ns)
        except OSError:
            continue
    mtimes.extend(
        _distribution_generation(distribution)
        for distribution in _RUNTIME_DISTRIBUTIONS
    )
    return max(mtimes, default=0)


BUILD_ID = _build_id()
BUILD_GENERATION = _build_generation()
INSTALL_ID = f"{INSTALL_PATH_ID}-{BUILD_ID}"
SOCKET_FILENAME = f"lean-v{PROTOCOL_VERSION}-{INSTALL_ID}.sock"


class LeanRuntimeError(RuntimeError):
    """Base error raised by the node-local Lean runtime client."""


class LeanRuntimeUnavailable(LeanRuntimeError):
    """No runtime was listening before a request was dispatched."""


class LeanRuntimeProtocolError(LeanRuntimeError):
    """The runtime spoke an incompatible or malformed protocol."""


class LeanRuntimeOutcomeUnknown(LeanRuntimeError):
    """A dispatched runtime request did not produce a trustworthy response."""


class LeanRuntimeRemoteError(LeanRuntimeError):
    """The runtime rejected a well-formed request."""


def _protocol_version_from_socket(socket_path: Path) -> int:
    """Read the wire generation from an installation-owned socket name."""
    prefix = "lean-v"
    install_marker = f"-{INSTALL_PATH_ID}-"
    name = socket_path.name
    if not name.startswith(prefix) or not name.endswith(".sock"):
        raise LeanRuntimeProtocolError(
            f"cannot determine Lean runtime protocol from socket name: {socket_path}"
        )
    version, separator, build = name[len(prefix) :].partition(install_marker)
    if not separator or not version.isdecimal() or not build.removesuffix(".sock"):
        raise LeanRuntimeProtocolError(
            f"cannot determine Lean runtime protocol from socket name: {socket_path}"
        )
    return int(version)


def _response_timeout_from_environment() -> float:
    raw = os.environ.get(
        "AUTOFORM_RUNTIME_RESPONSE_TIMEOUT",
        str(DEFAULT_RESPONSE_TIMEOUT),
    )
    try:
        value = float(raw)
    except ValueError as error:
        raise LeanRuntimeError(
            f"AUTOFORM_RUNTIME_RESPONSE_TIMEOUT must be a number, got {raw!r}"
        ) from error
    if not math.isfinite(value) or value <= 0:
        raise LeanRuntimeError(
            "AUTOFORM_RUNTIME_RESPONSE_TIMEOUT must be a finite positive number"
        )
    return value


@dataclass(frozen=True)
class RuntimePaths:
    """Filesystem locations used by one per-user runtime instance."""

    directory: Path
    socket: Path
    lock: Path
    lifetime_lock: Path
    log: Path
    compatibility_lifetime_locks: tuple[Path, ...] = ()
    compatibility_locks: tuple[Path, ...] = ()


def _private_runtime_directory(path: Path) -> Path:
    """Create and validate a directory that only the current user can enter."""
    try:
        path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        raise LeanRuntimeError(f"runtime path is not a directory: {path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise LeanRuntimeError(f"runtime directory is not owned by this user: {path}")
    if info.st_mode & 0o077:
        raise LeanRuntimeError(
            f"runtime directory must not be accessible by group or other users: {path}"
        )
    return path


def default_runtime_paths() -> RuntimePaths:
    """Return short, node-local paths for the current Unix user."""
    if not hasattr(socket, "AF_UNIX") or not hasattr(os, "getuid"):
        raise LeanRuntimeError("the shared Lean runtime currently requires Unix-domain sockets")

    configured = os.environ.get("AUTOFORM_RUNTIME_DIR")
    if configured:
        directory = Path(configured).expanduser()
        if not directory.is_absolute():
            raise LeanRuntimeError("AUTOFORM_RUNTIME_DIR must be an absolute path")
    else:
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        if xdg and Path(xdg).is_absolute():
            directory = Path(xdg) / "autoform"
        else:
            directory = Path("/tmp") / f"autoform-{os.getuid()}"

    directory = _private_runtime_directory(directory)
    socket_path = directory / SOCKET_FILENAME

    # Most Unix implementations cap sockaddr_un paths at roughly 108 bytes.
    # A uid-specific /tmp fallback remains node-local and is still private.
    if len(os.fsencode(socket_path)) > 100 and not configured:
        directory = _private_runtime_directory(Path("/tmp") / f"autoform-{os.getuid()}")
        socket_path = directory / SOCKET_FILENAME
    if len(os.fsencode(socket_path)) > 100:
        raise LeanRuntimeError(f"Lean runtime socket path is too long: {socket_path}")

    return RuntimePaths(
        directory=directory,
        socket=socket_path,
        # Stable locks serialize every build and wire-protocol generation for
        # this installation. Legacy protocol-specific locks are also acquired
        # during migration by LeanRuntimeClient.
        lock=directory / f"lean-{INSTALL_PATH_ID}.lock",
        lifetime_lock=directory / f"lean-{INSTALL_PATH_ID}.lifetime.lock",
        log=directory / f"lean-v{PROTOCOL_VERSION}-{INSTALL_ID}.log",
        compatibility_locks=tuple(
            directory / f"lean-v{version}-{INSTALL_PATH_ID}.lock"
            for version in LEGACY_LOCK_PROTOCOL_VERSIONS
        ),
        compatibility_lifetime_locks=tuple(
            directory
            / f"lean-v{version}-{INSTALL_PATH_ID}.lifetime.lock"
            for version in LEGACY_LOCK_PROTOCOL_VERSIONS
        ),
    )


def runtime_paths_for_socket(socket_path: str | os.PathLike[str]) -> RuntimePaths:
    """Derive lock and log paths for an explicit socket, primarily for tests/CLI."""
    path = Path(socket_path).expanduser()
    if not path.is_absolute():
        raise LeanRuntimeError("Lean runtime socket path must be absolute")
    directory = _private_runtime_directory(path.parent)
    if len(os.fsencode(path)) > 100:
        raise LeanRuntimeError(f"Lean runtime socket path is too long: {path}")
    return RuntimePaths(
        directory=directory,
        socket=path,
        lock=path.with_suffix(".lock"),
        lifetime_lock=path.with_suffix(".lifetime.lock"),
        log=path.with_suffix(".log"),
    )


class LeanRuntimeClient:
    """Make one-request-per-connection calls to the shared Lean runtime."""

    def __init__(
        self,
        socket_path: str | os.PathLike[str] | None = None,
        *,
        autostart: bool = True,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        response_timeout: float | None = None,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    ) -> None:
        self._uses_default_paths = socket_path is None
        self.paths = (
            runtime_paths_for_socket(socket_path)
            if socket_path is not None
            else default_runtime_paths()
        )
        self.autostart = autostart
        self.connect_timeout = connect_timeout
        self.response_timeout = (
            _response_timeout_from_environment()
            if response_timeout is None
            else response_timeout
        )
        self.startup_timeout = startup_timeout

    @property
    def socket_path(self) -> Path:
        return self.paths.socket

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        autostart: bool | None = None,
        response_timeout: float | None = None,
        deadline: float | None = None,
    ) -> Any:
        """Call a runtime method, starting the daemon only before dispatch."""
        should_start = self.autostart if autostart is None else autostart
        try:
            return self._request_once(
                method,
                params or {},
                response_timeout=response_timeout,
                deadline=deadline,
            )
        except LeanRuntimeUnavailable:
            if not should_start:
                raise

        self.ensure_running(deadline=deadline)
        return self._request_once(
            method,
            params or {},
            response_timeout=response_timeout,
            deadline=deadline,
        )

    def ping(
        self,
        *,
        autostart: bool = False,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Return daemon identity without warming a Lean project."""
        result = self.request(
            "daemon.ping",
            autostart=autostart,
            response_timeout=min(self.response_timeout, 5.0),
            deadline=deadline,
        )
        if not isinstance(result, dict):
            raise LeanRuntimeProtocolError("daemon.ping returned a non-object result")
        if result.get("install_id") != INSTALL_ID:
            raise LeanRuntimeProtocolError(
                "Lean runtime belongs to a different Autoform installation"
            )
        return result

    @staticmethod
    def _remaining(deadline: float, purpose: str) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LeanRuntimeUnavailable(
                f"timed out waiting for {purpose}; a previous Lean runtime "
                "may still be cleaning up"
            )
        return remaining

    @classmethod
    def _acquire_file_lock(
        cls,
        fd: int,
        *,
        deadline: float,
        purpose: str,
    ) -> None:
        try:
            import fcntl
        except ImportError as error:  # pragma: no cover - guarded by AF_UNIX above
            raise LeanRuntimeError("runtime bootstrap requires POSIX file locking") from error

        delay = 0.025
        while True:
            wait = cls._remaining(deadline, purpose)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                pass
            except OSError as error:
                if error.errno != errno.EACCES:
                    raise
            time.sleep(min(delay, wait))
            delay = min(delay * 1.7, 0.25)

    def _acquire_bootstrap_locks(self, deadline: float) -> list[int]:
        """Hold stable and pre-stable startup locks during lifecycle changes."""
        lock_paths = (self.paths.lock, *self.paths.compatibility_locks)
        lock_fds: list[int] = []
        try:
            for lock_path in dict.fromkeys(lock_paths):
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                lock_fds.append(lock_fd)
                self._acquire_file_lock(
                    lock_fd,
                    deadline=deadline,
                    purpose="Lean runtime startup coordination",
                )
        except BaseException:
            for lock_fd in reversed(lock_fds):
                os.close(lock_fd)
            raise
        return lock_fds

    def _lifetime_lock_paths(self) -> tuple[Path, ...]:
        return tuple(
            dict.fromkeys(
                (
                    self.paths.lifetime_lock,
                    *self.paths.compatibility_lifetime_locks,
                )
            )
        )

    def _acquire_lifetime_locks(self, deadline: float) -> list[int]:
        lock_fds: list[int] = []
        try:
            for lock_path in self._lifetime_lock_paths():
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                lock_fds.append(lock_fd)
                self._acquire_file_lock(
                    lock_fd,
                    deadline=deadline,
                    purpose="the previous Lean runtime",
                )
        except BaseException:
            for lock_fd in reversed(lock_fds):
                os.close(lock_fd)
            raise
        return lock_fds

    def ensure_running(self, *, deadline: float | None = None) -> dict[str, Any]:
        """Race-safely start one detached runtime for this user and node."""
        startup_deadline = time.monotonic() + self.startup_timeout
        deadline = (
            startup_deadline
            if deadline is None
            else min(deadline, startup_deadline)
        )

        try:
            return self.ping(autostart=False, deadline=deadline)
        except LeanRuntimeUnavailable:
            pass

        _private_runtime_directory(self.paths.directory)
        lock_fds = self._acquire_bootstrap_locks(deadline)
        try:
            try:
                return self.ping(autostart=False, deadline=deadline)
            except LeanRuntimeUnavailable:
                pass

            self._stop_previous_builds(deadline=deadline)

            # A daemon owns this lock for its complete lifetime. If it has
            # stopped accepting connections but is still draining requests,
            # wait here rather than starting an overlapping replacement.
            lifetime_fds = self._acquire_lifetime_locks(deadline)
            try:
                self._remaining(deadline, "Lean runtime startup")
                self._remove_stale_socket()
                process = self._spawn_daemon(
                    bootstrap_fds=tuple(lock_fds),
                    lifetime_fds=tuple(lifetime_fds),
                )
            finally:
                for lifetime_fd in reversed(lifetime_fds):
                    os.close(lifetime_fd)

            try:
                delay = 0.025
                last_error: BaseException | None = None
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        break
                    try:
                        return self.ping(autostart=False, deadline=deadline)
                    except LeanRuntimeUnavailable as error:
                        last_error = error
                    wait = deadline - time.monotonic()
                    if wait <= 0:
                        break
                    time.sleep(min(delay, wait))
                    delay = min(delay * 1.7, 0.25)

                exit_detail = (
                    f" (exit code {process.returncode})"
                    if process.poll() is not None
                    else ""
                )
                detail = f": {last_error}" if last_error else ""
                raise LeanRuntimeUnavailable(
                    f"Lean runtime did not become ready{exit_detail}; "
                    f"log: {self.paths.log}{detail}"
                )
            except BaseException:
                self._terminate_failed_start(process)
                raise
        finally:
            for lock_fd in reversed(lock_fds):
                os.close(lock_fd)

    def stop(self, *, deadline: float | None = None) -> dict[str, Any]:
        """Ask a running daemon to finish active calls within one deadline."""
        deadline = (
            time.monotonic() + self.response_timeout
            if deadline is None
            else deadline
        )
        lock_fds = self._acquire_bootstrap_locks(deadline)
        try:
            try:
                result = self._stop_protocol(PROTOCOL_VERSION, deadline=deadline)
            except LeanRuntimeUnavailable:
                stopped = self._stop_previous_builds(deadline=deadline)
                self._wait_for_lifetimes(deadline=deadline)
                if stopped:
                    return {"stopping": False, "stopped_previous": stopped}
                raise
            self._wait_for_lifetimes(deadline=deadline)
            return result
        finally:
            for lock_fd in reversed(lock_fds):
                os.close(lock_fd)

    def _stop_previous_builds(self, *, deadline: float | None = None) -> list[int]:
        """Gracefully replace older protocol/build generations for this install."""
        if not self._uses_default_paths:
            return []
        pattern = f"lean-v*-{INSTALL_PATH_ID}-*.sock"
        stopped: list[int] = []
        stale: list[LeanRuntimeClient] = []
        needs_lifetime_wait = False
        for socket_path in sorted(self.paths.directory.glob(pattern)):
            if socket_path == self.paths.socket:
                continue
            protocol_version = _protocol_version_from_socket(socket_path)
            if protocol_version > PROTOCOL_VERSION:
                raise LeanRuntimeProtocolError(
                    f"Lean runtime protocol v{protocol_version} is newer than "
                    f"supported v{PROTOCOL_VERSION}; restart with the newer "
                    "Autoform installation"
                )
            if (
                protocol_version != PROTOCOL_VERSION
                and protocol_version not in SUPPORTED_PREVIOUS_PROTOCOL_VERSIONS
            ):
                raise LeanRuntimeProtocolError(
                    f"Lean runtime protocol v{protocol_version} is older than "
                    f"the supported versions for v{PROTOCOL_VERSION}; stop it "
                    "with its matching Autoform installation"
                )
            previous = LeanRuntimeClient(
                socket_path=socket_path,
                autostart=False,
                connect_timeout=self.connect_timeout,
                response_timeout=self.response_timeout,
                startup_timeout=self.startup_timeout,
            )
            try:
                status = previous._request_once(
                    "daemon.ping",
                    {},
                    deadline=deadline,
                    response_timeout=min(self.response_timeout, 5.0),
                    protocol_version=protocol_version,
                )
            except LeanRuntimeUnavailable:
                stale.append(previous)
                needs_lifetime_wait = True
                continue
            if not isinstance(status, dict):
                raise LeanRuntimeProtocolError(
                    f"runtime at {socket_path} returned a non-object status"
                )
            if status.get("protocol") != protocol_version:
                raise LeanRuntimeProtocolError(
                    f"runtime at {socket_path} does not match protocol "
                    f"v{protocol_version}"
                )
            install_id = status.get("install_id")
            if not isinstance(install_id, str) or not install_id.startswith(
                f"{INSTALL_PATH_ID}-"
            ):
                raise LeanRuntimeProtocolError(
                    f"runtime at {socket_path} does not belong to this Autoform "
                    "installation"
                )
            generation = status.get("build_generation")
            if protocol_version == PROTOCOL_VERSION:
                if not isinstance(generation, int):
                    raise LeanRuntimeProtocolError(
                        f"runtime at {socket_path} did not report a build generation"
                    )
                if generation > BUILD_GENERATION:
                    raise LeanRuntimeProtocolError(
                        "a newer Autoform runtime build is already active; "
                        "restart this plugin session before using Lean tools"
                    )
            result = previous._stop_protocol(protocol_version, deadline=deadline)
            needs_lifetime_wait = True
            pid = result.get("pid") if isinstance(result, dict) else None
            if isinstance(pid, int):
                stopped.append(pid)
        if needs_lifetime_wait:
            self._wait_for_lifetimes(deadline=deadline)
        for previous in stale:
            previous._remove_stale_socket()
        return stopped

    def _stop_protocol(
        self,
        protocol_version: int,
        *,
        deadline: float,
    ) -> dict[str, Any]:
        result = self._request_once(
            "daemon.shutdown",
            {},
            response_timeout=min(self.response_timeout, 10.0),
            deadline=deadline,
            protocol_version=protocol_version,
        )
        if not isinstance(result, dict):
            raise LeanRuntimeProtocolError("daemon.shutdown returned a non-object result")
        while self.paths.socket.exists():
            wait = self._remaining(deadline, "Lean runtime shutdown")
            time.sleep(min(0.025, wait))
        return result

    def _wait_for_lifetime(self, path: Path, *, deadline: float) -> None:
        lifetime_fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            self._acquire_file_lock(
                lifetime_fd,
                deadline=deadline,
                purpose="Lean runtime cleanup",
            )
        finally:
            os.close(lifetime_fd)

    def _wait_for_lifetimes(self, *, deadline: float) -> None:
        for path in self._lifetime_lock_paths():
            self._wait_for_lifetime(path, deadline=deadline)

    def _spawn_daemon(
        self,
        *,
        bootstrap_fds: tuple[int, ...],
        lifetime_fds: tuple[int, ...],
    ) -> subprocess.Popen[bytes]:
        """Spawn a daemon that inherits every already-acquired lifecycle lock."""
        command = [
            sys.executable,
            "-m",
            "servers.lean_runtime",
            "--socket",
            str(self.paths.socket),
            "--log",
            str(self.paths.log),
            "--lock",
            str(self.paths.lock),
            "--lifetime-lock",
            str(self.paths.lifetime_lock),
        ]
        for path in self.paths.compatibility_lifetime_locks:
            command.extend(("--compatibility-lifetime-lock", str(path)))
        for path in self.paths.compatibility_locks:
            command.extend(("--compatibility-lock", str(path)))
        for descriptor in bootstrap_fds:
            command.extend(("--inherited-bootstrap-lock-fd", str(descriptor)))
        for descriptor in lifetime_fds:
            command.extend(("--inherited-lifetime-lock-fd", str(descriptor)))
        command.append("serve")
        inherited_fds = tuple(dict.fromkeys((*bootstrap_fds, *lifetime_fds)))
        with self.paths.log.open("ab", buffering=0) as log:
            return subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                pass_fds=inherited_fds,
                start_new_session=True,
                cwd=PACKAGE_ROOT,
            )

    def _remove_stale_socket(self) -> None:
        try:
            info = self.paths.socket.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode):
            raise LeanRuntimeError(
                f"refusing to replace non-socket runtime path: {self.paths.socket}"
            )
        if info.st_uid != os.getuid():
            raise LeanRuntimeError(
                f"refusing to replace socket owned by another user: {self.paths.socket}"
            )
        self.paths.socket.unlink()

    @staticmethod
    def _terminate_failed_start(process: subprocess.Popen[bytes]) -> None:
        """Request shutdown without killing a daemon that may own active work."""
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # The daemon may already be serving another client and draining a
            # Lean child. Its lifetime lock prevents a replacement from being
            # admitted while fail-closed cleanup continues.
            logger.warning(
                "spawned Lean runtime is still shutting down; leaving it under "
                "its lifetime lock"
            )
        except OSError:
            logger.exception("failed to request shutdown of the spawned Lean runtime")

    def _request_once(
        self,
        method: str,
        params: dict[str, Any],
        *,
        response_timeout: float | None = None,
        deadline: float | None = None,
        protocol_version: int | None = None,
    ) -> Any:
        wire_protocol = (
            PROTOCOL_VERSION if protocol_version is None else protocol_version
        )
        request_id = uuid.uuid4().hex
        payload = json.dumps(
            {
                "v": wire_protocol,
                "id": request_id,
                "method": method,
                "params": params,
            },
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        if len(payload) > MAX_MESSAGE_BYTES:
            raise LeanRuntimeProtocolError("Lean runtime request exceeds the message limit")

        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dispatched = False

        def outcome_unknown() -> LeanRuntimeOutcomeUnknown:
            return LeanRuntimeOutcomeUnknown(
                "Lean runtime request may have completed, but no trustworthy "
                "response was received after request dispatch. The request was "
                "not retried and must not be replayed."
            )

        def bounded_timeout(configured: float) -> float:
            if deadline is None:
                return configured
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LeanRuntimeUnavailable(
                    "Lean runtime request deadline expired before dispatch"
                )
            return min(configured, remaining)

        try:
            connection.settimeout(bounded_timeout(self.connect_timeout))
            try:
                connection.connect(str(self.paths.socket))
            except (FileNotFoundError, ConnectionRefusedError) as error:
                raise LeanRuntimeUnavailable(
                    f"Lean runtime is not listening at {self.paths.socket}"
                ) from error
            except OSError as error:
                if error.errno in {2, 61, 111}:
                    raise LeanRuntimeUnavailable(
                        f"Lean runtime is not listening at {self.paths.socket}"
                    ) from error
                raise LeanRuntimeError(f"cannot connect to Lean runtime: {error}") from error

            configured_response_timeout = (
                self.response_timeout
                if response_timeout is None
                else response_timeout
            )
            connection.settimeout(bounded_timeout(configured_response_timeout))
            # From this point onward, any failure is ambiguous: the daemon may
            # have received the request. Never auto-replay Lean execution.
            dispatched = True
            connection.sendall(payload)
            raw = self._read_line(connection)
        except socket.timeout as error:
            if dispatched:
                raise outcome_unknown() from error
            raise LeanRuntimeError("timed out waiting for Lean runtime connection") from error
        except LeanRuntimeError as error:
            if dispatched:
                raise outcome_unknown() from error
            raise
        except OSError as error:
            if not dispatched:
                raise LeanRuntimeUnavailable(
                    f"Lean runtime is not listening at {self.paths.socket}"
                ) from error
            raise outcome_unknown() from error
        finally:
            connection.close()

        try:
            try:
                response = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise LeanRuntimeProtocolError(
                    "Lean runtime returned invalid JSON"
                ) from error
            if not isinstance(response, dict):
                raise LeanRuntimeProtocolError("Lean runtime response is not an object")
            if response.get("v") != wire_protocol:
                raise LeanRuntimeProtocolError(
                    "Lean runtime protocol mismatch: expected "
                    f"{wire_protocol}, got {response.get('v')!r}"
                )
            if response.get("id") != request_id:
                raise LeanRuntimeProtocolError(
                    "Lean runtime response id does not match the request"
                )
            if response.get("ok") is True:
                return response.get("result")
            if response.get("ok") is not False:
                raise LeanRuntimeProtocolError(
                    "Lean runtime response has an invalid success marker"
                )
            remote_error = response.get("error")
            if not isinstance(remote_error, dict):
                raise LeanRuntimeProtocolError("Lean runtime returned a malformed error")
            error_type = remote_error.get("type", "RuntimeError")
            message = remote_error.get("message", "unspecified runtime error")
        except LeanRuntimeProtocolError as error:
            raise outcome_unknown() from error
        raise LeanRuntimeRemoteError(f"{error_type}: {message}")

    @staticmethod
    def _read_line(connection: socket.socket) -> str:
        data = bytearray()
        while len(data) <= MAX_MESSAGE_BYTES:
            chunk = connection.recv(min(65536, MAX_MESSAGE_BYTES + 1 - len(data)))
            if not chunk:
                raise LeanRuntimeProtocolError("Lean runtime closed without a response")
            data.extend(chunk)
            newline = data.find(b"\n")
            if newline >= 0:
                if data[newline + 1 :]:
                    raise LeanRuntimeProtocolError("Lean runtime returned trailing response data")
                return bytes(data[:newline]).decode("utf-8")
        raise LeanRuntimeProtocolError("Lean runtime response exceeds the message limit")
