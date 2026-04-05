#!/usr/bin/python3
"""OpenHands launcher with monkey-patches for Docker bugs.

Applies two workarounds before delegating to the real OpenHands CLI:

1. NoOpCondenser — replaces LLMSummarizingCondenser which crashes on startup
   in Docker containers (fires on empty context before any agent action).

2. max_output_tokens cap — litellm reports inflated max_output_tokens for some
   models (e.g. 262100 for qwen3-coder), consuming the entire context window.
   Capped to 16384 to leave room for input tokens.

Both bugs are in OpenHands 1.13.1. Remove this launcher when upstream fixes land.
See docs/openhands-docker-investigation.md for details.
"""

import sys

# Patch 1: Replace LLMSummarizingCondenser with NoOpCondenser.
# The summarizing condenser makes an LLM call on startup that fails in Docker.
import openhands.tools.preset.default as preset
from openhands.sdk.context.condenser.no_op_condenser import NoOpCondenser

preset.get_default_condenser = lambda llm: NoOpCondenser()

# Patch 2: Cap max_output_tokens after model info initialization.
# litellm reports inflated values for some models on OpenRouter.
MAX_OUTPUT_TOKENS_CAP = 16384

from openhands.sdk.llm.llm import LLM

_orig_init_model_info = LLM._init_model_info_and_caps


def _patched_init_model_info(self):
    _orig_init_model_info(self)
    if self.max_output_tokens and self.max_output_tokens > MAX_OUTPUT_TOKENS_CAP:
        self.max_output_tokens = MAX_OUTPUT_TOKENS_CAP


LLM._init_model_info_and_caps = _patched_init_model_info

# Delegate to the real OpenHands CLI entry point.
from openhands_cli.entrypoint import main

main()
