# Public generation showcase

This directory is a sanitized snapshot of the server's PPT generation evidence.

## Included

- `logs/`: every generation log found at snapshot time. Credentials, session values, uploaded source content, and absolute server paths are redacted.
- `records/index.csv`: all recorded jobs with timestamps, status, page setting, model, image flag, duration, and privacy-safe presence flags.
- `records/outputs-index.csv`: generated PPTX output inventory (metadata only).
- `records/beb9df56e190/`: a successful 10-page case.
- `records/68ed86f6b68c/`: a successful 10-page case with timeouts, 429 retries, and quality-loop failures before recovery.
- `ISSUES.md`: the normal generation flow and the observed failure points.

The original uploads, `app.db`, `.env`, API keys, user/session tables, server backups, and other runtime data are not included. The two PPTX files are included because they were explicitly selected as public examples; other production outputs remain metadata-only.
