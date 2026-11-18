"""The written output: compact values, grouped under the primary key.

Every extracted value is written with the SAME keys every time (nothing
appears only when set, so the structure is deterministic):

    {"value": str | [str],                   # a LIST of every stated value when conflicts is non-empty
     ["option": str | null,]                 # option only on fields with options (spec-driven)
     ["date": DateParts | [DateParts],]      # only on date fields with `parts` (spec-driven); mirrors value's shape
     "confidence": "high"|"medium"|"low", "confidence_reasons": [str], "label_reason": str,
     "model_label": str | null,              # the model's label when the checks lowered it, else null
     "citations": [Quote], "conflicts": [Conflict],
     "checks": {"text_backed": bool, "value_in_quote": float, "unverified_numbers": [str]}}

    Quote = {"page": int, "quote": str, "match": "exact"|"fuzzy"|"not_found",
             "found_on_page": int | null, "page_exists": bool}
    Conflict = Quote + {"value": str}        # the differing value that statement gives
    DateParts = {"year": int | null, "month": int | null}   # the parts the spec lists, parsed by date_parts()

`date` (user request 2026-11-04, the workbook wants year + month for the US
timing): parsed by code from the as-written value, no model involvement --
a 4-digit year (or a 2-digit one after an apostrophe, "Jun '24" = 2024),
and a month only when the value names one ("Mar 2031", "March 2031",
"03/2031", "2031-03"); a quarter or half-year ("Q4 2030",
"H2 2031") gives month null; a range with two different years gives year
null (fail closed, nothing guessed). With a list value it is a list, one
object per stated value, in the same order.

A value with conflicts (user decision 2026-11-04): `value` is the list of
every value the file states for it -- the model's chosen value first (the
one the citations and checks are about), then each conflict's value,
distinct, in the order listed. The confidence, reasons and label_reason are
those of the chosen value, unchanged. Without conflicts `value` is a string.

For a deck, `page` and `found_on_page` are the DECK'S SLIDE NUMBERS (hidden
slides are left out of the sent PDF, so its page numbers would differ;
grounding.check maps them back). For a PDF input they are its page numbers.

With a spec `primary_key` (cdp: indications; ci: competitors), the body is
one entry per key, in the order found, with every records field nested
under it as one record -- or as a LIST of records when the field has
`"per_key": "many"` (ci: a competitor's phase per indication). A record
stated for the program as a whole (key null) goes under every key that
has no record of its own, marked `"applies_to": "program"`. Nothing is
dropped: a second record for the same key (per_key "one"), or a
program-wide record every key already overrides, goes to `other_records`
with the reason.

Without a primary key the body keeps the spec's own field structure.
"""

from __future__ import annotations

import re

from structx.settings.spec import DateRule, FieldSpec, Spec

_MONTHS = {m: i + 1 for i, m in enumerate(
    "january february march april may june july august september october november december".split())}
_MONTHS.update({m[:3]: n for m, n in list(_MONTHS.items())})
_MONTHS["sept"] = 9
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_SHORT_YEAR_RE = re.compile(r"(?<![\w'’‘])['’‘](\d{2})(?!\d)")   # '24 / ’24: a 2-digit year after an apostrophe
_MONTH_NAME_RE = re.compile(r"(?<![a-z])(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")(?![a-z])")
# A numeric month only next to the year: 03/2031, 03-2031, 2031-03, 2031/03.
_MONTH_NUM_RE = re.compile(r"(?<!\d)(0?[1-9]|1[0-2])[/\-.](?:19|20)\d{2}(?!\d)|(?<!\d)(?:19|20)\d{2}[/\-.](0?[1-9]|1[0-2])(?!\d)")


def date_parts(value: str, parts: tuple[str, ...]) -> dict:
    """The year and/or month the as-written value states, else null per part.
    Deterministic text parsing; see the module docstring for the rules."""
    text = _SHORT_YEAR_RE.sub(lambda m: "20" + m.group(1), value.casefold())
    out: dict = {}
    years = sorted({int(y) for y in _YEAR_RE.findall(text)})
    if "year" in parts:
        out["year"] = years[0] if len(years) == 1 else None
    if "month" in parts:
        names = {_MONTHS[m] for m in _MONTH_NAME_RE.findall(text)}
        nums = {int(a or b) for a, b in _MONTH_NUM_RE.findall(text)}
        found = names or nums
        out["month"] = next(iter(found)) if len(found) == 1 and len(years) == 1 else None
    return out


def option_for(date: dict, rule: "DateRule") -> str | None:
    """The one option whose range contains the parsed date: with a month,
    the (year, month) itself; without one, only when every month of that
    year is inside the range. None when no range (or more than one) does."""
    year, month = date.get("year"), date.get("month")
    if year is None:
        return None
    lo, hi = ((year, month), (year, month)) if month is not None else ((year, 1), (year, 12))
    hits = [r.option for r in rule.ranges if r.start <= lo and (r.end is None or hi <= r.end)]
    return hits[0] if len(hits) == 1 else None


def _quote(c: dict) -> dict:
    """A citation or conflict as checked by grounding.check(), fixed keys."""
    return {
        "page": c["page"],
        "quote": c["quote"],
        "match": c.get("match", "not_found"),
        "found_on_page": c.get("found_on_page"),
        "page_exists": c.get("page_exists", True),
    }


def _conflict(c: dict) -> dict:
    return {**_quote(c), "value": c.get("value", "")}


def all_values(node: dict) -> str | list[str]:
    """The written `value`: the string itself, or -- when the model listed
    conflicts -- the list of every stated value, chosen value first, each
    conflict's value after it, without repeats (exact text after stripping)."""
    chosen = node["value"]
    if not node.get("conflicts"):
        return chosen
    values = [chosen.strip()]
    for c in node["conflicts"]:
        v = (c.get("value") or "").strip()
        if v and v not in values:
            values.append(v)
    return values


def compact(node: dict | None, f: FieldSpec | None = None) -> dict | None:
    """`f` is the value's field spec; it drives the spec-dependent keys."""
    if node is None or node.get("value") is None:
        return None
    values = all_values(node)
    out: dict = {"value": values}
    if "option" in node:
        out["option"] = node["option"]
    if f is not None and f.parts:
        out["date"] = ([date_parts(v, f.parts) for v in values] if isinstance(values, list)
                       else date_parts(values, f.parts))
    # The confidence is the model's label, possibly lowered by the checks
    # (then the model's own label is kept as model_label, else null).
    out["confidence"] = node.get("confidence")
    out["confidence_reasons"] = list(node.get("confidence_reasons", []))
    out["label_reason"] = node.get("label_reason", "")
    lowered = node.get("confidence_label") not in (None, node.get("confidence"))
    out["model_label"] = node["confidence_label"] if lowered else None
    out["citations"] = [_quote(c) for c in node.get("citations", [])]
    out["conflicts"] = [_conflict(c) for c in node.get("conflicts", [])]
    g = node.get("grounding") or {}
    out["checks"] = {
        "text_backed": bool(g.get("text_backed", False)),
        "value_in_quote": g.get("value_in_quote", 0.0),
        "unverified_numbers": list(g.get("unverified_numbers", [])),
    }
    return out


def _record(f: FieldSpec, rec: dict, key: str | None) -> dict:
    return {sub.id: compact(rec[sub.id], sub) for sub in f.fields if sub.id != key}


def _plain(spec: Spec, data: dict) -> dict:
    body: dict = {}
    for f in spec.fields:
        items = data.get(f.id)
        if items is None:
            body[f.id] = None
        elif f.kind == "value":
            body[f.id] = compact(items, f)
        elif f.kind == "list":
            body[f.id] = [compact(n, f) for n in items]
        else:
            body[f.id] = [_record(f, rec, None) for rec in items]
    return body


def _grouped(spec: Spec, data: dict) -> dict:
    list_id, key = spec.primary_key
    list_field = next(f for f in spec.fields if f.id == list_id)
    entries: dict[str, dict] = {}
    for node in data.get(list_id) or []:
        name = node["value"]
        if name not in entries:  # the model may list one twice
            entries[name] = {key: compact(node, list_field)}
    other: list[dict] = []
    asset: dict = {}

    for f in spec.fields:
        if f.id == list_id:
            continue
        if f.kind != "records":
            asset[f.id] = _plain(Spec(spec.name, spec.description, (f,)), data)[f.id]
            continue
        records = data.get(f.id)
        if records is None:  # the field's call failed (see summary.failed_fields)
            for entry in entries.values():
                entry[f.id] = None
            continue
        own: dict[str, list[dict]] = {}
        program: list[dict] = []
        for rec in records:
            (own.setdefault(rec[key], []) if rec[key] is not None else program).append(rec)
        program_used = False
        many = f.per_key == "many"  # a LIST of records per key (spec `per_key`), nothing to other_records
        for name, entry in entries.items():
            if own.get(name):
                records_out = [{"applies_to": key, **_record(f, rec, key)} for rec in own[name]]
                entry[f.id] = records_out if many else records_out[0]
                for extra in ([] if many else own[name][1:]):
                    other.append({"field": f.id, key: name, "reason": f"more than one {f.id} record for this {key}",
                                  **_record(f, extra, key)})
            elif program:
                records_out = [{"applies_to": "program", **_record(f, rec, key)} for rec in program]
                entry[f.id] = records_out if many else records_out[0]
                program_used = True
            else:
                entry[f.id] = None
        for name, recs in own.items():
            if name not in entries:  # matched no key (extract.match_keys): kept, not dropped
                for rec in recs:
                    other.append({"field": f.id, key: name, "reason": f"this {key} is not in the {list_id} list",
                                  **_record(f, rec, key)})
        for i, rec in enumerate(program):
            if (i > 0 and not many) or not program_used:
                reason = ("more than one program-wide record" if i > 0
                          else f"program-wide, but every {key} has its own record" if entries
                          else f"program-wide, and no {key} was found")
                other.append({"field": f.id, key: None, "reason": reason, **_record(f, rec, key)})

    body: dict = {list_id: list(entries.values())}
    if asset:
        body["asset"] = asset
    body["other_records"] = other
    return body


def build_body(spec: Spec, data: dict) -> dict:
    return _grouped(spec, data) if spec.primary_key else _plain(spec, data)
