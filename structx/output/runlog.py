"""`<stem>_run_log.md`: a per-run record of exactly what was sent to the model.

Written by `cli.py` for every run, including a failed one. It contains:
- the run's settings (`StructConfig`; the API key never appears, the
  endpoint only as "set");
- the dataclass definitions (fields, types, defaults, generated from the
  code) and the objects of this run (the `Spec` tree, the `SourceFile`
  without its bytes);
- **every model call** in start order: name, timing, attempts, status,
  token counts, and the complete `instructions`, every `input` part and the
  complete JSON schema.

The attached file itself is listed by name, type and size, not as base64.
The indication names in the schemas (per_field mode) are deck content, so
this log is as sensitive as the extraction output next to it.
"""

from __future__ import annotations

import dataclasses
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


@dataclasses.dataclass
class CallRecord:
    name: str
    started: float
    instructions: str
    content: list[dict]
    schema: dict
    max_output_tokens: int
    reasoning_effort: str | None
    seconds: float | None = None
    attempts: int = 0
    status: str = "started"  # completed | incomplete | failed | ...
    error: str | None = None
    usage: dict | None = None


class RunLog:
    """Collects call records from all threads of one run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: list[CallRecord] = []
        self.started = time.monotonic()
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.notes: list[str] = []
        self.source = None  # set by extract.run() once the file is prepared

    def start_call(self, **kwargs) -> CallRecord:
        rec = CallRecord(started=time.monotonic(), **kwargs)
        with self._lock:
            self._calls.append(rec)
        return rec

    @property
    def calls(self) -> list[CallRecord]:
        with self._lock:
            return sorted(self._calls, key=lambda c: c.started)


def _fence(text: str, lang: str = "text") -> str:
    # A longer fence than any backtick run inside, so content can't close it.
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text}\n{ticks}"


def _dataclass_definition(cls) -> str:
    lines = [f"@dataclass{'(frozen=True)' if cls.__dataclass_params__.frozen else ''}", f"class {cls.__name__}:"]
    for f in dataclasses.fields(cls):
        default = ""
        if f.default is not dataclasses.MISSING:
            default = f" = {f.default!r}"
        elif f.default_factory is not dataclasses.MISSING:
            default = " = <factory>"
        lines.append(f"    {f.name}: {f.type}{default}")
    return "\n".join(lines)


def _part(p: dict) -> str:
    if p.get("type") == "input_file":
        data = p.get("file_data", "")
        mime = data[5:data.find(";")] if data.startswith("data:") else "?"
        approx = (len(data) - data.find(",") - 1) * 3 // 4 if "," in data else 0
        return f"**[input_file]** filename `{p.get('filename')}`, type `{mime}`, ~{approx:,} bytes (base64 not shown)"
    return "**[input_text]**\n\n" + _fence(p.get("text", ""))


def write(path: Path, run_log: RunLog, *, cfg, spec, source, result: dict | None, error: str | None,
          command: list[str]) -> None:
    from structx.checks.grounding import Claim
    from structx.settings.config import StructConfig
    from structx.settings.spec import FieldSpec, Spec
    from structx.source.source import Match, SourceFile, Text

    out: list[str] = [f"# Run log: {getattr(source, 'path', Path('?')).name if source else '?'}", ""]
    total = time.monotonic() - run_log.started
    out += [
        "## Run",
        "",
        f"- command: `{' '.join(command)}`",
        f"- started (UTC): {run_log.started_at} · total {total:.1f} s",
        f"- result: {'**FAILED**: ' + error if error else 'completed'}",
    ]
    if result:
        s = result["summary"]
        out.append(
            f"- values {s['values']} (text-backed {s['text_backed_values']}), confidence {s['confidence']}, "
            f"lowered by checks: {s['lowered_by_checks']}, failed fields: {list((s.get('failed_fields') or {}))}"
        )
    out += [f"- note: {n}" for n in run_log.notes] + [""]

    if cfg is not None:
        shown = {f.name: getattr(cfg, f.name) for f in dataclasses.fields(cfg)}
        shown["api_key"] = "<redacted>"
        shown["base_url"] = "<set, https>" if cfg.base_url else "<not set>"
        out += ["## Settings: `StructConfig` (from `.env`)", "", _fence(json.dumps(shown, indent=2), "json"), ""]

    out += ["## Dataclass definitions (generated from the code)", ""]
    for cls in (FieldSpec, Spec, StructConfig, Text, Match, SourceFile, Claim):
        out += [f"`{cls.__module__}.{cls.__name__}`", "", _fence(_dataclass_definition(cls), "python"), ""]

    if spec is not None:
        out += [f"## This run's `Spec` (from `structx/specs/{spec.name}.json`)", "", _fence(_spec_tree(spec), "python"), ""]

    if source is not None:
        out += [
            "## This run's `SourceFile` (bytes not shown)",
            "",
            _fence(
                f"SourceFile(path={source.path.name!r}, delivery={source.delivery!r},\n"
                f"           payload_name={source.payload_name!r}, payload_mime={source.payload_mime!r}, payload={len(source.payload):,} bytes,\n"
                f"           pages={len(source.pages)} (text pieces per page: "
                f"{ {n: len(t) for n, t in source.pages.items()} }),\n"
                f"           hidden_removed={list(source.hidden_removed)})",
                "python",
            ),
            "",
        ]

    calls = run_log.calls
    out += [f"## Model calls ({len(calls)}, in start order)", ""]
    out += ["| # | call | start (s) | time (s) | attempts | status | tokens in / out (reasoning) |", "|---|---|---|---|---|---|---|"]
    for i, c in enumerate(calls, 1):
        u = c.usage or {}
        tokens = f"{u.get('input', '?')} / {u.get('output', '?')} ({u.get('reasoning', '?')})" if u else "—"
        out.append(f"| {i} | `{c.name}` | {c.started - run_log.started:.1f} | "
                   f"{'—' if c.seconds is None else f'{c.seconds:.1f}'} | {c.attempts} | {c.status} | {tokens} |")
    out.append("")
    for i, c in enumerate(calls, 1):
        out += [
            f"### {i}. `{c.name}`: {c.status}",
            "",
            f"`client.responses.create(model={cfg.model if cfg else '?'!r}, max_output_tokens={c.max_output_tokens}, "
            f"reasoning={{'effort': {c.reasoning_effort!r}}}, store=False, stream=True, "
            f"text={{'format': {{'type': 'json_schema', 'name': {c.name!r}, 'strict': True, 'schema': <below>}}}})`",
            "",
        ]
        if c.error:
            out += [f"**Error:** {c.error}", ""]
        out += ["#### instructions", "", _fence(c.instructions), "", "#### input (user message parts, in order)", ""]
        for p in c.content:
            out += [_part(p), ""]
        out += ["#### JSON schema (`text.format.schema`)", "", _fence(json.dumps(c.schema, indent=2, ensure_ascii=False), "json"), ""]
    path.write_text("\n".join(out), encoding="utf-8")


def _spec_tree(spec) -> str:
    lines = [f"Spec(name={spec.name!r}, primary_key={spec.primary_key!r},",
             f"     description={spec.description!r},", "     fields=("]
    for f in spec.fields:
        lines.append(f"    FieldSpec(id={f.id!r}, kind={f.kind!r}, type={f.type!r}, options={f.options!r},")
        lines.append(f"              description={f.description!r},")
        if f.fields:
            lines.append("              fields=(")
            for sub in f.fields:
                lines.append(f"        FieldSpec(id={sub.id!r}, kind={sub.kind!r}, type={sub.type!r}, options={sub.options!r},")
                lines.append(f"                  description={sub.description!r}),")
            lines.append("              )),")
        else:
            lines.append("              fields=()),")
    if spec.aliases:
        lines.append(f"     ), aliases={spec.aliases!r})")
    else:
        lines.append("))")
    return "\n".join(lines)
