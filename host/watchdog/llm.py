"""LLM analysis helpers for watchdog anomalies."""

from __future__ import annotations

import logging
import subprocess

import requests

log = logging.getLogger("watchdog")


def analyze(log_snippet: str, provider: str, model: str, api_key: str, base_url: str | None = None) -> str:
    """Analyze a log snippet with the configured provider."""
    provider = (provider or "none").lower()
    try:
        if provider == "none":
            return ""
        if provider == "ollama":
            return _ollama(log_snippet, model=model)
        if provider == "openrouter":
            return _openrouter(log_snippet, model=model, api_key=api_key, base_url=base_url)
        raise ValueError(f"Unsupported llm provider: {provider}")
    except (OSError, subprocess.CalledProcessError, requests.RequestException, ValueError, KeyError, TypeError) as e:
        log.warning("LLM analysis failed for provider %s: %s", provider, e)
        return ""


def _ollama(snippet: str, model: str) -> str:
    """Run Ollama locally."""
    result = subprocess.run(
        ["ollama", "run", model, snippet],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _openrouter(snippet: str, model: str, api_key: str, base_url: str | None = None) -> str:
    """Call the OpenRouter chat completions API."""
    base = (base_url or "https://openrouter.ai/api/v1").rstrip("/")
    resp = requests.post(
        f"{base}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "Analyze the log snippet and summarize the problem."},
                {"role": "user", "content": snippet},
            ],
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]
