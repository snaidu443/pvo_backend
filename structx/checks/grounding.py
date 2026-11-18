"""Claims (one per extracted value), their automatic checks against the
file's own text, and their confidence.

**The confidence is the extracting model's label: `high` / `medium` /
`low`.** The model sets it in the same call, after searching the whole deck
for contradicting statements (prompt: extract._INSTRUCTIONS). Code then
applies hard evidence, and it can only LOWER the label, never raise it.
Every lowering adds a reason code:

| Evidence (automatic)                                         | Label becomes at most | Reason code                |
|--------------------------------------------------------------|-----------------------|----------------------------|
| the model listed a conflict (enforced in `_cited_value`)      | low                   | (named in label_reason)    |
| a listed conflict's quote is in the file's own text           | low                   | `conflicting_statements`   |
| not text-backed: its quote isn't in the file's text           | medium                | `quote_not_found`          |
| not text-backed: the value isn't in its quote                 | medium                | `value_not_in_quote`       |
| no citation at all                                            | low                   | `no_citation`              |
| a `number`/`date` value with no digit ("TBD")                 | low                   | `type_mismatch`            |
| the model gave no label                                       | low                   | `no_label`                 |

Informational (label unchanged): `wrong_page`, `conflict_not_in_text`,
`no_option`, `option_from_date` (the category was set by code from the
record's date, `apply_option_rules`, and differs from the model's).

There is no verification call (removed 2026-11-03, user decision: the
model's own label is enough), so a real quote attached to the wrong record
or mapped to the wrong category is not caught by code.

Each conflict also names the differing value that statement gives (model
output, checked only for shape); `output.py` writes a value that has
conflicts as the list of every stated value, with this same label and
reasons (user decision 2026-11-04).

**Text-backed** = one citation whose quote is found in the file's own text
(slide text, tables, chart data, PDF text layer) AND contains
the value. The model then only *selected* the file's words; anything else
was read from an image/chart or paraphrased.

A null value means the model found the field absent (`not_in_document`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from structx.checks import scoring
from structx.model.llm import ExtractionError
from structx.source.source import Match, SourceFile, normalize
from structx.settings.spec import CONFIDENCE_LABELS, FieldSpec, Spec

_RANK_LABEL = {"low": 0, "medium": 1, "high": 2}

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
_STOP = frozenset("a an and as at by for from in into is of on or per the to with".split())


@dataclass
class Claim:
    id: str
    path: str  # e.g. "studies[2].first_patient_in"
    field: FieldSpec
    record_label: str | None
    node: dict  # the model's cited-value object; results are written into it
    matches: list[Match] = field(default_factory=list)

    @property
    def value(self) -> str | None:
        return self.node["value"]

    @property
    def citations(self) -> list[dict]:
        return self.node["citations"]


_LLM_KEYS = {"confidence_label", "label_reason", "conflicts"}


def _quotes_ok(items: object, *, with_value: bool = False) -> bool:
    """Citations are {page, quote}; conflicts also name the differing value."""
    return isinstance(items, list) and all(
        isinstance(c, dict) and isinstance(c.get("page"), int) and isinstance(c.get("quote"), str)
        and (not with_value or isinstance(c.get("value"), str))
        for c in items
    )


def _cited_value(node: object, path: str, f: FieldSpec) -> dict:
    keys = {"value", "citations"} | _LLM_KEYS | ({"option"} if f.options else set())
    if not isinstance(node, dict) or set(node) != keys:
        raise ExtractionError(f"Model output at {path} is not a cited value.")
    if f.options and node["option"] is not None and node["option"] not in f.options:
        # The schema's enum should make this impossible; fail closed anyway.
        raise ExtractionError(f"Model output at {path}: option {node['option']!r} is not one of the allowed options.")
    if node["value"] is not None and not isinstance(node["value"], str):
        raise ExtractionError(f"Model output at {path}: value is not a string.")
    if not _quotes_ok(node["citations"]):
        raise ExtractionError(f"Model output at {path}: malformed citations.")
    if not _quotes_ok(node["conflicts"], with_value=True):
        raise ExtractionError(f"Model output at {path}: malformed conflicts.")
    if node["confidence_label"] is not None and node["confidence_label"] not in CONFIDENCE_LABELS:
        # The schema's enum should make this impossible; fail closed anyway.
        raise ExtractionError(f"Model output at {path}: confidence_label {node['confidence_label']!r} is not allowed.")
    if not isinstance(node["label_reason"], str):
        raise ExtractionError(f"Model output at {path}: label_reason is not a string.")
    if isinstance(node["value"], str) and not node["value"].strip():
        node["value"] = None
    if node["value"] is None:
        # Nothing to label without a value.
        node.update(confidence_label=None, label_reason="", conflicts=[])
        if f.options:
            node["option"] = None  # no category without a value to ground it
    elif node["conflicts"] and node["confidence_label"] != "low":
        # The prompt's rule is "any conflict -> low"; enforce it, don't trust it.
        node["label_reason"] = (
            node["label_reason"] + f" [lowered from {node['confidence_label']} to low: conflicts listed]"
        ).strip()
        node["confidence_label"] = "low"
    return node


PROGRAM_WIDE = "the program as a whole"


def collect_claims(
    spec: Spec, data: dict, *, id_prefix: str = "c", keys: list[str] | None = None
) -> list[Claim]:
    """Validates the model output's shape (fail closed) and returns one claim
    per non-null value, ids `<id_prefix><n>` (unique per extraction call).
    Null list entries and all-null records are dropped from `data` in place.

    Under a primary key, a record's key sub-field is a plain string (one of
    `keys` when given) or null (program-wide); it labels the record's
    claims and is not itself a claim. Without `keys` it is matched to the
    keys later (`extract.match_keys`)."""
    claims: list[Claim] = []

    def add(f: FieldSpec, node: dict, path: str, label: str | None) -> None:
        if node["value"] is not None:
            claims.append(Claim(f"{id_prefix}{len(claims) + 1}", path, f, label, node))

    for f in spec.fields:
        if f.id not in data:
            raise ExtractionError(f"Model output is missing field {f.id}.")
        if f.kind == "value":
            add(f, _cited_value(data[f.id], f.id, f), f.id, None)
            continue
        items = data[f.id]
        if not isinstance(items, list):
            raise ExtractionError(f"Model output at {f.id} is not a list.")
        if f.kind == "list":
            kept = [_cited_value(n, f"{f.id}[{i}]", f) for i, n in enumerate(items)]
            data[f.id] = kept = [n for n in kept if n["value"] is not None]
            for i, node in enumerate(kept):
                add(f, node, f"{f.id}[{i}]", None)
            continue
        key = spec.key_of(f)
        cited = [sub for sub in f.fields if sub.id != key]
        records = []
        for i, rec in enumerate(items):
            if not isinstance(rec, dict) or set(rec) != {s.id for s in f.fields}:
                raise ExtractionError(f"Model output at {f.id}[{i}] is not a {f.id} record.")
            if key is not None and rec[key] is not None:
                if not isinstance(rec[key], str):
                    raise ExtractionError(f"Model output at {f.id}[{i}].{key} is not a string.")
                if keys is not None and rec[key] not in keys:
                    # The schema's enum should make this impossible; fail closed anyway.
                    raise ExtractionError(f"Model output at {f.id}[{i}].{key}: {rec[key]!r} is not one of the extracted keys.")
                rec[key] = rec[key].strip()  # "" matches no key: other_records, not program-wide
            for sub in cited:
                _cited_value(rec[sub.id], f"{f.id}[{i}].{sub.id}", sub)
            if any(rec[sub.id]["value"] is not None for sub in cited):
                records.append(rec)
        data[f.id] = records
        for i, rec in enumerate(records):
            if key is not None:
                # Only null is program-wide; "" is a name that matches no key.
                label = PROGRAM_WIDE if rec[key] is None else rec[key]
            else:
                label = next((rec[s.id]["value"] for s in f.fields if rec[s.id]["value"]), None)
            for sub in cited:
                add(sub, rec[sub.id], f"{f.id}[{i}].{sub.id}", label)
    return claims


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(normalize(text)) if t not in _STOP and (len(t) > 1 or t.isdigit())]


_RANK = {"exact": 2, "fuzzy": 1, "not_found": 0}


def check(source: SourceFile, claim: Claim) -> None:
    """Locates every citation in the file's own text and writes the
    per-citation and per-value grounding into the claim's node."""
    def located(quote: dict, m: Match) -> dict:
        # The same keys for every citation and conflict (output.py copies
        # them). The model cites the sent PDF's pages; written as the
        # deck's slide numbers (`source.slide_of`).
        return {
            "page": source.slide_of(quote["page"]),
            "quote": quote["quote"],
            "match": m.found,
            "match_ratio": m.ratio,
            "found_on_page": None if m.page is None else source.slide_of(m.page),
            "page_exists": quote["page"] in source.pages,
        }

    cited = claim.citations  # as the model wrote them (sent-page numbers)
    claim.matches = [source.locate(c["quote"], c["page"]) for c in cited]
    cites_out = [located(cite, m) for cite, m in zip(cited, claim.matches)]
    claim.node["citations"] = cites_out

    # The model's listed contradictions, checked like citations; each keeps
    # the differing value it names (output.py lists every stated value).
    conflicts_out = [{**located(cf, source.locate(cf["quote"], cf["page"])), "value": cf["value"].strip()}
                     for cf in claim.node.get("conflicts", [])]
    claim.node["conflicts"] = conflicts_out

    value = claim.value or ""
    tokens = _tokens(value)

    def share_in(quote: str) -> float:
        q = normalize(quote)
        hits = sum(1 for t in tokens if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", q))
        return hits / len(tokens) if tokens else 1.0

    # Per citation: the found-in-file check and the value-in-quote check
    # must hold for the SAME citation. (Measuring value-in-quote over all
    # quotes together let an unrelated quote that is in the file "back" a
    # value whose own quote isn't -- found 2026-11-02.)
    shares = [share_in(c["quote"]) for c in cites_out]

    def strength(i: int) -> tuple:
        m = claim.matches[i]
        # Backing citation first; otherwise the one that states the value
        # (so the reason is "its quote isn't in the file", not "an unrelated
        # quote lacks the value").
        return (m.found != "not_found" and shares[i] == 1.0, shares[i], _RANK[m.found],
                m.page == cited[i]["page"], m.ratio)

    best_i = max(range(len(claim.matches)), key=strength, default=None)
    best = claim.matches[best_i] if best_i is not None else None
    share = shares[best_i] if best_i is not None else 0.0
    pages = {c["page"] for c in cited} | {m.page for m in claim.matches if m.page is not None}
    page_text = source.page_text(pages if cites_out else None)
    claim.node["grounding"] = {
        "quote_found": best.found if best else "no_citation",
        "match_ratio": best.ratio if best else 0.0,
        "wrong_page": bool(best and best.page is not None and best.page != cited[best_i]["page"]),
        "value_in_quote": round(share, 3),
        # One citation that is both in the file's text and contains the value.
        "text_backed": bool(best and best.found != "not_found" and share == 1.0),
        # docparse's grounding measures (scoring.py), against the cited pages' text
        # (the whole file's when uncited).
        "grounding": round(scoring.grounding(value, page_text), 3),
        "unverified_numbers": scoring.unverified_numbers(value, page_text),
        "conflicts_listed": len(conflicts_out),
        "conflicts_in_file": sum(1 for c in conflicts_out if c["match"] != "not_found"),
    }


def score(claim: Claim, *, page_numbers_reliable: bool) -> None:
    """Sets `confidence` (high/medium/low) and `confidence_reasons`: the
    model's label, lowered by hard evidence (see the module docstring)."""
    g = claim.node["grounding"]
    model_label = claim.node.get("confidence_label")
    label = model_label or "low"
    reasons: list[str] = [] if model_label else ["no_label"]

    def at_most(cap: str, reason: str) -> None:
        # The reason is recorded whether or not it lowers the label: it is
        # evidence either way (`model_label` in the output shows a lowering).
        nonlocal label
        if _RANK_LABEL[cap] < _RANK_LABEL[label]:
            label = cap
        reasons.append(reason)

    if g.get("conflicts_in_file"):
        at_most("low", "conflicting_statements")
    elif g.get("conflicts_listed"):
        reasons.append("conflict_not_in_text")
    if not claim.citations:
        at_most("low", "no_citation")
    elif not g["text_backed"]:
        at_most("medium", "quote_not_found" if g["quote_found"] == "not_found" else "value_not_in_quote")
    if claim.field.type in ("number", "date") and not re.search(r"\d", claim.value or ""):
        at_most("low", "type_mismatch")
    if g["wrong_page"] and page_numbers_reliable:
        reasons.append("wrong_page")
    if claim.field.options and claim.node.get("option") is None:
        reasons.append("no_option")
    claim.node["confidence"] = label
    claim.node["confidence_reasons"] = reasons


def apply_option_rules(spec: Spec, data: dict) -> int:
    """`option_by_date` (spec.py): in every record, a sub-field whose
    category follows from a sibling date gets that category set by code
    when the sibling's value parses into exactly one of the rule's ranges
    (`output.option_for`). Differing from the model's option adds the
    reason `option_from_date` (label unchanged: a deterministic rule, not
    evidence against the value); `no_option` is dropped when code filled
    it. Returns how many options were changed."""
    from structx.output.output import date_parts, option_for  # output imports spec only; no cycle

    changed = 0
    for f in spec.fields:
        if f.kind != "records" or not data.get(f.id):
            continue
        for sub in f.fields:
            rule = sub.option_by_date
            if rule is None:
                continue
            for rec in data[f.id]:
                node, date_node = rec[sub.id], rec[rule.field]
                if node.get("value") is None or date_node.get("value") is None:
                    continue
                option = option_for(date_parts(date_node["value"], ("year", "month")), rule)
                if option is None or option == node.get("option"):
                    continue
                node["option"] = option
                reasons = [r for r in node.get("confidence_reasons", []) if r != "no_option"]
                node["confidence_reasons"] = [*reasons, "option_from_date"]
                changed += 1
    return changed


def mark_absent(spec: Spec, data: dict) -> list[str]:
    """Annotates null single-value fields (`not_in_document`) and returns the
    ids of every field the model found nothing for."""
    absent: list[str] = []
    for f in spec.fields:
        if data.get(f.id) is None:  # its extraction call failed: not "absent"
            continue
        if f.kind == "value":
            node = data[f.id]
            if node["value"] is None:
                node.update(confidence=None, confidence_reasons=["not_in_document"])
                absent.append(f.id)
        elif not data[f.id]:
            absent.append(f.id)
    return absent
