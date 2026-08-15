"""Small fail-closed Markdown subset for Feishu document projection."""
from __future__ import annotations

import html
import re


_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_UNORDERED = re.compile(r"^\s*[-*+]\s+(.+?)\s*$")
_ORDERED = re.compile(r"^\s*\d+[.)]\s+(.+?)\s*$")
_TABLE_SEPARATOR = re.compile(r"^:?-{3,}:?$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def markdown_to_feishu_xml(value: str) -> str:
    """Render headings, lists and pipe tables without accepting raw XML."""

    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue

        table = _table_at(lines, index)
        if table is not None:
            xml, index = table
            blocks.append(xml)
            continue

        heading = _HEADING.match(line)
        if heading is not None:
            level = min(8, len(heading.group(1)) + 2)
            blocks.append(
                f"<h{level}>{_inline(heading.group(2))}</h{level}>"
            )
            index += 1
            continue

        unordered = _UNORDERED.match(line)
        if unordered is not None:
            items = []
            while index < len(lines):
                matched = _UNORDERED.match(lines[index])
                if matched is None:
                    break
                items.append(f"<li>{_inline(matched.group(1))}</li>")
                index += 1
            blocks.append("<ul>" + "".join(items) + "</ul>")
            continue

        ordered = _ORDERED.match(line)
        if ordered is not None:
            items = []
            while index < len(lines):
                matched = _ORDERED.match(lines[index])
                if matched is None:
                    break
                items.append(
                    f'<li seq="auto">{_inline(matched.group(1))}</li>'
                )
                index += 1
            blocks.append("<ol>" + "".join(items) + "</ol>")
            continue

        paragraph = [line.strip()]
        index += 1
        while index < len(lines) and lines[index].strip():
            if _starts_block(lines, index):
                break
            paragraph.append(lines[index].strip())
            index += 1
        blocks.append("<p>" + "<br/>".join(_inline(item) for item in paragraph) + "</p>")
    return "".join(blocks)


def _starts_block(lines: list[str], index: int) -> bool:
    line = lines[index]
    return bool(
        _HEADING.match(line)
        or _UNORDERED.match(line)
        or _ORDERED.match(line)
        or _table_at(lines, index)
    )


def _table_at(lines: list[str], index: int) -> tuple[str, int] | None:
    if index + 1 >= len(lines):
        return None
    header = _cells(lines[index])
    separator = _cells(lines[index + 1])
    if (
        len(header) < 2
        or len(separator) != len(header)
        or not all(_TABLE_SEPARATOR.fullmatch(cell.strip()) for cell in separator)
    ):
        return None
    rows: list[list[str]] = []
    cursor = index + 2
    while cursor < len(lines) and lines[cursor].strip():
        cells = _cells(lines[cursor])
        if len(cells) != len(header):
            break
        rows.append(cells)
        cursor += 1
    head = "".join(f"<th>{_inline(cell)}</th>" for cell in header)
    body = "".join(
        "<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return (
        "<table><thead><tr>" + head + "</tr></thead><tbody>" + body + "</tbody></table>",
        cursor,
    )


def _cells(line: str) -> list[str]:
    normalized = line.strip()
    if normalized.startswith("|"):
        normalized = normalized[1:]
    if normalized.endswith("|"):
        normalized = normalized[:-1]
    return [cell.strip() for cell in normalized.split("|")]


def _inline(value: str) -> str:
    escaped = html.escape(value, quote=False)
    return _BOLD.sub(lambda match: f"<b>{match.group(1)}</b>", escaped)


__all__ = ["markdown_to_feishu_xml"]
