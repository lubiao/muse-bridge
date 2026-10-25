# SPDX-License-Identifier: Apache-2.0
"""Render Muse's Markdown as a Feishu interactive card.

Muse writes its answers in Markdown: ``## headings``, ``**bold**``, ``- bullets``
and bare URLs. A Feishu ``text`` message shows that syntax literally, which is
what made the daily report look like source code. Feishu cards render a subset of
Markdown natively, so the text is adapted where the two dialects differ:

* ``## heading`` has no card equivalent (Feishu supports no headings), so it is
  promoted to a bold line and the markers are removed.
* ``| a | b |`` tables are not supported at all; they are reflowed as list lines.
* Bare URLs become ``[host/path](url)`` links so the card wraps them properly.
* Everything else (bold, italics, strikethrough, lists, links, code) is already
  understood by the card renderer and is passed through untouched.

Anything that is not recognisably Markdown is left as plain text and sent as a
normal message, so short conversational replies keep their simple appearance.
"""

from __future__ import annotations

import json
import re

# A fenced block or an obvious structural marker. Bare URLs alone are excluded:
# a one-line reply containing a link should stay a plain text message.
_MARKDOWN_SIGNS = re.compile(
    r"(^```)|(^#{1,6}\s)|(^\s*[-*+]\s+\S)|(^\s*\d+[.)]\s+\S)|(\*\*[^*\n]+\*\*)|"
    r"(\[[^\]\n]+\]\([^)\n]+\))|(^\s*>\s)|(^\|.*\|\s*$)|(^\s*---+$)",
    re.MULTILINE)

_HEADING = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_TABLE_RULE = re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(\|[ \t]*:?-{2,}:?[ \t]*)+\|?[ \t]*$")
_TABLE_ROW = re.compile(r"^[ \t]*\|(.+)\|[ \t]*$")
_BARE_URL = re.compile(r"(?<!\]\()(?<!href=['\"])(https?://[^\s<>()\[\]\"'，。；！？、]+)")
_FENCE = re.compile(r"^```", re.MULTILINE)


def looks_like_markdown(text: str) -> bool:
    """True when the text carries structure a text message would render badly."""
    if not isinstance(text, str) or not text.strip():
        return False
    return _MARKDOWN_SIGNS.search(text) is not None


def _promote_headings(text: str) -> str:
    def replace(match: re.Match) -> str:
        title = match.group(2).strip()
        # Headings already bold must not become "** **title** **".
        if title.startswith("**") and title.endswith("**"):
            return title
        return "**" + title + "**"

    return _HEADING.sub(replace, text)


def _reflow_tables(text: str) -> str:
    """Turn pipe tables into "- cell: cell" lines; cards render no tables."""
    lines = text.split("\n")
    result: list[str] = []
    index = 0
    while index < len(lines):
        row = _TABLE_ROW.match(lines[index])
        rule_next = index + 1 < len(lines) and _TABLE_RULE.match(lines[index + 1]) is not None
        if row and rule_next:
            index += 2  # skip header row and its delimiter row
            while index < len(lines):
                body = _TABLE_ROW.match(lines[index])
                if not body:
                    break
                cells = [cell.strip() for cell in body.group(1).split("|")]
                cells = [cell for cell in cells if cell]
                if cells:
                    first, rest = cells[0], cells[1:]
                    result.append("- " + first + (("：" + " · ".join(rest)) if rest else ""))
                index += 1
            continue
        result.append(lines[index])
        index += 1
    return "\n".join(result)


def _linkify(text: str) -> str:
    """Bare URLs get a label so the card wraps them instead of overflowing."""
    def replace(match: re.Match) -> str:
        url = match.group(1)
        label = re.sub(r"^https?://(www\.)?", "", url)
        if len(label) > 48:
            label = label[:45] + "…"
        return "[" + label + "](" + url + ")"

    return _BARE_URL.sub(replace, text)


def _inside_fences(text: str) -> list[tuple[int, int]]:
    """Character ranges covered by fenced code blocks."""
    spans: list[tuple[int, int]] = []
    position = 0
    open_at: int | None = None
    for line in text.split("\n"):
        if _FENCE.match(line.lstrip()):
            if open_at is None:
                open_at = position
            else:
                spans.append((open_at, position + len(line)))
                open_at = None
        position += len(line) + 1
    if open_at is not None:
        spans.append((open_at, len(text)))
    return spans


def _apply_outside_fences(text: str, transform) -> str:
    spans = _inside_fences(text)
    if not spans:
        return transform(text)
    pieces: list[str] = []
    cursor = 0
    for start, end in spans:
        pieces.append(transform(text[cursor:start]))
        pieces.append(text[start:end])
        cursor = end
    pieces.append(transform(text[cursor:]))
    return "".join(pieces)


def to_card_markdown(text: str) -> str:
    """Adapt Muse's Markdown to the dialect Feishu cards render."""
    if not isinstance(text, str):
        return text
    body = _promote_headings(text)
    body = _apply_outside_fences(body, _reflow_tables)
    body = _apply_outside_fences(body, _linkify)
    return body


def card_content(text: str) -> str:
    """The JSON ``content`` value for an interactive card carrying one text block."""
    return json.dumps({
        "config": {"wide_screen_mode": True},
        "elements": [{"tag": "markdown", "content": to_card_markdown(text)}],
    }, ensure_ascii=False)
