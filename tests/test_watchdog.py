"""Tests for the global watchdog."""

from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _write_registration(projects_d: Path, name: str, *, path: Path, log: Path, pid: int) -> Path:
    reg = projects_d / f"{name}.yaml"
    reg.write_text(
        f"path: {path}\n"
        f"log: {log}\n"
        f"pid: {pid}\n"
        "started: 2026-04-28T00:00:00+00:00\n"
    )
    return reg


def test_discover_projects_from_projects_d(tmp_path, monkeypatch):
    from host.watchdog import scanner

    projects_d = tmp_path / ".nightshift" / "projects.d"
    projects_d.mkdir(parents=True)
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    _write_registration(projects_d, "project-a", path=project_a, log=project_a / "watcher.log", pid=os.getpid())
    _write_registration(projects_d, "project-b", path=project_b, log=project_b / "watcher.log", pid=999999999)
    (projects_d / "ignore.txt").write_text("nope")

    monkeypatch.setattr(scanner, "PROJECTS_D", projects_d)

    projects = list(scanner.discover_projects())

    assert {project.name for project in projects} == {"project-a", "project-b"}


def test_cleans_stale_registrations(tmp_path, monkeypatch):
    from host.watchdog import scanner

    projects_d = tmp_path / ".nightshift" / "projects.d"
    projects_d.mkdir(parents=True)
    project = tmp_path / "project-a"
    project.mkdir()
    reg = _write_registration(projects_d, "project-a", path=project, log=project / "watcher.log", pid=999999999)

    monkeypatch.setattr(scanner, "PROJECTS_D", projects_d)

    projects = list(scanner.discover_projects(clean_stale=True))

    assert projects == []
    assert not reg.exists()


def test_detects_stale_log(tmp_path):
    from host.watchdog import rules

    log_path = tmp_path / "watcher.log"
    log_path.write_text("INFO starting\n")
    old_mtime = 1710000000
    os.utime(log_path, (old_mtime, old_mtime))

    anomalies = rules.check_stale(log_path, threshold_s=60)

    assert len(anomalies) == 1
    assert anomalies[0].type == "stale_log"


def test_detects_error_threshold(tmp_path):
    from host.watchdog import rules

    lines = [
        "INFO ok",
        "ERROR one",
        "ERROR two",
        "ERROR three",
    ]

    anomalies = rules.check_errors(lines, threshold=2)

    assert anomalies
    assert any(a.type == "error_threshold" for a in anomalies)


def test_detects_repeated_errors(tmp_path):
    from host.watchdog import rules

    lines = [
        "ERROR database unavailable",
        "ERROR database unavailable",
        "ERROR database unavailable",
    ]

    anomalies = rules.check_repeated(lines, threshold=3)

    assert anomalies
    assert any(a.type == "repeated_error" for a in anomalies)


def test_llm_analysis_called_on_anomaly(monkeypatch):
    from host.watchdog import llm

    analyze = MagicMock(return_value="summary")
    monkeypatch.setattr(llm, "_ollama", analyze)

    result = llm.analyze("snippet", provider="ollama", model="phi3:mini", api_key="")

    assert result == "summary"
    analyze.assert_called_once()
    assert "snippet" in analyze.call_args.args[0]


def test_llm_provider_ollama(monkeypatch):
    from host.watchdog import llm

    calls = {}

    def fake_run(cmd, check, capture_output, text):
        calls["cmd"] = cmd
        return SimpleNamespace(stdout="ok")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = llm._ollama("prompt", model="phi3:mini")

    assert result == "ok"
    assert calls["cmd"] == ["ollama", "run", "phi3:mini", "prompt"]


def test_llm_provider_openrouter(monkeypatch):
    from host.watchdog import llm

    response = SimpleNamespace(json=lambda: {"choices": [{"message": {"content": "analysis"}}]})
    post = MagicMock(return_value=response)
    monkeypatch.setattr(llm.requests, "post", post)

    result = llm._openrouter("prompt", model="m", api_key="key", base_url="https://example.com")

    assert result == "analysis"
    post.assert_called_once()
    args, kwargs = post.call_args
    assert args[0] == "https://example.com/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer key"


def test_llm_provider_none_skips(monkeypatch):
    from host.watchdog import llm

    analyze = MagicMock(side_effect=AssertionError("should not be called"))
    monkeypatch.setattr(llm, "_ollama", analyze)
    monkeypatch.setattr(llm, "_openrouter", analyze)

    result = llm.analyze("prompt", provider="none", model="m", api_key="")

    assert result == ""
    analyze.assert_not_called()


def test_notify_telegram(monkeypatch):
    from host.watchdog import notify

    post = MagicMock(return_value=SimpleNamespace(json=lambda: {"ok": True}))
    monkeypatch.setattr(notify.requests, "post", post)

    config = notify.WatchdogConfig(
        notify=notify.NotifyConfig(
            telegram=notify.TelegramNotifyConfig(token="tok", chat_id="42")
        )
    )

    notify.send_alert("project-a", [notify.Anomaly("error_threshold", "too many errors", "")], "summary", config)

    args, kwargs = post.call_args
    assert "bottok" in args[0]
    assert kwargs["json"]["chat_id"] == "42"
    assert "project-a" in kwargs["json"]["text"]


def test_config_loading(tmp_path, monkeypatch):
    from host.watchdog import config

    cfg_file = tmp_path / "watchdog.yaml"
    cfg_file.write_text(
        """
llm:
  provider: ollama
  model: phi3:mini
watch:
  interval_s: 15
  watcher_stale_s: 120
  log_lines: 25
rules:
  error_threshold: 4
  repeat_threshold: 2
notify:
  telegram:
    token: tok
    chat_id: "42"
"""
    )

    loaded = config.load_config(cfg_file)

    assert loaded.llm.provider == "ollama"
    assert loaded.watch.interval_s == 15
    assert loaded.watch.watcher_stale_s == 120
    assert loaded.rules.error_threshold == 4
    assert loaded.notify.telegram.chat_id == "42"
