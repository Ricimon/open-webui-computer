"""Chat file export: regenerate .cptr/chats/{id}.json from DB.

The JSON file on disk is the portable backup. DB is the primary store.
This function rebuilds the file from DB state.

Step series (consecutive assistant rows sharing one meta.task_root) are
merged into a single assistant entry so the backup matches what the user
sees: one message per turn. Legacy rows carry no task_root and export as-is.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import Request
from cptr.env import DATA_DIR
from cptr.models import Chat, ChatMessage
from cptr.utils.runtime import Runtime

logger = logging.getLogger(__name__)


def chat_directory(workspace: str | None) -> Path:
    """Return the portable chat directory for a workspace or Home."""
    return Path(workspace) / ".cptr" / "chats" if workspace else DATA_DIR / "chats"


def _series_root(m: ChatMessage) -> str:
    """The series root row id for an assistant row (itself for legacy rows)."""
    meta = m.meta if isinstance(m.meta, dict) else {}
    root = meta.get("task_root")
    return root if isinstance(root, str) and root else m.id


def _find_step_series(messages: list[ChatMessage]) -> dict[str, list[ChatMessage]]:
    """Group contiguous step rows into series: root row id -> ordered rows.

    A row joins its series when it is an assistant step row (meta.task_root
    set) whose parent is the previous row in creation order within the same
    series. Only groups of two or more rows are returned; single rows and
    legacy monolithic rows stay standalone.
    """
    series: dict[str, list[ChatMessage]] = {}
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        if m.role != "assistant" or _series_root(m) != m.id:
            i += 1
            continue
        root_id = m.id
        has_task_root = isinstance((m.meta or {}).get("task_root"), str)
        chain = [m]
        j = i + 1
        if has_task_root:
            while (
                j < n
                and messages[j].role == "assistant"
                and messages[j].parent_id == chain[-1].id
                and _series_root(messages[j]) == root_id
            ):
                chain.append(messages[j])
                j += 1
        if len(chain) > 1:
            series[root_id] = chain
        i = j if len(chain) > 1 else i + 1
    return series


def _merge_chain(root: ChatMessage, chain: list[ChatMessage]) -> dict:
    """Build one export entry for a merged step series."""
    parts = [(r.content or "").strip() for r in chain]
    content = "\n\n".join(p for p in parts if p)
    output: list = []
    for r in chain:
        if r.output:
            output.extend(r.output)
    usage = None
    chat_summary = None
    for r in reversed(chain):
        if usage is None and isinstance(r.usage, dict):
            usage = r.usage
        if chat_summary is None and r.chat_summary:
            chat_summary = r.chat_summary
    entry: dict = {
        "id": root.id,
        "parentId": root.parent_id,
        "childrenIds": [],
        "role": "assistant",
        "content": content,
        "timestamp": root.created_at,
        "model": root.model,
        "done": chain[-1].done,
        "output": output,
    }
    if usage is not None:
        entry["usage"] = usage
    if chat_summary is not None:
        entry["chat_summary"] = chat_summary
    return entry


async def export_chat_to_file(request: Request, chat_id: str) -> None:
    """Regenerate .cptr/chats/{id}.json from the DB."""
    chat = await Chat.get_by_id(chat_id)
    if not chat:
        return

    workspace = (chat.meta or {}).get("workspace")

    messages = await ChatMessage.get_all_by_chat(chat_id)

    # Merge step series (same visible turn split across step rows).
    step_series = _find_step_series(messages)
    skip_ids: set[str] = set()
    follow_root: dict[str, str] = {}
    for root_id, chain in step_series.items():
        for row in chain[1:]:
            skip_ids.add(row.id)
            follow_root[row.id] = root_id

    # Build history tree
    msg_map: dict[str, dict] = {}
    for m in messages:
        if m.id in skip_ids:
            continue
        if m.role == "assistant" and m.id in step_series:
            entry = _merge_chain(m, step_series[m.id])
        else:
            entry: dict = {
                "id": m.id,
                "parentId": m.parent_id,
                "childrenIds": [],
                "role": m.role,
                "content": m.content or "",
                "timestamp": m.created_at,
            }
            if m.role == "user":
                entry["models"] = [m.model] if m.model else []
            else:
                entry["model"] = m.model
                entry["done"] = m.done
                entry["output"] = m.output or []
                if m.usage:
                    entry["usage"] = m.usage
            if m.chat_summary:
                entry["chat_summary"] = m.chat_summary

        msg_map[m.id] = entry

    # Wire up childrenIds; children of a merged follow row re-home to the root.
    for m in messages:
        if m.parent_id and m.parent_id in msg_map:
            msg_map[m.parent_id]["childrenIds"].append(m.id)
        elif m.parent_id in follow_root:
            target = follow_root[m.parent_id]
            if target in msg_map:
                msg_map[target]["childrenIds"].append(m.id)

    current_id = chat.current_message_id or ""
    if current_id in follow_root:
        current_id = follow_root[current_id]

    chat_data = {
        "id": chat.id,
        "title": chat.title,
        "summary": chat.summary,
        "created_at": chat.created_at,
        "updated_at": chat.updated_at,
        "history": {
            "currentId": current_id,
            "messages": msg_map,
        },
    }

    try:
        content = json.dumps(chat_data, indent=2, ensure_ascii=False)
        target = chat_directory(workspace) / f"{chat_id}.json"
        if workspace:
            await Runtime.write_file(request, str(target), content)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    except Exception:
        logger.exception(f"Failed to export chat {chat_id}")
