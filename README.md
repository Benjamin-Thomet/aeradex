# batzen

**Swiss bookkeeping your agent can run.**
Die erste agentenorientierte Open-Source-Buchhaltung für die Schweiz: Finanzbuchhaltung, Lohnbuchhaltung und QR-Rechnungen, gespeichert als lesbare Textdateien in git, bedient von dir oder von einem LLM-Agenten.

> *Batzen*: alte Schweizer Münze, und auf Schweizerdeutsch einfach Geld («e Batze Gäld»).

---

## Warum noch eine Buchhaltung?

Klassische Buchhaltungssoftware versteckt die Bücher in einer Datenbank hinter einer Oberfläche. Ein Agent (Claude Code, opencode, Codex …) kann damit kaum arbeiten: Er klickt sich durch Formulare oder ruft eine API auf, die nie für ihn gebaut wurde.

batzen dreht das um:

| | |
|---|---|
| **Dateien statt Datenbank** | Journal als Markdown-Tabelle pro Monat, Kunden, Mitarbeitende, Rechnungen und Lohnabrechnungen als Markdown mit YAML-Frontmatter. Ein Mensch liest sie in jedem Editor, ein LLM ohne Adapter. |
| **Rechnen tut die Engine, nie das LLM** | Salden, Bilanz, Löhne, Quellensteuer, QR-Referenzen: alles deterministisches Python. Der Agent entscheidet *was* gebucht wird, die Engine prüft und rechnet. |
| **git ist das Audit-Trail** | Jede Änderung ist ein Commit mit sprechender Nachricht. Ein pre-commit-Hook führt `batzen check` aus, ein ungültiges Buch lässt sich nicht committen. |
| **Unveränderlichkeit, wo das Gesetz sie verlangt** | Gesperrte Perioden sind gehasht, ausgestellte Rechnungen und abgeschlossene Lohnabrechnungen tragen einen Fingerprint. Jede nachträgliche Änderung fällt auf (GeBüV). |
| **Mensch im Loop** | Im Standardmodus darf ein Agent freie Buchungen nur *vorschlagen*; du gibst sie mit `batzen approve` frei. |
| **Schweizer Recht eingebaut** | KMU-Kontenrahmen, Bilanz und Erfolgsrechnung nach OR 959a/959b, Anhang, Gewinnverwendung, MWST (effektiv und Saldosteuersatz, Abrechnung nach ESTV-Ziffern), Swiss QR-Bill (QRR/SCOR), AHV/IV/EO/ALV/UVG/KTG/BVG/FAK, Quellensteuer-Tarife nach KS 45, Lohnausweis Formular 11. |

## Schnellstart

```bash
pip install -e ".[ui,mcp,scan]"  # Python ≥ 3.11

batzen init ~/buchhaltung/muster --firma "Muster GmbH" --jahr 2026 \
  --strasse Bahnhofstrasse --nr 1 --plz 3000 --ort Bern \
  --iban "CH93 0076 2011 6238 5295 7" --uid CHE-123.456.789
cd ~/buchhaltung/muster

# Eröffnungsbilanz in kontenplan.yaml eintragen (eroeffnung: 20000 bei 1020, -20000 bei 2800), dann:
batzen check
batzen book --datum 2026-01-05 --soll 6500 --haben 1020 --betrag 45.80 \
  --text "Büromaterial" --datei inbox/quittung.pdf
batzen balance
```

### Die Oberfläche

```bash
batzen ui                        # öffnet batzen im Browser (nur lokal, mit Zugangsschlüssel im Link)
```

Eine lokale Web-App über denselben Kern wie CLI und Agenten. Jeder Klick wird geprüft und in git festgehalten.

| Bereich | Was du dort machst |
|---|---|
| **Übersicht** | Liquidität, Ergebnis, offene Debitoren, was ansteht, letzte Änderungen |
| **Prüfen** | Inbox mit Vorschau (PDF, Bild, Text), Buchung daneben erfassen, Agenten-Vorschläge freigeben, Lohnentwürfe abschliessen |
| **Journal** | Monate, Suche und Filter, einfache und Sammelbuchungen mit Beleg-Datei, Storno, Belege nachreichen |
| **Konten** | Kontenplan bearbeiten, Saldenliste nach Periode, Kontoblatt |
| **Debitoren** | Kunden, Rechnungen mit Live-Total und QR-PDF, Zahlung zuordnen, Gutschrift, Storno, offene Posten |
| **Bank** | camt.053 importieren, automatische Zuordnung, offene Bewegungen buchen/zuordnen/ignorieren, Saldoabstimmung |
| **Kreditoren** | QR-Rechnungen aus der Inbox erkennen und erfassen, Lieferanten, offene Posten, Zahlungslauf als pain.001-Datei, Ausführung verbuchen |
| **Lohn** | Lohnlauf pro Monat, Eingaben (Stunden, QST, Korrekturen), Abschluss, Lohnkonto, Lohnausweis |
| **MWST** | Abrechnung je Quartal/Semester nach ESTV-Ziffern, Belege je Code, Buchen, PDF-Hilfsblatt |
| **Abschluss** | Bilanz und Erfolgsrechnung mit Vorjahr und Drill-down, Gewinnverwendung, Anhang, Periode sperren |
| **Verlauf** | jeder Commit mit Diff, Änderungen von Agenten markiert |
| **Einstellungen** | Firma, IBAN-Prüfung, Systemkonten, Lohnsätze, Agentenmodus |

Rechts sitzt der **Agent** (Claude): «Bereite die Quittungen in der Inbox vor», «Welche Rechnungen sind überfällig?». Er arbeitet mit denselben Werkzeugen wie der MCP-Server; seine Vorschläge erscheinen sofort unter *Prüfen*. Er läuft über dein lokales **Claude Code** (dein Login, kein API-Schlüssel nötig) oder, falls gesetzt, über die Claude API (`ANTHROPIC_API_KEY`); `BATZEN_CHAT_BACKEND=api|claude-code` erzwingt eines davon. Mit Claude Code darf er zusätzlich Dateien in `inbox/` und `belege/` lesen (auch PDFs und Fotos), aber nichts direkt ändern. Ändert ein Agent oder das CLI das Buch, aktualisiert sich die offene Seite selbst.

Tastatur: `N` neue Buchung, `/` Suche, `A` Agent, `⌘/Ctrl+Enter` Formular absenden.

### Auf dem eigenen Server (mit Login)

```bash
batzen user add benjamin --rolle admin --anzeige "Benjamin Thomet"
batzen user add treuhand --rolle lesen
batzen --buch /srv/buecher/muster serve --port 8080 --https     # hinter Caddy/nginx mit HTTPS
```
Rollen `lesen`, `buchhaltung`, `admin`; jede Änderung trägt im git-Verlauf den Namen der angemeldeten Person.
Anleitung mit systemd, Caddy und Datensicherung: [docs/server.md](docs/server.md).

### Debitoren und QR-Rechnungen
```bash
batzen customer add --name "Anna Beispiel" --firma "Beispiel AG" --strasse Marktgasse --nr 5 --plz 3011 --ort Bern
batzen invoice create --kunde K0001 --pos "Beratung;10 h;150" --pos "Spesen;1;80;3600"
#   → rechnungen/2026/R-2026-0001.md (eingefroren) + .pdf mit QR-Einzahlungsschein, verbucht 1100 an 3400/3600
batzen invoice match --betrag 1580 --text "<Zeile aus dem Bankauszug>"
batzen invoice pay R-2026-0001 --datum 2026-03-01
batzen receivables --pdf
```

### MWST
```bash
batzen book --datum 2026-01-12 --soll 6500 --haben 1020 --betrag 108.10 --mwst V81 --text "Papier"
#   → 6500 100.00 · 1170 8.10 · an 1020 108.10 (Betrag immer brutto, Steuer wird abgespalten)
batzen mwst abrechnung 2026-Q1      # Ziffern 200 … 500 wie im ESTV-Formular
batzen mwst buchen 2026-Q1          # MWST-Konten auf das Abrechnungskonto 2201
batzen mwst export 2026-Q1          # eMWST-Datei (eCH-0217 v2.0) für den Upload im ESTV-Portal
```

### Kreditoren und Zahlungen
```bash
batzen kreditor scan inbox/rechnung.pdf          # liest den QR-Zahlteil: IBAN, Betrag, Referenz, Lieferant
batzen lieferant add --name "Papeterie Muster AG" --iban CH44… --konto 6500 --mwst V81
batzen kreditor add --lieferant L0001 --betrag 86.40 --referenz 0000… --datei inbox/rechnung.pdf
batzen zahlungslauf erstellen E-2026-0001 E-2026-0002 --datum 2026-03-20   # pain.001 fürs E-Banking
batzen zahlungslauf bezahlt 2026-03-20-ab12cd.xml                         # nach der Ausführung verbuchen
```
Die Zahlungsdatei folgt den Swiss Payment Standards (pain.001.001.09) und validiert gegen die offiziellen
SIX-Schemas SPS 2025 und 2026. QR-Codes lesen: `pip install -e ".[scan]"`.

### Bank (camt.053)
```bash
batzen bank import inbox/auszug-maerz.xml   # Kontoauszug aus dem E-Banking (ISO 20022 camt.053)
batzen bank list --status offen
batzen bank book B1a2b3c4d5e --konto 6940 --text "Kontoführung"
batzen bank zuordnen B… R-2026-0007         # mit Rechnung oder Kreditor begleichen
batzen bank abstimmung                      # Schlusssaldo Bank gegen Buchhaltung
```
Beim Import bucht batzen Zahlungen mit QR-/SCOR-Referenz oder Rechnungsnummer selbst, erkennt Zahlungen aus
eigenen Zahlungsläufen (EndToEndId) und gleicht bereits Gebuchtes (z.B. Löhne) nur ab. Der Rest landet unter
*Prüfen*; der Agent kann zuordnen oder Buchungen vorschlagen.

### Lohn
```bash
batzen employee add --vorname Lea --nachname Muster --monatslohn 6000 --pensum 80 --bvg-betrag 250 \
  --ahv-nr 756.1234.5678.97 --qst-code A0N --qst-kanton BS --qst-jahr 2026
batzen payroll run 2026-01                     # Entwürfe für alle Mitarbeitenden
batzen payroll run 2026-01 --mitarbeiter M0001 --qst-gesamtpensum 80
batzen payroll close 2026-01 M0001             # einfrieren, verbuchen, PDF
batzen payroll lohnausweis 2026 M0001          # Formular 11, ausgefüllt
```

### Abschluss
```bash
batzen report --jahr 2026 --pdf                # Bilanz, Erfolgsrechnung, Gewinnverwendung, Anhang
batzen allocation set 2026 --dividende 5000 --reserve 500
batzen allocation book 2026 --datum 2027-05-20 # nach dem GV-Beschluss
batzen lock 2026-12-31                         # Periode sperren (gehasht)
```

## Mit einem Agenten arbeiten

Jedes Buch enthält ein `AGENTS.md` (und `CLAUDE.md`) mit den Regeln für Agenten. Zwei Wege:

**CLI**: jeder Befehl kann `--json`, Fehler kommen als `{"ok": false, "fehler": "…"}`.

**MCP-Server**: für Claude Code, opencode und andere MCP-Clients:
```bash
claude mcp add batzen -- batzen --buch ~/buchhaltung/muster mcp
```
Dann z.B.: *«Verbuche die Quittungen in der Inbox»*, *«Welche Rechnungen sind über 30 Tage offen?»*, *«Mach den Lohnlauf für Januar»*.

`agent_modus` in `batzen.yaml`:
- `vorschlag` (Standard): Agenten dürfen freie Buchungen nur vorschlagen (`vorschlaege.md`), du gibst frei.
- `direkt`: Agenten dürfen selbst buchen. Rechnungen und Lohnläufe sind in beiden Modi erlaubt, weil sie aus expliziten Eingaben deterministisch entstehen.

## Das Buch auf der Festplatte

```
muster/
├── batzen.yaml              Firma, Bank, Systemkonten, Sperrdatum, agent_modus
├── kontenplan.yaml          Kontenplan mit Eröffnungssalden
├── journal/2026/2026-01.md  Journal: | Datum | Beleg | Text | Soll | Haben | Betrag | Quelle |
├── vorschlaege.md           vom Agenten vorgeschlagen, noch nicht gebucht
├── belege/2026/             Quittungen, Dateiname beginnt mit der Belegnummer
├── inbox/                   Unverarbeitetes für den Agenten
├── kunden/K0001-….md        Kunden
├── rechnungen/2026/         R-2026-0001.md (eingefroren) + .pdf
├── personal/M0001-….md      Mitarbeitende
├── lohn/einstellungen.yaml  Sätze und Konten der Sozialversicherungen
├── lohn/2026/01/M0001.md    Lohnabrechnung (+ .pdf nach Abschluss)
├── lohnausweise/2026/       Formular 11
├── abschluss/2026/          anhang.md, gewinnverwendung.yaml
└── .batzen/locks.yaml       Hashes der gesperrten Periode
```

Das genaue Dateiformat steht in [docs/format.md](docs/format.md).

## Regeln, die `batzen check` durchsetzt

- Jede Journalzeile: gültiges Datum in der richtigen Monatsdatei, existierende Konten, positiver Betrag mit höchstens zwei Nachkommastellen, Belegnummer.
- Jeder Beleg ist ausgeglichen (Soll = Haben), steht an einer Stelle und an einem Datum, ohne doppelte Zeilen.
- Die Eröffnungsbilanz geht auf, Erfolgskonten eröffnen bei null.
- Zeilen mit `Quelle` (`rechnung:`, `zahlung:`, `gutschrift:`, `lohn:`, `abschluss:`) gehören ihrem Dokument und müssen genau dazu passen.
- Ausgestellte Rechnungen und abgeschlossene Lohnabrechnungen sind unverändert (Fingerprint).
- Die gesperrte Periode ist unverändert (Hash pro Monat). Entsperren geht nur mit Grund und wird protokolliert.
- Hinweise: Buchungen ohne Beleg-Datei, unverarbeitete Inbox, Lohn-Entwürfe.

## Herkunft

Die Fachlogik (Saldenmotor mit Jahresverkettung, OR-Gliederung, Lohnberechnung inkl. Quellensteuer nach KS 45, Swiss QR-Bill, Lohnausweis) stammt aus der produktiv genutzten internen Buchhaltung von Thomet GmbH und wurde gegen deren echte Zahlen geprüft: Bilanz, Erfolgsrechnung und Lohnabrechnungen stimmen auf den Rappen.

## Stand und Roadmap

v0.4: Finanzbuchhaltung, MWST mit eMWST-Export (eCH-0217), Bankimport camt.053 mit automatischem Abgleich, Debitoren mit QR-Rechnung, Kreditoren mit QR-Scan und pain.001, Lohn, Agenten-Schnittstelle (CLI + MCP), Web-Oberfläche mit eingebautem Agenten, lokal oder als Server mit Login.

Als Nächstes:
- Weitere Quellensteuer-Kantone, ALV-Höchstgrenze, Swissdec/ELM
- Mehrere Mandanten in einer Instanz
- camt.054 (Sammelgutschriften im Detail), ELM/Swissdec für Lohnmeldungen

Vor dem ersten Einsatz mit echten Rechnungen: ein erzeugtes PDF im offiziellen SIX-Validator prüfen (https://validation.iso-payments.ch/). Sozialversicherungssätze in `lohn/einstellungen.yaml` an deine Ausgleichskasse, UVG- und KTG-Police anpassen.

## Lizenz

[AGPL-3.0-or-later](LICENSE). batzen bleibt offen, auch wenn jemand es als Dienst betreibt.
Mitgeliefert: HTMX (Zero-Clause BSD), Hanken Grotesk und IBM Plex Mono (SIL Open Font License 1.1).

---

### English summary

batzen is an open-source, agent-first accounting and payroll system for Swiss SMEs. Books are plain Markdown/YAML files in git; a deterministic Python engine does every calculation and validation, and the LLM never adds up numbers itself. It ships a CLI (`--json` everywhere) and an MCP server, enforces Swiss rules (OR 959a/b statements, KMU chart of accounts, Swiss QR-bill, AHV/ALV/BVG/UVG/KTG, withholding tax per KS 45, salary certificate form 11), and makes closed periods, issued invoices and closed payslips tamper-evident.
