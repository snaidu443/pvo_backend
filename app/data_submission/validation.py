"""Checks on what the extraction package returns, made before anything is saved.

The AI output is untrusted input: a malformed or oversized answer is rejected (nothing is saved), never repaired.
The package already checks the main keys when it joins the results; these checks are the API's own, and cover the
shape the save step relies on.
"""
from typing import Any

# kind -> (key of the list of records, key of the cited value that names each record)
_SHAPE = {'cdp': ('indications', 'indication'), 'ci': ('competitors', 'competitor'), 'protocol': ('studies', 'study')}
MAX_RECORDS = 2000
MAX_NAMES = 100
MAX_NAME_CHARS = 500


class InvalidExtraction(ValueError):
    """The AI output is not in the expected shape. The message names the problem, never document content."""


def cited_text(node: Any) -> str | None:
    """The text of a cited value: {"value": "x"} or {"value": ["x", "y"]} (first one chosen); None when empty or not text."""
    value = node.get('value') if isinstance(node, dict) else None
    if isinstance(value, list):
        value = value[0] if value else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def check_names(names: Any) -> list[str]:
    """The indication names every other file is mapped to: a non-empty list of distinct, bounded, non-empty strings."""
    if not isinstance(names, list) or not names:
        raise InvalidExtraction('There are no indication names to map the documents to.')
    clean = []
    for n in names:
        if not isinstance(n, str) or not n.strip() or len(n) > MAX_NAME_CHARS:
            raise InvalidExtraction('An indication name is empty or too long.')
        clean.append(n.strip())
    clean = list(dict.fromkeys(clean))
    if len(clean) > MAX_NAMES:
        raise InvalidExtraction('The document lists too many indications.')
    return clean


def check_output(kind: str, out: Any) -> dict:
    """One extraction output ('cdp', 'ci' or 'protocol'): its source, summary and record list have the expected shape."""
    list_key, name_key = _SHAPE[kind]
    if not isinstance(out, dict):
        raise InvalidExtraction(f'The {kind} result is not an object.')
    source = out.get('source')
    if not isinstance(source, dict) or source.get('spec') != kind:
        raise InvalidExtraction(f'The {kind} result does not say it is a {kind} extraction.')
    if not isinstance(out.get('summary'), dict):
        raise InvalidExtraction(f'The {kind} result has no summary.')
    records = out.get(list_key)
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise InvalidExtraction(f"The {kind} result has no valid '{list_key}' list.")
    if kind == 'cdp' and not records:
        raise InvalidExtraction('No indication was found in the CDP document.')
    for rec in records:
        if not isinstance(rec, dict) or cited_text(rec.get(name_key)) is None:
            raise InvalidExtraction(f'A {kind} record has no {name_key} name.')
        if len(cited_text(rec[name_key]) or '') > MAX_NAME_CHARS:
            raise InvalidExtraction(f'A {kind} record name is too long.')
    return out
