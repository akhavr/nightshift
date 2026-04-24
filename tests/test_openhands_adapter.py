"""Tests for OpenHands usage extraction.

REQ: REQ-032
"""

import json

import pytest

from adapters.agents.openhands import OpenHandsAgent


def _message_event(**extra) -> str:
    ev = {"kind": "MessageEvent", "source": "agent"}
    ev.update(extra)
    return json.dumps(ev)


def _observation_event(**extra) -> str:
    ev = {
        "kind": "ObservationEvent",
        "source": "environment",
        "observation_type": "CmdOutputObservation",
    }
    ev.update(extra)
    return json.dumps(ev)


def test_parse_extracts_usage():
    agent = OpenHandsAgent()
    raw = json.dumps({
        "kind": "LLMCompletionLogEvent",
        "model_name": "openrouter/qwen/qwen3-coder",
        "usage_id": "agent",
        "filename": "completion.json",
        "log_data": json.dumps({
            "cost": 0.0125,
            "usage_summary": {
                "prompt_tokens": 1200,
                "completion_tokens": 80,
                "cache_read_tokens": 400,
            },
        }),
    })

    ev = agent._parse(raw)

    assert ev is not None
    assert ev.metadata["usage"] == {
        "input_tokens": 1200,
        "output_tokens": 80,
        "cost_usd": 0.0125,
        "model": "openrouter/qwen/qwen3-coder",
    }


def test_usage_accumulated_across_events():
    agent = OpenHandsAgent()
    raw1 = _message_event(
        content="first",
        usage={"prompt_tokens": 100, "completion_tokens": 25},
        cost=0.001,
        model="openrouter/qwen/qwen3-coder",
    )
    raw2 = _observation_event(
        content="second",
        tokens={"input": 300, "output": 40, "cost": 0.002},
        model="openrouter/qwen/qwen3-coder",
    )

    events = [agent._parse(raw1), agent._parse(raw2)]
    usages = [ev.metadata["usage"] for ev in events if ev is not None]

    assert sum(usage["input_tokens"] for usage in usages) == 400
    assert sum(usage["output_tokens"] for usage in usages) == 65
    assert sum(usage["cost_usd"] for usage in usages) == pytest.approx(0.003)
    assert usages[0]["model"] == "openrouter/qwen/qwen3-coder"
