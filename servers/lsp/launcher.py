"""Exec a supervised Lean language server with Autoform's process boundary.

``leanclient`` owns LSP framing and request routing.  This launcher owns the
process concerns that its public API does not expose: a scrubbed Lake
environment, process identity, and runtime-crash cleanup.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from servers import clean_lake_environment
from servers.process_supervisor import supervise_process


def main(argv: list[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) >= 3 and arguments[1] == "--":
        identity_path = Path(arguments[0])
        if os.getpgrp() != os.getpid():
            raise RuntimeError(
                "Lean LSP launcher must be started in its own process group"
            )
        identity_path.write_text(
            json.dumps({"pid": os.getpid(), "pgid": os.getpgrp()}),
            encoding="utf-8",
        )
        os.execvpe(
            arguments[2],
            arguments[2:],
            clean_lake_environment(),
        )
    try:
        separator = arguments.index("--")
    except ValueError:
        separator = -1
    if separator < 3 or separator == len(arguments) - 1:
        raise SystemExit(
            "usage: python -m servers.lsp.launcher PID_FILE PARENT_PID "
            "LIFETIME_LOCK [LIFETIME_LOCK ...] -- COMMAND [ARG ...]"
        )

    identity_path = Path(arguments[0])
    try:
        parent_pid = int(arguments[1])
    except ValueError as error:
        raise SystemExit("PARENT_PID must be an integer") from error
    supervise_process(
        arguments[separator + 1 :],
        parent_pid=parent_pid,
        lifetime_locks=tuple(Path(path) for path in arguments[2:separator]),
        identity_path=identity_path,
        environment=clean_lake_environment(),
    )


if __name__ == "__main__":
    main()
