"""Prompt construction — supports WORKFLOW.md Jinja2 templates and fallback."""

import re
from typing import Callable

try:
    import jinja2
except ImportError:
    jinja2 = None  # type: ignore[assignment]

from core.state import StateManager
from core.protocols import TrackerIssue

RECENT_CONVERSATION_LINES = 50


def render_template(
    template: str,
    issue: TrackerIssue,
    related_context: str = "",
    attempt: int | None = None,
    **extra_vars,
) -> str:
    """Render a WORKFLOW.md prompt template with issue context.

    Supports simple {{ var }} and {% if %} / {% endif %} syntax.
    For full Jinja2 support, install jinja2 and switch to that engine.
    Extra keyword arguments are passed as additional template variables
    (e.g. diff, base_branch, agent_branch for REVIEW.md).
    """
    if jinja2 is not None:
        env = jinja2.Environment(undefined=jinja2.Undefined)
        tmpl = env.from_string(template)
        return tmpl.render(
            issue=issue, related_context=related_context, attempt=attempt,
            **extra_vars,
        )
    else:
        # Fallback: simple {{ var }} replacement (no conditionals)
        result = template
        result = result.replace("{{ issue.title }}", issue.title)
        result = result.replace("{{ issue.body }}", issue.body)
        result = result.replace("{{ issue.identifier }}", issue.identifier)
        result = result.replace("{{ related_context }}", related_context)
        result = result.replace("{{ attempt }}", str(attempt) if attempt else "")
        for k, v in extra_vars.items():
            result = result.replace(f"{{{{ {k} }}}}", str(v))
        # Strip unresolved {% %} blocks
        result = re.sub(r"\{%.*?%\}", "", result, flags=re.DOTALL)
        return result.strip()


def build_initial_prompt(
    issue_title: str, issue_body: str, related_context: str,
    markers: dict[str, str] | None = None,
) -> str:
    """Fallback prompt when WORKFLOW.md has no prompt template.

    The markers parameter is kept for backward compatibility but no longer
    used — signal detection is handled by MCP tools and file signals.
    """
    return (
        f"You are working on the following issue:\n\n"
        f"**Title:** {issue_title}\n"
        f"**Description:**\n{issue_body}\n\n"
        f"**Related previous issues:**\n{related_context}\n\n"
        f"RULES:\n"
        f"1. Work on the current branch. The repo is already checked out.\n"
        f"2. If you have a blocking question, include all relevant context IN the\n"
        f"   question itself (code snippets, file paths, what you did, options you\n"
        f"   see) — the human reads ONLY the question text, they cannot see your\n"
        f"   other output.\n"
        f"3. Commit frequently. Write tests where appropriate.\n\n"
        f"Begin by reading the codebase, then plan your approach."
    )


def build_resume_prompt(
    issue_title: str, issue_body: str, related_context: str,
    state_mgr: StateManager, checkpoint_summary: str | None = None,
    diff_fn: Callable[[], str] | None = None,
) -> str:
    state = state_mgr.load_state()
    cp = checkpoint_summary or ("\n".join(
        f"- Step {c.step}: {c.description} [{c.commit[:8]}]"
        for c in state.checkpoints) or "None")
    qa = "\n".join(f"Q: {q.question}\nA: {q.answer}"
                   for q in state.human_answers) or "None"
    diff = diff_fn() if diff_fn else "N/A"
    recent = state_mgr.get_recent_conversation(RECENT_CONVERSATION_LINES)

    ticks = "```"
    prompt = (
        f"Resuming work. Session was interrupted.\n\n"
        f"## Issue\n**Title:** {issue_title}\n**Description:** {issue_body}\n\n"
        f"## Work Done\n{cp}\n\n"
        f"## Changes vs base\n{ticks}\n{diff}\n{ticks}\n\n"
        f"## Q&A\n{qa}\n\n"
        f"## Recent\n{recent}\n\n"
        f"## Instructions\nContinue. Same marker rules apply."
    )
    state_mgr.write_resume_prompt(prompt)
    return prompt
