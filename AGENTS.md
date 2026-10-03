# batzen: notes for agents working on this codebase

This file is for developing batzen itself. Agents *operating a book* read the
`AGENTS.md` that `batzen init` writes into every book (`src/batzen/data/BUCH_AGENTS.md`).

## Architecture
- `files.py`: Markdown tables, YAML frontmatter, amount/date parsing. Writers are deterministic (stable git diffs, stable hashes).
- `book.py`: `Book` loads one book folder: settings, chart of accounts, journal rows. No arithmetic.
- `ledger.py`: `BalanceEngine` (signed balances, year chaining, result folded into Gewinnvortrag), Saldenliste, Kontoblatt.
- `statements.py`: OR 959a/b groups, Jahresrechnung, Gewinnverwendung, Anhang.
- `journal.py`: posting, Beleg numbers, lock check, storno, proposals.
- `invoices.py`, `qrbill_ch.py`: customers, frozen invoices, payments/credit notes as `Quelle` rows, Swiss QR-bill.
- `payroll.py`, `qst.py`, `qst_import.py`, `lohnausweis.py`: payroll.
- `check.py`: every invariant + period lock hashes. `api.py`: the operations (guard → write → check → git commit).
- `cli.py` and `mcp_server.py` are thin layers over `api.py`. New features go into `api.py` first.

## Rules
- Money is `Decimal`, rounded half-up to cents (`files.CENT`). Never use floats for amounts.
- Every write goes through `api._guard` and `api._done`, so the book is checked and committed.
- Documents that own journal rows (`Quelle`) must regenerate those rows deterministically; `check` compares them.
- Payroll logic was ported 1:1 from a production system and verified to the cent; change it only with a test that cites the source (KS 45, AHV-Merkblatt, …).
- User-facing text is German (Swiss spelling, no ß). Code and comments are English.

## Tests
```bash
pip install -e ".[mcp,dev]"
pytest
```
Tests build throwaway books in a temp dir; they never touch a real book.
