"""The database: the indications of the CDP as a table, and one table per
file with its attributes for those indications, each attribute with its
confidence label, confidence reason and citations in the same table.
The loader writes a combined result (`combine.combine`) into a SQLite
file next to the JSON. Pure code: no gateway call. Written by
`workflow.py`; `python -m structx.join.database` prints the DDL (with
`--write docs/schema.sql` it writes it, which is how the tracked copy is
kept in step -- `testing/test_database.py` checks that).

The tables (SQLite dialect; for another engine replace `INTEGER PRIMARY
KEY` with its identity column), column names = the keys of the combined
JSON:

    asset        the asset the UI creates (name = the code, asset_name, stage,
                 owner_user_id, created_by, created_at, updated_at) plus its extraction columns
                 (cdp_file, null until a run, ci_file, protocol_file, model, extracted_at,
                 seconds, aliases)
    indication   one row per indication (`indication` never null): include (1 = considered),
                 source (cdp from the extraction, user when added by hand), position (1-based
                 order), then confidence, confidence_reason, citations (null for user rows)
    cdp          one row per indication: the asset's own values --
                 indication_id, indication, then per field <field>, <field>_confidence,
                 <field>_confidence_reason, <field>_citations
    ci           one row per competitor and indication: indication_id, indication,
                 competitor (+ its confidence / reason / citations), ci_indication (the
                 deck's own wording), mapping_confidence, mapping_confidence_reason,
                 then the fields as in cdp
    protocol     one row per study and indication: the same with study and protocol_indication
    file_type, document, document_file_type  the upload slots' four file types (CI, Protocol, CDP, Forecast; seeded) and the documents attached to an asset with their types (documents.py; a re-extraction keeps them)
    input, input_option, input_value  the workbook's inputs (how each is edited, which document answers it, which page owns it) and an asset's values for them, seeded from structx/specs/inputs.json (inputs.py; the catalogue is not keyed by asset)

The fields are the records fields of each spec, in spec order (`<field>`
= the field id, the key in the JSON: `phase_of_development`,
`us_launch_timing`, ...). The `<field>` cell holds the
answer: the **date** when the field has a date sub-field (`timing`,
`submission_timing`): the JSON `date` exactly as the output wrote it,
the parsed `{"year": 2031, "month": 5}` object (`month` null when the
file gave no month, e.g. a quarter, or the spec parses the year only;
both null when it could not be read) -- or, when the file states more
than one date, the list of those objects, the chosen one first; else the
category when the answer sub-field has options (`Phase III`,
`Placebo-controlled`), else the value as written; null when the file did
not give it. Likewise a text `value` that is a list (conflicts) is kept
as a JSON list, the chosen one first, never only the first. The answer sub-field is the date one, else the
one with options, else the first (`answer_sub`); the other sub-fields
(milestone, comparator, basis, the MAA `pursuing_maa` wording) stay in
the JSON and HTML. `<field>_confidence` is that value's label (`high` / `medium` /
`low`, the model's label lowered by the local checks);
`<field>_confidence_reason` is the model's own reason for the label,
then the local checks that lowered it in brackets and any other value the
file states elsewhere (`[lowered by checks: ...; also stated: ... (p.N)]`);
`<field>_citations` is a JSON object `{"citations": [{page, quote}],
"conflicts": [{page, quote, value}]}`: where the value was cited from
(`page` = the slide or page) and, under `conflicts`, where each other
value the file states is cited from and what it says (empty when there
is none). The same three columns follow
`indication`, `competitor`, `study`, and the mapping (`mapping_confidence`
= how surely the competitor's / study's own indication wording is this
CDP indication, `combine.map_record`).

Only the join is loaded: a competitor or study record the model could not
attach to a CDP indication, a duplicate, or a record that matched no key
is not in the database (the JSON and HTML keep them, with the reason).

`write_sqlite` recreates its SQLite file on every run (it is an output,
like the JSON). The pieces it is made of work on a database that keeps
many assets: `ensure_schema` (CREATE TABLE IF NOT EXISTS, then the input
catalogue seeded from structx/specs/inputs.json), `load_cdp`
(keeps an existing asset row -- ids, owner -- and replaces its
indication + cdp rows; deletes its ci / protocol rows and its
indication-level input values, their indication ids are gone; inserts
the asset row only when it is new) and `load_joined` (replaces one
asset's ci or protocol rows, indication ids looked up by name);
`asset_indications` and `read_asset` read one asset back. What the API
edits: `asset_row`, `create_asset`, `update_asset`, `list_assets`,
`add_indication`, `update_indication`, `delete_indication` (their refusals are typed:
`AssetTaken`, `IndicationNotFound`, `NameTaken`, `ProtectedIndication`, all
`ValueError`s). The API
(`api/`) uses all of these on its shared database.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

from structx.join.combine import _JOINED, text_of
from structx.join.documents import DOCUMENT_TABLES, seed_file_types
from structx.join.inputs import INPUT_INDEXES, INPUT_TABLES, load_catalogue, seed_catalogue
from structx.join.users import USER_TABLES, seed_roles
from structx.settings.spec import FieldSpec, Spec


class AssetTaken(ValueError):
    """The asset code exists."""


class IndicationNotFound(ValueError):
    """The indication is not this asset's."""


class NameTaken(ValueError):
    """The indication name is already listed."""


class ProtectedIndication(ValueError):
    """An extracted indication is unticked, not deleted."""

_TABLES = ("cdp", *_JOINED)   # the attribute tables, in run order
_ASSET_TABLES = ("indication", *_TABLES)   # the tables keyed by asset_id (the input catalogue tables are not)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row(conn: sqlite3.Connection, sql: str, *args) -> dict | None:
    cur = conn.execute(sql, args)
    r = cur.fetchone()
    return dict(zip([d[0] for d in cur.description], r)) if r else None


def workbook_row(f: FieldSpec) -> int | None:
    """The input workbook row a field answers, from its `workbook_input` note ("row 10: Phase of Development ...")."""
    m = re.search(r"\brows?\s+(\d+)", f.workbook_input)
    return int(m.group(1)) if m else None


def _records_fields(spec: Spec) -> list[FieldSpec]:
    return [f for f in spec.fields if f.kind == "records"]


def _record_subs(spec: Spec, f: FieldSpec) -> list[FieldSpec]:
    """A records field's sub-fields without the key and the indication (those are the row's key and indication)."""
    return [s for s in f.fields if s.id not in (spec.primary_key[1], "indication")]


def answer_sub(spec: Spec, f: FieldSpec) -> FieldSpec:
    """The sub-field whose value is the field's answer: the date, else the one with options, else the first."""
    subs = _record_subs(spec, f)
    return next((s for s in subs if s.parts), None) or next((s for s in subs if s.options), None) or subs[0]


def answers(sub: FieldSpec, node: dict) -> list:
    """Every value the JSON has for a cited value, the chosen one first: `value` is a list when the file
    stated more than one. Per value: the option (the chosen value only) for a sub-field with options, else
    the value as written. A date sub-field's answer is its JSON `date` instead: see `_value_cells`."""
    values = node.get("value")
    values = values if isinstance(values, list) else [values]
    return [(node.get("option") or v) if sub.options and i == 0 else v for i, v in enumerate(values)]


def _value_columns(name: str) -> list[tuple[str, str]]:
    return [(name, "TEXT"), (f"{name}_confidence", "TEXT"), (f"{name}_confidence_reason", "TEXT"), (f"{name}_citations", "TEXT")]


def _reason(node: dict, *, reason: str | None = None, checks: list | None = None) -> str | None:
    """The model's own reason for the label, then the checks that lowered it and the other values the file states."""
    why = re.sub(r"\s+", " ", str(reason if reason is not None else node.get("label_reason") or "")).strip()
    notes = []
    codes = [str(c) for c in (checks if checks is not None else node.get("confidence_reasons") or [])]
    if codes:
        notes.append("lowered by checks: " + ", ".join(codes))
    for c in node.get("conflicts") or []:
        if isinstance(c, dict):
            notes.append(f"also stated: {c.get('value')!r} (p.{c.get('page')})")
    if notes:
        why += (" " if why else "") + "[" + "; ".join(notes) + "]"
    return why or None


def _citations(node: dict) -> str | None:
    """The value's citations and the conflicts' own, as one JSON object: {"citations": [{page, quote}],
    "conflicts": [{page, quote, value}]} (the conflicts: where each other stated value is, and what it says);
    None when the value has neither."""
    cites = [{"page": c.get("page"), "quote": c.get("quote")} for c in node.get("citations") or [] if isinstance(c, dict)]
    conflicts = [{"page": c.get("page"), "quote": c.get("quote"), "value": c.get("value")}
                 for c in node.get("conflicts") or [] if isinstance(c, dict)]
    return _json({"citations": cites, "conflicts": conflicts}) if cites or conflicts else None


def _value_cells(name: str, node: dict | None, *, sub: FieldSpec | None = None) -> dict:
    """The four cells of one cited value: the value(s) (`answers` when `sub` is given, else as written; a JSON
    list when the JSON has a list, chosen value first), label, reason, citations. A date sub-field's cell is
    the JSON `date` exactly as written there: the `{year, month}` object, or the list of them (chosen first)."""
    if not isinstance(node, dict):
        return {}
    if sub is not None and sub.parts and "date" in node:
        cell = _json(node["date"])
    else:
        if sub is not None:
            vals = answers(sub, node)
        else:
            written = node.get("value")
            vals = written if isinstance(written, list) else [written]
        cell = vals[0] if len(vals) == 1 else _json(vals)
    return {name: cell, f"{name}_confidence": node.get("confidence"),
            f"{name}_confidence_reason": _reason(node), f"{name}_citations": _citations(node)}


def _key_of(spec: Spec) -> str:
    return spec.primary_key[1]


def _check_specs(specs: dict[str, Spec]) -> None:
    if "cdp" not in specs or specs["cdp"].primary_key is None:
        raise ValueError("the schema needs the cdp spec (keyed by indication)")
    for name in specs:
        if name not in _TABLES:
            raise ValueError(f"no table layout for a spec named {name!r}: only cdp, ci, protocol")
        if specs[name].primary_key is None:
            raise ValueError(f"spec {name!r} has no primary_key; the schema is keyed by it")


def table_columns(name: str, spec: Spec) -> list[tuple[str, str]]:
    """An attribute table's columns (name, SQL type): the indication, the key and mapping (ci / protocol), the fields."""
    cols = [("row_id", "INTEGER PRIMARY KEY"), ("asset_id", "INTEGER NOT NULL REFERENCES asset(asset_id)"),
            ("indication_id", "INTEGER NOT NULL REFERENCES indication(indication_id)"), ("indication", "TEXT NOT NULL")]
    if name != "cdp":
        key = _key_of(spec)
        cols += [(key, "TEXT NOT NULL")] + _value_columns(key)[1:]
        cols += [(_JOINED[name].wording, "TEXT"), ("mapping_confidence", "TEXT"), ("mapping_confidence_reason", "TEXT")]
    for f in _records_fields(spec):
        cols += _value_columns(f.id)
    return cols


def tables(specs: dict[str, Spec]) -> dict[str, list[tuple[str, str]]]:
    """Every table's columns for these specs: asset, indication, one attribute table per spec, then the
    input tables (`inputs.INPUT_TABLES`, the same whatever the specs)."""
    _check_specs(specs)
    out = {"asset": [
        ("asset_id", "INTEGER PRIMARY KEY"), ("name", "TEXT NOT NULL UNIQUE"), ("asset_name", "TEXT"), ("stage", "TEXT"), ("owner_user_id", "INTEGER REFERENCES user(user_id)"),
        ("created_by", "INTEGER REFERENCES user(user_id)"), ("created_at", "TEXT"), ("updated_at", "TEXT"),
        ("cdp_file", "TEXT"), ("ci_file", "TEXT"), ("protocol_file", "TEXT"),
        ("model", "TEXT"), ("extracted_at", "TEXT"), ("seconds", "REAL"), ("aliases", "TEXT"),
    ], "indication": [
        ("indication_id", "INTEGER PRIMARY KEY"), ("asset_id", "INTEGER NOT NULL REFERENCES asset(asset_id)"),
        ("indication", "TEXT NOT NULL"), ("include", "INTEGER NOT NULL DEFAULT 1"), ("source", "TEXT NOT NULL DEFAULT 'cdp'"),
        ("position", "INTEGER"), ("confidence", "TEXT"), ("confidence_reason", "TEXT"), ("citations", "TEXT"),
    ]}
    for name in _TABLES:
        if name in specs:
            out[name] = table_columns(name, specs[name])
    out.update(USER_TABLES)
    out.update(DOCUMENT_TABLES)
    out.update(INPUT_TABLES)
    return out


def _create(name: str, columns: list[tuple[str, str]]) -> str:
    body = ",\n".join(f'    "{c}" {t}' if t else f'    {c}' for c, t in columns)   # empty type: a table constraint, unquoted
    return f'CREATE TABLE "{name}" (\n{body}\n);\n'


def ddl(specs: dict[str, Spec]) -> str:
    """The schema for these specs (`cdp` required; `ci`, `protocol` optional) and the input tables, as SQL."""
    layout = tables(specs)
    parts = ["-- Generated by structx.join.database.ddl() from structx/specs/" + ", ".join(f"{n}.json" for n in specs) + "; the input",
             "-- tables are seeded from structx/specs/inputs.json (structx.join.inputs).",
             "-- Do not edit: run `python -m structx.join.database --write docs/schema.sql` after a spec change.",
             "-- SQLite dialect (INTEGER PRIMARY KEY = row id); see the module docstring for the layout.", ""]
    parts += [_create(name, cols) for name, cols in layout.items()]
    parts += [index + "\n" for index in INPUT_INDEXES]
    return "\n".join(parts)


# ----------------------------------------------------------------- the loader
def _insert(conn: sqlite3.Connection, table: str, row: dict) -> int:
    cols = ", ".join(f'"{c}"' for c in row)
    marks = ", ".join("?" for _ in row)
    return conn.execute(f'INSERT INTO "{table}" ({cols}) VALUES ({marks})', list(row.values())).lastrowid


def _record_cells(spec: Spec, records: dict) -> dict:
    """The field cells of one entry (`records` maps a field id to its record, a dict of sub-field values)."""
    out = {}
    for f in _records_fields(spec):
        rec = records.get(f.id)
        if not isinstance(rec, dict):
            continue
        sub = answer_sub(spec, f)
        node = rec.get(sub.id)
        if isinstance(node, str):
            node = {"value": node}
        if isinstance(node, dict):
            out.update(_value_cells(f.id, node, sub=sub))
    return out


def ensure_schema(conn: sqlite3.Connection, specs: dict[str, Spec], catalogue: dict | None = None) -> None:
    """Creates the tables of `tables(specs)` that the database does not have yet (CREATE TABLE IF NOT EXISTS),
    drops the superseded manual_input tables, and seeds the input catalogue (`catalogue`, else
    `structx/specs/inputs.json`; `inputs.seed_catalogue`: an upsert, ids kept). Does not commit."""
    for name, cols in tables(specs).items():
        conn.execute(_create(name, cols).replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1))
    for index in INPUT_INDEXES:
        conn.execute(index.replace("CREATE UNIQUE INDEX", "CREATE UNIQUE INDEX IF NOT EXISTS", 1))
    for old in ("manual_input_option", "manual_input"):
        conn.execute(f'DROP TABLE IF EXISTS "{old}"')
    seed_roles(conn)
    seed_file_types(conn)
    seed_catalogue(conn, catalogue if catalogue is not None else load_catalogue())


def _asset_id(conn: sqlite3.Connection, asset: str) -> int | None:
    row = conn.execute('SELECT asset_id FROM "asset" WHERE name = ?', (asset,)).fetchone()
    return row[0] if row else None


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def load_cdp(conn: sqlite3.Connection, cdp_output: dict, spec: Spec, *, asset: str, files: dict,
             seconds: float | None = None, aliases: dict | None = None) -> dict[str, int]:
    """Replaces the asset's extraction: keeps its asset row (ids, owner;
    inserts one when it is new), replaces the indication + cdp rows, deletes
    its ci / protocol rows and every indication-level input value (their
    indication ids are gone). `files` = {"cdp": name, "ci": name or None,
    "protocol": name or None}; the indications come from
    `cdp_output["indications"]` (a cdp extraction output, or the combined
    result's entries). Returns {"asset": 1, "indication": n, "cdp": n}.
    Does not commit."""
    asset = asset.strip()
    if not asset:
        raise ValueError("load_cdp: the asset name is empty")
    src = cdp_output["source"]
    old = _asset_id(conn, asset)
    extraction = {"cdp_file": files["cdp"], "ci_file": files.get("ci"), "protocol_file": files.get("protocol"),
                  "model": src.get("model"), "extracted_at": src.get("extracted_at"),
                  "seconds": src.get("seconds") if seconds is None else seconds, "aliases": _json(aliases or {}), "updated_at": _now()}
    if old is not None:
        present = _existing_tables(conn)
        for t in [*(t for t in reversed(_TABLES) if t in present), "indication"]:
            conn.execute(f'DELETE FROM "{t}" WHERE asset_id = ?', (old,))
        if "input_value" in present:
            conn.execute('DELETE FROM "input_value" WHERE asset_id = ? AND indication_id IS NOT NULL', (old,))
        sets = ", ".join(f'"{c}" = ?' for c in extraction)
        conn.execute(f'UPDATE "asset" SET {sets} WHERE asset_id = ?', [*extraction.values(), old])
        asset_id = old
    else:
        asset_id = _insert(conn, "asset", {"name": asset, "asset_name": asset, "created_at": _now(), **extraction})
    counts = {"asset": 1, "indication": 0, "cdp": 0}
    for e in cdp_output["indications"]:
        node = e["indication"]
        name = text_of(node["value"]).strip()
        if not name:
            continue   # an indication is never null
        ind_id = _insert(conn, "indication", {"asset_id": asset_id, "indication": name, "include": 1, "source": "cdp", "position": counts["indication"] + 1,
                                              "confidence": node.get("confidence"), "confidence_reason": _reason(node), "citations": _citations(node)})
        counts["indication"] += 1
        _insert(conn, "cdp", {"asset_id": asset_id, "indication_id": ind_id, "indication": name, **_record_cells(spec, e)})
        counts["cdp"] += 1
    return counts


def load_joined(conn: sqlite3.Connection, combined: dict, name: str, spec: Spec, *, asset: str) -> int:
    """Replaces the asset's rows of the table `name` ("ci" / "protocol")
    with the records of `combined` (combine.combine's result holding that
    run) mapped to its indications, `indication_id` looked up by name in
    the asset's `indication` rows (the first row when a name repeats);
    sets `asset.<name>_file`. Returns the row count. ValueError when the
    asset is not in the database or an indication of `combined` is not
    one of its rows. Does not commit."""
    if name not in _JOINED:
        raise ValueError(f"load_joined: no joined table named {name!r}: only {', '.join(_JOINED)}")
    j = _JOINED[name]
    asset = asset.strip()
    asset_id = _asset_id(conn, asset)
    if asset_id is None:
        raise ValueError(f"load_joined: asset {asset!r} is not in the database (load_cdp first)")
    ids: dict[str, int] = {}
    for ind_id, ind in conn.execute('SELECT indication_id, indication FROM "indication" WHERE asset_id = ? ORDER BY indication_id', (asset_id,)):
        ids.setdefault(ind, ind_id)
    conn.execute(f'DELETE FROM "{name}" WHERE asset_id = ?', (asset_id,))
    conn.execute(f'UPDATE "asset" SET "{name}_file" = ? WHERE asset_id = ?', ((combined["source"].get(name) or {}).get("file"), asset_id))
    # The cited key values (competitors / studies), by name.
    keys = {text_of(node["value"]).strip(): node for node in combined[j.list_id]}
    count = 0
    for e in combined["indications"]:
        ind = text_of(e["indication"]["value"]).strip()
        if not ind:
            continue
        if ind not in ids:
            raise ValueError(f"load_joined: indication {ind!r} is not in the database for asset {asset!r}")
        for entry in e[j.list_id]:
            key = text_of(entry[j.key]["value"]).strip()
            mapping = entry["mapping"]
            row = {"asset_id": asset_id, "indication_id": ids[ind], "indication": ind,
                   **_value_cells(j.key, keys.get(key, {"value": key})),
                   j.wording: mapping.get(j.wording), "mapping_confidence": mapping.get("confidence"),
                   "mapping_confidence_reason": _reason({}, reason=mapping.get("label_reason"), checks=mapping.get("confidence_reasons") or []),
                   **_record_cells(spec, entry)}
            row[j.key] = key
            _insert(conn, name, row)
            count += 1
    return count


def asset_indications(conn: sqlite3.Connection, asset: str, *, included_only: bool = False) -> list[str]:
    """The asset's indication names, distinct, in indication_id order (only the ticked ones with `included_only`); [] when it is not in the database."""
    rows = conn.execute('SELECT i.indication FROM "indication" i JOIN "asset" a ON a.asset_id = i.asset_id '
                        'WHERE a.name = ?' + (' AND i.include = 1' if included_only else '') + ' ORDER BY i.indication_id', (asset.strip(),)).fetchall()
    return list(dict.fromkeys(r[0] for r in rows))


# ---------------------------------------------------------------- assets and indications the UI edits
def asset_row(conn: sqlite3.Connection, code: str) -> dict | None:
    return _row(conn, 'SELECT * FROM "asset" WHERE name = ?', code.strip())


def touch_asset(conn: sqlite3.Connection, asset_id: int) -> None:
    conn.execute('UPDATE "asset" SET updated_at = ? WHERE asset_id = ?', (_now(), asset_id))


def create_asset(conn: sqlite3.Connection, code: str, *, asset_name: str | None = None, user_id: int | None = None) -> dict:
    """A new asset row with no extraction yet; ValueError when the code is taken. Does not commit."""
    code = code.strip()
    if asset_row(conn, code) is not None:
        raise AssetTaken(f"asset {code!r} exists")
    now = _now()
    asset_id = _insert(conn, "asset", {"name": code, "asset_name": (asset_name or "").strip() or code,
                                       "owner_user_id": user_id, "created_by": user_id, "created_at": now, "updated_at": now})
    return _row(conn, 'SELECT * FROM "asset" WHERE asset_id = ?', asset_id)


_ASSET_FIELDS = ("asset_name", "stage", "owner_user_id")


def update_asset(conn: sqlite3.Connection, asset_id: int, **fields) -> dict:
    """Sets any of asset_name, stage, owner_user_id; ValueError for another field or an unknown asset."""
    bad = set(fields) - set(_ASSET_FIELDS)
    if bad:
        raise ValueError(f"update_asset: not an editable field: {sorted(bad)}")
    if fields:
        sets = ", ".join(f'"{c}" = ?' for c in fields)
        if not conn.execute(f'UPDATE "asset" SET {sets}, updated_at = ? WHERE asset_id = ?', [*fields.values(), _now(), asset_id]).rowcount:
            raise ValueError(f"update_asset: no asset {asset_id}")
    row = _row(conn, 'SELECT * FROM "asset" WHERE asset_id = ?', asset_id)
    if row is None:
        raise ValueError(f"update_asset: no asset {asset_id}")
    return row


def list_assets(conn: sqlite3.Connection, asset_ids: list[int] | None = None) -> list[dict]:
    """Every asset (or those ids) by code, with the owner's name and the document / indication counts."""
    where, args = "", []
    if asset_ids is not None:
        if not asset_ids:
            return []
        where = f"WHERE a.asset_id IN ({', '.join('?' for _ in asset_ids)})"
        args = list(asset_ids)
    present = _existing_tables(conn)
    docs = '(SELECT COUNT(*) FROM "document" d WHERE d.asset_id = a.asset_id)' if "document" in present else "0"
    owner = '(SELECT u.name FROM "user" u WHERE u.user_id = a.owner_user_id)' if "user" in present else "NULL"
    cur = conn.execute(f'SELECT a.*, {owner} AS owner, {docs} AS documents, '
                       f'(SELECT COUNT(*) FROM "indication" i WHERE i.asset_id = a.asset_id) AS indications FROM "asset" a {where} ORDER BY a.name', args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _indication(conn: sqlite3.Connection, asset_id: int, indication_id: int) -> dict:
    row = _row(conn, 'SELECT * FROM "indication" WHERE indication_id = ? AND asset_id = ?', indication_id, asset_id)
    if row is None:
        raise IndicationNotFound(f"indication {indication_id} is not asset {asset_id}'s")
    return row


def _check_name(conn: sqlite3.Connection, asset_id: int, name: str, *, except_id: int | None = None) -> str:
    name = re.sub(r"\s+", " ", name or "").strip()
    if not name or len(name) > 80:
        raise ValueError("an indication name is 1 to 80 characters")
    clash = conn.execute('SELECT indication_id FROM "indication" WHERE asset_id = ? AND indication = ? AND indication_id IS NOT ?', (asset_id, name, except_id)).fetchone()
    if clash:
        raise NameTaken(f"indication {name!r} is already listed")
    return name


def add_indication(conn: sqlite3.Connection, asset_id: int, name: str) -> dict:
    """A user-added indication (source user, included, last position). Does not commit."""
    name = _check_name(conn, asset_id, name)
    position = (conn.execute('SELECT COALESCE(MAX(position), 0) FROM "indication" WHERE asset_id = ?', (asset_id,)).fetchone()[0] or 0) + 1
    ind_id = _insert(conn, "indication", {"asset_id": asset_id, "indication": name, "include": 1, "source": "user", "position": position})
    touch_asset(conn, asset_id)
    return _indication(conn, asset_id, ind_id)


def update_indication(conn: sqlite3.Connection, asset_id: int, indication_id: int, *, name: str | None = None, include: bool | None = None) -> dict:
    """Renames (the cdp / ci / protocol rows follow) and / or ticks an indication. Does not commit."""
    _indication(conn, asset_id, indication_id)   # the ownership check
    if name is not None:
        name = _check_name(conn, asset_id, name, except_id=indication_id)
        conn.execute('UPDATE "indication" SET indication = ? WHERE indication_id = ?', (name, indication_id))
        for t in _TABLES:
            if t in _existing_tables(conn):
                conn.execute(f'UPDATE "{t}" SET indication = ? WHERE indication_id = ?', (name, indication_id))
    if include is not None:
        conn.execute('UPDATE "indication" SET include = ? WHERE indication_id = ?', (1 if include else 0, indication_id))
    touch_asset(conn, asset_id)
    return _indication(conn, asset_id, indication_id)


def delete_indication(conn: sqlite3.Connection, asset_id: int, indication_id: int) -> None:
    """Deletes a user-added indication and its input values; an AI one is unticked instead (ValueError). Does not commit."""
    row = _indication(conn, asset_id, indication_id)
    if row["source"] != "user":
        raise ProtectedIndication("an extracted indication is unticked, not deleted")
    if "input_value" in _existing_tables(conn):
        conn.execute('DELETE FROM "input_value" WHERE indication_id = ?', (indication_id,))
    conn.execute('DELETE FROM "indication" WHERE indication_id = ?', (indication_id,))
    touch_asset(conn, asset_id)


def read_asset(conn: sqlite3.Connection, asset: str, specs: dict[str, Spec]) -> dict | None:
    """The asset's rows as dicts (column name -> cell): {"asset": row,
    "indication": [rows], "cdp": [rows], ...} for every table of
    `tables(specs)` the database has, each in primary-key order; None when
    the asset is not in the database."""
    def rows(sql: str, *args) -> list[dict]:
        cur = conn.execute(sql, args)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    found = rows('SELECT * FROM "asset" WHERE name = ?', asset.strip())
    if not found:
        return None
    out = {"asset": found[0]}
    present = _existing_tables(conn)
    for t, cols in tables(specs).items():
        if t in _ASSET_TABLES and t in present:
            out[t] = rows(f'SELECT * FROM "{t}" WHERE asset_id = ? ORDER BY "{cols[0][0]}"', found[0]["asset_id"])
    return out


def write_sqlite(combined: dict, specs: dict[str, Spec], path: Path, *, asset: str) -> dict[str, int]:
    """Writes `combined` (combine.combine's result, from the runs of these
    `specs`) to a fresh SQLite file at `path` for the asset named `asset`.
    Returns the row count per table. `load_cdp` and `load_joined` do the work; use
    them directly on a database that keeps more than one run."""
    src = combined["source"]
    present = {n: j for n, j in _JOINED.items() if src.get(n) is not None}
    for name in ("cdp", *present):
        if name not in specs:
            raise ValueError(f"write_sqlite: the combined result has a {name} run but no {name} spec was given")
    used = {n: s for n, s in specs.items() if n == "cdp" or n in present}
    path = Path(path)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(ddl(used))
        seed_catalogue(conn, load_catalogue())
        files = {"cdp": src["cdp"]["file"], **{n: src[n]["file"] for n in present}}
        counts = load_cdp(conn, {"source": src["cdp"], "indications": combined["indications"]}, used["cdp"],
                          asset=asset, files=files, seconds=src.get("seconds"), aliases=src.get("aliases"))
        for n in present:
            counts[n] = load_joined(conn, combined, n, used[n], asset=asset)
        conn.commit()
        return counts
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    from structx.settings.spec import load_spec

    parser = argparse.ArgumentParser(description="Print (or write) the database schema generated from structx/specs/cdp.json, ci.json and protocol.json.")
    parser.add_argument("--write", type=Path, help="write the DDL to this file instead of printing it (docs/schema.sql)")
    args = parser.parse_args(argv)
    text = ddl({name: load_spec(name) for name in ("cdp", "ci", "protocol")})
    if args.write:
        args.write.write_text(text, encoding="utf-8")
        print(f"wrote {args.write}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
