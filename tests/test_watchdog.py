"""Tests for the global watchdog."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import requests
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
    dead_reg = _write_registration(
        projects_d,
        "project-b",
        path=project_b,
        log=project_b / "watcher.log",
        pid=999999999,
    )
    (projects_d / "ignore.txt").write_text("nope")

    monkeypatch.setattr(scanner, "PROJECTS_D", projects_d)

    projects = list(scanner.discover_projects())

    assert {project.name for project in projects} == {"project-a"}
    assert not dead_reg.exists()


def test_cleans_stale_registrations(tmp_path, monkeypatch):
    from host.watchdog import scanner

    projects_d = tmp_path / ".nightshift" / "projects.d"
    projects_d.mkdir(parents=True)
    project = tmp_path / "project-a"
    project.mkdir()
    reg = _write_registration(projects_d, "project-a", path=project, log=project / "watcher.log", pid=999999999)

    monkeypatch.setattr(scanner, "PROJECTS_D", projects_d)

    projects = list(scanner.discover_projects())

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


def test_detects_error_threshold():
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


def test_detects_repeated_errors():
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

    response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"choices": [{"message": {"content": "analysis"}}]},
    )
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


def test_llm_analysis_failure_returns_empty(monkeypatch):
    from host.watchdog import llm

    def boom(*args, **kwargs):
        raise FileNotFoundError("ollama missing")

    monkeypatch.setattr(llm, "_ollama", boom)

    result = llm.analyze("snippet", provider="ollama", model="phi3:mini", api_key="")

    assert result == ""


def test_llm_openrouter_failure_returns_empty(monkeypatch):
    from host.watchdog import llm

    response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: (_ for _ in ()).throw(ValueError("bad json")),
    )
    post = MagicMock(return_value=response)
    monkeypatch.setattr(llm.requests, "post", post)

    result = llm.analyze("prompt", provider="openrouter", model="m", api_key="key")

    assert result == ""


def test_notify_telegram(monkeypatch):
    from host.watchdog import config as watchdog_config
    from host.watchdog import notify

    post = MagicMock(
        return_value=SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"ok": True},
        )
    )
    monkeypatch.setattr(notify.requests, "post", post)

    config = watchdog_config.WatchdogConfig(
        notify=watchdog_config.NotifyConfig(
            telegram=watchdog_config.TelegramNotifyConfig(token="tok", chat_id="42")
        )
    )

    notify.send_alert("project-a", [notify.Anomaly("error_threshold", "too many errors", "")], "summary", config)

    args, kwargs = post.call_args
    assert "bottok" in args[0]
    assert kwargs["json"]["chat_id"] == "42"
    assert "project-a" in kwargs["json"]["text"]


def test_notify_failure_returns_false(monkeypatch):
    from host.watchdog import config as watchdog_config
    from host.watchdog import notify

    def boom(*args, **kwargs):
        raise requests.RequestException("network down")

    monkeypatch.setattr(notify.requests, "post", boom)

    config = watchdog_config.WatchdogConfig(
        notify=watchdog_config.NotifyConfig(
            telegram=watchdog_config.TelegramNotifyConfig(token="tok", chat_id="42")
        )
    )

    sent = notify.send_alert("project-a", [notify.Anomaly("error_threshold", "too many errors", "")], "summary", config)

    assert sent is False


def test_config_loading(tmp_path):
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


def test_config_rejects_non_mapping_root(tmp_path):
    from host.watchdog import config

    cfg_file = tmp_path / "watchdog.yaml"
    cfg_file.write_text("- not a mapping\n")

    with pytest.raises(ValueError, match="root must be a mapping"):
        config.load_config(cfg_file)


def test_run_once_continues_after_llm_failure(monkeypatch):
    from host.watchdog import config as watchdog_config
    from host.watchdog import main as watchdog_main
    from host.watchdog.rules import Anomaly

    statuses = [
        SimpleNamespace(project="project-a", log_path=Path("/tmp/project-a.log")),
        SimpleNamespace(project="project-b", log_path=Path("/tmp/project-b.log")),
    ]
    monkeypatch.setattr(watchdog_main, "discover_projects", lambda clean_stale=True: iter(statuses))
    monkeypatch.setattr(watchdog_main, "read_log_tail", lambda path, lines: ["ERROR broken"])
    monkeypatch.setattr(
        watchdog_main.rules,
        "check_stale",
        lambda path, threshold_s: [Anomaly("stale_log", "stale", "")],
    )
    monkeypatch.setattr(watchdog_main.rules, "check_errors", lambda lines, threshold: [])
    monkeypatch.setattr(watchdog_main.rules, "check_repeated", lambda lines, threshold: [])

    analyze = MagicMock(side_effect=[RuntimeError("boom"), "summary-b"])
    monkeypatch.setattr(watchdog_main.llm, "analyze", analyze)

    alerts = []

    def fake_send_alert(project, anomalies, llm_summary, config):
        alerts.append((project, llm_summary))
        return True

    monkeypatch.setattr(watchdog_main.notify, "send_alert", fake_send_alert)

    cfg = watchdog_config.WatchdogConfig(
        llm=watchdog_config.LlmConfig(provider="ollama", model="phi3:mini"),
    )

    issues = watchdog_main.run_once(cfg)

    assert issues == 2
    assert alerts == [("project-a", ""), ("project-b", "summary-b")]
    assert analyze.call_count == 2


def test_run_once_continues_after_notify_failure(monkeypatch):
    from host.watchdog import config as watchdog_config
    from host.watchdog import main as watchdog_main
    from host.watchdog.rules import Anomaly

    statuses = [
        SimpleNamespace(project="project-a", log_path=Path("/tmp/project-a.log")),
        SimpleNamespace(project="project-b", log_path=Path("/tmp/project-b.log")),
    ]
    monkeypatch.setattr(watchdog_main, "discover_projects", lambda clean_stale=True: iter(statuses))
    monkeypatch.setattr(watchdog_main, "read_log_tail", lambda path, lines: ["ERROR broken"])
    monkeypatch.setattr(
        watchdog_main.rules,
        "check_stale",
        lambda path, threshold_s: [Anomaly("stale_log", "stale", "")],
    )
    monkeypatch.setattr(watchdog_main.rules, "check_errors", lambda lines, threshold: [])
    monkeypatch.setattr(watchdog_main.rules, "check_repeated", lambda lines, threshold: [])
    monkeypatch.setattr(watchdog_main.llm, "analyze", lambda snippet, **kwargs: "summary")

    alerts = []

    def fake_send_alert(project, anomalies, llm_summary, config):
        alerts.append(project)
        if project == "project-a":
            raise RuntimeError("telegram down")
        return True

    monkeypatch.setattr(watchdog_main.notify, "send_alert", fake_send_alert)

    cfg = watchdog_config.WatchdogConfig(
        llm=watchdog_config.LlmConfig(provider="ollama", model="phi3:mini"),
    )

    issues = watchdog_main.run_once(cfg)

    assert issues == 2
    assert alerts == ["project-a", "project-b"]


def test_run_once_skips_llm_when_provider_none(monkeypatch):
    from host.watchdog import config as watchdog_config
    from host.watchdog import main as watchdog_main
    from host.watchdog.rules import Anomaly

    statuses = [SimpleNamespace(project="project-a", log_path=Path("/tmp/project-a.log"))]
    monkeypatch.setattr(watchdog_main, "discover_projects", lambda clean_stale=True: iter(statuses))
    monkeypatch.setattr(watchdog_main, "read_log_tail", lambda path, lines: ["ERROR broken"])
    monkeypatch.setattr(
        watchdog_main.rules,
        "check_stale",
        lambda path, threshold_s: [Anomaly("stale_log", "stale", "")],
    )
    monkeypatch.setattr(watchdog_main.rules, "check_errors", lambda lines, threshold: [])
    monkeypatch.setattr(watchdog_main.rules, "check_repeated", lambda lines, threshold: [])

    analyze = MagicMock(side_effect=AssertionError("llm should not be called"))
    monkeypatch.setattr(watchdog_main.llm, "analyze", analyze)

    alerts = []

    def fake_send_alert(project, anomalies, llm_summary, config):
        alerts.append((project, llm_summary))
        return True

    monkeypatch.setattr(watchdog_main.notify, "send_alert", fake_send_alert)

    cfg = watchdog_config.WatchdogConfig(
        llm=watchdog_config.LlmConfig(provider="none", model="phi3:mini"),
    )

    issues = watchdog_main.run_once(cfg)

    assert issues == 1
    assert alerts == [("project-a", "")]
    analyze.assert_not_called()


def test_main_reports_config_error(tmp_path):
    from host.watchdog import main as watchdog_main

    cfg_file = tmp_path / "watchdog.yaml"
    cfg_file.write_text("- not a mapping\n")

    assert watchdog_main.main(["--check", "--config", str(cfg_file)]) == 2
