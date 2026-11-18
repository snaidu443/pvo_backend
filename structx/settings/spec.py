"""Extraction specs: which fields to pull out of a document, as a JSON file
(`structx/specs/<name>.json`), turned into the strict JSON schema sent with the
Responses API call and the field guide written into its instructions.

Every leaf the model fills is a *cited value*: `{"value": str | null,
"citations": [{"page": int, "quote": str}], "conflicts": [{"page": int,
"value": str, "quote": str}], ...}` -- a conflict names the different value
that statement gives, so the output can list every stated value. A field is
one of:

- `value`: one cited value;
- `list`: a list of cited values (e.g. indications);
- `records`: a list of objects whose sub-fields are each a cited value
  (e.g. studies: id, phase, start date...). The first sub-field labels the
  record in claims and the report.

`type` (`text`, `number`, `date`) only drives a light automatic check (a
number or date with no digit is flagged `type_mismatch`, see grounding.py);
values stay strings in the document's own wording, so their quotes can be
checked.

`parts` (optional, only on a `date` field): a list of `"year"`, `"month"`.
Not sent to the model; code parses the as-written value locally and the
written output gets a `date` object with those parts (`output.date_parts`):
`"Q4 2030"` -> `{"year": 2030, "month": null}`, `"Mar 2031"` -> `{"year":
2031, "month": 3}`. A part the value doesn't state is null (a quarter or
half-year gives no month). The cdp spec asks for year and month on the
US timings and the MAA submission and for the year only on the EU
timings (what the workbook wants for each).

`option_by_date` (optional, on a record sub-field with `options`): the
category follows from a sibling date sub-field by a fixed rule, e.g. an MAA
bucket `pursuing_maa` from `submission_timing` (the 27 Oct 2026 workbook's
options; no current spec uses the rule): `{"field": "submission_timing",
"ranges": [{"option": "December 2026-December 2028", "from": "2026-12",
"to": "2028-12"}, {"option": "January 2029 and beyond", "from": "2029-01"}]}`.
Not sent (the model still maps the category from the wording); after the
answer is checked, code parses the sibling's value (`output.date_parts`)
and, when it falls inside exactly one range, SETS the option to that
range's (`grounding.apply_option_rules`, reason `option_from_date` when it
differed from the model's). A year without a month matches a range only
if every month of that year does. No date, or a date outside every range
(e.g. not pursuing), leaves the model's option alone.

`options` (optional, on a `value`/`list` field or a record sub-field): a
fixed list of categories, e.g. a workbook's choices. The cited value then
also gets an `"option"` key, a strict JSON-schema `enum` of those options or
null -- the model maps the as-written value onto the list ("Phase 2b" ->
"Phase II") while `value` keeps the file's wording for grounding.

`primary_key` (optional, top level): `{"list": "<list field id>", "key":
"<sub-field id>", "null_when": "<text>"?}`, e.g. `{"list": "indications",
"key": "indication"}`. Every records field must have the key sub-field,
which is *not* a cited value but a plain string naming the key, or null
(= stated for the whole program; `null_when` replaces that wording in the
field guide, e.g. for a spec keyed by competitor). A records field may add
`"per_key": "many"` when one key can legitimately have several records
(e.g. a competitor's phase per indication): the output then nests a LIST
of records under the key instead of one record (default `"one"`: the
first record, the rest to `other_records`). All fields are extracted at
once, so the keys aren't known when
the records are written: code matches each record's key to the list
field's values afterwards (`extract.match_keys`; case, punctuation and word
order ignored, nothing fuzzier). A record that matches no key goes to
`other_records` (see output.py). Given `keys`, `json_schema` and
`field_guide` still make the key a strict enum of them.

`aliases` (optional, top level): `{"<abbreviation>": "<wording>"}`, e.g.
`{"LR": "lower-risk", "MF": "myelofibrosis"}`. Never sent to the model: the
CDP + CI join (`combine.words`) replaces each abbreviation by its wording,
on both sides, before its word-overlap mapping check, so `TD MF` and
`Myelofibrosis - Transfusion Dependent` share every word. Keys are
distinct once normalised like a key (`key_words`), nothing is empty, and
no key is its own wording.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from structx.settings.config import ConfigError

SPECS_DIR = Path(__file__).resolve().parent.parent / "specs"  # the package's own folder
KINDS = ("value", "list", "records")
TYPES = ("text", "number", "date")
DATE_PARTS = ("year", "month")
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_YM_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


@dataclass(frozen=True)
class DateRange:
    option: str
    start: tuple[int, int]  # (year, month), inclusive
    end: tuple[int, int] | None = None  # inclusive; None = open-ended


@dataclass(frozen=True)
class DateRule:
    field: str  # the sibling date sub-field the category follows from
    ranges: tuple[DateRange, ...]


@dataclass(frozen=True)
class FieldSpec:
    id: str
    kind: str
    description: str
    type: str = "text"
    fields: tuple["FieldSpec", ...] = ()
    options: tuple[str, ...] = ()
    parts: tuple[str, ...] = ()  # date parts written next to the value (year, month); not sent
    option_by_date: DateRule | None = None  # the option set by code from a sibling date; not sent
    per_key: str = "one"  # records under a primary key: "one" record per key (default) or "many"
    workbook_input: str = ""  # the note naming the workbook row and input this field answers; not sent (the database names its column from it)


PER_KEY = ("one", "many")
KEY_NULL_WHEN = "the file states it for the program as a whole"


@dataclass(frozen=True)
class Spec:
    name: str
    description: str
    fields: tuple[FieldSpec, ...]
    primary_key: tuple[str, str] | None = None  # (list field id, key sub-field id)
    key_null_when: str = KEY_NULL_WHEN  # when a record's key may be null, as told to the model
    aliases: dict[str, str] = dataclasses.field(default_factory=dict)  # abbreviation -> wording, for the join's overlap check; not sent

    def key_of(self, f: FieldSpec) -> str | None:
        """The key sub-field id of records field `f`, under a primary key."""
        return self.primary_key[1] if self.primary_key and f.kind == "records" else None


def _field(raw: object, *, nested: bool) -> FieldSpec:
    if not isinstance(raw, dict):
        raise ConfigError("Every spec field must be an object.")
    fid = raw.get("id")
    if not isinstance(fid, str) or not _ID_RE.match(fid):
        raise ConfigError(f"Spec field id {fid!r} must match {_ID_RE.pattern}.")
    kind = "value" if nested else raw.get("kind")
    if kind not in KINDS:
        raise ConfigError(f"Spec field {fid!r}: kind must be one of {', '.join(KINDS)}.")
    per_key = raw.get("per_key", "one")
    if per_key not in PER_KEY or (per_key != "one" and kind != "records"):
        raise ConfigError(f"Spec field {fid!r}: per_key must be one of {', '.join(PER_KEY)}, on a records field.")
    ftype = raw.get("type", "text")
    if ftype not in TYPES:
        raise ConfigError(f"Spec field {fid!r}: type must be one of {', '.join(TYPES)}.")
    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ConfigError(f"Spec field {fid!r} needs a description (it is the model's only guide).")
    options = _options(raw.get("options"), fid=fid, kind=kind)
    parts = _parts(raw.get("parts"), fid=fid, ftype=ftype)
    rule = _option_by_date(raw.get("option_by_date"), fid=fid, nested=nested, options=options)
    subfields: tuple[FieldSpec, ...] = ()
    if kind == "records":
        raw_sub = raw.get("fields")
        if not isinstance(raw_sub, list) or not raw_sub:
            raise ConfigError(f"Spec field {fid!r}: a records field needs a non-empty 'fields' list.")
        subfields = tuple(_field(sub, nested=True) for sub in raw_sub)
        _unique(subfields, where=fid)
        for sub in subfields:
            if sub.option_by_date is not None:
                sibling = next((s for s in subfields if s.id == sub.option_by_date.field), None)
                if sibling is None or sibling.type != "date" or sibling is sub:
                    raise ConfigError(f"Spec field {fid}.{sub.id}: option_by_date.field must be another sub-field of type 'date'.")
    note = raw.get("workbook_input", "")
    if not isinstance(note, str):
        raise ConfigError(f"Spec field {fid!r}: workbook_input must be a string (a note for people).")
    return FieldSpec(
        id=fid, kind=kind, description=description.strip(), type=ftype, fields=subfields, options=options, parts=parts,
        option_by_date=rule, per_key=per_key, workbook_input=note.strip(),
    )


def _option_by_date(raw: object, *, fid: str, nested: bool, options: tuple[str, ...]) -> DateRule | None:
    if raw is None:
        return None
    if not nested or not options:
        raise ConfigError(f"Spec field {fid!r}: 'option_by_date' is only for a record sub-field with 'options'.")
    if not isinstance(raw, dict) or set(raw) != {"field", "ranges"} or not isinstance(raw["field"], str) \
            or not isinstance(raw["ranges"], list) or not raw["ranges"]:
        raise ConfigError(f"Spec field {fid!r}: option_by_date must be {{\"field\": <date sub-field id>, \"ranges\": [...]}}.")

    def ym(s: object, where: str) -> tuple[int, int]:
        m = _YM_RE.match(s) if isinstance(s, str) else None
        if m is None:
            raise ConfigError(f"Spec field {fid!r}: option_by_date {where} must be \"YYYY-MM\".")
        return int(m.group(1)), int(m.group(2))

    ranges: list[DateRange] = []
    for r in raw["ranges"]:
        if not isinstance(r, dict) or not {"option", "from"} <= set(r) <= {"option", "from", "to"} or r["option"] not in options:
            raise ConfigError(f"Spec field {fid!r}: each option_by_date range needs 'option' (one of the options), 'from' and optionally 'to'.")
        start = ym(r["from"], "from")
        end = ym(r["to"], "to") if "to" in r else None
        if end is not None and end < start:
            raise ConfigError(f"Spec field {fid!r}: option_by_date range for {r['option']!r} ends before it starts.")
        ranges.append(DateRange(option=r["option"], start=start, end=end))
    return DateRule(field=raw["field"], ranges=tuple(ranges))


def _parts(raw: object, *, fid: str, ftype: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if ftype != "date":
        raise ConfigError(f"Spec field {fid!r}: 'parts' is only for a field of type 'date'.")
    if (
        not isinstance(raw, list) or not raw or len(set(raw)) != len(raw)
        or not all(isinstance(p, str) and p in DATE_PARTS for p in raw)
    ):
        raise ConfigError(f"Spec field {fid!r}: 'parts' must be a list of distinct names from {', '.join(DATE_PARTS)}.")
    return tuple(raw)


_MAX_OPTIONS = 50


def _options(raw: object, *, fid: str, kind: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if kind == "records":
        raise ConfigError(f"Spec field {fid!r}: put 'options' on a sub-field, not on a records field.")
    if (
        not isinstance(raw, list) or not raw or len(raw) > _MAX_OPTIONS
        or not all(isinstance(o, str) and o.strip() for o in raw)
    ):
        raise ConfigError(f"Spec field {fid!r}: 'options' must be a list of 1-{_MAX_OPTIONS} non-empty strings.")
    options = tuple(o.strip() for o in raw)
    if len(set(options)) != len(options):
        raise ConfigError(f"Spec field {fid!r}: duplicate options.")
    return options


def _unique(fields: tuple[FieldSpec, ...], *, where: str) -> None:
    ids = [f.id for f in fields]
    if len(ids) != len(set(ids)):
        raise ConfigError(f"Duplicate field ids in {where}.")


def key_words(name: str) -> list[str]:
    """A name's words, in order: case, punctuation and "&"/"and" don't count
    (the normalisation `extract.match_keys` and `combine.words` use)."""
    return re.sub(r"[\W_]+", " ", name.casefold().replace("&", " and ")).split()


def load_spec(name_or_path: str) -> Spec:
    """A spec by name (`structx/specs/<name>.json`) or by path to a JSON file."""
    path = Path(name_or_path)
    if not path.is_file():
        if not _ID_RE.match(name_or_path):
            raise ConfigError(f"Spec {name_or_path!r} is neither a file nor a spec name in {SPECS_DIR}.")
        path = SPECS_DIR / f"{name_or_path}.json"
        if not path.is_file():
            raise ConfigError(f"No spec named {name_or_path!r} in {SPECS_DIR}.")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Could not read spec {path.name}: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("fields"), list) or not raw["fields"]:
        raise ConfigError(f"Spec {path.name} needs a non-empty 'fields' list.")
    name = raw.get("name") or path.stem
    if not isinstance(name, str) or not _ID_RE.match(name):
        raise ConfigError(f"Spec name {name!r} must match {_ID_RE.pattern}.")
    fields = tuple(_field(f, nested=False) for f in raw["fields"])
    _unique(fields, where=path.name)
    primary_key, null_when = _primary_key(raw.get("primary_key"), fields, where=path.name)
    if primary_key is None and any(f.per_key != "one" for f in fields):
        raise ConfigError(f"{path.name}: per_key needs a primary_key.")
    return Spec(name=name, description=str(raw.get("description") or name), fields=fields, primary_key=primary_key,
                key_null_when=null_when, aliases=_aliases(raw.get("aliases"), where=path.name))


def with_sub_options(spec: Spec, sub_id: str, options: list[str]) -> Spec:
    """A copy of `spec` in which every records sub-field named `sub_id`
    has `options` (de-duplicated, validated like a spec's own), so its
    `option` becomes a strict enum of them. The workflow uses it to make
    the ci spec's `indication` sub-fields choose among the CDP's indication
    names. `sub_id` must exist in at least one records field and must not
    be the primary key."""
    if spec.primary_key and sub_id == spec.primary_key[1]:
        raise ConfigError(f"with_sub_options: {sub_id!r} is the primary key; its options are the extracted keys.")
    opts = _options(list(dict.fromkeys(options)), fid=sub_id, kind="value")
    found = False
    fields: list[FieldSpec] = []
    for f in spec.fields:
        if f.kind != "records":
            fields.append(f)
            continue
        subs = []
        for s in f.fields:
            if s.id == sub_id:
                found = True
                s = dataclasses.replace(s, options=opts)
            subs.append(s)
        fields.append(dataclasses.replace(f, fields=tuple(subs)))
    if not found:
        raise ConfigError(f"with_sub_options: no records field of spec {spec.name!r} has a sub-field {sub_id!r}.")
    return dataclasses.replace(spec, fields=tuple(fields))


def with_fields(spec: Spec, ids: Iterable[str]) -> Spec:
    """A copy of `spec` with only the fields named in `ids` (spec order kept);
    the primary key's list field is always kept, since the records hang off
    it. The steps use it to skip the fields an asset's launch market does not
    need: fewer calls, and the skipped fields are absent from the output (null
    in the database). ConfigError for an id the spec does not have."""
    wanted = set(ids)
    unknown = sorted(wanted - {f.id for f in spec.fields})
    if unknown:
        raise ConfigError(f"with_fields: spec {spec.name!r} has no field(s) {', '.join(unknown)}.")
    key_list = spec.primary_key[0] if spec.primary_key else None
    return dataclasses.replace(spec, fields=tuple(f for f in spec.fields if f.id in wanted or f.id == key_list))

def _aliases(raw: object, *, where: str) -> dict[str, str]:
    """The spec's `aliases` map, validated (see the module docstring); {} when absent."""
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items()):
        raise ConfigError(f"{where}: aliases must be an object {{\"<abbreviation>\": \"<wording>\"}} of strings.")
    seen: dict[str, str] = {}
    for k, v in raw.items():
        kn, vn = " ".join(key_words(k)), " ".join(key_words(v))
        if not kn or not vn:
            raise ConfigError(f"{where}: alias {k!r}: {v!r} has an empty side.")
        if kn == vn:
            raise ConfigError(f"{where}: alias {k!r} is its own wording.")
        if kn in seen:
            raise ConfigError(f"{where}: alias {k!r} repeats {seen[kn]!r} (case and punctuation don't count).")
        seen[kn] = k
    return dict(raw)


def _primary_key(raw: object, fields: tuple[FieldSpec, ...], *, where: str) -> tuple[tuple[str, str] | None, str]:
    if raw is None:
        return None, KEY_NULL_WHEN
    if (
        not isinstance(raw, dict) or not {"list", "key"} <= set(raw) <= {"list", "key", "null_when"}
        or not all(isinstance(raw[k], str) and _ID_RE.match(raw[k]) for k in ("list", "key"))
        or not isinstance(raw.get("null_when", ""), str) or not raw.get("null_when", "x").strip()
    ):
        raise ConfigError(f"{where}: primary_key must be {{\"list\": <field id>, \"key\": <sub-field id>, \"null_when\"?: <text>}}.")
    list_id, key = raw["list"], raw["key"]
    by_id = {f.id: f for f in fields}
    if by_id.get(list_id) is None or by_id[list_id].kind != "list":
        raise ConfigError(f"{where}: primary_key list {list_id!r} must be a field of kind 'list'.")
    for f in fields:
        if f.kind != "records":
            continue
        sub = next((x for x in f.fields if x.id == key), None)
        if sub is None:
            raise ConfigError(f"{where}: records field {f.id!r} has no {key!r} sub-field (the primary key).")
        if sub.options:
            raise ConfigError(f"{where}: {f.id}.{key} is the primary key; its options are the extracted keys.")
    return (list_id, key), str(raw.get("null_when", KEY_NULL_WHEN)).strip()


CONFIDENCE_LABELS = ("high", "medium", "low")

_QUOTE_LIST = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["page", "quote"],
        "properties": {"page": {"type": "integer"}, "quote": {"type": "string"}},
    },
}

# A contradicting statement: where it is, the different value it gives (as
# written, so output.py can list every stated value), and its quote.
_CONFLICT_LIST = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["page", "value", "quote"],
        "properties": {"page": {"type": "integer"}, "value": {"type": "string"}, "quote": {"type": "string"}},
    },
}

# Every value the model fills. `confidence_label`, `label_reason` and
# `conflicts` are the extracting model's own judgement (see the labelling
# rules in extract._INSTRUCTIONS); code checks the conflict quotes too.
_CITED_VALUE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["value", "citations", "confidence_label", "label_reason", "conflicts"],
    "properties": {
        "value": {"type": ["string", "null"]},
        "citations": _QUOTE_LIST,
        "confidence_label": {"type": ["string", "null"], "enum": [*CONFIDENCE_LABELS, None]},
        "label_reason": {"type": "string"},
        "conflicts": _CONFLICT_LIST,
    },
}


def cited_value_schema(f: FieldSpec) -> dict:
    """The cited value, plus a strict `option` enum when the field has options."""
    if not f.options:
        return _CITED_VALUE
    return {
        **_CITED_VALUE,
        "required": [*_CITED_VALUE["required"], "option"],
        "properties": {**_CITED_VALUE["properties"], "option": {"type": ["string", "null"], "enum": [*f.options, None]}},
    }


def _object(properties: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}


def json_schema(spec: Spec, keys: list[str] | None = None) -> dict:
    """Strict-mode schema: every property required, no extra keys. Under a
    primary key, a records field's key sub-field is a string or null -- an
    enum of `keys` when they are given."""
    properties: dict = {}
    for f in spec.fields:
        if f.kind == "value":
            properties[f.id] = cited_value_schema(f)
        elif f.kind == "list":
            properties[f.id] = {"type": "array", "items": cited_value_schema(f)}
        else:
            key = spec.key_of(f)
            key_schema: dict = {"type": ["string", "null"]}
            if keys is not None:
                key_schema["enum"] = [*keys, None]
            properties[f.id] = {"type": "array", "items": _object({
                sub.id: key_schema if sub.id == key else cited_value_schema(sub) for sub in f.fields
            })}
    return _object(properties)


def _option_note(f: FieldSpec) -> str:
    if not f.options:
        return ""
    listed = "; ".join(f'"{o}"' for o in f.options)
    return (
        f" Also set option to the one of these categories that the value falls into: {listed} "
        "-- or null if none fits or the file doesn't say enough to choose. value stays as written."
    )


def field_guide(spec: Spec, keys: list[str] | None = None, *, all_fields: tuple[FieldSpec, ...] = ()) -> str:
    """`all_fields`: the whole spec's fields when `spec` is narrowed to one
    field (the key sub-field borrows the list field's description)."""
    spec_fields = all_fields or spec.fields
    lines: list[str] = []
    for f in spec.fields:
        kind = {"value": "one value", "list": "list of values", "records": "list of records"}[f.kind]
        type_note = f", {f.type}" if f.kind != "records" else ""
        lines.append(f"- {f.id} ({kind}{type_note}): {f.description}{_option_note(f)}")
        key = spec.key_of(f)
        for sub in f.fields:
            if sub.id == key and keys is not None:
                listed = "; ".join(f'"{k}"' for k in keys) or "(none were found)"
                lines.append(
                    f"    - {sub.id} (plain string, no citation): exactly one of {listed} -- the one this record is "
                    f"about, written exactly as listed -- or null when {spec.key_null_when}."
                )
            elif sub.id == key:
                # The same naming rule as the list field, so both calls name a key the same way.
                list_id = spec.primary_key[0]
                list_field = next((x for x in spec_fields if x.id == list_id), None)
                rule = f" ({list_field.description})" if list_field is not None else ""
                lines.append(
                    f"    - {sub.id} (plain string, no citation): the {sub.id} this record is about, written in FULL "
                    f"exactly as it would appear in the list '{list_id}'{rule} -- never a "
                    f"shortened name. Or null when {spec.key_null_when}."
                )
            else:
                lines.append(f"    - {sub.id} ({sub.type}): {sub.description}{_option_note(sub)}")
    return "\n".join(lines)
