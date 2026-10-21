# aeradex: notes for agents working on this codebase

This file is for developing aeradex itself. Agents *operating a book* read the
`AGENTS.md` that `aeradex init` writes into every book (`src/aeradex/data/BUCH_AGENTS.md`).

## Architecture
- `files.py`: Markdown tables, YAML frontmatter, amount/date parsing. Writers are deterministic (stable git diffs, stable hashes).
- `book.py`: `Book` loads one book folder: settings, chart of accounts, journal rows. No arithmetic.
- `ledger.py`: `BalanceEngine` (signed balances, year chaining, result folded into Gewinnvortrag), Saldenliste, Kontoblatt.
- `statements.py`: OR 959a/b groups, Jahresrechnung, Gewinnverwendung, Anhang.
- `journal.py`: posting, Beleg numbers, lock check, storno, proposals.
- `invoices.py`, `qrbill_ch.py`: customers, frozen invoices, payments/credit notes as `Quelle` rows, Swiss QR-bill.
- `kunden.py`: the customer as a CRM: `dossier` (invoices, revenue like `reports.umsatz`, payment behaviour, timeline),
  contact log + follow-ups in the customer's frontmatter; plugins add panels via `aeradex_kunde_teile` (`KundeTeil`).
- `payroll.py`, `qst.py`, `qst_import.py`, `lohnausweis.py`: payroll.
- `lohnmeldung.py`: company Lohnkonto (`payroll.lohnkonto_matrix`, month matrix like the Command Station) and the
  annual wage declarations (Ausgleichskasse, UVG, KTG, BVG, QST) from closed payslips, with Prüfhinweise for what
  the calculation does not model (Rentnerfreibetrag, Jugendliche, Höchstlohn, NBU < 8 h) and a ledger check.
- `check.py`: every invariant + period lock hashes. `api.py`: the operations (guard → write → check → git commit).
- `cli.py`, `mcp_server.py` and `web/` are thin layers over `api.py`. New features go into `api.py` first.
- `tools.py`: the one agent tool registry, used by the MCP server and the UI's chat panel.
- `web/app.py`: Starlette app, token/CSRF middleware, file serving, SSE live refresh (watchfiles).
  `web/views.py`: one handler per page/form; forms post via HTMX and get `204 + HX-Redirect` or an error box.
  `web/chat.py`: Claude via the SDK tool runner over `tools.py`; commits it makes are attributed to the agent.
  Subclasses (other channels, plugins) narrow `tool_list()`, add `server_tools()` (e.g. web search) and
  `extra_instructions`, and set `author_label`/`effort`; text events carry `raw` besides the rendered `html`.
  `mcp_server.py` serves only the tools named in `AERADEX_MCP_TOOLS` when it is set.
  Templates are Jinja in `web/templates/`, styles in `web/static/aeradex.css` (tokens at the top, light + dark).
- `web/auth.py`: server mode (`aeradex serve`): users outside the book, scrypt hashes, signed session cookies,
  roles, login throttle. The middleware in `web/app.py` sets the git author per request. Without e-mail, `Invites`
  are one-time links (only their SHA-256 stored, 7 days): an invitation up to the inviter's own role, or a password
  link an admin makes; `UserLog` (`benutzer-log.jsonl`) is the users' own trail. Pages in `web/benutzer_views.py`:
  `/konto` (own name and password), `/benutzer` (list, invite; roles/removal/password links admin only, `ADMIN_ONLY`),
  `/einladung/<token>` (public like `/login`); `/konto` and `/benutzer/einladen` are open to every role (`SELF_SERVICE`).
- `gitlog.beleg_index`: who booked what, read from the audit trail itself (journal rows added/changed/removed per
  commit, cached per HEAD in `.aeradex/lokal/belegindex.json`, extended incrementally). Journal column «Von»,
  `/verlauf?beleg=|von=|q=`, `api.booking_history`.
- `plugins.py`: plugin API (pluggy hooks, discovery via entry points `aeradex.plugins`, per-book activation in
  `aeradex.yaml → plugins`). `builtin.py` provides the core's own data and the camt.053 format through the same hooks.
  `testing.py`: helpers for plugin tests. Example plugins and the template live in `plugins/`; docs in `docs/plugins.md`.
  When a feature could be a plugin (a bank, a canton, an export), prefer a plugin over growing the core.
- Plugins for a book with a team and a server: `aeradex-team` (sync of the employees' phone app into Leistungen,
  requests, check list, encrypted payroll documents; protocol in its `PROTOCOL.md`) with `aeradex-team-portal`
  (the phone app + sync API, Starlette/SQLite, no aeradex dependency; one `kennung` per installation names cookie,
  header and key salts), `aeradex-zugriff` (ssh keys for Claude Code, locked to the forced command `aeradex-ssh`),
  `aeradex-anruf` + `aeradex-telegram` (voice/Telegram assistant over `web/chat.py`). Server templates: `deploy/`.
  Nothing customer-specific in them: names, domains, logos and trade rules are settings or branding.
- `erfassung.py`: Belegeingang → drafts (`eingang/`) of three kinds (kreditor, quittung, debitor; legacy drafts in
  `kreditoren/entwuerfe/` are still read). Receipts are matched with open bank movements / existing bookings so
  nothing is booked twice; own invoices from outside aeradex become `invoices.record_external`. Reader chain (QR, PDF text, Tesseract OCR,
  plugin readers via `aeradex_beleg_leser`), account chain (known supplier → Jev → agent via `web/chat` backends,
  writing through the `complete_bill_draft` tool). Jev also picks, among the amounts and dates on the document, the total and
  the invoice date, and the Vorsteuer code (`jev.read_bill`): a confident pick fills an empty field, a different one only
  warns; with a known supplier Jev still checks the account and a confident other one warns. The agent runs when account, amount or date is missing, or a period the text names was not read (`missing`). Never books; IBAN/name conflicts are flagged, never auto-matched.
- `auslesen.py`: «Beleg auslesen» in the journal grid: a receipt (Belegeingang readers + Jev) or a card statement (the
  book's agent via `propose_journal_lines`, every amount checked against the text) → grid lines with the counter account
  open; file and reading in `.aeradex/lokal/auslesen/<ref>/` until booked. `post_entries` books lines with the same
  `gruppe` as one Beleg and files the receipt once.
- `jahresabschluss.py`: the year-end checklist (Abschluss page, phases laufend → abschliessen; status from the books,
  ticks in `abschluss/<J>/checkliste.yaml`, plugin steps via `aeradex_abschluss_schritte`) and the closing entries
  per 31.12.: stocks (angefangene Arbeiten, Material, Delkredere, Ferien/Überstunden) booked as the difference to the
  account's balance, Privatanteile Privat an Aufwand; rows owned as `Quelle jahresabschluss:<J>:<art>` (builtin
  Source), proposals from plugins via `aeradex_abschluss_vorschlaege`.
- `jahreswechsel.py`: business years: bookings only in open years (first year … last opened year or the current
  calendar year; `year_problem` via `journal.lock_problem`), opening the next year, moving the first year, the
  change-of-year checklist. `eroeffnung.py`: the Eröffnungsbilanz of the first year from the old bookkeeping:
  closing figures per account (`vorjahr`, reading convention in the UI) folded into `eroeffnung` (Privat, result
  account, unbooked profit → Gewinnvortrag); import of a Saldenliste (CSV/Excel/pasted, sign convention detected
  by what balances), staged in `.aeradex/lokal/eroeffnung.yaml` until saved. UI: Einstellungen › Geschäftsjahre, `/eroeffnung`.
- `spesen.py`: employee expense claims (own their rows on 2210); `payroll.run/close` add open claims to the payslip
  (`werte.spesen`, `auszahlung`) without touching the wage calculation; Lohnausweis 13.1.2.
- `mahnungen.py`: reminders (3 steps, PDF with QR-bill for the open amount). Plugin pages: `plugins.Page`, routes
  `/p/<plugin>/<slug>`, templates from the plugin's `templates/` folder (`web/app.py` `_PluginTemplates`).
- `marktplatz.py`: plugin catalog (bundled JSON or `AERADEX_PLUGIN_KATALOG` https URL), maintainer review with a
  SHA-256 over the plugin's source files, install via pip (local UI only, then `web.app.restart_later`).
- `bank.py` (camt.053 import, matching, reconciliation), `kreditoren.py` (QR scan, pain.001), `mwst.py` (incl. eCH-0217).
- `bankformat.py`: statements in other formats. CSV/Excel: the agent describes the *format* once (`bank/formate/`,
  confirmed by a person), aeradex reads the numbers. Credit card PDFs: the agent transcribes the transactions
  (`bank/karten/<sha>.yaml`); imported only if the balance adds up and every amount is in the PDF text. Both are
  served to the import as a `BankFormat` (fallback in `plugins.bank_format_for`), so reconciliation re-reads them.
- `dossier.py`: Abschlussunterlagen (`aeradex dossier`): a registry of parts (`TEILE`, plus plugin parts via
  `aeradex_dossier_teile`) that build PDF/CSV for both the ZIP and single downloads; Belegordner with stamped pages,
  Lückenverzeichnis (`belegluecken`), manifest with SHA-256. The Beleg number in the journal is the only running
  number; `Book.remember_numbers` keeps the high-water mark in `.aeradex/belegnummern.yaml` so it is never reused.
- `austausch.py`: the `.aeradex` file (whole book + git bundle, optional AES-256-GCM/scrypt), import with hash,
  path and version checks.
- `reports.py`: reports for any period, one table shape for every output (`spalten`, `zeilen` with `werte` per
  column key and account detail): Erfolgsrechnung/Bilanz with month/quarter columns and comparison (Vorperiode,
  Vorjahr, Budget), Geldfluss (indirect, must reconcile with cash), Kennzahlen, aging, revenue; registry + plugin hook
  `aeradex_reports`. For a full year it must equal `statements.year_end_statement` (tested). `budget.py`:
  budget/<JJJJ>.yaml per account and month. `berichte.py`: text/PDF/xlsx/CSV with the Stand (commit, check status),
  templates and comments in `auswertungen/` (comments carry a fingerprint of the figures → «veraltet»), monthly
  package in `berichte/` (not in git). `mail.py`: SMTP, password only from `AERADEX_SMTP_PASSWORD`.
  UI: `web/berichte_views.py` (Berichte, Budget grid); the Kontoblatt takes `von`/`bis` for the drill-down.
- Writes are serialised by a lock file (`.aeradex/write.lock`) so UI, CLI, MCP and chat never interleave.

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
