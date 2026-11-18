"""Data Submission: running the AI team's extraction (the `structx` package) on an asset's uploaded CDP / CI / Protocol files.

    extraction.py   checks, background run, status       (upload -> call the extraction functions)
    validation.py   checks on the returned JSON          (receive -> validate)
    storage.py      one transaction into structx.sqlite  (map -> save)

The `structx` package itself is not edited and is imported only when an extraction runs, so the API starts even where the
package's own dependencies (openai, python-pptx, pypdfium2) are not installed yet.
"""
