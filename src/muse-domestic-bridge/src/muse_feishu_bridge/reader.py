# SPDX-License-Identifier: Apache-2.0
"""Read-only Feishu context for the Muse main chat.

The bridge is otherwise a relay: Feishu text in, Muse replies out. This module
lets the owner pull Feishu-side context into a Muse turn on demand, so a question
like "who messaged me in Feishu" can be answered from the real timeline instead
of from Muse's imagination.

Design constraints, because this reaches into the owner's actual account:

* Every method is read-only. Nothing here sends, edits, recalls or reacts.
* Output is bounded: a hard cap per message, per chat and per call, so one busy
  group cannot flood the Muse turn (and the owner's quota).
* Message text is truncated per message rather than silently dropped, and every
  truncation is marked, so Muse can tell a short message from a clipped one.
* Failures degrade to a sentence rather than raising into the relay: a missing
  permission must not stop ordinary chat from working.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterable
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import (ListChatRequest, ListMessageRequest,
                                 GetChatRequest)

LOGGER = logging.getLogger(__name__)

MAX_MESSAGES = 50          # messages pulled per /read
MAX_TEXT_CHARS = 500       # per message, before an ellipsis marker
MAX_TOTAL_CHARS = 12000    # whole context handed to Muse
MESSAGE_TYPES = {"text", "post", "interactive", "image", "file", "audio", "media",
                 "sticker", "share_chat", "share_user", "merge_forward", "system"}

# Muse embeds client-only widget references in its replies; they are noise here.
WIDGET_REFERENCE = re.compile(r"\[\[hatch_[a-z_]*widget[^\]]*\]\]", re.IGNORECASE)


class FeishuReadError(RuntimeError):
    """A log-safe read failure; the message names the API, never the payload."""


def _ms_to_local(ms: Any) -> str:
    try:
        seconds = int(ms) / 1000
    except (TypeError, ValueError):
        return "?"
    return time.strftime("%m-%d %H:%M", time.localtime(seconds))


def _card_text(parsed: Any) -> str:
    """Pull readable text out of a card body.

    The bridge's own replies are cards, so without this every Muse message in the
    transcript would collapse to "[卡片消息]" and the context would be useless.
    """
    if not isinstance(parsed, dict):
        return "[卡片]"
    out: list[str] = []
    title = parsed.get("title")
    if isinstance(title, str) and title.strip():
        out.append(title.strip())
    elif isinstance(title, dict) and isinstance(title.get("content"), str):
        out.append(title["content"].strip())

    def collect(node: Any, depth: int = 0) -> None:
        if depth > 6 or node is None:
            return
        if isinstance(node, str):
            return
        if isinstance(node, dict):
            tag = node.get("tag")
            if tag in ("text", "plain_text", "lark_md") and isinstance(node.get("text"), str):
                out.append(node["text"])
                return
            if isinstance(node.get("content"), str) and tag == "markdown":
                out.append(node["content"])
                return
            for value in node.values():
                collect(value, depth + 1)
            return
        if isinstance(node, list):
            for item in node:
                collect(item, depth + 1)

    collect(parsed.get("elements"))
    text = "\n".join(part for part in (p.strip() for p in out) if part)
    return text if text else "[卡片]"


def _body_content(body: Any) -> Any:
    """lark returns a MessageBody object here, not the plain dict its docs imply."""
    if body is None:
        return ""
    if isinstance(body, dict):
        return body.get("content", "")
    return getattr(body, "content", "") or ""


def _extract_text(msg_type: str, body: Any) -> str:
    """Best-effort plain text for one message body; never raises."""
    content = _body_content(body)
    if not isinstance(content, str):
        return ""
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return ""
    if msg_type == "text":
        return parsed.get("text", "") if isinstance(parsed, dict) else ""
    if msg_type == "post":
        # A post carries a locale map of paragraph blocks.
        lines: list[str] = []
        for locale in ("zh_cn", "en_us", "ja_jp"):
            block = parsed.get(locale) if isinstance(parsed, dict) else None
            if isinstance(block, dict):
                if isinstance(block.get("title"), str) and block["title"]:
                    lines.append(block["title"])
                for para in block.get("content") or []:
                    if not isinstance(para, list):
                        continue
                    row = "".join(
                        str(item.get("text", "")) for item in para
                        if isinstance(item, dict) and item.get("tag") == "text")
                    if row:
                        lines.append(row)
                if lines:
                    break
        return "\n".join(lines)
    if msg_type == "interactive":
        return _card_text(parsed)
    if msg_type == "image":
        return "[图片]"
    if msg_type == "file":
        name = parsed.get("file_name") if isinstance(parsed, dict) else None
        return "[文件 " + str(name or "?") + "]"
    if msg_type in ("audio", "media"):
        return "[语音/视频]"
    if msg_type == "merge_forward":
        return "[合并转发]"
    if msg_type in ("share_chat", "share_user"):
        return "[名片]"
    if msg_type == "sticker":
        return "[表情]"
    return "[" + msg_type + "]"


class FeishuReader:
    """Read-only views over the owner's Feishu account."""

    def __init__(self, app_id: str, app_secret: str, *, owner_chat: str = ""):
        self._client = (lark.Client.builder()
                        .app_id(app_id).app_secret(app_secret)
                        .log_level(lark.LogLevel.CRITICAL).build())
        self._owner_chat = owner_chat

    def _check(self, response: Any, api: str) -> Any:
        code = getattr(response, "code", None)
        if code == 0:
            return response
        msg = str(getattr(response, "msg", "") or "")
        if "Access denied" in msg or code in (99991672, 99991679):
            raise FeishuReadError("permission_missing:" + api)
        raise FeishuReadError("api_failed:" + api + ":" + str(code))

    def recent_messages(self, limit: int = 20, *, chat_id: str = "",
                        include_bot: bool = True) -> list[dict]:
        """Latest messages of one chat, newest first, oldest-first on return.

        ``include_bot`` keeps the assistant side of the conversation, which makes
        a summary coherent. Callers that only want what people said to the owner
        turn it off.
        """
        target = chat_id or self._owner_chat
        if not target:
            raise FeishuReadError("no_chat")
        request = (ListMessageRequest.builder()
                   .container_id_type("chat").container_id(target)
                   .page_size(max(1, min(limit, MAX_MESSAGES)))
                   .sort_type("ByCreateTimeDesc").build())
        response = self._check(self._client.im.v1.message.list(request), "message.list")
        items = getattr(getattr(response, "data", None), "items", None) or []
        rows: list[dict] = []
        for item in items:
            sender = getattr(item, "sender", None)
            kind = getattr(sender, "sender_type", "") or ""
            if kind != "user" and not include_bot:
                continue
            msg_type = getattr(item, "msg_type", "") or ""
            text = _extract_text(msg_type, getattr(item, "body", None))
            if not text:
                continue
            text = WIDGET_REFERENCE.sub("", text).strip()
            if not text:
                continue
            rows.append({"sender": "我" if kind == "user" else "机器人",
                         "at": _ms_to_local(getattr(item, "create_time", None)),
                         "type": msg_type, "text": text})
        rows.reverse()
        return rows

    def chats(self, limit: int = 20) -> list[dict]:
        """The bot's chats. A personal bridge usually has exactly one."""
        request = (ListChatRequest.builder()
                   .page_size(max(1, min(limit, 50))).build())
        response = self._check(self._client.im.v1.chat.list(request), "chat.list")
        items = getattr(getattr(response, "data", None), "items", None) or []
        return [{"chat_id": getattr(c, "chat_id", ""), "name": getattr(c, "name", "") or "(私聊)",
                 "mode": getattr(c, "chat_mode", "")} for c in items]

    def chat_info(self, chat_id: str) -> dict:
        request = GetChatRequest.builder().chat_id(chat_id).build()
        response = self._check(self._client.im.v1.chat.get(request), "chat.get")
        data = getattr(response, "data", None)
        return {"chat_id": chat_id, "name": getattr(data, "name", "") or "(私聊)",
                "description": getattr(data, "description", "") or ""}


def format_messages(rows: Iterable[dict], *, header: str = "") -> str:
    """Render read rows as a compact transcript for the Muse turn."""
    lines: list[str] = []
    if header:
        lines.append(header)
        lines.append("")
    total = 0
    clipped = 0
    for row in rows:
        text = str(row.get("text", "")).replace("\n", " ⏎ ")
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS] + "…（此处截断）"
            clipped += 1
        line = "[" + str(row.get("at", "?")) + "] " + str(row.get("sender", "?")) + "：" + text
        if total + len(line) > MAX_TOTAL_CHARS:
            lines.append("…（后续消息因长度上限省略）")
            break
        total += len(line)
        lines.append(line)
    if clipped:
        lines.append("")
        lines.append("（有 " + str(clipped) + " 条消息过长已截断）")
    return "\n".join(lines)
