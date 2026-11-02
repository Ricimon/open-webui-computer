"""Compaction boundary and checkpoint helpers for chat history."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import Request

from cptr.models import ChatMessage
from cptr.utils.prompt_templates import load_system_prompt
from cptr.utils.skills import format_skill_content, load_skill
from cptr.utils.summarize import summarize_messages

logger = logging.getLogger(__name__)


def _find_safe_split(messages: list[dict], target_keep: int) -> int:
    """Find a safe split index that doesn't break tool call pairs.

    Returns the index where keep_zone starts. Ensures:
    - Never splits between an assistant tool_call and its tool result
    - keep_zone doesn't start with a tool result message
    - At least 2 messages are kept

    Step rows of one assistant task may be split: each row co-locates a
    generation's tool calls with their results, so any boundary that doesn't
    start on a tool result is pair-safe. Allowing mid-series boundaries is
    what lets mid-task compaction trim the running task's own older steps
    instead of only pre-task history (which made compaction a no-op once the
    series dominated the context).
    """
    n = len(messages)
    split = min(max(2, n - target_keep), max(0, n - 2))

    # Don't start keep_zone with a tool result; keep its assistant call too.
    while split > 0 and messages[split].get("role") == "tool":
        split -= 1

    return split


def _advance_split_past_checkpoint(
    messages: list[dict], split: int, checkpointed_ids: set[str]
) -> int:
    """Move a keep-zone boundary past a row that already carries a checkpoint.

    A step row's dicts (its assistant dict plus one tool dict per call) all
    share the row's id, so a boundary inside such a group — or the tool-pair
    back-walk landing on it — anchors the checkpoint on that same row. If the
    row already has a ``chat_summary``, writing the new summary there would
    clobber the earlier checkpoint: the visible marker for it disappears and
    a reload truncates at the row without the new summary covering its
    content. Walk the split forward past the whole id group instead, so the
    row's content joins the drop zone (the rolled-up summary covers it) and
    the new checkpoint lands on a later step. If advancing would leave fewer
    than 2 messages to keep, return the original split unchanged.
    """
    n = len(messages)
    for _ in range(4):  # at most one prior checkpoint sits in scope; allow slack
        anchor = next((m.get("id") for m in messages[split:] if m.get("id")), None)
        if anchor is None or anchor not in checkpointed_ids:
            return split
        new_split = split
        while new_split < n and messages[new_split].get("id") == anchor:
            new_split += 1
        if new_split <= split or new_split > n - 2:
            return split
        while new_split > 0 and messages[new_split].get("role") == "tool":
            new_split -= 1
        split = new_split
    return split


def _compaction_boundary_viable(
    messages: list[dict], split: int, checkpointed_ids: set[str]
) -> bool:
    """Whether a compaction pass at ``split`` can actually proceed.

    False when there is nothing to drop (boundary at the start) or when the
    row that would receive the new summary already carries one — writing it
    there would clobber the earlier checkpoint and truncate its content on
    reload. ``_advance_split_past_checkpoint`` normally avoids the second case
    but gives up in the degenerate tail, leaving such a boundary behind.
    """
    if split <= 0:
        return False
    anchor = next((m.get("id") for m in messages[split:] if m.get("id")), None)
    if anchor is None:
        return True  # no row id to anchor on; _summary_checkpoint_message_id falls back
    return anchor not in checkpointed_ids


# Safety cap on how many times one task may compact-and-retry after a
# context-exceeded error. Each pass keeps ~40% of the messages, so a healthy
# overflow converges in a few passes; the cap only binds when compaction
# cannot fix the overflow (e.g. a max_tokens mismatch whose message merely
# mentions tokens), or guards against a misbehaving endpoint.
_MAX_OVERFLOW_COMPACTIONS = 8


# Case-insensitive phrases OpenAI-compatible backends include in the error
# response when a request exceeds the model's context window. llama.cpp master
# returns HTTP 400 with error.type "exceed_context_size_error" and messages
# like "input (N tokens) is larger than the max context size (M tokens).
# skipping", "request (N tokens) exceeds the available context size (M tokens),
# try increasing it", or "Context size has been exceeded."; OpenAI says
# "maximum context length", Anthropic "prompt is too long", Ollama "the input
# is too long for its N context window".
_CONTEXT_EXCEEDED_MARKERS = (
    "maximum context length",
    "prompt is too long",
    "input is too long",
    "max context size",
    "exceeds the available context size",
    "context size has been exceeded",
    "exceed_context_size_error",
    "context length exceeded",
    "too many tokens",
)


async def _is_context_exceeded_error(exc: BaseException) -> bool:
    """Whether exc is a provider 'prompt exceeds the context window' error.

    Matches known overflow phrases in the exception text and, for HTTP errors,
    in the response body — reading it lazily when the stream path raised
    without pre-reading it.
    """
    parts = [str(exc)]
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            text = response.text or ""
        except Exception:
            try:
                await response.aread()
                text = response.text or ""
            except Exception:
                text = ""
        if text:
            parts.append(text)
    blob = " ".join(parts).lower()
    return any(marker in blob for marker in _CONTEXT_EXCEEDED_MARKERS)


def _summary_checkpoint_message_id(keep_zone: list[dict], fallback: str) -> str:
    """Anchor the summary on the first keep-zone message (the split boundary),
    whatever its role. _find_safe_split already aligned the boundary, so the
    keep zone survives reloads intact."""
    for message in keep_zone:
        if message.get("id"):
            return message["id"]
    return fallback


# Successful-pass result: (new_messages, new_system, new_loaded_summary).
# The caller resets its usage tracking (last_usage=None, new_messages_since=0).
CompactionResult = tuple[list[dict], str, str | None] | None


async def compact_chat_history(
    *,
    messages: list[dict],
    keep_percent: int,
    chat_id: str,
    message_id: str,
    summary_message_id: str,
    loaded_summary: str | None,
    connection: dict,
    model: str,
    request: Request,
    workspace: str,
    user_id: str | None,
    attached_skill_ids: list[str],
    chat_params: dict,
    regeneration_prompt: str | None,
    reason: str,
    emit: Callable[..., Awaitable[None]],
    memory_recall_inputs: Callable[[list[dict], str | None], tuple[str, list[str]]],
    apply_voice_mode_system_prompt: Callable[[str, dict], Awaitable[str]],
    ensure_current_prompt: Callable[..., Awaitable[list[dict]]],
) -> CompactionResult:
    """Run one standard compaction pass over a task's message list.

    The list starts at the last summary checkpoint. Keeps the newest
    ``keep_percent``% anchored past any existing checkpoint, summarizes what
    is dropped, writes the checkpoint, and rebuilds the system prompt and
    history for the next request. Returns None without side effects when
    nothing can be dropped or the boundary would land on a row that already
    carries a summary; otherwise returns (new_messages, new_system,
    new_loaded_summary).

    The prompt-assembly steps that live in cptr.utils.chat_task (memory-recall
    inputs, voice-mode suffix, current-prompt restoration) are passed in as
    callables so this module owns only the boundary/checkpoint/summarize flow.
    """
    target_keep = max(2, len(messages) * keep_percent // 100)
    split_idx = _find_safe_split(messages, target_keep)
    # The boundary may land on (or inside the dict group of) a row that
    # already carries a compaction checkpoint — e.g. the checkpoint written
    # by an earlier compaction in this same task, or a pre-task checkpoint at
    # the front of the list. Advance past it so the new checkpoint lands on
    # a later step and the old one is preserved.
    all_rows = await ChatMessage.get_all_by_chat(chat_id)
    checkpointed_ids = {r.id for r in all_rows if r.chat_summary}
    split_idx = _advance_split_past_checkpoint(messages, split_idx, checkpointed_ids)

    if not _compaction_boundary_viable(messages, split_idx, checkpointed_ids):
        # Either nothing is older than the keep zone, or the boundary would
        # clobber an existing summary: this pass cannot shrink the context
        # without losing a checkpoint.
        logger.warning(
            "[task %s] compaction (%s) cannot proceed: nothing to drop or boundary would clobber an existing summary",
            message_id[:8],
            reason,
        )
        return None

    drop_zone = messages[:split_idx]
    keep_zone = messages[split_idx:]

    summary = await summarize_messages(drop_zone, loaded_summary, connection, model)

    checkpoint_message_id = _summary_checkpoint_message_id(keep_zone, summary_message_id)
    await ChatMessage.update(checkpoint_message_id, chat_summary=summary)
    # Tell connected clients where the checkpoint landed so the compaction
    # indicator can appear mid-task without a reload.
    await emit(type="message_compacted", row_id=checkpoint_message_id, chat_summary=summary)

    # Append summary to system prompt (works for all providers)
    memory_message, memory_files = memory_recall_inputs(keep_zone, regeneration_prompt)
    system = await load_system_prompt(
        request,
        workspace,
        model,
        user_id=user_id,
        current_message=memory_message,
        recent_messages=keep_zone,
        mentioned_files=memory_files,
    )
    system += f"\n\n[CONVERSATION SUMMARY]\n{summary}"
    # Re-inject attached skills after compaction (protect from pruning)
    if attached_skill_ids:
        skill_blocks = []
        for sid in attached_skill_ids:
            skill = load_skill(workspace, sid)
            if skill:
                skill_blocks.append(format_skill_content(skill))
        if skill_blocks:
            system += "\n\n" + "\n\n".join(skill_blocks)
    system = await apply_voice_mode_system_prompt(system, chat_params)
    # Keep the prompt that started this task in the user content (the
    # checkpoint above is already written, so its position is unchanged; the
    # prompt also remains captured in the summary).
    current_prompt_kept = summary_message_id != message_id and not any(
        m.get("id") == summary_message_id for m in keep_zone
    )
    new_messages = await ensure_current_prompt(
        keep_zone, chat_id, summary_message_id, message_id, source=drop_zone
    )

    logger.info(
        "[task %s] compacted (%s): checkpoint=%s dropped %d msgs, kept %d, summary=%d chars, current_prompt_kept=%s",
        message_id[:8],
        reason,
        checkpoint_message_id[:8],
        len(drop_zone),
        len(keep_zone),
        len(summary),
        current_prompt_kept,
    )
    return new_messages, system, summary
