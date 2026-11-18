"""Joins a `cdp` extraction with a `ci` extraction and/or a `protocol`
extraction -- all as written by `extract.written_output` (the
`_extraction.json` files) -- into one result: per CDP indication, the
asset's values, the competitors mapped to it and the studies mapped to it.
Pure code: no gateway call. Written by `workflow.py`.

The ci and protocol runs were made with the CDP's indication names injected
as the categories of every `indication` sub-field (`spec.with_sub_options`),
so each of their records has the file's wording as `indication.value` and
the model's choice of CDP indication as `option`, with the model's label.
Code then applies lower-only evidence (`map_record`):

| Evidence                                                     | Mapping label at most | Reason code                |
|--------------------------------------------------------------|-----------------------|----------------------------|
| all record words in the CDP name, or all CDP words in the record's wording | unchanged | --                      |
| some words shared, not all either way                        | medium                | `indication_match_partial` |
| no word shared                                               | low                   | `indication_match_loose`   |
| option null (or not a listed name); code picked the name sharing the most words | low | `indication_mapped_by_code` |
| option null and no name shares a word: not mapped            | --                    | `indication_unmapped`      |

Words are `spec.key_words`' (case, punctuation, &/and, order ignored).
Stop words (`grounding._STOP`) are dropped, so a shared 'in' or 'of' alone is no overlap.
Abbreviations the specs list under `aliases` (`{"LR": "lower-risk", ...}`,
passed in by `workflow.py`) are replaced by their wording on both sides
first, so `TD MF pts` and `Myelofibrosis - Transfusion Dependent` share
every word; the map used is written under `source.aliases`.
The joined file's own values (phase, dates, design) keep their confidence
from their run; the mapping never changes them. The mapping label is
always high / medium / low, never a number.

The two joined sources are the same shape (`_JOINED`): ci is keyed by
competitor, protocol by study; each record names its indication. Per CDP
indication the result holds `competitors` and `studies`, one entry per
competitor/study mapped to it, each with a `mapping` object: the wording
behind the entry's label (`ci_indication` / `protocol_indication`), every
distinct wording seen (`ci_indications` / `protocol_indications`),
`cdp_indication`, `confidence` (the lowest label of its records),
`confidence_reasons` (the union) and `label_reason`. Records nothing could
be attached to go to `competitors_unmapped` / `studies_unmapped` (reasons
`indication_unmapped`, `product_wide_only`); duplicates to `other_records`.
Nothing is dropped. The key lists themselves (`competitors`, `studies`)
are carried over as written, so the database loader has every key.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Mapping

from structx.checks.grounding import _STOP
from structx.model.llm import ExtractionError
from structx.output.report import _CSS, _LABEL_CLASS, _reason_meaning, _rows, _table
from structx.settings.spec import key_words

_RANK = {"low": 0, "medium": 1, "high": 2}

Aliases = Mapping[str, str] | None  # abbreviation -> wording (a spec's `aliases`), or None


@dataclass(frozen=True)
class Joined:
    """How a joined source (ci, protocol) is keyed and where its entries go."""
    name: str          # the spec name and the key under source/summary/other_records
    list_id: str       # the key list in its output (competitors, studies)
    key: str           # the key sub-field / entry key (competitor, study)
    wording: str       # the mapping key holding the file's wording (ci_indication, protocol_indication)
    unmapped: str      # the top-level bucket for records nothing could be attached to
    whole: str         # applies_to for a key-wide record (indication null)

    @property
    def wordings(self) -> str:
        return self.wording + "s"


_JOINED = {
    "ci": Joined("ci", "competitors", "competitor", "ci_indication", "competitors_unmapped", "product"),
    "protocol": Joined("protocol", "studies", "study", "protocol_indication", "studies_unmapped", "study"),
}
# The reason a key-wide record gets when its key has no mapped entry to attach it to.
_WHOLE_ONLY = {"ci": "product_wide_only", "protocol": "study_wide_only"}


def words(text: str, aliases: Aliases = None) -> set[str]:
    """The words the overlap check compares: `key_words`, each alias replaced
    by its wording's words first, stop words dropped."""
    return set(_expand(key_words(text), aliases)) - _STOP


def _expand(tokens: list[str], aliases: Aliases) -> list[str]:
    """`tokens` with every alias (one or more tokens) replaced by its wording's
    tokens: longest alias first, left to right, each token replaced once
    (a wording is not expanded again)."""
    if not aliases:
        return tokens
    table = {tuple(key_words(k)): key_words(v) for k, v in aliases.items()}
    table.pop((), None)
    if not table:
        return tokens
    longest = max(len(k) for k in table)
    out: list[str] = []
    i = 0
    while i < len(tokens):
        for n in range(min(longest, len(tokens) - i), 0, -1):
            wording = table.get(tuple(tokens[i:i + n]))
            if wording is not None:
                out.extend(wording)
                i += n
                break
        else:
            out.append(tokens[i])
            i += 1
    return out


def overlap(ci_wording: str, cdp_name: str, aliases: Aliases = None) -> str:
    """'full' when one side's words are all in the other's, 'partial' when
    some are shared, 'none' otherwise (an empty side is 'none')."""
    a, b = words(ci_wording, aliases), words(cdp_name, aliases)
    if not a or not b:
        return "none"
    if a <= b or b <= a:
        return "full"
    return "partial" if a & b else "none"


def nearest(ci_wording: str, names: list[str], aliases: Aliases = None) -> str | None:
    """The name sharing the most words with the wording (first in list
    order on a tie); None when no name shares a word."""
    a = words(ci_wording, aliases)
    best, best_shared = None, 0
    for name in names:
        shared = len(a & words(name, aliases))
        if shared > best_shared:
            best, best_shared = name, shared
    return best


def text_of(value: str | list) -> str:
    """A written `value` is a list when it had conflicts; the first element
    is the cited one."""
    return value[0] if isinstance(value, list) else value


def map_record(indication: dict, names: list[str], aliases: Aliases = None, *, wording_key: str = "ci_indication") -> dict:
    """The mapping of one record from its `indication` written value: the
    model's label, lowered by the overlap evidence (words compared after
    the `aliases` are expanded); `cdp_indication` None when nothing could
    be mapped (reason `indication_unmapped`). `wording_key` names the key
    holding the file's wording (ci_indication / protocol_indication)."""
    wording = text_of(indication["value"])
    label = indication.get("confidence") or "low"
    reasons: list[str] = []

    def at_most(cap: str, reason: str) -> None:
        nonlocal label
        if _RANK[cap] < _RANK[label]:
            label = cap
        reasons.append(reason)

    target = indication.get("option")
    if target not in names:
        target = nearest(wording, names, aliases)
        if target is None:
            return {wording_key: wording, "cdp_indication": None, "confidence": "low",
                    "confidence_reasons": ["indication_unmapped"], "label_reason": indication.get("label_reason", "")}
        at_most("low", "indication_mapped_by_code")
    else:
        kind = overlap(wording, target, aliases)
        if kind == "partial":
            at_most("medium", "indication_match_partial")
        elif kind == "none":
            at_most("low", "indication_match_loose")
    return {wording_key: wording, "cdp_indication": target, "confidence": label,
            "confidence_reasons": reasons, "label_reason": indication.get("label_reason", "")}


def _merge_mapping(into: dict, m: dict, j: Joined) -> None:
    """Several fields may map the same key to the same indication: the
    entry keeps the lowest label (with that record's reason and wording)
    and the union of the reason codes and wordings, so nothing is hidden."""
    if _RANK[m["confidence"]] < _RANK[into["confidence"]]:
        into["confidence"], into["label_reason"] = m["confidence"], m["label_reason"]
        into[j.wording] = m[j.wording]
    if m[j.wording] not in into[j.wordings]:
        into[j.wordings].append(m[j.wording])
    for r in m["confidence_reasons"]:
        if r not in into["confidence_reasons"]:
            into["confidence_reasons"].append(r)


def _body(rec: dict) -> dict:
    """A record's sub-fields without the keys the entry carries itself."""
    return {k: v for k, v in rec.items() if k not in ("applies_to", "indication")}


def _check(label: str, out: object, key: str) -> None:
    if (not isinstance(out, dict) or not isinstance(out.get(key), list) or "summary" not in out
            or not isinstance(out.get("source"), dict) or out["source"].get("spec") != label):
        raise ExtractionError(f"The {label} result lacks '{key}', 'summary' or a source with spec {label!r}.")


def _join(names: list[str], out: dict, j: Joined, aliases: Aliases) -> dict:
    """One joined source: its entries per (cdp indication, key), its unmapped
    records, its other records and the mapped-record count."""
    key_names = [text_of(k[j.key]["value"]) for k in out[j.list_id]]
    fields = [k for k in (out[j.list_id][0] if out[j.list_id] else {}) if k != j.key]
    entries: dict[tuple[str, str], dict] = {}  # (cdp indication, key name) -> entry, in first-seen order
    unmapped: list[dict] = []
    other: list[dict] = list(out.get("other_records") or [])
    whole: list[tuple[str, str, dict]] = []
    mapped = 0
    for item, kname in zip(out[j.list_id], key_names):
        for fid in fields:
            for rec in item.get(fid) or []:
                if rec.get("indication") is None:
                    whole.append((kname, fid, _body(rec)))
                    continue
                m = map_record(rec["indication"], names, aliases, wording_key=j.wording)
                if m["cdp_indication"] is None:
                    unmapped.append({j.key: kname, "field": fid, j.wording: m[j.wording],
                                     "reason": "indication_unmapped", **_body(rec)})
                    continue
                entry = entries.get((m["cdp_indication"], kname))
                if entry is None:
                    mapping = {j.wording: m[j.wording], j.wordings: [m[j.wording]],
                               **{k: v for k, v in m.items() if k != j.wording}}
                    entry = entries[(m["cdp_indication"], kname)] = {
                        j.key: item[j.key], "mapping": mapping, **{f: None for f in fields}}
                else:
                    _merge_mapping(entry["mapping"], m, j)
                if entry[fid] is not None:
                    other.append({"field": fid, j.key: kname, "indication": m["cdp_indication"],
                                  "reason": f"more than one {fid} record for this {j.key} and indication", **_body(rec)})
                    continue
                entry[fid] = {"applies_to": j.key, **_body(rec)}
                mapped += 1
    for kname, fid, body in whole:
        targets = [e for (_, k), e in entries.items() if k == kname]
        if not targets:
            unmapped.append({j.key: kname, "field": fid, j.wording: None, "reason": _WHOLE_ONLY[j.name], **body})
            continue
        for entry in targets:
            if entry[fid] is None:
                entry[fid] = {"applies_to": j.whole, **body}
                mapped += 1
            else:
                other.append({"field": fid, j.key: kname, "indication": entry["mapping"]["cdp_indication"],
                              "reason": f"{j.whole}-wide, but this {j.key} has its own {fid} record for this indication", **body})
    return {"entries": entries, "key_names": key_names, "unmapped": unmapped, "other": other, "mapped": mapped}


def combine(cdp: dict, ci: dict | None = None, protocol: dict | None = None, *,
            seconds: float | None = None, aliases: Aliases = None) -> dict:
    """The combined result (see the module docstring and the README):
    `indications` = the cdp entries, each with `competitors` (from `ci`)
    and `studies` (from `protocol`): one entry per key mapped to it, in
    the key list's order, holding the key's record for every field of that
    run (None when it has none for this indication; key-wide records are
    attached as `applies_to: product` / `study`). At least one of `ci` and
    `protocol` is needed. `seconds`: the workflow's wall time, else the
    runs' sum. `aliases`: expanded before every word comparison and
    recorded under `source.aliases`."""
    _check("cdp", cdp, "indications")
    present = {"ci": ci, "protocol": protocol}
    if all(v is None for v in present.values()):
        raise ExtractionError("combine needs a ci result, a protocol result, or both.")
    for name, out in present.items():
        if out is not None:
            _check(name, out, _JOINED[name].list_id)
    names = list(dict.fromkeys(text_of(e["indication"]["value"]).strip() for e in cdp["indications"]))

    joined = {name: _join(names, out, _JOINED[name], aliases) for name, out in present.items() if out is not None}
    indications = []
    for e in cdp["indications"]:
        name = text_of(e["indication"]["value"]).strip()
        entry = dict(e)
        for jname, j in _JOINED.items():
            res = joined.get(jname)
            entry[j.list_id] = ([res["entries"][(name, k)] for k in dict.fromkeys(res["key_names"]) if (name, k) in res["entries"]]
                                if res else [])
        indications.append(entry)
    total = seconds if seconds is not None else round(
        cdp["source"].get("seconds", 0) + sum(out["source"].get("seconds", 0) for out in present.values() if out), 1)

    def per_source(fn) -> dict:
        return {jname: (fn(joined[jname]) if jname in joined else None) for jname in _JOINED}

    return {
        "source": {"cdp": cdp["source"], **{n: (out["source"] if out else None) for n, out in present.items()},
                   "seconds": total, "aliases": dict(aliases or {})},
        "summary": {
            "indications": len(names),
            **{j.list_id: (len(joined[n]["key_names"]) if n in joined else None) for n, j in _JOINED.items()},
            "mapped_records": per_source(lambda r: r["mapped"]),
            # key-and-indication entries per label, not records: the sum is not mapped_records
            "mapping_confidence": per_source(lambda r: {lab: sum(1 for e in r["entries"].values() if e["mapping"]["confidence"] == lab)
                                                        for lab in ("high", "medium", "low")}),
            "unmapped_records": per_source(lambda r: len(r["unmapped"])),
            "cdp": cdp["summary"],
            **{n: (out["summary"] if out else None) for n, out in present.items()},
        },
        "indications": indications,
        **{j.list_id: ([item[j.key] for item in present[n][j.list_id]] if present[n] else []) for n, j in _JOINED.items()},
        **{j.unmapped: (joined[n]["unmapped"] if n in joined else []) for n, j in _JOINED.items()},
        "other_records": {"cdp": list(cdp.get("other_records") or []),
                          **{n: (joined[n]["other"] if n in joined else []) for n in _JOINED}},
    }


def indications_only(names: list[str], file: str) -> dict:
    """A cdp output holding the indication names only, the least `combine`
    accepts as its `cdp`: for a join run from names kept elsewhere (the
    API's database) when no cdp output is at hand. `file` is recorded as
    `source.file`."""
    return {"source": {"file": file, "spec": "cdp"}, "summary": {},
            "indications": [{"indication": {"value": n}} for n in names], "other_records": []}


# ----------------------------------------------------------------- the HTML
_COLUMNS = {
    "ci": {
        "phase_of_development": ("phase",),
        "us_regulatory_approval_timing": ("milestone", "timing"),
        "us_generic_entry_timing": ("basis", "timing"),
    },
    "protocol": {
        "clinical_trial_comparator_design": ("design", "comparator"),
        "clinical_trial_comparator_type": ("comparator_type",),
        "clinical_trial_blinding": ("blinding",),
        "alignment_of_stratification_factors": ("stratification",),
        "subgroup_endpoints": ("subgroups",),
        "patient_centered_outcomes": ("endpoints",),
    },
}


def _badge(label: str | None) -> str:
    return f"<span class='score {_LABEL_CLASS.get(label, 'na')}'>{escape(label or '—')}</span>"


def _value_cell(node: dict | None) -> str:
    if node is None:
        return "<span class=muted>—</span>"
    value = node["value"]
    text = " / ".join(value) if isinstance(value, list) else str(value)
    category = f" <span class=small>[{escape(node['option'])}]</span>" if node.get("option") else ""
    date = ""
    if "date" in node:
        parts = node["date"] if isinstance(node["date"], list) else [node["date"]]
        date = " <span class='small muted'>" + escape("; ".join(
            " ".join(f"{k} {v if v is not None else '—'}" for k, v in d.items()) for d in parts)) + "</span>"
    return f"{escape(text)}{category}{date} {_badge(node.get('confidence'))}"


def _record_cell(rec: dict | None, subs: tuple[str, ...], j: Joined) -> str:
    if rec is None:
        return "<span class=muted>—</span>"
    lines = [_value_cell(rec.get(sub)) for sub in subs if sub in rec]
    if not lines:  # a field this report has no column list for: every sub-field
        lines = [f"{escape(k)}: {_value_cell(v)}" for k, v in rec.items() if k != "applies_to"]
    if rec.get("applies_to") == j.whole:
        lines.append(f"<span class='small muted'>({escape(j.whole)}-wide)</span>")
    return "<br>".join(lines)


def _joined_table(entries: list[dict], fields: list[str], j: Joined) -> str:
    head = (f"<tr><th>{escape(j.key.capitalize())}</th><th>{escape(j.name)} wording</th><th>Mapping</th>"
            + "".join(f"<th>{escape(f)}</th>" for f in fields) + "</tr>")
    rows = []
    for c in entries:
        m = c["mapping"]
        mapping = (_badge(m["confidence"]) + f"<br><span class=small>{escape(', '.join(m['confidence_reasons']))}</span>"
                   + (f"<br><span class='small muted'>{escape(m['label_reason'])}</span>" if m.get("label_reason") else ""))
        cells = "".join(f"<td>{_record_cell(c.get(f), _COLUMNS[j.name].get(f, ()), j)}</td>" for f in fields)
        rows.append(f"<tr><td>{_value_cell(c[j.key])}</td><td>{escape(m[j.wording] or '—')}</td><td>{mapping}</td>{cells}</tr>")
    return f"<table>{head}{''.join(rows)}</table>" if rows else f"<p class=muted>No {j.key} mapped to this indication.</p>"


def _fields_of(combined: dict, j: Joined) -> list[str]:
    for e in combined["indications"]:
        for c in e[j.list_id]:
            return [k for k in c if k not in (j.key, "mapping")]
    return []


def write_combined_report(combined: dict, path: Path) -> None:
    """The combined result as static HTML (no script, CSP default-src
    'none', every value escaped): header cards, the mapping reasons, one
    section per CDP indication (the asset's values, then its competitors
    and studies), the unmapped records and the other records."""
    src, s = combined["source"], combined["summary"]
    present = [j for n, j in _JOINED.items() if src.get(n)]
    unit = {"cdp": "slide " if src["cdp"].get("slides") is not None else "p."}
    unit.update({j.name: ("slide " if src[j.name].get("slides") is not None else "p.") for j in present})
    fields = {j.name: _fields_of(combined, j) for j in present}
    reasons: dict[str, int] = {}
    for e in combined["indications"]:
        for j in present:
            for c in e[j.list_id]:
                for r in c["mapping"]["confidence_reasons"]:
                    reasons[r] = reasons.get(r, 0) + 1
    for j in present:
        for u in combined[j.unmapped]:
            reasons[u["reason"]] = reasons.get(u["reason"], 0) + 1
    title = "cdp + " + " + ".join(j.name for j in present) + " — " + " + ".join([src["cdp"]["file"], *(src[j.name]["file"] for j in present)])
    counts = lambda d: " · ".join(f"{k} {v}" for k, v in d.items() if v is not None)  # noqa: E731
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'\">",
        f"<title>{escape(title)}</title><style>{_CSS}</style></head><body>",
        f"<h1>{escape(title)}</h1>",
        "<p class='small muted'>" + escape(
            f"asset: {src['cdp']['file']} ({src['cdp'].get('model', '')})"
            + "".join(f" · {j.list_id}: {src[j.name]['file']}" for j in present) + f" · {src['seconds']} s") + "</p>",
        "<div class=cards>",
        f"<div class=card>Indications<br><b>{s['indications']}</b></div>",
        *(f"<div class=card>{escape(j.list_id.capitalize())}<br><b>{s[j.list_id]}</b></div>" for j in present),
        f"<div class=card>Mapped records<br><b>{escape(counts(s['mapped_records']))}</b></div>",
        "<div class=card>Mapping confidence<br><b>" + escape(" | ".join(
            f"{n}: {counts(v)}" for n, v in s["mapping_confidence"].items() if v is not None)) + "</b></div>",
        f"<div class=card>Unmapped records<br><b>{escape(counts(s['unmapped_records']))}</b></div>",
        "</div>",
    ]
    if reasons:
        parts.append("<h2>Mapping reasons</h2><table><tr><th>Code</th><th>Records</th><th>Meaning</th></tr>")
        parts += [f"<tr><td><code>{escape(r)}</code></td><td>{n}</td><td>{escape(_reason_meaning(r))}</td></tr>"
                  for r, n in sorted(reasons.items(), key=lambda kv: -kv[1])]
        parts.append("</table>")
    if src.get("aliases"):
        parts.append("<p class='small muted'>" + escape("Abbreviations expanded before the word-overlap check: "
                     + " · ".join(f"{k} = {v}" for k, v in src["aliases"].items())) + "</p>")
    skip = {j.list_id for j in _JOINED.values()}
    for e in combined["indications"]:
        name = text_of(e["indication"]["value"])
        parts.append(f"<h2>{escape(name)}</h2><h3>Asset</h3>")
        parts.append(_table(_rows("", {k: v for k, v in e.items() if k not in skip}, unit["cdp"])))
        for j in present:
            parts.append(f"<h3>{escape(j.list_id.capitalize())} ({len(e[j.list_id])})</h3>")
            parts.append(_joined_table(e[j.list_id], fields[j.name], j))
    for j in present:
        parts.append(f"<h2>Unmapped {escape(j.list_id)} ({len(combined[j.unmapped])})</h2>")
        if not combined[j.unmapped]:
            parts.append("<p class=muted>None.</p>")
        for u in combined[j.unmapped]:
            parts.append(f"<p class=small><b>{escape(u[j.key])}</b> · {escape(u['field'])} · {escape(j.name)} wording: "
                         f"{escape(u[j.wording] or f'({j.whole}-wide)')} — <code>{escape(u['reason'])}</code></p>")
            parts.append(_table(_rows("", {k: v for k, v in u.items() if k not in (j.key, 'field', j.wording, 'reason')}, unit[j.name])))
    others = combined["other_records"]
    labels = ["cdp", *(j.name for j in present)]
    parts.append("<h2>Other records (" + escape(", ".join(f"{lab} {len(others[lab])}" for lab in labels)) + ")</h2>")
    for lab in labels:
        for rec in others[lab]:
            meta = " · ".join(f"{k}: {rec[k]}" for k in ("field", "competitor", "study", "indication", "reason") if rec.get(k) is not None)
            parts.append(f"<p class=small><b>{escape(lab)}</b> {escape(meta)}</p>")
            parts.append(_table(_rows("", {k: v for k, v in rec.items() if k not in ('field', 'competitor', 'study', 'indication', 'reason')},
                                      unit[lab])))
    parts.append("<p class='small muted'>Mapping confidence = the model's label on the record's indication (chosen among the asset's "
                 "indications), lowered -- never raised -- by the word-overlap check. See README.md.</p>")
    parts.append("</body></html>")
    path.write_text("\n".join(parts), encoding="utf-8")
