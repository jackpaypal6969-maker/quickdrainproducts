"""A deliberately tiny markdown renderer that escapes first and formats second.

Supports: # / ## / ### headings, paragraphs, - and 1. lists, > quotes, ---,
**bold**, *italic*, `code`, and [text](https://...) links. Nothing else, and no
raw HTML ever passes through, so the output is safe to mark as Markup.
"""
from __future__ import annotations

import re

from markupsafe import Markup, escape

_LINK_RE = re.compile(r"\[([^\]]+)\]\(((?:https?://|mailto:|/)[^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])")
_CODE_RE = re.compile(r"`([^`]+)`")
_UL_ITEM_RE = re.compile(r"^\s*[-*]\s+")
_OL_ITEM_RE = re.compile(r"^\s*\d+[.)]\s+")


def _inline(text: str) -> str:
    text = _CODE_RE.sub(r"<code>\1</code>", text)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_RE.sub(r"<em>\1</em>", text)

    def link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        external = url.startswith("http")
        rel = ' rel="noopener"' if external else ""
        return f'<a href="{url}"{rel}>{label}</a>'

    return _LINK_RE.sub(link, text)


def render(source: str) -> Markup:
    if not source:
        return Markup("")
    text = str(escape(source.replace("\r\n", "\n")))
    blocks = re.split(r"\n\s*\n", text.strip())
    out: list[str] = []
    for block in blocks:
        lines = block.split("\n")
        first = lines[0]
        if re.match(r"^#{1,3}\s", first) and len(lines) == 1:
            level = len(first.split(" ", 1)[0])
            out.append(f"<h{level + 1}>{_inline(first.split(' ', 1)[1].strip())}</h{level + 1}>")
        elif all(_UL_ITEM_RE.match(ln) for ln in lines):
            items = "".join("<li>" + _inline(_UL_ITEM_RE.sub("", ln)) + "</li>" for ln in lines)
            out.append(f"<ul>{items}</ul>")
        elif all(_OL_ITEM_RE.match(ln) for ln in lines):
            items = "".join("<li>" + _inline(_OL_ITEM_RE.sub("", ln)) + "</li>" for ln in lines)
            out.append(f"<ol>{items}</ol>")
        elif all(ln.startswith("&gt;") for ln in lines):
            body = " ".join(ln[4:].strip() for ln in lines)
            out.append(f"<blockquote><p>{_inline(body)}</p></blockquote>")
        elif block.strip() in {"---", "***"}:
            out.append("<hr>")
        else:
            out.append(f"<p>{_inline(' '.join(ln.strip() for ln in lines))}</p>")
    return Markup("\n".join(out))
