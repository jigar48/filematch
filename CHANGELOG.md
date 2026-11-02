# Changelog

All notable changes to this project are documented here.

## [0.1.1] - 2026-11-02

### Fixed

- Duplicate-key classification. A key is now ambiguous if it occurs more than once in File A **or** more than once in File B, and every row with an ambiguous key in **both** files is classed as duplicate key and excluded from the join. Previously, a key repeated in one file but present once in the other left that single row classed as "only in A/B", which was misleading. "Only in A/B" now means the key is absent from the other file.

### Changed

- Demo data: one key duplicated in A now also appears once in B, so the demo exercises the rule above. `demo/expected.json` was regenerated: File B has 1,987 rows and there are 17 duplicate-key rows (was 1,986 and 16).

## [0.1.0] - 2026-11-01

First release.

### Added

- Local web app (`filematch` command, FastAPI + Uvicorn, single worker) on `127.0.0.1:8765`, with a `--port` flag. The bind host comes from `FILEMATCH_HOST` only.
- Upload of File A (legacy), File B (target) and an optional File C (mapping file) as `.xlsx` or `.csv`. Uploads are streamed into a per-session temp directory with a per-file limit (`FILEMATCH_MAX_UPLOAD_MB`, default 200).
- CSV reading as text only: UTF-8 (BOM-aware) with a Windows-1252 fallback, and delimiter sniffing among `, ; tab |`. XLSX reading of the first sheet via calamine, every value as text.
- Upload summary per file: rows, columns, empty cells, and duplicate keys on the first column.
- Column mapping: header normalisation, a small synonym list, rapidfuzz fuzzy matching (≥ 60) and one-to-one assignment, each with a confidence and source label. Every row is editable, and any column can be ignored.
- Mapping-file import with per-row error messages that name the file.
- Composite keys of 1–4 columns per side.
- Optional AI suggestions (`pip install "filematch[ai]"` plus `ANTHROPIC_API_KEY`). You see exactly what will be sent before anything is sent: header names and up to 5 samples of up to 50 characters per column. Default model `claude-haiku-5-5`, overridable with `FILEMATCH_AI_MODEL`.
- Key-based compare with trim, case-insensitive, numeric-tolerance and empty-equals-null options. Rows are classed as matched, mismatched, only in A, only in B, or duplicate key. Duplicate-key rows are excluded from the join.
- Results cached in Parquet, two tabs (all rows / not matched), 100-row server-side pages, and mismatched cells highlighted with a screen-reader marker.
- Downloads: both tabs as `.xlsx` (frozen header, autofilter, red mismatch cells) and the not-matched tab as CSV. Above Excel's row limit the xlsx download is refused and a CSV is offered.
- Security: extension and content checks, an xlsx zip-bomb guard, a 500-column cap, CSP / nosniff / no-referrer headers, a trusted-host check, autoescaped templates, and formula-injection guards on export.
- Session cleanup on "Clear session", after 60 minutes idle, and on shutdown.
- Fake demo dataset generator, benchmark script, screenshot script, Dockerfile, and CI (ruff, pytest on Python 3.11–3.13, docker build and run).

[0.1.1]: https://github.com/jigar48/filematch/releases/tag/v0.1.1
[0.1.0]: https://github.com/jigar48/filematch/releases/tag/v0.1.0
