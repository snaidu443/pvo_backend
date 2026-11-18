"""`<stem>_<spec>_extraction.html`: every extracted value with its
confidence, reasons, citations (page, quote, where it was found in the
file's text) and checks, plus the run's timings. For a deck, citations
carry the deck's slide numbers (not the sent PDF's pages).

Static and self-contained: every value is HTML-escaped on the server side,
no script at all, CSP `default-src 'none'` (inline styles only).
"""

from __future__ import annotations

from html import escape
from pathlib import Path

from structx.settings.spec import Spec

_CSS = """
body{font:14px/1.45 system-ui,Segoe UI,sans-serif;margin:24px;color:#1f2328}
h1{font-size:20px}h3{font-size:15px;margin:18px 0 4px}h2{font-size:16px;margin-top:28px;border-bottom:1px solid #d0d7de;padding-bottom:4px}
table{border-collapse:collapse;width:100%;margin:8px 0}
th,td{border:1px solid #d0d7de;padding:6px 8px;vertical-align:top;text-align:left}
th{background:#f6f8fa}.muted{color:#656d76}.small{font-size:12px}
.score{font-weight:600;padding:2px 6px;border-radius:4px;white-space:nowrap}
.hi{background:#dafbe1}.mid{background:#fff8c5}.lo{background:#ffebe9}.na{background:#eaeef2}
.cards{display:flex;gap:12px;flex-wrap:wrap}.card{border:1px solid #d0d7de;border-radius:6px;padding:8px 12px}
q{font-style:italic}code{font-size:12px}
"""

_REASONS = {
    "conflicting_statements": "a contradicting statement the model listed is in the file's text: confidence low",
    "quote_not_found": "its quote is not in the file's text (read from an image/chart, or invented): at most medium",
    "value_not_in_quote": "the value is not in its quote: at most medium",
    "no_citation": "no citation given: confidence low",
    "type_mismatch": "a number/date field whose value has no digit (e.g. 'TBD'): confidence low",
    "no_label": "the model gave no label: confidence low",
    "wrong_page": "its quote is on another page than cited (informational)",
    "conflict_not_in_text": "a contradicting statement the model listed isn't in the file's text (informational)",
    "no_option": "the value fits none of the field's categories (informational)",
    "option_from_date": "the category was set by code from the record's date (spec rule) and differs from the model's (informational)",
    "not_in_document": "the model found no such value in the file",
    # Mapping of ci / protocol records to the asset's indications (combine.py)
    "indication_match_partial": "the record's wording and the chosen asset indication share only some words: mapping at most medium",
    "indication_match_loose": "the record's wording and the chosen asset indication share no word: mapping low",
    "indication_mapped_by_code": "the model mapped it to no asset indication; code chose the one sharing the most words: mapping low",
    "indication_unmapped": "no asset indication shares a word with the record's wording: listed under the unmapped records",
    "product_wide_only": "the competitor has only product-wide records, so there is no indication to attach them to",
    "study_wide_only": "the study has only study-wide records (indication null), so there is no indication to attach them to",
}


def _reason_meaning(code: str) -> str:
    if code.endswith("_name_partial"):  # "<key>_name_partial", built from the spec's key name
        key = code.removesuffix("_name_partial")
        return (f"the record named its {key} by a shorter or longer name that fits only one listed {key}; "
                "attached to it by code: at most medium")
    return _REASONS.get(code, "")


_LABEL_CLASS = {"high": "hi", "medium": "mid", "low": "lo"}


def _p(page: int, unit: str) -> str:
    """'slide 13' for a deck, 'p.13' for a PDF input."""
    return f"{unit}{page}"


def _value_row(label: str, node: dict | None, unit: str) -> str:
    if node is None:
        return (f"<tr><td>{escape(label)}</td><td colspan=5><span class=muted>not stated</span></td></tr>")
    conf = node.get("confidence")  # high / medium / low
    cites = []
    for c in node.get("citations", []):
        where = []
        if c.get("found_on_page") is not None and c["found_on_page"] != c["page"]:
            where.append(f"found on {_p(c['found_on_page'], unit)}")
        if not c.get("page_exists", True):
            where.append("CITED PAGE DOES NOT EXIST IN THE FILE SENT")
        cites.append(
            f"<div>{_p(c['page'], unit)} <q>{escape(c['quote'])}</q><br><span class='small muted'>"
            f"{escape(c.get('match') or '')}{' · ' if where else ''}{escape(' · '.join(where))}</span></div>"
        )
    k = node.get("checks") or {}
    checks = ""
    if k:
        checks = escape(
            f"{'text-backed' if k['text_backed'] else 'AI-read'} · value in quote {k['value_in_quote']:.0%}"
            + (f" · numbers not in text: {', '.join(k['unverified_numbers'])}" if k["unverified_numbers"] else "")
        )
    category = f"<br><span class=small>category: <b>{escape(node['option'] or '—')}</b></span>" if "option" in node else ""
    if "date" in node:
        # One line per stated value: "year 2030 · month —" (parts from the spec).
        for d in (node["date"] if isinstance(node["date"], list) else [node["date"]]):
            category += ("<br><span class=small>" + escape(" · ".join(f"{k} {v if v is not None else '—'}" for k, v in d.items()))
                         + "</span>")
    label_html = ""
    if node.get("label_reason"):
        label_html = f"<br><span class='small muted'>{escape(node['label_reason'])}</span>"
    if node.get("model_label"):
        label_html += f"<br><span class='small'>model said <b>{escape(node['model_label'])}</b>; lowered by the checks</span>"
    for cf in node.get("conflicts", []):
        label_html += (f"<div class=small><b>conflict</b> {_p(cf['page'], unit)}"
                       + (f" says <b>{escape(cf['value'])}</b>" if cf.get("value") else "")
                       + f": <q>{escape(cf['quote'])}</q> <span class=muted>({escape(cf.get('match') or '')})</span></div>")
    value = node["value"]
    if isinstance(value, list):
        # Every stated value (output.all_values); the first is the one the
        # citations and checks are about.
        value_html = "<br>".join(escape(v) + (" <span class='small muted'>(cited)</span>" if i == 0 else "")
                                 for i, v in enumerate(value))
    else:
        value_html = escape(str(value))
    return (
        f"<tr><td>{escape(label)}</td><td>{value_html}{category}</td>"
        f"<td><span class='score {_LABEL_CLASS.get(conf, 'na')}'>{escape(conf or '—')}</span>{label_html}</td>"
        f"<td class=small>{escape(', '.join(node.get('confidence_reasons', [])))}</td>"
        f"<td>{''.join(cites)}</td><td class=small>{checks}</td></tr>"
    )


_META_KEYS = ("applies_to", "field", "reason")


def _rows(label: str, obj: object, unit: str) -> list[str]:
    """Rows for any part of the written output: a value, a record, a list."""
    if obj is None or (isinstance(obj, dict) and "value" in obj and "confidence" in obj):
        return [_value_row(label, obj, unit)]
    if isinstance(obj, dict):
        if obj.get("applies_to") == "program":
            label += " (program-wide)"
        rows: list[str] = []
        for key, val in obj.items():
            if key not in _META_KEYS:
                rows += _rows(f"{label} › {key}" if label else key, val, unit)
        return rows
    if isinstance(obj, list):
        rows = []
        for i, val in enumerate(obj):
            rows += _rows(f"{label} [{i + 1}]", val, unit)
        return rows or [f"<tr><td>{escape(label)}</td><td colspan=5><span class=muted>none found</span></td></tr>"]
    return []


def _table(rows: list[str]) -> str:
    head = "<tr><th>Field</th><th>Value</th><th>Confidence</th><th>Reasons</th><th>Citations</th><th>Checks</th></tr>"
    return f"<table>{head}{''.join(rows)}</table>"


_STEP_NAMES = {"prepare_file": "Prepare the file", "model_call": "Model call", "local_checks": "Local checks",
               "group_output": "Group the output"}


def _secs(v: float | None) -> str:
    return "—" if v is None else f"{v:.2f}"


def _timings_table(t: dict) -> list[str]:
    """One row per step; start/end are seconds from the start of the run, so
    the parallel model calls show as overlapping."""
    steps = t.get("steps") or []
    if not steps:
        return []
    rows = []
    for s in steps:
        what = _STEP_NAMES.get(s["step"], s["step"])
        if s.get("field"):
            what += f": {s['field']}"
        end = None if s.get("seconds") is None else s["start"] + s["seconds"]
        extra = []
        if s["step"] == "model_call":
            extra.append(f"{s.get('status')}, {s.get('attempts')} attempt(s)")
            if s.get("input_tokens") is not None:
                extra.append(f"tokens {s['input_tokens']} in / {s['output_tokens']} out ({s.get('reasoning_tokens')} reasoning)")
        if s.get("detail"):
            extra.append(s["detail"])
        rows.append(
            f"<tr><td>{escape(what)}</td><td>{_secs(s.get('start'))}</td><td>{_secs(end)}</td>"
            f"<td><b>{_secs(s.get('seconds'))}</b></td><td class=small>{escape(' · '.join(extra))}</td></tr>"
        )
    return [
        f"<h2>Timings (total {_secs(t.get('total_seconds'))} s)</h2>",
        "<table><tr><th>Step</th><th>Start (s)</th><th>End (s)</th><th>Duration (s)</th><th>Details</th></tr>",
        *rows,
        "</table><p class='small muted'>Start and end are seconds from the start of the run; the per-field model "
        "calls run in parallel, so their times overlap.</p>",
    ]


def write_report(output: dict, spec: Spec, path: Path) -> None:
    """Renders the written output (`extract.written_output`) as static HTML."""
    src, summary = output["source"], output["summary"]
    removed = src.get("hidden_slides_removed") or []
    deck = src.get("slides") is not None
    unit = "slide " if deck else "p."
    about = (f"{src['slides']} slide(s)"
             + (f", hidden slide(s) {', '.join(map(str, removed))} left out" if removed else "")
             + f" · sent as {src['sent_as']} ({src['pages']} page(s)); citations use the deck's slide numbers"
             if deck else f"{src['pages']} page(s) · sent as {src['sent_as']}")
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'\">",
        f"<title>{escape(spec.name)} extraction — {escape(src['file'])}</title><style>{_CSS}</style></head><body>",
        f"<h1>{escape(spec.name)} extraction — {escape(src['file'])}</h1>",
        "<p class='small muted'>" + escape(
            f"{about} · model {src['model']} · {src['extracted_at']}"
        ) + "</p>",
        "<div class=cards>",
        f"<div class=card>Values<br><b>{summary['values']}</b></div>",
        f"<div class=card>Text-backed<br><b>{summary['text_backed_values']}</b></div>",
        "<div class=card>Confidence<br><b>" + escape(" · ".join(f"{k} {v}" for k, v in summary["confidence"].items())) + "</b></div>",
        f"<div class=card>Lowered by checks<br><b>{summary.get('lowered_by_checks', 0)}</b></div>",
        f"<div class=card>With conflicts<br><b>{summary.get('values_with_conflicts', 0)}</b></div>",
        f"<div class=card>Fields not found<br><b>{len(summary['fields_not_found'])}</b></div>",
        f"<div class=card>Failed fields<br><b>{len(summary.get('failed_fields') or {})}</b></div>",
        f"<div class=card>Time<br><b>{(output.get('timings') or {}).get('total_seconds', src.get('seconds', '—'))} s</b></div>",
        "</div>",
    ]
    if summary["reasons"]:
        parts.append("<h2>Reasons</h2><table><tr><th>Code</th><th>Values</th><th>Meaning</th></tr>")
        parts += [f"<tr><td><code>{escape(r)}</code></td><td>{n}</td><td>{escape(_reason_meaning(r))}</td></tr>"
                  for r, n in summary["reasons"].items()]
        parts.append("</table>")
    parts += _timings_table(output.get("timings") or {})
    failed = summary.get("failed_fields") or {}
    if failed:
        parts.append("<h2>Failed fields</h2><table><tr><th>Field</th><th>Error</th></tr>")
        parts += [f"<tr><td>{escape(fid)}</td><td class=small>{escape(err)}</td></tr>" for fid, err in failed.items()]
        parts.append("</table>")

    body = {k: v for k, v in output.items() if k not in ("source", "summary", "timings")}
    if spec.primary_key:
        list_id, key = spec.primary_key
        entries = body.pop(list_id) or []
        parts.append(f"<h2>{escape(list_id)} ({len(entries)})</h2>")
        if not entries:
            parts.append("<p class=muted>None found.</p>")
        for entry in entries:
            name = (entry.get(key) or {}).get("value", "?")
            parts.append(f"<h3>{escape(str(name))}</h3>")
            parts.append(_table(_rows("", entry, unit)))
        others = body.pop("other_records", [])
        if others:
            parts.append(f"<h2>Other records ({len(others)})</h2>")
            for rec in others:
                named = "program-wide" if rec.get(key) is None else f"{key}: {rec[key]}"
                parts.append(f"<p class=small><b>{escape(rec['field'])}</b> ({escape(named)}) — {escape(rec['reason'])}</p>")
                parts.append(_table(_rows("", rec, unit)))
    for section, val in body.items():
        parts.append(f"<h2>{escape(section)}</h2>")
        parts.append(_table(_rows("", val, unit)))
    parts.append(
        "<p class='small muted'>Confidence = the extracting model's high/medium/low label (after its search for "
        "contradicting statements), lowered -- never raised -- by the automatic checks. See README.md.</p>"
    )
    parts.append("</body></html>")
    path.write_text("\n".join(parts), encoding="utf-8")
