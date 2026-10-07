---
name: aeradex
description: Operate a Swiss aeradex accounting book (Buchhaltung, Lohn, QR-Rechnungen) via the aeradex CLI or MCP server. Use when the user asks to book receipts, issue invoices, record payments, run payroll, create a Lohnausweis, or produce a Bilanz/Erfolgsrechnung in a folder containing aeradex.yaml.
---

# aeradex

1. Find the book: the nearest folder upward with `aeradex.yaml` (or `--buch <path>`). Read its `AGENTS.md` first. It holds the rules for that book.
2. Run every command with `--json`. On `{"ok": false}` read `fehler` and fix the cause; do not retry blindly.
3. Never compute balances, totals, wages or tax yourself. Ask `aeradex balance|ledger|report|payroll run`.
4. Free postings: `aeradex propose … --begruendung "…"` unless `agent_modus: direkt`. Then tell the user what to approve.
5. Never edit booked rows, rows with a `Quelle`, issued invoices, closed payslips or locked periods by hand. Use `reverse`, `invoice void|credit`, `payroll reopen`.
6. After any manual file edit: `aeradex check --json`.
7. Report what you did with the Beleg numbers and the commit hashes from the output.
