# batzen

**Swiss bookkeeping your agent can run.**
Die erste agentenorientierte Open-Source-Buchhaltung für die Schweiz: Finanzbuchhaltung, Lohnbuchhaltung und QR-Rechnungen, gespeichert als lesbare Textdateien in git, bedient von dir oder von einem LLM-Agenten.

> *Batzen*: alte Schweizer Münze, und auf Schweizerdeutsch einfach Geld («e Batze Gäld»).

> **Vorschau — ohne Gewähr.** batzen ist neu und noch nicht in breitem produktivem Einsatz. Prüfe die Zahlen
> selbst, bevor du dich darauf verlässt — besonders Lohn (Sozialversicherungssätze, Quellensteuer), MWST-Ziffern
> und Abschreibungssätze — und lass den Jahresabschluss von einer Fachperson ansehen. Die Software steht unter der
> AGPL und wird ohne jede Gewährleistung bereitgestellt (siehe [LICENSE](LICENSE)). Lohnmeldungen über ELM
> (Swissdec) sind nicht möglich: dafür braucht es eine Zertifizierung durch Swissdec.

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
# --rechtsform GmbH (Standard), AG, Einzelfirma oder Verein wählt den passenden Kontenplan
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
| **Journal** | Buchungen in einem Raster wie in Excel erfassen (Datum, Beschreibung, Soll, Haben, Betrag, MWST-Code; Enter, leeres Datum = wie oben, Zeilen aus Excel einfügen, alles oder nichts buchen), Monate, Suche, Storno, Belege |
| **Konten** | Kontenplan bearbeiten, Saldenliste nach Periode, Kontoblatt |
| **Debitoren** | Kunden, Rechnungen mit Live-Total und QR-PDF, Zahlung zuordnen, Gutschrift, Storno, offene Posten |
| **Bank** | camt.053 importieren, automatische Zuordnung, offene Bewegungen buchen/zuordnen/ignorieren, Saldoabstimmung |
| **Kreditoren** | QR-Rechnungen aus der Inbox erkennen und erfassen, Lieferanten, offene Posten, Zahlungslauf als pain.001-Datei, Ausführung verbuchen |
| **Lohn** | Lohnlauf pro Monat, Eingaben (Stunden, QST, Korrekturen), Abschluss, Lohnkonto, Lohnausweis |
| **MWST** | Abrechnung je Quartal/Semester nach ESTV-Ziffern, vereinbarte/vereinnahmte Entgelte, Jahresabstimmung mit PDF, Abgrenzung offener Posten |
| **Abschluss** | Bilanz und Erfolgsrechnung mit Vorjahr und Drill-down, Gewinnverwendung, Anhang, Periode sperren |
| **Verlauf** | jeder Commit mit Diff, Änderungen von Agenten markiert |
| **Einstellungen** | Firma, IBAN-Prüfung, Systemkonten, Lohnsätze, Agentenmodus |

Rechts sitzt der **Agent** (Claude): «Bereite die Quittungen in der Inbox vor», «Welche Rechnungen sind überfällig?». Er arbeitet mit denselben Werkzeugen wie der MCP-Server; seine Vorschläge erscheinen sofort unter *Prüfen*. Er läuft wahlweise über **Claude Code**, **Codex** oder **opencode** (jeweils dein eigener Login, kein zusätzlicher Schlüssel) oder über die Claude API (`ANTHROPIC_API_KEY`). Auswahl pro Buch unter *Einstellungen → Agent*, oder `BATZEN_CHAT_BACKEND=claude-code|codex|opencode|api`. Der Agent darf das Buch lesen, ändern kann er es nur über die batzen-Werkzeuge (Codex läuft in der Read-only-Sandbox, opencode ohne Edit/Shell). Im Verlauf steht, welcher Agent was gemacht hat.

**Eigene Agenten im Terminal** nutzen denselben MCP-Server:
```bash
claude mcp add batzen -- batzen --buch <buch> mcp
codex mcp add batzen -- batzen --buch <buch> mcp
opencode mcp add batzen      # oder in opencode.json unter "mcp"
``` Ändert ein Agent oder das CLI das Buch, aktualisiert sich die offene Seite selbst.

Tastatur: `N` neue Buchung, `/` Suche, `A` Agent, `⌘/Ctrl+Enter` Formular absenden.

### Auf dem eigenen Server (mit Login)

```bash
batzen user add anna --rolle admin --anzeige "Anna Muster"
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
batzen invoice create --kunde K0002 --pos "Beratung;10 h;150" --waehrung EUR   # QR-Rechnung in EUR, BAZG-Kurs
batzen mahnung list                            # überfällige Rechnungen
batzen mahnung erstellen R-2026-0001           # Zahlungserinnerung → 2. → 3. Mahnung, je PDF mit QR-Zahlteil
```
Rechnungen in EUR werden zum BAZG-Kurs des Rechnungsdatums gebucht; beim Zahlungseingang wird der Buchwert
ausgeglichen und die Differenz als Kursgewinn/-verlust gebucht, Gutschriften laufen zum Rechnungskurs. Mahnungen
tragen die Referenz der Rechnung, damit der Bankimport die Zahlung zuordnet; Gebühren und Verzugszins fügt batzen
nicht hinzu (in der Schweiz nur mit Grundlage in den AGB).

### MWST
```bash
batzen book --datum 2026-01-12 --soll 6500 --haben 1020 --betrag 108.10 --mwst V81 --text "Papier"
#   → 6500 100.00 · 1170 8.10 · an 1020 108.10 (Betrag immer brutto, Steuer wird abgespalten)
batzen mwst abrechnung 2026-Q1      # Ziffern 200 … 500 wie im ESTV-Formular
batzen mwst buchen 2026-Q1          # MWST-Konten auf das Abrechnungskonto 2201
batzen mwst export 2026-Q1          # eMWST-Datei (eCH-0217 v2.0) für den Upload im ESTV-Portal
batzen mwst abstimmung 2026 --pdf   # Buchhaltung gegen gebuchte Abrechnungen, mit PDF für den Abschluss
batzen mwst abgrenzung 2026        # vereinnahmt: Steuer auf offenen Posten per 31.12., Rückbuchung 1.1.
```
Bezugsteuer (Art. 45 MWSTG) für Dienstleistungen aus dem Ausland: Code `B81` bzw. `B26` auf der
Lieferantenrechnung. Die Steuer wird geschuldet (Ziffern 382/383) und — bei der effektiven Methode — als Vorsteuer
wieder abgezogen (400/405); bei der Saldosteuersatzmethode ist sie Aufwand. Sie erscheint in der Abrechnung, in der
Buchung und in der eMWST-Datei.

Mit `mwst.abrechnungsart: vereinnahmt` zählen Rechnungen und Kreditoren in der Abrechnung bei Zahlung,
anteilig bei Teilzahlungen. Die Jahresabstimmung überleitet vom Belegdatum über die offenen Posten zur
Abrechnung und prüft Ertrags- und MWST-Konten. In der Oberfläche: MWST → Abstimmung → PDF.
Festgestellte Fehler sind über die Jahresabstimmung (Berichtigungsabrechnung nach Art. 72 MWSTG)
zu bereinigen; die [ESTV](https://www.estv.admin.ch/de/mwst-jahresabstimmung) nennt dafür 240 Tage nach Geschäftsjahresende.

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

**Fremdwährung und Aufteilung:**
```bash
batzen kreditor add --lieferant L0003 --betrag 1190 --waehrung EUR --datum 2026-10-01 \
    --position 6570:240:"":Webhosting --position 6600:950:"":Flyer        # Kurs: BAZG am Rechnungsdatum
batzen kreditor pay E-2026-0007 --datum 2026-10-14 --betrag 1112.30    # CHF laut Kontoauszug
```
Eine Rechnung in EUR/USD … wird zum BAZG-Kurs des Rechnungsdatums gebucht (der Fremdwährungsbetrag bleibt auf
jeder Zeile). Bei der Zahlung wird der Buchwert ausgeglichen und die Differenz zum tatsächlich bezahlten Betrag als
Kursgewinn/-verlust gebucht — vom CHF-Konto mit dem belasteten CHF-Betrag, vom EUR-Konto zum BAZG-Kurs des
Zahltags. Der Zahlungslauf (pain.001) zahlt in der Rechnungswährung, je Währung ein Block, vom Bankkonto dieser
Währung, falls unter `bankkonten` eines hinterlegt ist. Offene Fremdwährungs-Kreditoren werden mit den
Fremdwährungskonten per Stichtag bewertet. Positionen (`--position`, im Formular «Auf mehrere Konten aufteilen»)
tragen je ein Konto, einen Bruttobetrag und einen eigenen MWST-Code; der Agent schlägt Aufteilungen im Entwurf vor.

### Belegeingang: Quittungen, Lieferantenrechnungen, eigene extern erstellte Rechnungen
Oberfläche: Prüfen → Belege hochladen (alle Arten), Kreditoren → Rechnungen einlesen, Debitoren → Rechnungen einlesen.
```bash
batzen eingang einlesen inbox/*.pdf inbox/foto.jpg   # → Entwürfe, nichts wird gebucht; die Art wird erkannt
batzen eingang list
batzen eingang buchen ENT-0004                      # eine Quittung so buchen, wie sie vorbereitet ist
```
Jeder Beleg wird ein Entwurf einer von drei Arten — erkannt und von Hand änderbar:

| Art | woran erkannt | wird zu |
|---|---|---|
| Lieferantenrechnung | Zahlungsangaben (QR-Zahlteil, IBAN) | Kreditor: offener Posten, Zahlungslauf (pain.001) |
| Quittung | Kassenbon-Merkmale, bezahlt, keine IBAN | Buchung Aufwand an Kasse/Bank/Kreditkarte |
| eigene Rechnung | die eigene IBAN oder UID auf dem Beleg | Debitor: offener Posten, Zahlungseingang wird zugeordnet |

**Quittungen werden nie doppelt gebucht:** steht die Zahlung schon als offene Bankbewegung im Kontoauszug (gleicher
Betrag, ±5 Tage), wird diese Bewegung mit der Quittung gebucht; ist sie schon im Journal gebucht, wird nur die
Quittung dort abgelegt; sonst wird gegen Kasse (bar), Bank (Karte, TWINT) oder Kreditkarte gebucht — oder gegen das
Konto der Person, die privat bezahlt hat. Quittungen mit mehreren MWST-Sätzen werden auf Positionen aufgeteilt.

**Eigene Rechnungen, die nicht in batzen erstellt wurden** (Word, anderes Programm), werden als Debitor erfasst — mit
eigener batzen-Nummer und der Nummer des Originals; bezahlt der Kunde mit dieser Nummer im Zahlungstext, ordnet der
Bankimport die Zahlung zu.

Ausgelesen wird in Stufen — QR-Zahlteil, Textebene des PDFs, Tesseract-OCR für Scans und Fotos — und kontiert
ebenso: bekannter Lieferant → Jev (falls eingeschaltet und sicher) → Agent (Claude Code, Codex, opencode oder API,
wie im Seitenpanel). Jedes Feld zeigt seine Quelle. Nennt die Rechnung einen anderen Namen als der Inhaber der IBAN,
ordnet batzen nicht zu und warnt; eine IBAN aus dem QR-Zahlteil kann auch der Agent nicht ändern. Gebucht wird
erst, wenn ein Mensch den Entwurf prüft. OCR braucht Tesseract mit Sprachdaten (Arch:
`pacman -S tesseract tesseract-data-deu tesseract-data-fra tesseract-data-ita`, Debian: `apt install tesseract-ocr-deu …`).

### Bank (camt.053)
```bash
batzen bank import inbox/auszug-maerz.xml   # Kontoauszug aus dem E-Banking (ISO 20022 camt.053)
batzen bank list --status offen
batzen bank book B1a2b3c4d5e --konto 6940 --text "Kontoführung"
batzen bank zuordnen B… R-2026-0007         # mit Rechnung oder Kreditor begleichen
batzen bank abstimmung                      # Schlusssaldo Bank gegen Buchhaltung
```
Beim Import bucht batzen Zahlungen mit QR-/SCOR-Referenz oder Rechnungsnummer selbst, erkennt Zahlungen aus
eigenen Zahlungsläufen (EndToEndId) und gleicht bereits Gebuchtes (z.B. Löhne) nur ab. Danach greifen die
**Bankregeln** für Wiederkehrendes (Miete, Abos, Spesen): «Immer so buchen» bei einer gebuchten Bewegung oder
`batzen bank regel add --gegenpartei SWISSCOM --konto 6510`. Der Rest landet unter *Prüfen*; der Agent kann zuordnen
oder Buchungen vorschlagen. Auszüge von Fremdwährungskonten (z.B. EUR) werden in der Kontowährung importiert und
zum BAZG-Kurs gebucht; die Saldoabstimmung vergleicht den Saldo in der Währung.

### Fremdwährungen
```bash
batzen account-add 1021 "Bank EUR" --waehrung EUR
batzen kurs EUR 2026-03-02                  # BAZG-Tageskurs (die Kurse der ESTV)
batzen book --datum 2026-03-02 --soll 1021 --haben 3200 --betrag 1000 --waehrung EUR --text "Verkauf DE"
#   → 1021 an 3200 CHF 944.45, Zeile trägt EUR 1000.00 und den Kurs
batzen bewertung 2026-12-31                 # Vorschau: Saldo EUR × Stichtagskurs gegen CHF-Buchwert
batzen bewertung 2026-12-31 --buchen        # Kursdifferenz auf 6952 Kursgewinne / 6942 Kursverluste
```
Die Bilanz zeigt Fremdwährungskonten danach zum Kurs des Stichtags, mit dem Saldo in der Währung daneben.

### Optional: Jev (TypeSafe) für schnelle Kontierung
[Jev](https://typesafe.ai/) ist ein «System One»-Modell: Es schreibt keinen Text, sondern trifft typisierte
Entscheidungen mit kalibrierter Konfidenz. batzen nutzt es optional, um für offene Bankbewegungen das Gegenkonto
vorzuschlagen, mit dem bisherigen Konto derselben Gegenpartei als stärkstem Hinweis.
```bash
export TYPESAFE_API_KEY=…              # Early-Access-Schlüssel von console.typesafe.ai
# Einstellungen → Jev einschalten (pro Buch), Schwelle wählen
batzen bank kontieren                  # oder Knopf «Konten vorschlagen (Jev)» auf der Bank-Seite
```
Ab der Schwelle entsteht ein Vorschlag unter *Prüfen*, darunter nur ein Hinweis; gebucht wird nie automatisch.
Gesendet werden Gegenpartei, Mitteilung, Betrag, Kontenplan und bis zu fünf frühere Buchungen derselben
Gegenpartei. Bewegungen von Mitarbeitenden werden nie gesendet. TypeSafe ist ein US-Anbieter (Early Access,
nicht Open Source); für Mandantenbücher Einverständnis klären.

### Lohn
```bash
batzen employee add --vorname Lea --nachname Muster --monatslohn 6000 --pensum 80 --bvg-betrag 250 \
  --ahv-nr 756.1234.5678.97 --qst-code A0N --qst-kanton BS --qst-jahr 2026
batzen payroll run 2026-01                     # Entwürfe für alle Mitarbeitenden
batzen payroll run 2026-01 --mitarbeiter M0001 --qst-gesamtpensum 80
batzen payroll close 2026-01 M0001             # einfrieren, verbuchen, PDF
batzen payroll lohnausweis 2026 M0001          # Formular 11, ausgefüllt
```
**Spesen:** Auslagen, die Mitarbeitende privat bezahlt haben (Quittung unter Prüfen hochladen und «Privat bezahlt
von …» wählen, oder Lohn → Spesen, oder `batzen spesen add`), werden gegen 2210 «Sonstige kurzfristige Verbindlichkeiten» (`konten.spesen`) gebucht
und mit dem nächsten Lohn ausbezahlt — nicht AHV-pflichtig, nach dem Nettolohn. Übrige effektive Spesen erscheinen
im Lohnausweis unter Ziffer 13.1.2, Reisespesen sind mit dem Kreuz in 13.1.1 abgedeckt.

Lohnmeldungen über ELM (Swissdec) gibt es nicht: übermitteln darf nur von Swissdec zertifizierte Lohnsoftware.
Lohnausweis (Formular 11) und Lohnkonto entstehen als PDF.


### Abschluss
```bash
batzen report --jahr 2026 --pdf                # Bilanz, Erfolgsrechnung, Gewinnverwendung, Anhang
batzen allocation set 2026 --dividende 5000 --reserve 500
batzen allocation book 2026 --datum 2027-05-20 # nach dem GV-Beschluss
batzen allocation dividende 2026 --datum 2027-06-10  # 65 % auszahlen, 35 % Verrechnungssteuer (Formular 103)
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
- Fremdwährungskonten nur als Bilanzkonten; jede Zeile darauf trägt Währung, FW-Betrag und Kurs, CHF = FW × Kurs.
- Zeilen mit `Quelle` (`rechnung:`, `zahlung:`, `gutschrift:`, `lohn:`, `abschluss:`, `bewertung:` …) gehören ihrem Dokument und müssen genau dazu passen.
- Ausgestellte Rechnungen und abgeschlossene Lohnabrechnungen sind unverändert (Fingerprint).
- Die gesperrte Periode ist unverändert (Hash pro Monat). Entsperren geht nur mit Grund und wird protokolliert.
- Die Kasse ist an keinem Tag negativ; abgelieferte Verrechnungssteuer wird nach Ablauf der 30 Tage angemahnt.
- Hinweise: Buchungen ohne Beleg-Datei, unverarbeitete Inbox, Belege im Eingang, überfällige Rechnungen, Lohn-Entwürfe.

## Herkunft

Die Fachlogik (Saldenmotor mit Jahresverkettung, OR-Gliederung, Lohnberechnung inkl. Quellensteuer nach KS 45, Swiss QR-Bill, Lohnausweis) stammt aus der produktiv genutzten internen Buchhaltung von Thomet GmbH und wurde gegen deren echte Zahlen geprüft: Bilanz, Erfolgsrechnung und Lohnabrechnungen stimmen auf den Rappen.

## Plugins und Mitmachen

batzen ist Community-getragen. Was nicht in den Kern gehört — eine weitere Bank, ein Kanton, ein Kontenplan, ein
Export — kommt als Plugin, ein gewöhnliches Python-Paket:

```bash
pip install batzen-revolut && batzen plugins ein revolut
```

Plugins schreiben nur über die Prüfung von batzen; Buchungen, die ihnen gehören, prüft `batzen check` wie
Rechnungen und Lohn; ein Buch ohne ein Plugin, das es braucht, ist ungültig statt still unvollständig.
Ein Katalog zeigt verfügbare Plugins mit ihren Rechten; «geprüft» vergibt ein Maintainer mit festgehaltenem
Fingerabdruck des Codes, und batzen merkt, wenn der installierte Code davon abweicht. Lokal installiert man mit einem
Klick, auf dem Server per Befehl. Bauen: [docs/plugins.md](docs/plugins.md) und die Vorlage unter `plugins/vorlage`. Verzeichnis und Wunschliste:
[PLUGINS.md](PLUGINS.md). Beitragen: [CONTRIBUTING.md](CONTRIBUTING.md).

## Stand und Roadmap

v0.8: Finanzbuchhaltung, MWST mit Bezugsteuer und eMWST-Export (eCH-0217), Bankimport camt.053 (auch
Fremdwährungskonten) mit automatischem Abgleich und Bankregeln, Fremdwährungen mit BAZG-Tageskursen und
Stichtagsbewertung, Belegeingang mit OCR und Agent (Quittungen, Lieferantenrechnungen, extern erstellte Rechnungen),
Debitoren mit QR-Rechnung (CHF/EUR) und Mahnwesen, Kreditoren mit QR-Scan und pain.001 (auch Fremdwährung),
Dividende mit Verrechnungssteuer, Lohn mit Spesen, Anlagenbuchhaltung (Plugin), Plugin-System mit eigenen Seiten, Agenten-Schnittstelle (CLI + MCP), Web-Oberfläche mit
eingebautem Agenten, lokal oder als Server mit Login.

Als Nächstes:
- Swissdec-Zertifizierung für Lohnmeldungen über ELM (Voraussetzung: Zertifizierung durch Swissdec)
- Weitere Quellensteuer-Kantone, ALV-Höchstgrenze
- Mehrere Mandanten in einer Instanz
- camt.054 (Sammelgutschriften im Detail)

Vor dem ersten Einsatz mit echten Rechnungen: ein erzeugtes PDF im offiziellen SIX-Validator prüfen (https://validation.iso-payments.ch/). Sozialversicherungssätze in `lohn/einstellungen.yaml` an deine Ausgleichskasse, UVG- und KTG-Police anpassen.

## Lizenz

[AGPL-3.0-or-later](LICENSE). batzen bleibt offen, auch wenn jemand es als Dienst betreibt.
Mitgeliefert: HTMX (Zero-Clause BSD), Hanken Grotesk und IBM Plex Mono (SIL Open Font License 1.1).

---

### English summary

batzen is an open-source, agent-first accounting and payroll system for Swiss SMEs. Books are plain Markdown/YAML files in git; a deterministic Python engine does every calculation and validation, and the LLM never adds up numbers itself. It ships a CLI (`--json` everywhere) and an MCP server, enforces Swiss rules (OR 959a/b statements, KMU chart of accounts, Swiss QR-bill, AHV/ALV/BVG/UVG/KTG, withholding tax per KS 45, salary certificate form 11), and makes closed periods, issued invoices and closed payslips tamper-evident.
