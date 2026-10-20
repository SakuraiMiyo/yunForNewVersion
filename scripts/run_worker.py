# -*- coding: utf-8 -*-
"""Run one prepared, confirmed run in a process of its own.

Why a separate process instead of a thread. A record takes 16-25 minutes of wall clock,
and the process that starts it is usually a child of something else -- a robot plugin
runs inside the host, and the MCP server is spawned by the agent. When that host restarts
(or the session ends), a thread inside it dies with it, mid-upload: the observed outcome
was a run that got six minutes short of ``finish`` and left no record at all.

So the destructive half of the handshake gets its own process, detached from the parent's
lifetime. It:

* re-checks every gate in the child (token is single-use, so a double start is impossible);
* writes progress plus a final result to the shared run-state file, which is what lets any
  other process answer "is it still running, and how did it end?";
* honours the same abort flag (``abort.<pid>.flag``) as an in-process run.

Invoked by ``mcp_bridge.start_background_run``; not meant to be run by hand, though it can
be for debugging:

    python scripts/run_worker.py --token <token> --config <config.ini> --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "yunrun"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True)
    parser.add_argument("--project", default=str(PROJECT))
    parser.add_argument("--config", default="")
    parser.add_argument("--simulation-root", default="")
    parser.add_argument("--keep-runs", type=int, default=10)
    parser.add_argument("--keep-tasklists", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("--is-drift", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log", default="")
    parser.add_argument("--debug", action="store_true",
                        help="write a start-up trace too; a worker that dies quietly is "
                             "otherwise indistinguishable from one that never ran")
    args = parser.parse_args()

    project = Path(args.project).resolve()
    for extra in (str(project), str(project / "gps_track_lab" / "src"), str(project / "runner"),
                  str(project / "tools")):
        if extra not in sys.path:
            sys.path.insert(0, extra)

    # The worker's own log comes first: if the chain dies during import or start-up, a
    # silent exit with no output tells the operator nothing.
    log_handle = None
    if args.log:
        try:
            log_handle = open(args.log, "a", encoding="utf-8", buffering=1)
        except OSError:
            log_handle = None
        if log_handle is not None:
            sys.stdout = log_handle
            sys.stderr = log_handle

    def trace(message: str) -> None:
        target = log_handle
        if target is None:
            if not args.debug:
                return
            try:
                target = open(str(Path(os.environ.get("TEMP", ".")) / "yunrun-worker-debug.log"),
                              "a", encoding="utf-8", buffering=1)
            except OSError:
                return
        try:
            target.write(f"[worker {os.getpid()}] {message}\n")
            target.flush()
        except Exception:  # noqa: BLE001 - tracing must never break a run
            pass

    trace(f"start token={args.token} dry_run={args.dry_run} project={project}")

    from mcp_bridge import YunLocalBridge

    bridge = YunLocalBridge(
        project,
        args.config or None,
        args.simulation_root or None,
        allow_submit=True,
        keep_runs=args.keep_runs,
        keep_tasklists=args.keep_tasklists,
        allow_external_simulation_root=bool(args.simulation_root),
        # Running detached already; going through the in-process path keeps the state file
        # and the abort flag semantics identical to a threaded run.
        spawn_detached=False,
    )
    trace("bridge ready, executing")
    try:
        # publish_state is the whole point of running here: progress and the final verdict
        # go to the shared state file, which is how every other process can answer "is it
        # still running?" and "how did it end?".
        result = bridge.execute_run(
            args.token, confirmed=True, dry_run=bool(args.dry_run),
            is_drift=bool(args.is_drift), timeout_s=args.timeout, publish_state=True)
    except Exception as exc:  # noqa: BLE001 - a worker must always leave a verdict behind
        result = {"ok": False, "error": {"code": "worker_crashed",
                                         "message": f"{type(exc).__name__}: {exc}"}}
        trace("crash:\n" + traceback.format_exc())
    trace(f"done ok={bool(result.get('ok'))} error={result.get('error')}")
    if log_handle is not None:
        log_handle.flush()
        log_handle.close()
    return 0 if isinstance(result, dict) and result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
