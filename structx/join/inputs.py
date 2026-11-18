"""The inputs: the business workbook's inputs as a catalogue (`input`,
`input_option`, seeded from structx/specs/inputs.json) and the values an asset has
for them (`input_value`, one row per asset, input and, for indication- and
competitor-level inputs, indication and competitor). The catalogue says how
the front end edits each input (`kind`), which document answers it
(`source`: cdp, ci, protocol, forecast, or manual, calc, link, none) and
which page owns the value (`screen`) and which geography the input is
about (`geography`: Global, US or EU). The asset's Launch Markets value sets
its scope (`launch_scope`: US -> Global and US, EU -> Global and EU, else
all): the coverage counts only the inputs in scope and
`scoped_fields` names the spec fields an extraction should run (the steps'
`fields`). A Conditional input's Note ("Required if ...") is a rule in
`CONDITIONS`, tied to the Note's text; `coverage` evaluates it per slot
(`required_now`) and counts a required slot without a value as missing.
Values written by an extraction are
copies of the cdp / ci / protocol cells (`copy_extracted`), so the user can
edit them while those tables stay the untouched AI record. Pure code.

    input         input_id, id (snake_case, stable; competitor-section ids start with competitor_), name,
                  category (the workbook's grouping), section (asset | competitor), level (asset | indication),
                  kind, source, screen, geography (Global | US | EU), spec, field, required (1 = the workbook's
                  "Required"), condition (the
                  rule of a "Conditional" input, else null), expected (the workbook cell), source_text,
                  explanation, policies (JSON list), unit, workbook_row (the row on the input's sheet)
    input_option  option_id, input_id, position, value (a select's or multi-select's options, or the EU
                  reference countries of a series_eu)
    input_value   value_id, asset_id, input_id, indication_id (null = asset level), competitor (null unless
                  competitor level), value (JSON by kind), source (cdp | ci | protocol | user),
                  status (ai_extracted | entered | edited), confidence, confidence_reason, citations,
                  document_id, user_id (the user who entered or edited the value, or who uploaded the
                  document it was extracted from), updated_at; unique per asset, input, indication, competitor

Value JSON by kind: select -> option string; number -> number >= 0; percent -> 0-100; year -> 1900-2100;
year_month -> {"year", "month" (1-12 or null)}; series -> 15 numbers or nulls; series_eu -> {country: 15 values}
over the EU reference countries; multi_select -> list of the chosen options; list and calc are not stored.
Seeding is an upsert keyed by `id` (and by value within an input): ids stay put, a changed catalogue updates
rows, an input or option no longer listed is deleted.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path

from structx.settings.spec import SPECS_DIR

INPUT_FILE = SPECS_DIR / "inputs.json"
SECTIONS = ("asset", "competitor")
LEVELS = ("asset", "indication")
KINDS = ("select", "number", "percent", "year", "year_month", "series", "series_eu", "multi_select", "list", "calc")
SOURCES = ("cdp", "ci", "protocol", "forecast", "manual", "calc", "link", "none")
SCREENS = ("submission", "indications", "assumptions", "inputs")
POLICIES = ("IRA", "MFN", "JCA", "EU-GPL")
GEOGRAPHIES = ("Global", "US", "EU")   # the workbook's "Geography Scope"
LAUNCH_INPUT = "launch_markets"       # the asset-level input whose value sets the scope (Data Submission)
# its options -> the geographies in scope (checked against the catalogue: the option texts are the workbook's)
MARKET_SCOPE = {"US": ("Global", "US"), "EU": ("Global", "EU"), "Global launch (including US and EU)": GEOGRAPHIES}
SPECS = ("cdp", "ci", "protocol")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
COUNTRIES = ("United States", "Canada", "United Kingdom", "Germany", "France", "Italy", "Spain", "Netherlands", "Belgium",
             "Austria", "Switzerland", "Sweden", "Denmark", "Norway", "Ireland", "Czech Republic", "Poland", "Japan", "China",
             "South Korea", "Australia", "Brazil", "Mexico", "India", "Israel")
EU_REF_COUNTRIES = ("Austria", "Belgium", "Czech Republic", "Denmark", "France", "Germany", "Ireland", "Italy", "Netherlands",
                    "Norway", "Spain", "Sweden", "Switzerland", "United Kingdom")
_ID = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
_OPTION_KINDS = ("select", "multi_select", "series_eu")   # kinds whose options are stored

INPUT_TABLES: dict[str, list[tuple[str, str]]] = {
    "input": [
        ("input_id", "INTEGER PRIMARY KEY"), ("id", "TEXT NOT NULL UNIQUE"), ("name", "TEXT NOT NULL"), ("category", "TEXT NOT NULL"),
        ("section", "TEXT NOT NULL"), ("level", "TEXT NOT NULL"), ("kind", "TEXT NOT NULL"), ("source", "TEXT NOT NULL"),
        ("screen", "TEXT NOT NULL"), ("geography", "TEXT NOT NULL"), ("spec", "TEXT"), ("field", "TEXT"), ("required", "INTEGER NOT NULL"),
        ("condition", "TEXT"),
        ("expected", "TEXT NOT NULL"), ("source_text", "TEXT NOT NULL"), ("explanation", "TEXT"),
        ("policies", "TEXT NOT NULL"), ("unit", "TEXT"), ("workbook_row", "INTEGER"),
    ],
    "input_option": [
        ("option_id", "INTEGER PRIMARY KEY"), ("input_id", "INTEGER NOT NULL REFERENCES input(input_id)"),
        ("position", "INTEGER NOT NULL"), ("value", "TEXT NOT NULL"),
    ],
    "input_value": [
        ("value_id", "INTEGER PRIMARY KEY"), ("asset_id", "INTEGER NOT NULL REFERENCES asset(asset_id)"),
        ("input_id", "INTEGER NOT NULL REFERENCES input(input_id)"), ("indication_id", "INTEGER REFERENCES indication(indication_id)"),
        ("competitor", "TEXT"), ("value", "TEXT NOT NULL"), ("source", "TEXT NOT NULL"), ("status", "TEXT NOT NULL"),
        ("confidence", "TEXT"), ("confidence_reason", "TEXT"), ("citations", "TEXT"),
        ("document_id", "INTEGER REFERENCES document(document_id)"), ("user_id", "INTEGER REFERENCES user(user_id)"),
        ("updated_at", "TEXT NOT NULL"),
    ],
}
INPUT_INDEXES = (
    'CREATE UNIQUE INDEX "input_section_name" ON "input" ("section", "name");',
    'CREATE UNIQUE INDEX "input_option_value" ON "input_option" ("input_id", "value");',
    'CREATE UNIQUE INDEX "input_value_key" ON "input_value" ("asset_id", "input_id", COALESCE("indication_id", 0), COALESCE("competitor", \'\'));',
)
_COLUMNS = [c for c, _ in INPUT_TABLES["input"][1:]]


# ---------------------------------------------------------------- the conditional requirements (the workbook's Note column)
class Values:
    """An asset's values as the condition rules see them: `value(id, indication_id, competitor)` the stored value or None,
    `provided(...)` whether a non-empty one is there, `included` the included indication ids."""

    def __init__(self, rows: list[dict], included: list[int]) -> None:
        self._by = {(r["input"], r["indication_id"], r["competitor"]): r["value"] for r in rows}
        self.included = list(included)

    def value(self, id: str, indication_id: int | None = None, competitor: str | None = None) -> object | None:
        return self._by.get((id, indication_id, competitor))

    def provided(self, id: str, indication_id: int | None = None, competitor: str | None = None) -> bool:
        v = self.value(id, indication_id, competitor)
        return not (v is None or v == "" or v == [] or v == {} or (isinstance(v, list) and all(x is None for x in v)))


def _design(c: Values, ind: int | None) -> object | None:
    return c.value("clinical_trial_comparator_design", ind)


def _late_stage(c: Values) -> bool:
    return any(c.value("phase_of_development", i) in ("Phase II/III", "Phase III") for i in c.included)


def _needs_revenue(c: Values, ind: int | None, share: str, population: str) -> bool:
    """A revenue series is required unless the market share and the addressable population are both given."""
    return not (c.provided(share, ind) and c.provided(population, ind))


# input id -> (its Note as the workbook writes it, the rule: (values, indication_id, competitor) -> required now).
# The catalogue check ties the two: a Conditional input with no rule here, or whose Note no longer says this, is refused.
CONDITIONS: dict[str, tuple[str, Callable[[Values, int | None, str | None], bool]]] = {
    "clinical_trial_comparator_type": ("Required if Clinical Trial Comparator Design is head-to-head",
                                       lambda c, ind, comp: _design(c, ind) == "Head-to-head"),
    "indirect_treatment_comparison": ("Required if Clinical Trial Comparator Design is not head-to-head",
                                      lambda c, ind, comp: _design(c, ind) not in (None, "Head-to-head")),   # unknown design: not yet
    "expected_usa_yoy_revenue_year_1_15": ("Required if the Expected USA YoY Market Share and Size of USA YoY Addressable Population is not provided",
                                           lambda c, ind, comp: _needs_revenue(c, ind, "expected_usa_yoy_market_share_year_1_15", "size_of_usa_yoy_addressable_population_year_1_15")),
    "expected_eu_yoy_revenue_year_1_15": ("Required if the Expected EU YoY Market Share and Size of EU YoY Addressable Population is not provided",
                                          lambda c, ind, comp: _needs_revenue(c, ind, "expected_eu_yoy_market_share_year_1_15", "size_of_eu_yoy_addressable_population_year_1_15")),
    "regulatory_strategy_alignment": ("Required for late-stage assets, i.e., Phase of Development is Phase II/III or Phase III",
                                      lambda c, ind, comp: _late_stage(c)),
}


def _note_key(text: str) -> str:
    return " ".join(text.lower().split())


def required_now(row: dict, values: Values, indication_id: int | None = None, competitor: str | None = None) -> bool:
    """Whether the input is required for this slot: the workbook's Required, or a Conditional whose rule holds."""
    if row["required"]:
        return True
    if not row.get("condition"):
        return False
    return bool(CONDITIONS[row["id"]][1](values, indication_id, competitor))


def check_catalogue(doc: object) -> list[dict]:
    """The validated inputs of a catalogue document; ValueError naming the first problem."""
    if not isinstance(doc, dict) or not isinstance(doc.get("inputs"), list):
        raise ValueError("inputs: the catalogue must be an object with an \"inputs\" list")
    ids: set[str] = set()
    names: set[tuple[str, str]] = set()
    rows: set[tuple[str, int]] = set()   # (section, workbook row): each sheet numbers its own rows
    for n, item in enumerate(doc["inputs"]):
        where = f"inputs: input {n}"
        if not isinstance(item, dict):
            raise ValueError(f"{where} is not an object")
        for key in ("id", "name", "category", "section", "level", "kind", "source", "screen", "geography", "expected", "source_text"):
            if not isinstance(item.get(key), str) or (not item[key].strip() and key != "source_text"):
                raise ValueError(f"{where}: {key!r} must be a string" + ("" if key == "source_text" else ", not empty"))
        if not _ID.match(item["id"]):
            raise ValueError(f"{where}: id {item['id']!r} is not snake_case")
        if item["id"] in ids or (item["section"], item["name"]) in names:
            raise ValueError(f"{where}: id {item['id']!r} / name {item['name']!r} repeats in its section")
        ids.add(item["id"])
        names.add((item["section"], item["name"]))
        for key, allowed in (("section", SECTIONS), ("level", LEVELS), ("kind", KINDS), ("source", SOURCES), ("screen", SCREENS),
                             ("geography", GEOGRAPHIES)):
            if item[key] not in allowed:
                raise ValueError(f"{where} ({item['id']}): {key} must be one of {', '.join(allowed)}, not {item[key]!r}")
        if (item.get("spec") is None) != (item.get("field") is None) or (item.get("spec") is not None and item["spec"] not in SPECS):
            raise ValueError(f"{where} ({item['id']}): spec and field go together, spec one of {', '.join(SPECS)}")
        if item.get("spec") is not None and item["source"] not in SPECS:
            raise ValueError(f"{where} ({item['id']}): an input with a spec field has that spec as source")
        if not isinstance(item.get("required"), bool):
            raise ValueError(f"{where} ({item['id']}): required must be true or false")
        condition = item.get("condition")
        if condition is not None and (not isinstance(condition, str) or not condition.strip() or item["required"]):
            raise ValueError(f"{where} ({item['id']}): condition is the rule text of a conditional input (required false)")
        if condition is not None:
            rule = CONDITIONS.get(item["id"])
            if rule is None:
                raise ValueError(f"{where} ({item['id']}): a Conditional input needs its rule in inputs.CONDITIONS (the Note: {condition!r})")
            if _note_key(rule[0]) != _note_key(condition):
                raise ValueError(f"{where} ({item['id']}): the condition is now {condition!r} but CONDITIONS implements {rule[0]!r}: update the rule")
        for key in ("explanation", "unit", "note"):
            if item.get(key) is not None and not isinstance(item[key], str):
                raise ValueError(f"{where} ({item['id']}): {key} must be a string or absent")
        policies = item.get("policies", [])
        if not isinstance(policies, list) or any(p not in POLICIES for p in policies) or len(set(policies)) != len(policies):
            raise ValueError(f"{where} ({item['id']}): policies must be a list from {', '.join(POLICIES)} without repeats")
        row = item.get("workbook_row")
        if row is not None and (not isinstance(row, int) or isinstance(row, bool) or row < 1 or (item["section"], row) in rows):
            raise ValueError(f"{where} ({item['id']}): workbook_row must be a positive integer, unique within its section, or null for an input that is not in the workbook")
        if row is not None:
            rows.add((item["section"], row))
        options = item.get("options")
        if item["id"] == LAUNCH_INPUT and (not isinstance(options, list) or set(options) != set(MARKET_SCOPE)):
            raise ValueError(f"{where} ({item['id']}): the launch market options must be {', '.join(MARKET_SCOPE)} (MARKET_SCOPE maps them to geographies)")
        if item["kind"] in _OPTION_KINDS:
            if not isinstance(options, list) or len(options) < 2 or any(not isinstance(v, str) or not v.strip() for v in options):
                raise ValueError(f"{where} ({item['id']}): a {item['kind']} input needs at least two non-empty option strings")
            if len({v.strip() for v in options}) != len(options):
                raise ValueError(f"{where} ({item['id']}): an option value repeats")
        elif options not in (None, []):
            raise ValueError(f"{where} ({item['id']}): a {item['kind']} input has no options")
    return doc["inputs"]


def load_catalogue(path: Path = INPUT_FILE) -> dict:
    """The catalogue (`structx/specs/inputs.json`), validated."""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    check_catalogue(doc)
    return doc


def seed_catalogue(conn: sqlite3.Connection, doc: dict) -> dict[str, int]:
    """Makes `input` / `input_option` hold exactly the catalogue (upsert by `id`, by value within an input; the
    rest deleted; ids kept). Returns the row count per table. Does not commit."""
    inputs = check_catalogue(doc)
    keep: list[int] = []
    for item in inputs:
        cells = {"id": item["id"], "name": item["name"], "category": item["category"], "section": item["section"], "level": item["level"],
                 "kind": item["kind"], "source": item["source"], "screen": item["screen"], "geography": item["geography"], "spec": item.get("spec"),
                 "field": item.get("field"),
                 "required": int(item["required"]), "condition": item.get("condition"), "expected": item["expected"], "source_text": item["source_text"],
                 "explanation": item.get("explanation"), "policies": json.dumps(item.get("policies", [])), "unit": item.get("unit"),
                 "workbook_row": item["workbook_row"]}
        found = conn.execute('SELECT input_id FROM "input" WHERE id = ?', (item["id"],)).fetchone()
        if found:
            input_id = found[0]
            sets = ", ".join(f'"{c}" = ?' for c in _COLUMNS)
            conn.execute(f'UPDATE "input" SET {sets} WHERE input_id = ?', [cells[c] for c in _COLUMNS] + [input_id])
        else:
            cols = ", ".join(f'"{c}"' for c in _COLUMNS)
            marks = ", ".join("?" for _ in _COLUMNS)
            input_id = conn.execute(f'INSERT INTO "input" ({cols}) VALUES ({marks})', [cells[c] for c in _COLUMNS]).lastrowid
        keep.append(input_id)
        values = [v.strip() for v in item.get("options") or []]
        if values:
            marks = ", ".join("?" for _ in values)
            conn.execute(f'DELETE FROM "input_option" WHERE input_id = ? AND value NOT IN ({marks})', [input_id, *values])
        else:
            conn.execute('DELETE FROM "input_option" WHERE input_id = ?', (input_id,))
        for position, value in enumerate(values, start=1):
            if not conn.execute('UPDATE "input_option" SET position = ? WHERE input_id = ? AND value = ?', (position, input_id, value)).rowcount:
                conn.execute('INSERT INTO "input_option" (input_id, position, value) VALUES (?, ?, ?)', (input_id, position, value))
    marks = ", ".join("?" for _ in keep) or "NULL"
    conn.execute(f'DELETE FROM "input_option" WHERE input_id NOT IN ({marks})', keep)
    conn.execute(f'DELETE FROM "input_value" WHERE input_id NOT IN ({marks})', keep)
    conn.execute(f'DELETE FROM "input" WHERE input_id NOT IN ({marks})', keep)
    return {"input": conn.execute('SELECT COUNT(*) FROM "input"').fetchone()[0],
            "input_option": conn.execute('SELECT COUNT(*) FROM "input_option"').fetchone()[0]}


def _rows(conn: sqlite3.Connection, sql: str, *args) -> list[dict]:
    cur = conn.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def input_rows(conn: sqlite3.Connection) -> list[dict]:
    """The catalogue rows in workbook order (the asset sheet, then the competitor sheet; the inputs with no workbook
    row first), `policies` a list, `required` a bool, `options` the values in order."""
    inputs = _rows(conn, 'SELECT * FROM "input" ORDER BY CASE section WHEN ' + "'asset'" + ' THEN 0 ELSE 1 END, workbook_row')
    options: dict[int, list[str]] = {}
    for input_id, value in conn.execute('SELECT input_id, value FROM "input_option" ORDER BY input_id, position'):
        options.setdefault(input_id, []).append(value)
    for row in inputs:
        row["policies"] = json.loads(row["policies"] or "[]")
        row["required"] = bool(row["required"])
        row["options"] = options.get(row["input_id"], [])
    return inputs


def read_catalogue(conn: sqlite3.Connection) -> dict:
    return {"inputs": input_rows(conn)}


def input_by_id(conn: sqlite3.Connection, id: str) -> dict | None:
    return next((r for r in input_rows(conn) if r["id"] == id), None)


# ---------------------------------------------------------------- the launch scope
def launch_scope(conn: sqlite3.Connection, asset_id: int) -> dict:
    """The asset's scope: `launch_market` (its Launch Markets value, or None) and `geographies` (the Geography
    Scope values in scope: Global with US and / or EU; every geography until a market is chosen)."""
    row = conn.execute('SELECT v.value FROM "input_value" v JOIN "input" i ON i.input_id = v.input_id '
                       'WHERE v.asset_id = ? AND i.id = ? AND v.indication_id IS NULL', (asset_id, LAUNCH_INPUT)).fetchone()
    market = json.loads(row[0]) if row else None
    market = market if isinstance(market, str) else None
    return {"launch_market": market, "geographies": list(MARKET_SCOPE.get(market, GEOGRAPHIES))}


def in_scope(row: dict, scope: dict) -> bool:
    """Whether a catalogue row's geography is in `scope` (from `launch_scope`)."""
    return row["geography"] in scope["geographies"]


def scoped_fields(conn: sqlite3.Connection, asset_id: int, spec_name: str) -> list[str]:
    """The ids of `spec_name`'s fields whose input is in the asset's launch scope, in catalogue order: what an
    extraction for the asset should run (the steps' `fields`)."""
    scope = launch_scope(conn, asset_id)
    return [r["field"] for r in input_rows(conn) if r["spec"] == spec_name and in_scope(r, scope)]


# ---------------------------------------------------------------- values
def _num(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and (isinstance(v, int) or math.isfinite(v))


def _series(value: object, what: str) -> list:
    if not isinstance(value, list) or len(value) != 15:
        raise ValueError(f"{what}: a series has exactly 15 entries (Year 1-15)")
    for i, v in enumerate(value):
        if v is not None and (not _num(v) or v < 0):
            raise ValueError(f"{what}: Year {i + 1} must be a number >= 0 or null")
    return list(value)


def check_value(row: dict, value: object) -> object:
    """The value as stored, checked against the input's kind (and options); ValueError with the reason."""
    kind, what = row["kind"], row["id"]
    if kind in ("list", "calc"):
        raise ValueError(f"{what}: a {kind} input is not written")
    if kind == "select":
        if not isinstance(value, str) or value not in row["options"]:
            raise ValueError(f"{what}: must be one of {', '.join(row['options'])}")
        return value
    if kind == "number":
        if not _num(value) or value < 0:
            raise ValueError(f"{what}: must be a number >= 0")
        return value
    if kind == "percent":
        if not _num(value) or not 0 <= value <= 100:
            raise ValueError(f"{what}: must be a number from 0 to 100")
        return value
    if kind == "year":
        if not isinstance(value, int) or isinstance(value, bool) or not 1900 <= value <= 2100:
            raise ValueError(f"{what}: must be a four-digit year")
        return value
    if kind == "year_month":
        if not isinstance(value, dict) or set(value) - {"year", "month"}:
            raise ValueError(f"{what}: must be an object with year and month")
        year, month = value.get("year"), value.get("month")
        if not isinstance(year, int) or isinstance(year, bool) or not 1900 <= year <= 2100:
            raise ValueError(f"{what}: year must be a four-digit year")
        if month is not None and (not isinstance(month, int) or isinstance(month, bool) or not 1 <= month <= 12):
            raise ValueError(f"{what}: month must be 1-12 or null")
        return {"year": year, "month": month}
    if kind == "series":
        return _series(value, what)
    if kind == "series_eu":
        if not isinstance(value, dict) or not value or any(k not in row["options"] for k in value):
            raise ValueError(f"{what}: an object of EU reference countries, each a 15-entry series")
        return {k: _series(v, f"{what} {k}") for k, v in value.items()}
    if kind == "multi_select":
        if not isinstance(value, list) or not value or len(set(value)) != len(value) or any(v not in row["options"] for v in value):
            raise ValueError(f"{what}: a non-empty list of distinct options from the list")
        return list(value)
    raise ValueError(f"{what}: unknown kind {kind!r}")


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value_row(conn: sqlite3.Connection, value_id: int) -> dict:
    row = _rows(conn, 'SELECT v.*, i.id AS input FROM "input_value" v JOIN "input" i ON i.input_id = v.input_id WHERE value_id = ?', value_id)[0]
    row["value"] = json.loads(row["value"])
    return row


def _find_value(conn: sqlite3.Connection, asset_id: int, input_id: int, indication_id: int | None, competitor: str | None):
    return conn.execute('SELECT value_id, source FROM "input_value" WHERE asset_id = ? AND input_id = ? AND COALESCE(indication_id, 0) = ? '
                        'AND COALESCE(competitor, \'\') = ?', (asset_id, input_id, indication_id or 0, competitor or "")).fetchone()


def put_value(conn: sqlite3.Connection, asset_id: int, row: dict, value: object, *, indication_id: int | None = None,
              competitor: str | None = None, user_id: int | None = None) -> dict | None:
    """Writes a user's value for the input `row` (a catalogue row): `entered` when no AI value was there,
    `edited` over one; `value` None deletes the row (returns None). An indication-level input needs
    `indication_id` (one of the asset's); a competitor-section input also needs `competitor` (one of the
    asset's ci competitors); an asset-level input takes neither. ValueError otherwise. Does not commit."""
    if row["level"] == "asset":
        if indication_id is not None or competitor is not None:
            raise ValueError(f"{row['id']}: an asset-level input takes no indication or competitor")
    else:
        if indication_id is None:
            raise ValueError(f"{row['id']}: an indication-level input needs an indication")
        if not conn.execute('SELECT 1 FROM "indication" WHERE indication_id = ? AND asset_id = ?', (indication_id, asset_id)).fetchone():
            raise ValueError(f"{row['id']}: indication {indication_id} is not this asset's")
        if row["section"] == "competitor":
            if not competitor or not conn.execute('SELECT 1 FROM "ci" WHERE asset_id = ? AND competitor = ?', (asset_id, competitor)).fetchone():
                raise ValueError(f"{row['id']}: a competitor input needs one of the asset's competitors")
        elif competitor is not None:
            raise ValueError(f"{row['id']}: not a competitor input")
    if row["kind"] in ("list", "calc"):
        raise ValueError(f"{row['id']}: a {row['kind']} input is not entered by hand")
    found = _find_value(conn, asset_id, row["input_id"], indication_id, competitor)
    if value is None:
        if found:
            conn.execute('DELETE FROM "input_value" WHERE value_id = ?', (found[0],))
        return None
    stored = json.dumps(check_value(row, value), ensure_ascii=False)
    if found:
        status = "edited" if found[1] != "user" else conn.execute('SELECT status FROM "input_value" WHERE value_id = ?', (found[0],)).fetchone()[0]
        conn.execute('UPDATE "input_value" SET value = ?, source = ?, status = ?, confidence = NULL, confidence_reason = NULL, '
                     'citations = NULL, document_id = NULL, user_id = ?, updated_at = ? WHERE value_id = ?',
                     (stored, "user", status, user_id, _now(), found[0]))
        return _value_row(conn, found[0])
    value_id = conn.execute('INSERT INTO "input_value" (asset_id, input_id, indication_id, competitor, value, source, status, user_id, updated_at) '
                            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', (asset_id, row["input_id"], indication_id, competitor, stored, "user", "entered", user_id, _now())).lastrowid
    return _value_row(conn, value_id)


def read_values(conn: sqlite3.Connection, asset_id: int) -> list[dict]:
    """The asset's value rows (`value` parsed, `input` = the catalogue id), in value_id order."""
    out = _rows(conn, 'SELECT v.*, i.id AS input FROM "input_value" v JOIN "input" i ON i.input_id = v.input_id WHERE v.asset_id = ? ORDER BY v.value_id', asset_id)
    for r in out:
        r["value"] = json.loads(r["value"])
    return out


def _as_value(row: dict, cell: object) -> object | None:
    """The input_value for a cdp / ci / protocol cell (the JSON text of the table) by the input's kind, or None."""
    if cell is None:
        return None
    try:
        parsed = json.loads(cell) if isinstance(cell, str) and cell[:1] in "[{" else cell
    except ValueError:
        parsed = cell
    if isinstance(parsed, list):
        parsed = parsed[0] if parsed else None
    kind = row["kind"]
    if kind == "year_month":
        return parsed if isinstance(parsed, dict) and isinstance(parsed.get("year"), int) else None
    if kind == "year":
        return parsed["year"] if isinstance(parsed, dict) and isinstance(parsed.get("year"), int) else None
    if kind in ("select", "multi_select"):
        text = str(parsed).strip().lower()
        option = next((o for o in row["options"] if o.lower() == text), None)
        return [option] if option is not None and kind == "multi_select" else option
    return None


def _uploader(conn: sqlite3.Connection, document_id: int | None) -> int | None:
    """The user who uploaded a document (`document.uploaded_by`), or None without a document (or its table)."""
    if document_id is None or not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'document'").fetchone():
        return None
    row = conn.execute('SELECT uploaded_by FROM "document" WHERE document_id = ?', (document_id,)).fetchone()
    return row[0] if row else None


def copy_extracted(conn: sqlite3.Connection, asset_id: int, spec_name: str, *, document_id: int | None = None) -> int:
    """Replaces every value row of `spec_name`'s inputs for the asset (a user's `entered` and `edited` rows
    included: a fresh extraction supersedes them) with the cells of its table (cdp: per indication; ci: per
    competitor and indication; protocol: per indication, the first study row wins). An asset-level input
    (mode of administration) takes the first row's cell once, with no indication: a program-wide value is the
    same on every row. Every row carries `document_id` and, as `user_id`, the user who uploaded that document
    (`document.uploaded_by`; null when no document is given). Returns the row count written. Does not commit."""
    inputs = [r for r in input_rows(conn) if r["spec"] == spec_name and r["kind"] != "list"]
    uploaded_by = _uploader(conn, document_id)
    if not inputs:
        return 0
    marks = ", ".join("?" for _ in inputs)
    conn.execute(f'DELETE FROM "input_value" WHERE asset_id = ? AND input_id IN ({marks})', [asset_id, *(r["input_id"] for r in inputs)])
    table = spec_name
    rows = _rows(conn, f'SELECT * FROM "{table}" WHERE asset_id = ? ORDER BY row_id', asset_id)
    seen: set[tuple] = set()
    count = 0
    for src in rows:
        competitor = src["competitor"] if spec_name == "ci" else None
        key = (src["indication_id"], competitor)
        if spec_name == "protocol" and key in seen:
            continue
        seen.add(key)
        for r in inputs:
            asset_level = r["level"] == "asset"
            if asset_level and ("asset", r["input_id"]) in seen:
                continue
            value = _as_value(r, src.get(r["field"]))
            if value is None:
                continue
            try:
                value = check_value(r, value)
            except ValueError:
                continue   # an out-of-range cell is skipped, not stored
            conn.execute('INSERT INTO "input_value" (asset_id, input_id, indication_id, competitor, value, source, status, confidence, '
                         'confidence_reason, citations, document_id, user_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                         (asset_id, r["input_id"], None if asset_level else src["indication_id"], None if asset_level else competitor,
                          json.dumps(value, ensure_ascii=False), spec_name, "ai_extracted", src.get(f"{r['field']}_confidence"),
                          src.get(f"{r['field']}_confidence_reason"), src.get(f"{r['field']}_citations"), document_id, uploaded_by, _now()))
            if asset_level:
                seen.add(("asset", r["input_id"]))
            count += 1
    return count


def coverage(conn: sqlite3.Connection, asset_id: int) -> dict:
    """The mock's coverage: over the document-sourced inputs (cdp, ci, protocol, forecast; asset-level once,
    indication-level per included indication, competitor-level per ci competitor and indication), how many
    have a value; plus `missing`: the inputs required now (the workbook's Required, or a Conditional whose rule
    holds; any source but calc) without a value, and `required_now`: the slots where a Conditional input's rule
    holds (the UI marks them). Only the inputs in the asset's launch scope count (`launch_scope`)."""
    have_include = any(r[1] == "include" for r in conn.execute('PRAGMA table_info("indication")'))   # the column arrives with Task 3
    if have_include:
        sql = 'SELECT indication_id FROM "indication" WHERE asset_id = ? AND include = 1 ORDER BY position, indication_id'
    else:
        sql = 'SELECT indication_id FROM "indication" WHERE asset_id = ? ORDER BY indication_id'
    inds = [r[0] for r in conn.execute(sql, (asset_id,))]
    comps = [tuple(r) for r in conn.execute('SELECT DISTINCT competitor, indication_id FROM "ci" WHERE asset_id = ? ORDER BY row_id', (asset_id,))]
    rows = read_values(conn, asset_id)
    present = {(r["input_id"], r["indication_id"], r["competitor"]) for r in rows}
    values = Values(rows, inds)
    scope = launch_scope(conn, asset_id)
    total = covered = 0
    by: dict[str, int] = {}
    missing: list[dict] = []
    holds: list[dict] = []
    for r in input_rows(conn):
        if r["kind"] in ("list", "calc") or not in_scope(r, scope):
            continue
        if r["section"] == "competitor":
            slots = [(c, i) for c, i in comps if i in inds]
        elif r["level"] == "indication":
            slots = [(None, i) for i in inds]
        else:
            slots = [(None, None)]
        for competitor, ind in slots:
            has = (r["input_id"], ind, competitor) in present
            if r["source"] in ("cdp", "ci", "protocol", "forecast"):
                total += 1
                if has:
                    covered += 1
                    by[r["source"]] = by.get(r["source"], 0) + 1
            required = required_now(r, values, ind, competitor)
            if required and r["condition"]:
                holds.append({"input": r["id"], "indication_id": ind, "competitor": competitor})
            if required and not has:
                missing.append({"input": r["id"], "name": r["name"], "screen": r["screen"], "indication_id": ind, "competitor": competitor})
    return {"total": total, "covered": covered, "pct": round(covered * 100 / total) if total else 0, "by": by, "missing": missing,
            "required_now": holds}
