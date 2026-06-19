from __future__ import annotations


_BREAK_MARKS = ("\n\n", "\n", "。", "！", "？", ". ", "! ", "? ", "；", "; ", "，", ", ")


def split_forward_text(text: str, limit: int) -> list[str]:
    """Split text without dropping characters, preferring natural boundaries."""
    if not text:
        return []
    limit = max(100, int(limit))
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        minimum = limit // 2
        cut = 0
        for mark in _BREAK_MARKS:
            index = window.rfind(mark)
            if index >= minimum:
                cut = max(cut, index + len(mark))
        if cut == 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks
