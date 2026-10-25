"""Summarize older messages for context compaction.

Uses the same LLM as the active chat to generate a rolling summary.
Falls back to naive truncation if the LLM call fails.
"""

from __future__ import annotations

import logging

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

        # Summary generation must not spend tokens on reasoning. Models with
        # a thinking-by-default chat template would otherwise burn the
        # 2000-token budget on reasoning (same treatment as title generation).
        result = await generate_text(
            None,
            model_id=await configured_utility_model("summary_generation"),
            active_connection=connection,
            active_model=model,
            messages=[{"role": "user", "content": text}],
            system=_get_summarize_prompt(),
            max_tokens=2000,
            request_params={"chat_template_kwargs": {"enable_thinking": False}},
            timeout=_STREAM_TIMEOUT,
        )
        if not result or not result.strip():
            raise RuntimeError("summary generation returned an empty result")
        logger.info("[summarize] LLM summary: %d chars", len(result))
        return result
    except Exception:
        logger.warning("[summarize] LLM call failed, using naive fallback", exc_info=True)
        return _naive_summary(messages, existing_summary)


def _naive_summary(messages: list[dict], existing: str | None) -> str:
    """Fallback when LLM summarization fails."""
    parts: list[str] = []
    if existing:
        parts.append(existing)
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if isinstance(content, str) and role in ("user", "assistant"):
            parts.append(f"- {role}: {content[:200]}")
    return "\n".join(parts)[:2000]
