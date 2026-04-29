"""Command line entry point for nightshift-client daemon control."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from nightshift_client._daemon import (
    pidfile_path_for,
    pidfile_running,
    remove_pidfile,
    remove_socket,
    run_foreground,
    socket_path_for,
    read_pidfile,
)


def _wait_for_daemon_ready(
    proc: subprocess.Popen[object],
    pidfile: Path,
    socket_path: Path,
    timeout: float = 5.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        if pidfile.exists() and socket_path.exists() and read_pidfile(pidfile) == proc.pid:
            return True
        time.sleep(0.05)
    return False


def _start_daemon(repo_path: Path) -> int:
    pidfile = pidfile_path_for(repo_path)
    socket_path = socket_path_for(repo_path)
    if pidfile_running(pidfile):
        print(f"nightshift-client daemon already running ({read_pidfile(pidfile)})")
        return 0

    if pidfile.exists():
        remove_pidfile(pidfile)
    if socket_path.exists():
        remove_socket(socket_path)

    cmd = [
        sys.executable,
        "-m",
        "nightshift_client.cli",
        "daemon",
        "run",
        "--repo",
        str(repo_path),
    ]
    proc = subprocess.Popen(cmd, start_new_session=True)
    if not _wait_for_daemon_ready(proc, pidfile, socket_path):
        try:
            os.kill(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        remove_socket(socket_path)
        remove_pidfile(pidfile)
        print("nightshift-client daemon failed to start")
        return 1
    print(f"nightshift-client daemon started ({proc.pid})")
    return 0


def _stop_daemon(repo_path: Path) -> int:
    pidfile = pidfile_path_for(repo_path)
    socket_path = socket_path_for(repo_path)
    pid = read_pidfile(pidfile)
    if not pid:
        remove_socket(socket_path)
        remove_pidfile(pidfile)
        print("nightshift-client daemon not running")
        return 0

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        remove_socket(socket_path)
        remove_pidfile(pidfile)
        print("nightshift-client daemon not running")
        return 0

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    remove_socket(socket_path)
    remove_pidfile(pidfile)
    print("nightshift-client daemon stopped")
    return 0


def _status_daemon(repo_path: Path) -> int:
    pidfile = pidfile_path_for(repo_path)
    socket_path = socket_path_for(repo_path)
    pid = read_pidfile(pidfile)
    if not pid:
        print("nightshift-client daemon stopped")
        return 1

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        remove_socket(socket_path)
        remove_pidfile(pidfile)
        print("nightshift-client daemon stopped")
        return 1

    if socket_path.exists():
        print(f"nightshift-client daemon running ({pid})")
    else:
        print(f"nightshift-client daemon starting ({pid})")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nightshift-client")
    s = p.add_subparsers(dest="cmd", required=True)

    daemon = s.add_parser("daemon", help="Manage the tracker daemon")
    daemon_sub = daemon.add_subparsers(dest="daemon_cmd", required=True)

    start = daemon_sub.add_parser("start", help="Start the daemon in the background")
    start.add_argument("--repo", type=Path, default=Path.cwd())
    start.set_defaults(func=lambda a: _start_daemon(a.repo))

    stop = daemon_sub.add_parser("stop", help="Stop the daemon")
    stop.add_argument("--repo", type=Path, default=Path.cwd())
    stop.set_defaults(func=lambda a: _stop_daemon(a.repo))

    status = daemon_sub.add_parser("status", help="Show daemon status")
    status.add_argument("--repo", type=Path, default=Path.cwd())
    status.set_defaults(func=lambda a: _status_daemon(a.repo))

    run = daemon_sub.add_parser("run", add_help=False, help=argparse.SUPPRESS)
    run.add_argument("--repo", type=Path, default=Path.cwd())
    run.set_defaults(func=lambda a: run_foreground(a.repo))

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
