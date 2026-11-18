"""Grounding measures from docparse's confidence method
(`docparse/src/docparse/confidence.py`, as of 2026-11-02), copied so this
package stays standalone: `grounding()` (share of a text's numbers and
distinctive words found in the source text) and `unverified_numbers()`.
They are reported in each value's checks; the confidence itself is now the
model's high/medium/low label (see grounding.py), not a docparse score.
"""

from __future__ import annotations

import re


_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
_WORD_RE = re.compile(r"[^\W\d_]{4,}")
# Not evidence either way: common function words, and the vocabulary used to
# talk *about* a figure ("the bar chart shows...").
_UNGROUNDABLE_WORDS = frozenset(
    """with that this from which each their there these those into have also
    been were will would about over under than then they them what when where
    while shows show showing shown displays display figure chart graph table
    image diagram slide picture labeled labelled text visible appears including
    includes include across along between below above left right side bottom
    top column columns rows title titled section sections list listing listed
    green blue orange grey gray black white yellow purple dark light colored
    coloured color colour bars line lines arrow arrows boxes icon icons""".split()
)


def _normalize_number(token: str) -> str:
    return token.replace(",", "")


def unverified_numbers(ai_text: str, source_text: str) -> list[str]:
    """Numbers in `ai_text` that don't occur anywhere in `source_text`."""
    known = {_normalize_number(n) for n in _NUMBER_RE.findall(source_text)}
    missing: list[str] = []
    for token in _NUMBER_RE.findall(ai_text):
        if _normalize_number(token) not in known and token not in missing:
            missing.append(token)
    return missing


def evidence_tokens(text: str) -> set[str]:
    """The numbers and distinctive words (4+ letters, not figure-talk)."""
    numbers = {_normalize_number(n) for n in _NUMBER_RE.findall(text)}
    words = {w.casefold() for w in _WORD_RE.findall(text)} - _UNGROUNDABLE_WORDS
    return numbers | words


def grounding(ai_text: str, source_text: str) -> float:
    """Share of `ai_text`'s numbers and distinctive words that also occur in
    `source_text`; 0 if it has none."""
    claimed = evidence_tokens(ai_text)
    if not claimed:
        return 0.0
    return len(claimed & evidence_tokens(source_text)) / len(claimed)
