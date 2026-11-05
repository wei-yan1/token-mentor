"""Bounded extractive compression; no model or long-term message archive."""
import re


def compress_turn(previous: str, message: str, reply: str) -> str:
    def compact(text: str, limit: int) -> str:
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) <= limit:
            return text
        return text[:limit // 2] + ' … ' + text[-(limit // 2 - 3):]

    # Keep the previous turn's compressed context, current intent and reply highlights.
    # The next round compresses this again, rather than appending a message log.
    return '\n'.join((
        '上轮上下文：' + compact(previous, 800),
        '本轮用户：' + compact(message, 900),
        '本轮答复要点：' + compact(reply, 250),
    ))[:2000]
