"""Summarize older messages for context compaction.

Uses the same LLM as the active chat to generate a rolling summary.
Outputs that look like a role-play continuation of the conversation
(tool calls, agent narration, scaffold-label echoes) are rejected and
resampled; if the LLM call fails or keeps failing validation, falls
back to naive truncation.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

DEFAULT_SUMMARIZE_PROMPT = (
    "Summarize this conversation history concisely. Include:\n"
    "- Key decisions made\n"
    "- Files created, modified, or deleted\n"
    "- Current task state and progress\n"
    "- Important context the assistant needs going forward\n"
    "- Any user preferences or instructions that should persist\n\n"
    "Be factual and specific. Use bullet points. Keep under 500 words."
)

# The summarization input is a transcript of the agent's own first-person
# narration and tool-call XML. A model with weak instruction following (the
# chat model itself, when no utility model is configured) can ignore the
# "summarize" system instruction and continue the transcript in character:
# emitting the next narration line, the next tool call, or echoing the input
# scaffold labels. Such output must never be persisted as the rolling summary.
# _validate_summary() rejects it and summarize_messages() resamples.
#
# Rules (tuned against 63 real summarizer outputs — 37 historical checkpoints
# and 26 controlled replays — see tmp-compact-e2e/validate-summary.py):
#   R1 reject: any tool-call markup anywhere in the output
#   R2 reject: fewer than 2 list-item lines AND (len < 400 or ends with ':')
#   R3 reject: first line is first-person agent narration AND < 5 list items
#   C1 cleanup: strip leading scaffold-label lines (e.g. "[Previous summary]")
# The < 5 items gate in R3 keeps legitimately introsed summaries (a
# "Now I have the full picture." preamble followed by a real bulleted
# summary) accepted.
#
# Tool-call tag fragments are built from parts so the literal tags never
# appear in this file's source.
_TOOL_MARKUP = re.compile(
    "|".join(
        re.escape(tag)
        for _name in ("function", "parameter", "user_commentary")
        for tag in (f"<{_name}=", f"</{_name}>", f"<{_name}>", f"</{_name}")
    )
)
_LIST_ITEM = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_SCAFFOLD_LABEL = re.compile(r"^\s*\[[A-Za-z][A-Za-z _\-]{2,40}\]\s*$")
_NARRATION_START = re.compile(
    r"^\s*(?:\[Thinking\]|Now (?:let me|I'll|I have)|Let me|Let's|I'll |I will |"
    r"Looking at the recent|Good —|The user wants me to)",
    re.IGNORECASE,
)
_MAX_SUMMARY_ATTEMPTS = 3


def _validate_summary(raw: str | None) -> tuple[bool, str, str]:
    """Validate raw summarizer output.

    Returns (ok, reason, cleaned). When ok is False the output is a
    role-play continuation (or otherwise unusable) and must not be
    persisted. When ok is True, cleaned is the output with leading
    scaffold-label echoes removed.
    """
    text = (raw or "").strip()
    if not text:
        return False, "empty output", ""
    if _TOOL_MARKUP.search(text):
        return False, "tool-call markup", text
    items = sum(1 for line in text.splitlines() if _LIST_ITEM.match(line))
    if items < 2 and (len(text) < 400 or text.rstrip().endswith(":")):
        return False, "no list structure", text
    if _NARRATION_START.match(text) and items < 5:
        return False, "agent narration", text
    lines = text.splitlines()
    while lines and _SCAFFOLD_LABEL.match(lines[0]):
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    cleaned = "\n".join(lines).strip()
    return True, "ok", cleaned or text


def _get_summarize_prompt() -> str:
    """Read from config.toml [chat] section, fall back to default."""
    try:
        from cptr.utils.config import load_config

        config = load_config()
        return config.get("chat", {}).get("compact_summary_prompt", DEFAULT_SUMMARIZE_PROMPT)
    except Exception:
        return DEFAULT_SUMMARIZE_PROMPT


async def summarize_messages(
    messages: list[dict],
    existing_summary: str | None,
    connection: dict,
    model: str,
) -> str:
    """Summarize messages into a compact rolling summary.

    If existing_summary is provided, it's included so the new summary
    incorporates everything before it.
    """
    parts: list[str] = []
    if existing_summary:
        parts.append(f"[Previous summary]\n{existing_summary}\n")
    parts.append("[Recent messages to summarize]")
    for m in messages:
        role = m.get("role", "unknown")
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
        # Truncate very long messages (e.g. tool outputs)
        if len(content) > 2000:
            content = content[:1000] + "\n...(truncated)...\n" + content[-500:]
        parts.append(f"{role}: {content}")

    text = "\n".join(parts)
    # Cap input to summarization call
    if len(text) > 30_000:
        text = text[:15_000] + "\n...\n" + text[-10_000:]

    try:
        from cptr.utils.ai import _STREAM_TIMEOUT, generate_text
        from cptr.utils.utility_models import configured_utility_model

        # Retry with the identical prompt (resampling) on rejected output:
        # a "your output was invalid" nudge would re-prime the agent pattern.
        system = _get_summarize_prompt()
        model_id = await configured_utility_model("summary_generation")
        for attempt in range(1, _MAX_SUMMARY_ATTEMPTS + 1):
            # Summary generation must not spend tokens on reasoning. Models
            # with a thinking-by-default chat template would otherwise burn
            # the 2000-token budget on reasoning (same treatment as title
            # generation).
            result = await generate_text(
                None,
                model_id=model_id,
                active_connection=connection,
                active_model=model,
                messages=[{"role": "user", "content": text}],
                system=system,
                max_tokens=2000,
                request_params={"chat_template_kwargs": {"enable_thinking": False}},
                timeout=_STREAM_TIMEOUT,
            )
            ok, reason, cleaned = _validate_summary(result)
            if ok:
                logger.info(
                    "[summarize] LLM summary: %d chars (attempt %d/%d)",
                    len(cleaned),
                    attempt,
                    _MAX_SUMMARY_ATTEMPTS,
                )
                return cleaned
            logger.warning(
                "[summarize] attempt %d/%d rejected (%s): %r",
                attempt,
                _MAX_SUMMARY_ATTEMPTS,
                reason,
                (result or "")[:120],
            )
        raise RuntimeError(f"no valid summary after {_MAX_SUMMARY_ATTEMPTS} attempts")
    except Exception:
        logger.warning("[summarize] LLM call failed, using naive fallback", exc_info=True)
        return _naive_summary(messages, existing_summary)


def _naive_summary(messages: list[dict], existing: str | None) -> str:
    """Fallback when LLM summarization fails."""
    parts: list[str] = []
    if existing:
        # Don't propagate a corrupted previous checkpoint (tool-call XML or
        # agent narration) through the fallback: drop it rather than carry
        # the contamination forward.
        ok, reason, cleaned = _validate_summary(existing)
        if ok:
            parts.append(cleaned)
        else:
            logger.warning(
                "[summarize] existing summary failed validation (%s); not carrying it into the fallback",
                reason,
            )
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if isinstance(content, str) and role in ("user", "assistant"):
            parts.append(f"- {role}: {content[:200]}")
    return "\n".join(parts)[:2000]
