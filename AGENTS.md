# allkvitt: notes for agents working on this codebase

This file is for developing allkvitt itself. Agents *operating a book* read the
`AGENTS.md` that `allkvitt init` writes into every book (`src/allkvitt/data/BUCH_AGENTS.md`).

## Architecture
- `files.py`: Markdown tables, YAML frontmatter, amount/date parsing. Writers are deterministic (stable git diffs, stable hashes).
- `book.py`: `Book` loads one book folder: settings, chart of accounts, journal rows. No arithmetic.
- `ledger.py`: `BalanceEngine` (signed balances, year chaining, result folded into Gewinnvortrag), Saldenliste, Kontoblatt.
- `statements.py`: OR 959a/b groups, Jahresrechnung, Gewinnverwendung, Anhang.
- `journal.py`: posting, Beleg numbers, lock check, storno, proposals.
- `invoices.py`, `qrbill_ch.py`: customers, frozen invoices, payments/credit notes as `Quelle` rows, Swiss QR-bill.
- `payroll.py`, `qst.py`, `qst_import.py`, `lohnausweis.py`: payroll.
- `check.py`: every invariant + period lock hashes. `api.py`: the operations (guard → write → check → git commit).
- `cli.py`, `mcp_server.py` and `web/` are thin layers over `api.py`. New features go into `api.py` first.
- `tools.py`: the one agent tool registry, used by the MCP server and the UI's chat panel.
- `web/app.py`: Starlette app, token/CSRF middleware, file serving, SSE live refresh (watchfiles).
  `web/views.py`: one handler per page/form; forms post via HTMX and get `204 + HX-Redirect` or an error box.
  `web/chat.py`: Claude via the SDK tool runner over `tools.py`; commits it makes are attributed to the agent.
  Templates are Jinja in `web/templates/`, styles in `web/static/allkvitt.css` (tokens at the top, light + dark).
- `web/auth.py`: server mode (`allkvitt serve`): users outside the book, scrypt hashes, signed session cookies,
  roles, login throttle. The middleware in `web/app.py` sets the git author per request.
- `plugins.py`: plugin API (pluggy hooks, discovery via entry points `allkvitt.plugins`, per-book activation in
  `allkvitt.yaml → plugins`). `builtin.py` provides the core's own data and the camt.053 format through the same hooks.
  `testing.py`: helpers for plugin tests. Example plugins and the template live in `plugins/`; docs in `docs/plugins.md`.
  When a feature could be a plugin (a bank, a canton, an export), prefer a plugin over growing the core.
- `erfassung.py`: Belegeingang → drafts (`eingang/`) of three kinds (kreditor, quittung, debitor; legacy drafts in
  `kreditoren/entwuerfe/` are still read). Receipts are matched with open bank movements / existing bookings so
  nothing is booked twice; own invoices from outside allkvitt become `invoices.record_external`. Reader chain (QR, PDF text, Tesseract OCR,
  plugin readers via `allkvitt_beleg_leser`), account chain (known supplier → Jev → agent via `web/chat` backends,
  writing through the `complete_bill_draft` tool). Never books; IBAN/name conflicts are flagged, never auto-matched.
- `spesen.py`: employee expense claims (own their rows on 2210); `payroll.run/close` add open claims to the payslip
  (`werte.spesen`, `auszahlung`) without touching the wage calculation; Lohnausweis 13.1.2.
- `mahnungen.py`: reminders (3 steps, PDF with QR-bill for the open amount). Plugin pages: `plugins.Page`, routes
  `/p/<plugin>/<slug>`, templates from the plugin's `templates/` folder (`web/app.py` `_PluginTemplates`).
- `marktplatz.py`: plugin catalog (bundled JSON or `ALLKVITT_PLUGIN_KATALOG` https URL), maintainer review with a
  SHA-256 over the plugin's source files, install via pip (local UI only, then `web.app.restart_later`).
- `bank.py` (camt.053 import, matching, reconciliation), `kreditoren.py` (QR scan, pain.001), `mwst.py` (incl. eCH-0217).
- `bankformat.py`: statements in other formats. CSV/Excel: the agent describes the *format* once (`bank/formate/`,
  confirmed by a person), allkvitt reads the numbers. Credit card PDFs: the agent transcribes the transactions
  (`bank/karten/<sha>.yaml`); imported only if the balance adds up and every amount is in the PDF text. Both are
  served to the import as a `BankFormat` (fallback in `plugins.bank_format_for`), so reconciliation re-reads them.
- `dossier.py`: Abschlussunterlagen (`allkvitt dossier`): a registry of parts (`TEILE`, plus plugin parts via
  `allkvitt_dossier_teile`) that build PDF/CSV for both the ZIP and single downloads; Belegordner with stamped pages,
  Lückenverzeichnis (`belegluecken`), manifest with SHA-256. The Beleg number in the journal is the only running
  number; `Book.remember_numbers` keeps the high-water mark in `.allkvitt/belegnummern.yaml` so it is never reused.
- `austausch.py`: the `.allkvitt` file (whole book + git bundle, optional AES-256-GCM/scrypt), import with hash,
  path and version checks.
- `reports.py`: reports for any period, one table shape for every output (`spalten`, `zeilen` with `werte` per
  column key and account detail): Erfolgsrechnung/Bilanz with month/quarter columns and comparison (Vorperiode,
  Vorjahr, Budget), Geldfluss (indirect, must reconcile with cash), Kennzahlen, aging, revenue; registry + plugin hook
  `allkvitt_reports`. For a full year it must equal `statements.year_end_statement` (tested). `budget.py`:
  budget/<JJJJ>.yaml per account and month. `berichte.py`: text/PDF/xlsx/CSV with the Stand (commit, check status),
  templates and comments in `auswertungen/` (comments carry a fingerprint of the figures → «veraltet»), monthly
  package in `berichte/` (not in git). `mail.py`: SMTP, password only from `ALLKVITT_SMTP_PASSWORD`.
  UI: `web/berichte_views.py` (Berichte, Budget grid); the Kontoblatt takes `von`/`bis` for the drill-down.
- Writes are serialised by a lock file (`.allkvitt/write.lock`) so UI, CLI, MCP and chat never interleave.

## Rules
- Money is `Decimal`, rounded half-up to cents (`files.CENT`). Never use floats for amounts.
- Every write goes through `api._guard` and `api._done`, so the book is checked and committed.
- Documents that own journal rows (`Quelle`) must regenerate those rows deterministically; `check` compares them.
- Payroll logic was ported 1:1 from a production system and verified to the cent; change it only with a test that cites the source (KS 45, AHV-Merkblatt, …).
- User-facing text is German (Swiss spelling, no ß). Code and comments are English.

## Tests
```bash
pip install -e ".[ui,mcp,dev]"
pytest
```
Tests build throwaway books in a temp dir; they never touch a real book.
