"""The asset workflow as three functions, one per file, for use from code
(the command lines `cli.py` and `workflow.py` are thin wrappers):

    from structx.steps import extract_cdp, extract_ci, extract_protocol

    cdp = extract_cdp(Path("TAK-226 CDP.pptx"), out_dir=Path("testing"))   # step 1
    ci = extract_ci(Path("CI update.pdf"), cdp.indications, out_dir=...)    # step 2, and/or
    protocol = extract_protocol(Path("concept sheet.docx"), cdp.indications, out_dir=...)
    combined = combine.combine(cdp.output, ci, protocol, aliases=...)       # step 3 (combine.py, database.py)

- `extract_cdp(file)`: the `cdp` spec on the CDP deck -> `CdpExtraction(output, indications)`:
  the written output (one entry per indication) and the distinct indication
  names, the key every other file is mapped to.
- `extract_ci(file, indications)`: the `ci` spec on the CI deck with those
  names injected as the categories of every `indication` sub-field
  (`spec.with_sub_options`), so the model maps each competitor record to one.
- `extract_protocol(file, indications)`: the same for the `protocol` spec on
  the trial protocol concept sheet (.docx, .pdf or .pptx), per study.

Each writes `<stem>_<ext>_<spec>_extraction.json`, `.html` and
`_run_log.md` in `out_dir` (default: next to the file) like `cli.py`, and
returns the written output. With `write_files=False` they write nothing at all
(the API uses this: the run log, JSON and HTML do not exist then;
`extract_output` is that run). `cfg` defaults to `StructConfig.from_env()`.
They fail closed: `ConfigError` for settings, specs or an unusable
indication list (before any gateway call); `ExtractionError` when the run
itself failed (the run log is still written, the error already logged).
`extract_file` (with files) and `extract_output` (without) are the shared
per-file runs underneath them.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import NamedTuple

from structx.model.extract import run, written_output
from structx.model.llm import ExtractionError
from structx.output import runlog
from structx.output.report import write_report
from structx.settings.config import ConfigError, StructConfig
from structx.settings.spec import Spec, load_spec, with_fields, with_sub_options
from structx.source.source import SUFFIXES

_log = logging.getLogger(__name__)


class CdpExtraction(NamedTuple):
    output: dict          # the written cdp output (source, summary, timings, indications, other_records)
    indications: list[str]  # the distinct indication names, in the deck's order: the key for the other files


def extract_file(file: Path, spec: Spec, cfg: StructConfig, out_dir: Path, *, command: list[str]) -> dict | None:
    """Runs `spec` on `file`: writes the run log (every run, failed ones
    included), then `<stem>_<ext>_<spec>_extraction.json` and `.html` in
    `out_dir`. Returns the written output, or None when the extraction
    failed (already logged). `command` is what the run log records as the
    invocation (no secrets are ever on a command line)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # The file type is in the name: a deck and its PDF export give different results.
    stem = f"{file.stem}_{file.suffix.lower().lstrip('.')}_{spec.name}"
    json_out, html_out = out_dir / f"{stem}_extraction.json", out_dir / f"{stem}_extraction.html"
    log_out = out_dir / f"{stem}_run_log.md"

    run_log = runlog.RunLog()
    result, error = None, None
    try:
        result = run(file, spec, cfg=cfg, run_log=run_log)
    except ExtractionError as exc:
        error = str(exc)
        _log.error("Extraction failed: %s", exc)
    finally:
        # Every run, failed ones included: exactly what was sent to the model.
        try:
            runlog.write(log_out, run_log, cfg=cfg, spec=spec, source=run_log.source, result=result,
                         error=error, command=command)
            _log.info("Wrote the run log %s", log_out)
        except Exception as exc:  # noqa: BLE001 - the log must never hide the run's own outcome
            _log.warning("Could not write the run log: %s", exc)
    if error is not None:
        return None

    output = written_output(result, spec)
    json_out.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(output, spec, html_out)
    s = result["summary"]
    _log.info(
        "%d value(s) (%d text-backed), confidence %s, %d field(s) not found. Wrote %s and %s",
        s["values"], s["text_backed_values"], s["confidence"],
        len(s["fields_not_found"]), json_out, html_out,
    )
    return output


def indication_names(cdp_output: dict, source: object = "the cdp output") -> list[str]:
    """The distinct indication names of a cdp written output, in order;
    ConfigError when it is not one (indication value missing or empty)."""
    bad = ConfigError(f"{source}: not a cdp extraction written by this code (indication value missing or empty)")
    if not isinstance(cdp_output, dict) or not isinstance(cdp_output.get("indications"), list):
        raise bad
    names = []
    for entry in cdp_output["indications"]:
        ind = entry.get("indication") if isinstance(entry, dict) else None
        value = ind.get("value") if isinstance(ind, dict) else None
        if isinstance(value, list) and value:
            value = value[0]
        if not isinstance(value, str) or not value.strip():
            raise bad
        names.append(value.strip())
    return list(dict.fromkeys(names))


def _check_file(file: Path) -> Path:
    file = Path(file)
    if not file.is_file():
        raise ExtractionError(f"file not found: {file}")
    if file.suffix.lower() not in SUFFIXES:
        raise ExtractionError(f"{file.name}: the file must be one of {', '.join(SUFFIXES)}")
    return file


def _settings(cfg: StructConfig | None, file: Path, out_dir: Path | None, command: list[str] | None, name: str) -> tuple:
    return (cfg if cfg is not None else StructConfig.from_env(),
            Path(out_dir) if out_dir is not None else file.parent,
            list(command) if command is not None else [f"structx.steps.{name}", file.name])


def extract_output(file: Path, spec: Spec, cfg: StructConfig) -> dict:
    """Runs `spec` on `file` and returns the written output, writing nothing:
    no run log, no JSON, no HTML (`extract_file` is the same run with those
    files). ExtractionError when the run fails."""
    return written_output(run(file, spec, cfg=cfg), spec)


def _extract(file: Path, spec: Spec, cfg: StructConfig, out_dir: Path, command: list[str], write_files: bool) -> dict:
    """One run: with its files (`extract_file`) or without (`extract_output`); ExtractionError either way on failure."""
    if not write_files:
        return extract_output(file, spec, cfg)
    output = extract_file(file, spec, cfg, out_dir, command=command)
    if output is None:
        raise ExtractionError(f"The {spec.name} extraction of {file.name} failed (see its run log).")
    return output


def extract_cdp(file: Path, *, cfg: StructConfig | None = None, out_dir: Path | None = None,
                command: list[str] | None = None, write_files: bool = True, fields: list[str] | None = None) -> CdpExtraction:
    """Step 1: the indications and the CDP values of the asset (`cdp` spec).
    ExtractionError when the run fails or finds no indication. `write_files=False`:
    nothing is written (no run log, JSON or HTML) and `out_dir` is ignored.
    `fields`: the ids of the fields to extract (the `indications` list always
    is); default every field. The API passes the fields in the asset's launch
    scope (README 5.4); the others are not sent to the model and are absent
    from the output."""
    file = _check_file(file)
    spec = _restrict(load_spec("cdp"), fields)
    cfg, out_dir, command = _settings(cfg, file, out_dir, command, "extract_cdp")
    output = _extract(file, spec, cfg, out_dir, command, write_files)
    names = indication_names(output, file.name)
    if not names:
        raise ExtractionError(f"The cdp extraction of {file.name} found no indications; there is nothing to map the other files to.")
    return CdpExtraction(output, names)


def _restrict(spec: Spec, fields: list[str] | None) -> Spec:
    """The spec with only `fields` (its key list always kept), or the whole spec for None."""
    return spec if fields is None else with_fields(spec, fields)


def _mapped_run(spec_name: str, file: Path, indications: list[str], *, cfg: StructConfig | None,
                out_dir: Path | None, command: list[str] | None, func: str, write_files: bool, fields: list[str] | None) -> dict:
    """A spec run with `indications` injected as the `indication` categories, restricted to `fields` when given."""
    file = _check_file(file)
    names = [n.strip() for n in indications] if isinstance(indications, (list, tuple)) else []
    if not names or any(not isinstance(n, str) or not n for n in names):
        raise ConfigError(f"{func}: indications must be a non-empty list of non-empty names (from extract_cdp).")
    spec = _restrict(with_sub_options(load_spec(spec_name), "indication", list(dict.fromkeys(names))), fields)   # inject first: the restriction may keep the key list alone
    cfg, out_dir, command = _settings(cfg, file, out_dir, command, func)
    return _extract(file, spec, cfg, out_dir, command, write_files)


def extract_ci(file: Path, indications: list[str], *, cfg: StructConfig | None = None,
               out_dir: Path | None = None, command: list[str] | None = None, write_files: bool = True, fields: list[str] | None = None) -> dict:
    """Step 2a: the competitors of the CI deck (`ci` spec), every record
    mapped to one of `indications` (the names from `extract_cdp`).
    `write_files=False`: nothing is written, `out_dir` is ignored. `fields`:
    the records fields to extract (the competitor list always is); default all."""
    return _mapped_run("ci", file, indications, cfg=cfg, out_dir=out_dir, command=command, func="extract_ci", write_files=write_files, fields=fields)


def extract_protocol(file: Path, indications: list[str], *, cfg: StructConfig | None = None,
                     out_dir: Path | None = None, command: list[str] | None = None, write_files: bool = True, fields: list[str] | None = None) -> dict:
    """Step 2b: the studies of the trial protocol concept sheet (`protocol`
    spec; .docx sent as the PDF Word saves), every record mapped to one of
    `indications` (the names from `extract_cdp`).
    `write_files=False`: nothing is written, `out_dir` is ignored. `fields`:
    the records fields to extract (the study list always is); default all."""
    return _mapped_run("protocol", file, indications, cfg=cfg, out_dir=out_dir, command=command, func="extract_protocol", write_files=write_files, fields=fields)
