"""`structx`: the extraction pipeline, one sub-package per activity, in the
order a run goes through them:

    settings/   config.py (the `.env` settings), spec.py (what to extract)
    source/     source.py, powerpoint.py, export_pdf.ps1 (the deck: PDF + its own text)
    model/      llm.py (one gateway call), extract.py (the calls, the pipeline)
    checks/     grounding.py, scoring.py (quotes, values, confidence)
    output/     output.py, report.py, runlog.py (the JSON, the HTML, the run log)
    join/       combine.py (the CDP + CI + protocol join), database.py (the indication table + one table per file)
    steps.py    the workflow as functions: extract_cdp, extract_ci, extract_protocol

The entry points stay in the project folder: `cli.py` (one deck, one spec)
and `workflow.py` (CDP + CI). Run them from there; the package needs no
install. Imports are absolute (`from structx.checks.grounding import ...`).
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # the folder with .env, cli.py
