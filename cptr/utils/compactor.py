"""Compaction boundary and checkpoint helpers for chat history."""

from __future__ import annotations


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
