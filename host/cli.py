#!/usr/bin/env python3
"""CLI — reads WORKFLOW.md for config, delegates to launch.py and watcher."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import load_workflow


def repo_root() -> Path:
    return Path(subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    ).stdout.strip())


def sessions_dir() -> Path:
    return repo_root() / ".agent-worker" / "sessions"


def cmd_start(a):
    cmd = [sys.executable, str(Path(__file__).parent / "launch.py"), a.issue_id]
    if a.max_turns:
        cmd += ["--max-turns", str(a.max_turns)]
    if a.workflow:
        cmd += ["--workflow", a.workflow]
    subprocess.run(cmd)


def cmd_resume(a):
    cmd = [sys.executable, str(Path(__file__).parent / "launch.py"),
           a.issue_id, "--resume"]
    if a.workflow:
        cmd += ["--workflow", a.workflow]
    subprocess.run(cmd)


def cmd_answer(a):
    """Write answer.txt directly — works even if container is paused."""
    sid = a.issue_id[:12]
    sd = sessions_dir() / sid
    if sd.exists():
        (sd / "answer.txt").write_text(a.message)
        print(f"Answer written for {sid}")
    else:
        print(f"No session found for {sid}", file=sys.stderr)


def cmd_watcher(a):
    subprocess.run([
        sys.executable, str(Path(__file__).parent / "watcher.py"),
        "--sessions-dir", str(sessions_dir()),
    ])


def cmd_status(a):
    sd = sessions_dir()
    if not sd.exists():
        print("No sessions."); return
    print(f"{'SESSION':<14} {'STATUS':<26} {'STEP':>5} {'CPS':>4}")
    print("-" * 54)
    for f in sorted(sd.glob("*/state.json")):
        sid = f.parent.name
        try:
            s = json.loads(f.read_text())
            print(f"{sid:<14} {s.get('status','?'):<26} "
                  f"{s.get('step',0):>5} {len(s.get('checkpoints',[])):>4}")
        except Exception:
            print(f"{sid:<14} {'<error>':<26}")


def cmd_logs(a):
    log_file = sessions_dir() / a.issue_id[:12] / "raw-output.log"
    if not log_file.exists():
        print("No log file.", file=sys.stderr); return
    subprocess.run(["tail", "-f", str(log_file)])


def cmd_history(a):
    cf = sessions_dir() / a.issue_id[:12] / "conversation.jsonl"
    if not cf.exists():
        print("No history.", file=sys.stderr); return
    icons = {"thought":"💭","checkpoint":"📌","question":"❓","human_answer_sent":"👤",
             "tool_call":"🔧","tool_result":"📄","system":"⚙️","user":"📝"}
    for line in cf.read_text().strip().splitlines():
        try:
            e = json.loads(line)
            print(f"  {e['timestamp'][:19]}  {icons.get(e['role'],'•')} "
                  f"[{e['role']}] {e['content'][:120]}")
        except Exception:
            continue


def cmd_cleanup(a):
    r = repo_root()
    sid = a.issue_id[:12]

    # Read config to know workspace kind
    config = load_workflow(r / "WORKFLOW.md")

    if config.workspace.kind == "worktree":
        wt = r / config.workspace.root / f"agent-{sid}"
        if wt.exists():
            subprocess.run(["git", "worktree", "remove", str(wt), "--force"])
        subprocess.run(["git", "branch", "-D", f"agent/{sid}"], capture_output=True)
    else:
        import shutil
        ws = r / config.workspace.root / f"agent-{sid}"
        if ws.exists():
            shutil.rmtree(ws)

    ss = sessions_dir() / sid
    if ss.exists() and not a.keep_session:
        import shutil
        shutil.rmtree(ss)
    print(f"Cleaned up {sid}")


def main():
    p = argparse.ArgumentParser(prog="agent-worker")
    p.add_argument("--workflow", default=None, help="Path to WORKFLOW.md")
    s = p.add_subparsers(dest="cmd", required=True)

    sp = s.add_parser("start")
    sp.add_argument("issue_id")
    sp.add_argument("--max-turns", type=int, default=None)
    sp.set_defaults(func=cmd_start)

    sp = s.add_parser("resume")
    sp.add_argument("issue_id")
    sp.set_defaults(func=cmd_resume)

    sp = s.add_parser("answer")
    sp.add_argument("issue_id")
    sp.add_argument("message")
    sp.set_defaults(func=cmd_answer)

    sp = s.add_parser("watcher")
    sp.set_defaults(func=cmd_watcher)

    sp = s.add_parser("status")
    sp.set_defaults(func=cmd_status)

    sp = s.add_parser("logs")
    sp.add_argument("issue_id")
    sp.set_defaults(func=cmd_logs)

    sp = s.add_parser("history")
    sp.add_argument("issue_id")
    sp.set_defaults(func=cmd_history)

    sp = s.add_parser("cleanup")
    sp.add_argument("issue_id")
    sp.add_argument("--keep-session", action="store_true")
    sp.set_defaults(func=cmd_cleanup)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
