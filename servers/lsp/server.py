"""Lean execution backend for diagnostics and type information over LSP.

Wraps Lean 4 language server processes and provides file diagnostics and hover
information independently from the REPL pool.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
import errno
import os
import signal
import sys
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil
from leanclient.aio import (
    AsyncLeanLSPClient,
    LeanClientError,
    LeanRequestTimeout,
    LeanTransportError,
)

if TYPE_CHECKING:
    from fastmcp.server import FastMCP

from servers import resolve_lean_project_dir
from servers.lean_client import LeanRuntimeClient

logger = getLogger(__name__)

DEFAULT_LSP_TIMEOUT = 360
LSP_ABORT_TERM_SECONDS = 0.5
LSP_ABORT_KILL_SECONDS = 1.0
LSP_CLIENT_CLOSE_SECONDS = 6.0
LSP_LOOP_CLOSE_SECONDS = 1.0
LSP_STARTUP_CLEANUP_RETRY_SECONDS = 0.05
MAX_LSP_STARTUP_CLEANUP_RETRY_SECONDS = 1.0


class LspProtocolError(RuntimeError):
    """The Lean language server returned or emitted invalid JSON-RPC state."""

    def __init__(self, message: str, *, stderr_tail: str = "") -> None:
        self.stderr_tail = stderr_tail
        if stderr_tail:
            message = f"{message}\nLean stderr tail:\n{stderr_tail}"
        super().__init__(message)


class LspBusyError(TimeoutError):
    """A queued operation could not enter the shared LSP session in time."""


class LspCleanupError(LspProtocolError):
    """The Lean language-server process group could not be proven dead."""


@dataclass
class LspConfig:
    """Configuration for the Lean LSP server."""

    cwd: str = "."
    lake_command: list[str] = field(default_factory=lambda: ["lake", "serve"])
    timeout: float = DEFAULT_LSP_TIMEOUT


class LeanLspSession:
    """Synchronous Autoform boundary around leanclient's async LSP client.

    leanclient owns JSON-RPC framing, response routing, server requests, UTF-16
    conversion, and Lean's diagnostics barrier. Autoform retains project
    admission, absolute deadlines, environment isolation, and fail-closed
    process-group cleanup.
    """

    def __init__(
        self,
        config: LspConfig,
        *,
        client_factory: Callable[..., AsyncLeanLSPClient] = AsyncLeanLSPClient,
    ) -> None:
        self.config = config
        self._client_factory = client_factory
        self._client: AsyncLeanLSPClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_ready = threading.Event()
        self._operation_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._poisoned = True
        self._process_group_id: int | None = None
        self._identity_path: Path | None = None

    def start(self, *, deadline: float | None = None) -> None:
        """Start leanclient before an optional absolute monotonic deadline."""
        deadline = self._deadline_or_default(deadline)
        try:
            remaining = self._remaining(deadline)
        except TimeoutError as error:
            raise LspBusyError(
                "Lean LSP deadline expired before startup admission"
            ) from error
        if not self._lifecycle_lock.acquire(timeout=remaining):
            raise LspBusyError(
                "Lean LSP deadline expired while waiting to start the session"
            )
        try:
            try:
                self._remaining(deadline)
            except TimeoutError as error:
                raise LspBusyError(
                    "Lean LSP deadline expired after startup admission"
                ) from error
            if self._client is not None or self._loop is not None:
                raise RuntimeError("Lean LSP session has already been started")
            try:
                self._start_loop(deadline)
                self._remaining(deadline)
                descriptor, identity_name = tempfile.mkstemp(
                    prefix="autoform-lsp-", suffix=".json"
                )
                os.close(descriptor)
                self._identity_path = Path(identity_name)
                command = [
                    sys.executable,
                    "-I",
                    "-m",
                    "servers.lsp.launcher",
                    identity_name,
                    "--",
                    *self.config.lake_command,
                ]

                async def start_client() -> None:
                    self._remaining(deadline)
                    client = self._client_factory(
                        project_path=str(Path(self.config.cwd).resolve()),
                        max_workers=1,
                        request_timeout=self.config.timeout,
                        check_version=True,
                        server_command=command,
                        report_delay_ms=None,
                    )
                    self._client = client
                    await client.start()

                self._submit_until(start_client(), deadline, "starting Lean LSP")
                self._remaining(deadline)
                identity = self._read_process_identity()
                self._process_group_id = identity
                self._remaining(deadline)
                self._poisoned = False
            except BaseException:
                # A failed initialize can still leave lake, the watchdog, or a
                # worker alive. A factory exception is not inserted into the
                # project cache, so this stack frame must retain ownership and
                # retry cleanup until the group is verifiably gone.
                self._cleanup_failed_start()
                raise
        finally:
            self._lifecycle_lock.release()

    def close(self) -> None:
        """Close the client and return only after its process group is gone."""
        with self._lifecycle_lock:
            self._shutdown()

    def abort(self) -> None:
        """Discard a failed client and return only after its group is gone."""
        with self._lifecycle_lock:
            self._shutdown()

    def is_alive(self) -> bool:
        """Return whether the cached client and supervised process are usable."""
        client = self._client
        process_group_id = self._process_group_id
        return (
            client is not None
            and not self._poisoned
            and client.alive
            and process_group_id is not None
            and self._process_group_has_live_members(process_group_id)
        )

    def get_diagnostics(
        self,
        file_path: str,
        *,
        deadline: float | None = None,
    ) -> list[dict]:
        """Return barrier-complete diagnostics for an in-project Lean file."""
        return self._run_document_operation(
            file_path,
            lambda client, path, deadline: self._diagnostics(client, path, deadline),
            deadline=self._deadline_or_default(deadline),
        )

    def hover(
        self,
        file_path: str,
        line: int,
        character: int,
        *,
        deadline: float | None = None,
    ) -> str | None:
        """Return hover text at a zero-indexed codepoint position."""
        return self._run_document_operation(
            file_path,
            lambda client, path, deadline: self._hover(
                client, path, line, character, deadline
            ),
            deadline=self._deadline_or_default(deadline),
        )

    def _run_document_operation(
        self,
        file_path: str,
        operation: Callable[[AsyncLeanLSPClient, str, float], Awaitable[Any]],
        *,
        deadline: float,
    ) -> Any:
        try:
            remaining = self._remaining(deadline)
        except TimeoutError as error:
            raise LspBusyError(
                "Lean LSP deadline expired before session admission"
            ) from error
        if not self._operation_lock.acquire(timeout=remaining):
            raise LspBusyError(
                "Lean LSP deadline expired while waiting for the Lean LSP session"
            )
        try:
            try:
                self._remaining(deadline)
            except TimeoutError as error:
                raise LspBusyError(
                    "Lean LSP deadline expired while waiting for the Lean LSP session"
                ) from error
            client = self._client
            if client is None or self._poisoned or not client.alive:
                raise LspProtocolError("Lean LSP session is no longer usable")
            relative_path = self._relative_path(file_path)
            try:
                return self._submit_until(
                    operation(client, relative_path, deadline),
                    deadline,
                    "running Lean LSP operation",
                )
            except LspBusyError:
                # The coroutine was never submitted, so the existing client
                # remains safe to use after an admission timeout.
                raise
            except BaseException as error:
                try:
                    self.abort()
                except BaseException as cleanup_error:
                    note = f"Lean LSP abort also failed: {cleanup_error}"
                    add_note = getattr(error, "add_note", None)
                    if add_note is not None:
                        add_note(note)
                    else:  # pragma: no cover - Python 3.10 compatibility
                        logger.error("%s", note)
                if isinstance(error, Exception) and not isinstance(
                    error, (LspProtocolError, TimeoutError, OSError)
                ):
                    raise LspProtocolError(
                        "leanclient operation failed before its result could be "
                        f"validated: {error}"
                    ) from error
                raise
        finally:
            self._operation_lock.release()

    async def _diagnostics(
        self,
        client: AsyncLeanLSPClient,
        path: str,
        deadline: float,
    ) -> list[dict]:
        self._remaining(deadline)
        await client.open(path, wait=False)
        try:
            report = await client.diagnostics(
                path,
                fresh=True,
                timeout=self._remaining(deadline),
            )
            try:
                raw_diagnostics = report.items
                fatal_error = report.fatal_error
            except AttributeError as error:
                raise LspProtocolError(
                    f"LSP diagnostics returned a malformed report: {report!r}"
                ) from error
            if not isinstance(raw_diagnostics, list) or not isinstance(
                fatal_error, bool
            ):
                raise LspProtocolError(
                    f"LSP diagnostics returned a malformed report: {report!r}"
                )
            diagnostics = [
                self._validated_diagnostic(diagnostic, index)
                for index, diagnostic in enumerate(raw_diagnostics)
            ]
            if fatal_error:
                diagnostics.append(
                    {
                        "severity": 1,
                        "message": "Lean reported a fatal file-processing error.",
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 0},
                        },
                        "source": "leanclient",
                    }
                )
            return diagnostics
        finally:
            await client.close_file(path)

    async def _hover(
        self,
        client: AsyncLeanLSPClient,
        path: str,
        line: int,
        character: int,
        deadline: float,
    ) -> str | None:
        self._remaining(deadline)
        await client.open(path, wait=False)
        try:
            await client.barrier(path, timeout=self._remaining(deadline))
            self._remaining(deadline)
            result = await client.hover(path, line, character, fresh=False)
            return self._normalized_hover(result)
        finally:
            await client.close_file(path)

    @staticmethod
    def _validated_diagnostic(diagnostic: Any, index: int) -> dict:
        if not isinstance(diagnostic, dict):
            raise LspProtocolError(
                f"LSP diagnostic {index} is not an object: {diagnostic!r}"
            )
        message = diagnostic.get("message")
        diagnostic_range = diagnostic.get("range")
        if not isinstance(message, str) or not isinstance(diagnostic_range, dict):
            raise LspProtocolError(f"LSP diagnostic {index} is malformed")
        position = diagnostic_range.get("start")
        if not isinstance(position, dict):
            raise LspProtocolError(f"LSP diagnostic {index} is malformed")
        for coordinate in ("line", "character"):
            value = position.get(coordinate)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise LspProtocolError(f"LSP diagnostic {index} is malformed")
        severity = diagnostic.get("severity")
        if severity is not None and (
            isinstance(severity, bool) or not isinstance(severity, int)
        ):
            raise LspProtocolError(f"LSP diagnostic {index} is malformed")
        return dict(diagnostic)

    @staticmethod
    def _normalized_hover(result: Any) -> str | None:
        if result is None:
            return None
        if not isinstance(result, dict) or "contents" not in result:
            raise LspProtocolError(f"LSP hover returned malformed result: {result!r}")

        def text(content: Any) -> str:
            if isinstance(content, str):
                return content
            if isinstance(content, dict):
                value = content.get("value")
                if isinstance(value, str):
                    return value
            raise LspProtocolError("LSP hover contents must contain text")

        contents = result["contents"]
        if isinstance(contents, list):
            return "\n\n".join(text(content) for content in contents)
        return text(contents)

    def _relative_path(self, file_path: str) -> str:
        root = Path(self.config.cwd).resolve()
        path = Path(file_path).resolve(strict=True)
        try:
            relative = path.relative_to(root)
        except ValueError as error:
            raise ValueError(f"LSP file must stay inside project root: {path}") from error
        return relative.as_posix()

    def _submit(self, awaitable: Awaitable[Any], timeout: float, operation: str) -> Any:
        loop = self._loop
        if loop is None or not loop.is_running():
            if hasattr(awaitable, "close"):
                awaitable.close()  # type: ignore[attr-defined]
            raise LspProtocolError("Lean LSP event loop is not running")
        future = asyncio.run_coroutine_threadsafe(awaitable, loop)
        try:
            return future.result(timeout=max(0.0, timeout))
        except FutureTimeoutError as error:
            future.cancel()
            raise TimeoutError(f"timed out after {timeout:g}s {operation}") from error
        except LeanRequestTimeout as error:
            raise TimeoutError(str(error)) from error
        except LeanTransportError as error:
            raise LspProtocolError(
                f"leanclient failed: {error}",
                stderr_tail=error.stderr_tail,
            ) from error
        except LeanClientError as error:
            raise LspProtocolError(f"leanclient failed: {error}") from error

    def _submit_until(
        self,
        awaitable: Awaitable[Any],
        deadline: float,
        operation: str,
    ) -> Any:
        """Submit an awaitable using only the request's remaining budget."""
        try:
            remaining = self._remaining(deadline)
        except TimeoutError as error:
            if hasattr(awaitable, "close"):
                awaitable.close()  # type: ignore[attr-defined]
            raise LspBusyError(
                f"Lean LSP deadline expired before {operation}"
            ) from error
        return self._submit(awaitable, remaining, operation)

    def _start_loop(self, deadline: float) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        self._loop_ready.clear()

        def run() -> None:
            asyncio.set_event_loop(loop)
            self._loop_ready.set()
            try:
                loop.run_forever()
            finally:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()

        thread = threading.Thread(target=run, name="autoform-leanclient", daemon=False)
        self._loop_thread = thread
        try:
            thread.start()
        except BaseException:
            self._loop = None
            self._loop_thread = None
            loop.close()
            raise
        wait_timeout = min(LSP_LOOP_CLOSE_SECONDS, self._remaining(deadline))
        if not self._loop_ready.wait(timeout=wait_timeout):
            if time.monotonic() >= deadline:
                raise TimeoutError("Lean LSP startup exceeded its deadline")
            raise LspProtocolError("Lean LSP event loop failed to start")

    def _shutdown(self) -> None:
        self._poisoned = True
        self._claim_published_process_group()
        client = self._client
        loop = self._loop
        client_close_error: Exception | None = None
        if client is not None and loop is not None and loop.is_running():
            try:
                self._submit(client.close(), LSP_CLIENT_CLOSE_SECONDS, "closing leanclient")
            except Exception as error:
                client_close_error = error
                logger.warning("leanclient close failed; enforcing process cleanup", exc_info=True)
        elif client is not None and self._process_group_id is None:
            client_close_error = LspCleanupError(
                "leanclient cannot be closed and no supervised process identity is available"
            )

        # The launcher can publish its identity while leanclient is unwinding
        # a partial start. Claim it again before deciding whether close alone
        # established cleanup.
        self._claim_published_process_group()
        if self._process_group_id is None and client_close_error is not None:
            raise LspCleanupError(
                "could not verify cleanup after leanclient startup failed"
            ) from client_close_error

        # Do not dismantle the event loop or delete the identity file until the
        # process boundary has been verified. A failed attempt remains fully
        # retryable from the next cleanup pass.
        self._terminate_process_group()
        self._stop_loop()
        self._remove_identity_file()
        self._client = None
        self._process_group_id = None

    def _cleanup_failed_start(self) -> None:
        """Keep ownership of a partial start until verified cleanup succeeds."""
        delay = LSP_STARTUP_CLEANUP_RETRY_SECONDS
        while True:
            try:
                self._shutdown()
                return
            except BaseException:
                logger.exception(
                    "Lean LSP startup cleanup remains incomplete; retaining ownership"
                )
            try:
                time.sleep(delay)
            except BaseException:
                # Cleanup cannot be abandoned while this failed factory result
                # has no cache entry that could own it.
                continue
            delay = min(delay * 2, MAX_LSP_STARTUP_CLEANUP_RETRY_SECONDS)

    def _claim_published_process_group(self) -> None:
        if self._process_group_id is not None or self._identity_path is None:
            return
        try:
            self._process_group_id = self._read_process_identity()
        except LspCleanupError:
            pass

    def _stop_loop(self) -> None:
        loop, thread = self._loop, self._loop_thread
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=LSP_LOOP_CLOSE_SECONDS)
            if thread.is_alive():
                raise LspCleanupError("timed out stopping leanclient's event-loop thread")
        self._loop = None
        self._loop_thread = None

    def _read_process_identity(self) -> int:
        path = self._identity_path
        if path is None:
            raise LspCleanupError("Lean LSP supervisor identity file was not created")
        try:
            import json

            identity = json.loads(path.read_text(encoding="utf-8"))
            pid = identity["pid"]
            process_group_id = identity["pgid"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise LspCleanupError("Lean LSP supervisor did not publish a valid identity") from error
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or isinstance(process_group_id, bool)
            or not isinstance(process_group_id, int)
            or pid <= 0
            or process_group_id != pid
        ):
            raise LspCleanupError(f"invalid Lean LSP process identity: {identity!r}")
        return process_group_id

    def _terminate_process_group(self) -> None:
        process_group_id = self._process_group_id
        if (
            process_group_id is None
            or not self._process_group_has_live_members(process_group_id)
        ):
            return
        self._signal_process_group(process_group_id, signal.SIGTERM)
        if self._wait_for_group_exit(process_group_id, LSP_ABORT_TERM_SECONDS):
            return
        self._signal_process_group(process_group_id, signal.SIGKILL)
        if not self._wait_for_group_exit(process_group_id, LSP_ABORT_KILL_SECONDS):
            raise LspCleanupError(
                f"Lean LSP process group {process_group_id} survived SIGKILL"
            )

    @staticmethod
    def _process_group_exists(process_group_id: int) -> bool:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except OSError as error:
            if error.errno == errno.ESRCH:
                return False
            if error.errno == errno.EPERM:
                return True
            raise
        return True

    def _process_group_has_live_members(self, process_group_id: int) -> bool:
        """Treat an all-zombie group as dead, including Darwin's EPERM case."""
        if not self._process_group_exists(process_group_id):
            return False
        for candidate in psutil.process_iter(["pid", "status"]):
            try:
                if os.getpgid(candidate.info["pid"]) != process_group_id:
                    continue
                if candidate.info["status"] != psutil.STATUS_ZOMBIE:
                    return True
            except (ProcessLookupError, PermissionError, psutil.Error):
                continue
        return False

    @staticmethod
    def _signal_process_group(process_group_id: int, signal_number: int) -> None:
        try:
            os.killpg(process_group_id, signal_number)
        except ProcessLookupError:
            pass
        except OSError as error:
            if error.errno != errno.ESRCH:
                raise LspCleanupError(
                    f"failed to signal Lean LSP process group {process_group_id}"
                ) from error

    def _wait_for_group_exit(self, process_group_id: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self._process_group_has_live_members(process_group_id):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def _remove_identity_file(self) -> None:
        path = self._identity_path
        if path is None:
            return
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        self._identity_path = None

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Lean LSP operation exceeded its deadline")
        return remaining

    def _deadline_or_default(self, deadline: float | None) -> float:
        if deadline is not None:
            return deadline
        return time.monotonic() + self.config.timeout


class LeanLspProjects:
    """Lazily keep one language-server session per explicit Lean project."""

    def __init__(
        self,
        session_factory: Callable[[Path], LeanLspSession] | None = None,
    ) -> None:
        self._session_factory = session_factory or self._start_session
        self._sessions: dict[Path, LeanLspSession] = {}
        self._lock = threading.Lock()
        self._closed = False

    @staticmethod
    def _start_session(project_dir: Path) -> LeanLspSession:
        session = LeanLspSession(LspConfig(cwd=str(project_dir)))
        session.start()
        return session

    def get(self, project_dir: str) -> LeanLspSession:
        """Return the session for a validated absolute Lake project."""
        root = resolve_lean_project_dir(project_dir)
        with self._lock:
            if self._closed:
                raise RuntimeError("Lean LSP project router is closed")
            session = self._sessions.get(root)
            if session is None:
                session = self._session_factory(root)
                self._sessions[root] = session
            return session

    def close(self) -> None:
        """Close all sessions created by this router."""
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._closed = True
        for session in sessions:
            session.close()


def format_lsp_diagnostics(diagnostics: list[dict]) -> str:
    """Format Lean's LSP diagnostics for the MCP tool response."""
    if not diagnostics:
        return "No diagnostics — file compiles cleanly."

    lines: list[str] = []
    for diagnostic in diagnostics:
        severity = {1: "error", 2: "warning", 3: "info", 4: "hint"}.get(
            diagnostic.get("severity", 0), "unknown"
        )
        position = diagnostic.get("range", {}).get("start", {})
        line = position.get("line", 0) + 1
        column = position.get("character", 0)
        message = diagnostic.get("message", "")
        lines.append(f"{line}:{column}: {severity}: {message}")

    errors = sum(item.get("severity") == 1 for item in diagnostics)
    warnings = sum(item.get("severity") == 2 for item in diagnostics)
    return f"Diagnostics: {errors} error(s), {warnings} warning(s)\n" + "\n".join(lines)


def create_lsp_server(runtime: LeanRuntimeClient) -> FastMCP:
    """Create the public LSP MCP adapter for the shared Lean runtime."""
    from fastmcp.server import FastMCP

    server = FastMCP(name="autoform-lsp")

    @server.tool
    def lean_diagnostic_messages(project_dir: str, file_path: str) -> str:
        """Return Lean diagnostics for an in-project file.

        Args:
            project_dir: Absolute path to the Lake project root.
            file_path: Absolute path, or a path relative to project_dir, to a Lean file.
        """
        return runtime.request(
            "lsp.diagnostics",
            {"project_dir": project_dir, "file_path": file_path},
        )

    @server.tool
    def lean_hover(project_dir: str, file_path: str, line: int, character: int) -> str:
        """Return Lean hover information at a zero-indexed position.

        Args:
            project_dir: Absolute path to the Lake project root.
            file_path: Absolute path, or a path relative to project_dir, to a Lean file.
            line: Zero-indexed line number.
            character: Zero-indexed Unicode codepoint column.
        """
        return runtime.request(
            "lsp.hover",
            {
                "project_dir": project_dir,
                "file_path": file_path,
                "line": line,
                "character": character,
            },
        )

    return server


def main() -> None:
    create_lsp_server(LeanRuntimeClient()).run(transport="stdio")


if __name__ == "__main__":
    main()
