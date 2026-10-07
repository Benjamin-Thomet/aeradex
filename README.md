# aeradex

**Swiss bookkeeping your agent can run.**
Die erste agentenorientierte Open-Source-Buchhaltung für die Schweiz: Finanzbuchhaltung, Lohnbuchhaltung und QR-Rechnungen, gespeichert als lesbare Textdateien in git, bedient von dir oder von einem LLM-Agenten.

> *aeradex*: von lat. *aera*, den einzelnen Posten einer Rechnung, und *index*, dem Verzeichnis.
> «Si aera singula probasti, summam, quae ex his confecta sit, non probare?» (Cicero): Wer jeden Posten
> gebilligt hat, kann die Summe nicht ablehnen. Genau so arbeitet aeradex.

> **Vorschau — ohne Gewähr.** aeradex ist neu und noch nicht in breitem produktivem Einsatz. Prüfe die Zahlen
> selbst, bevor du dich darauf verlässt — besonders Lohn (Sozialversicherungssätze, Quellensteuer), MWST-Ziffern
> und Abschreibungssätze — und lass den Jahresabschluss von einer Fachperson ansehen. Die Software steht unter der
> AGPL und wird ohne jede Gewährleistung bereitgestellt (siehe [LICENSE](LICENSE)). Lohnmeldungen über ELM
> (Swissdec) sind nicht möglich: dafür braucht es eine Zertifizierung durch Swissdec.

---

## Warum noch eine Buchhaltung?

Klassische Buchhaltungssoftware versteckt die Bücher in einer Datenbank hinter einer Oberfläche. Ein Agent (Claude Code, opencode, Codex …) kann damit kaum arbeiten: Er klickt sich durch Formulare oder ruft eine API auf, die nie für ihn gebaut wurde.

aeradex dreht das um:

| | |
|---|---|
| **Dateien statt Datenbank** | Journal als Markdown-Tabelle pro Monat, Kunden, Mitarbeitende, Rechnungen und Lohnabrechnungen als Markdown mit YAML-Frontmatter. Ein Mensch liest sie in jedem Editor, ein LLM ohne Adapter. |
| **Rechnen tut die Engine, nie das LLM** | Salden, Bilanz, Löhne, Quellensteuer, QR-Referenzen: alles deterministisches Python. Der Agent entscheidet *was* gebucht wird, die Engine prüft und rechnet. |
| **git ist das Audit-Trail** | Jede Änderung ist ein Commit mit sprechender Nachricht. Ein pre-commit-Hook führt `aeradex check` aus, ein ungültiges Buch lässt sich nicht committen. |
| **Unveränderlichkeit, wo das Gesetz sie verlangt** | Gesperrte Perioden sind gehasht, ausgestellte Rechnungen und abgeschlossene Lohnabrechnungen tragen einen Fingerprint. Jede nachträgliche Änderung fällt auf (GeBüV). |
| **Mensch im Loop** | Im Standardmodus darf ein Agent freie Buchungen nur *vorschlagen*; du gibst sie mit `aeradex approve` frei. |
| **Schweizer Recht eingebaut** | KMU-Kontenrahmen, Bilanz und Erfolgsrechnung nach OR 959a/959b, Anhang, Gewinnverwendung, MWST (effektiv und Saldosteuersatz, Abrechnung nach ESTV-Ziffern), Swiss QR-Bill (QRR/SCOR), AHV/IV/EO/ALV/UVG/KTG/BVG/FAK, Quellensteuer-Tarife nach KS 45, Lohnausweis Formular 11. |

## Schnellstart

```bash
pip install -e ".[ui,mcp,scan]"  # Python ≥ 3.11

aeradex init ~/buchhaltung/muster --firma "Muster GmbH" --jahr 2026 \
  --strasse Bahnhofstrasse --nr 1 --plz 3000 --ort Bern \
  --iban "CH93 0076 2011 6238 5295 7" --uid CHE-123.456.789
# --rechtsform GmbH (Standard), AG, Einzelfirma oder Verein wählt den passenden Kontenplan
cd ~/buchhaltung/muster

# Eröffnungsbilanz in kontenplan.yaml eintragen (eroeffnung: 20000 bei 1020, -20000 bei 2800), dann:
aeradex check
aeradex book --datum 2026-01-05 --soll 6500 --haben 1020 --betrag 45.80 \
  --text "Büromaterial" --datei inbox/quittung.pdf
aeradex balance
```

### Die Oberfläche

```bash
aeradex ui                        # öffnet aeradex im Browser (nur lokal, mit Zugangsschlüssel im Link)
```

Eine lokale Web-App über denselben Kern wie CLI und Agenten. Jeder Klick wird geprüft und in git festgehalten.

Sechs Bereiche, jeder mit einem klaren Ablauf. Belege lädst du an einer Stelle hoch (**Beleg hochladen** oben
rechts); aeradex erkennt die Art und legt einen Entwurf an. Gebucht wird erst, wenn du freigibst.

| Bereich | Was du dort machst |
|---|---|
| **Übersicht** | Liquidität, Ergebnis, offene Debitoren; **Was ansteht** als eine Liste, jede Zeile führt dorthin, wo du es erledigst; Inbox-Dateien einlesen |
| **Einkauf** | Lieferantenrechnungen und Quittungen: **Entwürfe → Offen → Bezahlt** (plus Alle). Entwurf prüfen, «Freigeben» oder «Freigeben & nächster»; Zahlungslauf als pain.001, Lieferanten, offene Posten |
| **Verkauf** | Rechnungen mit Live-Total und QR-PDF: **Entwürfe → Offen → Bezahlt** (plus Alle); externe Rechnungen einlesen und freigeben, Gutschrift, Storno, Kunden, Mahnungen, offene Posten |
| **Bank** | **Abgleichen** wie bei Xero: eine Karte pro offener Bewegung, daneben der passende Kreditor, Debitor, die Quittung, Bankregel oder frühere Buchung; ein Klick auf OK. Sonst «Suchen» (Name, Nummer, Betrag), «Neu buchen» (optional «Immer so buchen» als Regel) oder «Ignorieren». Dazu Bewegungen, Regeln und Saldoabstimmung |
| **Lohn** | Lohnlauf pro Monat, Eingaben (Stunden, QST, Korrekturen), Abschluss, Lohnkonto, Lohnausweis |
| **Buchhaltung** | Journal (Raster wie in Excel: Enter, leeres Datum = wie oben, Zeilen aus Excel einfügen), Agenten-Vorschläge freigeben, Kontenplan, Saldenliste, MWST nach ESTV-Ziffern, Berichte, Budget, Abschluss (Bilanz, Erfolgsrechnung, Anhang, Periode sperren) |

Oben rechts: **Verlauf** (jeder Commit mit Diff, Agenten markiert) und **Einstellungen** (Firma, IBAN-Prüfung,
Systemkonten, Lohnsätze, Agentenmodus).

Rechts sitzt der **Agent** (Claude): «Bereite die Quittungen in der Inbox vor», «Welche Rechnungen sind überfällig?». Er arbeitet mit denselben Werkzeugen wie der MCP-Server; seine Vorschläge erscheinen sofort unter *Buchhaltung › Vorschläge*, eingelesene Belege unter *Einkauf/Verkauf › Entwürfe*. Er läuft wahlweise über **Claude Code**, **Codex** oder **opencode** (jeweils dein eigener Login, kein zusätzlicher Schlüssel) oder über die Claude API (`ANTHROPIC_API_KEY`). Auswahl pro Buch unter *Einstellungen → Agent*, oder `AERADEX_CHAT_BACKEND=claude-code|codex|opencode|api`. Der Agent darf das Buch lesen, ändern kann er es nur über die aeradex-Werkzeuge (Codex läuft in der Read-only-Sandbox, opencode ohne Edit/Shell). Im Verlauf steht, welcher Agent was gemacht hat.

**Eigene Agenten im Terminal** nutzen denselben MCP-Server:
```bash
claude mcp add aeradex -- aeradex --buch <buch> mcp
codex mcp add aeradex -- aeradex --buch <buch> mcp
opencode mcp add aeradex      # oder in opencode.json unter "mcp"
``` Ändert ein Agent oder das CLI das Buch, aktualisiert sich die offene Seite selbst.

Tastatur: `N` neue Buchung, `/` Suche, `A` Agent, `⌘/Ctrl+Enter` Formular absenden.

### Auf dem eigenen Server (mit Login)

```bash
aeradex user add anna --rolle admin --anzeige "Anna Muster"
aeradex user add treuhand --rolle lesen
aeradex --buch /srv/buecher/muster serve --port 8080 --https     # hinter Caddy/nginx mit HTTPS
```
Rollen `lesen`, `buchhaltung`, `admin`; jede Änderung trägt im git-Verlauf den Namen der angemeldeten Person.
Anleitung mit systemd, Caddy und Datensicherung: [docs/server.md](docs/server.md).

### Debitoren und QR-Rechnungen
```bash
aeradex customer add --name "Anna Beispiel" --firma "Beispiel AG" --strasse Marktgasse --nr 5 --plz 3011 --ort Bern
aeradex invoice create --kunde K0001 --pos "Beratung;10 h;150" --pos "Spesen;1;80;3600"
#   → rechnungen/2026/R-2026-0001.md (eingefroren) + .pdf mit QR-Einzahlungsschein, verbucht 1100 an 3400/3600
aeradex invoice match --betrag 1580 --text "<Zeile aus dem Bankauszug>"
aeradex invoice pay R-2026-0001 --datum 2026-03-01
aeradex receivables --pdf
aeradex invoice create --kunde K0002 --pos "Beratung;10 h;150" --waehrung EUR   # QR-Rechnung in EUR, BAZG-Kurs
aeradex mahnung list                            # überfällige Rechnungen
aeradex mahnung erstellen R-2026-0001           # Zahlungserinnerung → 2. → 3. Mahnung, je PDF mit QR-Zahlteil
```
Rechnungen in EUR werden zum BAZG-Kurs des Rechnungsdatums gebucht; beim Zahlungseingang wird der Buchwert
ausgeglichen und die Differenz als Kursgewinn/-verlust gebucht, Gutschriften laufen zum Rechnungskurs. Mahnungen
tragen die Referenz der Rechnung, damit der Bankimport die Zahlung zuordnet; Gebühren und Verzugszins fügt aeradex
nicht hinzu (in der Schweiz nur mit Grundlage in den AGB).

### MWST
```bash
aeradex book --datum 2026-01-12 --soll 6500 --haben 1020 --betrag 108.10 --mwst V81 --text "Papier"
#   → 6500 100.00 · 1170 8.10 · an 1020 108.10 (Betrag immer brutto, Steuer wird abgespalten)
aeradex mwst abrechnung 2026-Q1      # Ziffern 200 … 500 wie im ESTV-Formular
aeradex mwst buchen 2026-Q1          # MWST-Konten auf das Abrechnungskonto 2201
aeradex mwst export 2026-Q1          # eMWST-Datei (eCH-0217 v2.0) für den Upload im ESTV-Portal
aeradex mwst abstimmung 2026 --pdf   # Buchhaltung gegen gebuchte Abrechnungen, mit PDF für den Abschluss
aeradex mwst abgrenzung 2026        # vereinnahmt: Steuer auf offenen Posten per 31.12., Rückbuchung 1.1.
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
aeradex kreditor scan inbox/rechnung.pdf          # liest den QR-Zahlteil: IBAN, Betrag, Referenz, Lieferant
aeradex lieferant add --name "Papeterie Muster AG" --iban CH44… --konto 6500 --mwst V81
aeradex kreditor add --lieferant L0001 --betrag 86.40 --referenz 0000… --datei inbox/rechnung.pdf
aeradex zahlungslauf erstellen E-2026-0001 E-2026-0002 --datum 2026-03-20   # pain.001 fürs E-Banking
aeradex zahlungslauf bezahlt 2026-03-20-ab12cd.xml                         # nach der Ausführung verbuchen
```
Die Zahlungsdatei folgt den Swiss Payment Standards (pain.001.001.09) und validiert gegen die offiziellen
SIX-Schemas SPS 2025 und 2026. QR-Codes lesen: `pip install -e ".[scan]"`.

**Fremdwährung und Aufteilung:**
```bash
aeradex kreditor add --lieferant L0003 --betrag 1190 --waehrung EUR --datum 2026-10-01 \
    --position 6570:240:"":Webhosting --position 6600:950:"":Flyer        # Kurs: BAZG am Rechnungsdatum
aeradex kreditor pay E-2026-0007 --datum 2026-10-14 --betrag 1112.30    # CHF laut Kontoauszug
```
Eine Rechnung in EUR/USD … wird zum BAZG-Kurs des Rechnungsdatums gebucht (der Fremdwährungsbetrag bleibt auf
jeder Zeile). Bei der Zahlung wird der Buchwert ausgeglichen und die Differenz zum tatsächlich bezahlten Betrag als
Kursgewinn/-verlust gebucht — vom CHF-Konto mit dem belasteten CHF-Betrag, vom EUR-Konto zum BAZG-Kurs des
Zahltags. Der Zahlungslauf (pain.001) zahlt in der Rechnungswährung, je Währung ein Block, vom Bankkonto dieser
Währung, falls unter `bankkonten` eines hinterlegt ist. Offene Fremdwährungs-Kreditoren werden mit den
Fremdwährungskonten per Stichtag bewertet. Positionen (`--position`, im Formular «Auf mehrere Konten aufteilen»)
tragen je ein Konto, einen Bruttobetrag und einen eigenen MWST-Code; der Agent schlägt Aufteilungen im Entwurf vor.

### Belegeingang: Quittungen, Lieferantenrechnungen, eigene extern erstellte Rechnungen
Oberfläche: «Beleg hochladen» oben rechts (alle Arten); die Entwürfe erscheinen unter Einkauf › Entwürfe bzw. Verkauf › Entwürfe.
```bash
aeradex eingang einlesen inbox/*.pdf inbox/foto.jpg   # → Entwürfe, nichts wird gebucht; die Art wird erkannt
aeradex eingang list
aeradex eingang buchen ENT-0004                      # eine Quittung so buchen, wie sie vorbereitet ist
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

**Eigene Rechnungen, die nicht in aeradex erstellt wurden** (Word, anderes Programm), werden als Debitor erfasst — mit
eigener aeradex-Nummer und der Nummer des Originals; bezahlt der Kunde mit dieser Nummer im Zahlungstext, ordnet der
Bankimport die Zahlung zu.

Ausgelesen wird in Stufen — QR-Zahlteil, Textebene des PDFs, Tesseract-OCR für Scans und Fotos — und kontiert
ebenso: bekannter Lieferant → Jev (falls eingeschaltet und sicher) → Agent (Claude Code, Codex, opencode oder API,
wie im Seitenpanel). Jedes Feld zeigt seine Quelle. Nennt die Rechnung einen anderen Namen als der Inhaber der IBAN,
ordnet aeradex nicht zu und warnt; eine IBAN aus dem QR-Zahlteil kann auch der Agent nicht ändern. Gebucht wird
erst, wenn ein Mensch den Entwurf prüft. OCR braucht Tesseract mit Sprachdaten (Arch:
`pacman -S tesseract tesseract-data-deu tesseract-data-fra tesseract-data-ita`, Debian: `apt install tesseract-ocr-deu …`).

### Bank (camt.053, CSV, Excel, Kreditkarte)
```bash
aeradex bank import inbox/auszug-maerz.xml   # Kontoauszug aus dem E-Banking (ISO 20022 camt.053)
aeradex bank list --status offen
aeradex bank book B1a2b3c4d5e --konto 6940 --text "Kontoführung"
aeradex bank zuordnen B… R-2026-0007         # mit Rechnung oder Kreditor begleichen
aeradex bank abstimmung                      # Schlusssaldo Bank gegen Buchhaltung
```
Beim Import bucht aeradex Zahlungen mit QR-/SCOR-Referenz oder Rechnungsnummer selbst, erkennt Zahlungen aus
eigenen Zahlungsläufen (EndToEndId) und gleicht bereits Gebuchtes (z.B. Löhne) nur ab. Danach greifen die
**Bankregeln** für Wiederkehrendes (Miete, Abos, Spesen): «Immer so buchen» bei einer gebuchten Bewegung oder
`aeradex bank regel add --gegenpartei SWISSCOM --konto 6510`. Der Rest wartet unter *Bank › Abgleichen*; der Agent kann zuordnen
oder Buchungen vorschlagen. Auszüge von Fremdwährungskonten (z.B. EUR) werden in der Kontowährung importiert und
zum BAZG-Kurs gebucht; die Saldoabstimmung vergleicht den Saldo in der Währung.

**Andere Formate (CSV, Excel) und Kreditkarten (PDF)** liest der Agent — aber er liefert nie ungeprüfte Beträge:
```bash
aeradex bank format lernen inbox/ubs-export.csv   # Agent beschreibt das Format → bank/formate/ubs-….yaml
aeradex bank format pruefen inbox/ubs-export.csv  # Vorschau und Saldo-Prüfung Zeile für Zeile
aeradex bank format bestaetigen ubs-kontoauszug   # einmal pro Bank; danach liest aeradex jede Datei selbst
aeradex bank import inbox/ubs-export.csv

aeradex account-add 2040 "Kreditkarte Visa" --klasse passiv
aeradex bank karte inbox/visa-2026-09.pdf --konto 2040   # Agent liest die Transaktionen, aeradex prüft und importiert
```
Bei CSV/Excel beschreibt der Agent nur, welche Spalte was ist; die Zahlen liest aeradex selbst, und ein Mensch
bestätigt das Format einmal. Bei Kreditkartenabrechnungen schreibt der Agent die Transaktionen nach
`bank/karten/`; importiert wird nur, wenn alter Saldo + Buchungen = neuer Saldo auf den Rappen aufgeht und jeder
Betrag im Text der PDF steht. Die Karte ist ein Passivkonto: Einkäufe Aufwand an Kreditkarte, die monatliche
Belastung Kreditkarte an Bank — sie wird beim Import der Abrechnung mit der Bankbuchung abgeglichen. In der
Oberfläche genügt es, die Datei auf der Bank-Seite hochzuladen (bei Kreditkarten mit Konto).

### Fremdwährungen
```bash
aeradex account-add 1021 "Bank EUR" --waehrung EUR
aeradex kurs EUR 2026-03-02                  # BAZG-Tageskurs (die Kurse der ESTV)
aeradex book --datum 2026-03-02 --soll 1021 --haben 3200 --betrag 1000 --waehrung EUR --text "Verkauf DE"
#   → 1021 an 3200 CHF 944.45, Zeile trägt EUR 1000.00 und den Kurs
aeradex bewertung 2026-12-31                 # Vorschau: Saldo EUR × Stichtagskurs gegen CHF-Buchwert
aeradex bewertung 2026-12-31 --buchen        # Kursdifferenz auf 6952 Kursgewinne / 6942 Kursverluste
```
Die Bilanz zeigt Fremdwährungskonten danach zum Kurs des Stichtags, mit dem Saldo in der Währung daneben.

### Optional: Jev (TypeSafe) für schnelle Kontierung
[Jev](https://typesafe.ai/) ist ein «System One»-Modell: Es schreibt keinen Text, sondern trifft typisierte
Entscheidungen mit kalibrierter Konfidenz. aeradex nutzt es optional, um für offene Bankbewegungen das Gegenkonto
vorzuschlagen, mit dem bisherigen Konto derselben Gegenpartei als stärkstem Hinweis.
```bash
export TYPESAFE_API_KEY=…              # Early-Access-Schlüssel von console.typesafe.ai
# Einstellungen → Jev einschalten (pro Buch), Schwelle wählen
aeradex bank kontieren                  # oder Knopf «Konten vorschlagen (Jev)» auf der Bank-Seite
```
Ab der Schwelle entsteht ein Vorschlag (*Buchhaltung › Vorschläge*), darunter nur ein Hinweis; gebucht wird nie automatisch.
Gesendet werden Gegenpartei, Mitteilung, Betrag, Kontenplan und bis zu fünf frühere Buchungen derselben
Gegenpartei. Bewegungen von Mitarbeitenden werden nie gesendet. TypeSafe ist ein US-Anbieter (Early Access,
nicht Open Source); für Mandantenbücher Einverständnis klären.

### Lohn
```bash
aeradex employee add --vorname Lea --nachname Muster --monatslohn 6000 --pensum 80 --bvg-betrag 250 \
  --ahv-nr 756.1234.5678.97 --qst-code A0N --qst-kanton BS --qst-jahr 2026
aeradex payroll run 2026-01                     # Entwürfe für alle Mitarbeitenden
aeradex payroll run 2026-01 --mitarbeiter M0001 --qst-gesamtpensum 80
aeradex payroll close 2026-01 M0001             # einfrieren, verbuchen, PDF
aeradex payroll lohnausweis 2026 M0001          # nach Abschluss aller Anstellungsmonate
```
**Spesen:** Auslagen, die Mitarbeitende privat bezahlt haben (Quittung unter *Beleg hochladen* einlesen und «Privat bezahlt
von …» wählen, oder Lohn → Spesen, oder `aeradex spesen add`), werden gegen 2210 «Sonstige kurzfristige Verbindlichkeiten» (`konten.spesen`) gebucht
und mit dem nächsten Lohn ausbezahlt — nicht AHV-pflichtig, nach dem Nettolohn. Übrige effektive Spesen erscheinen
im Lohnausweis unter Ziffer 13.1.2, Reisespesen sind mit dem Kreuz in 13.1.1 abgedeckt.

Lohnmeldungen über ELM (Swissdec) gibt es nicht: übermitteln darf nur von Swissdec zertifizierte Lohnsoftware.
Lohnausweis (Formular 11) und Lohnkonto entstehen als PDF.
In der Oberfläche zeigt **Lohn** für das gewählte Jahr alle zwölf Monate mit fehlenden,
offenen und abgeschlossenen Abrechnungen. **Lohn → Mitarbeitende → Lohnkonto / Lohnausweis**
bietet die Jahresauswahl und die Erstellung. Ein Lohnausweis lässt sich erst erstellen,
wenn alle Monate der erfassten Anstellung im Jahr gerechnet und abgeschlossen sind;
Eintritt und Austritt begrenzen den Zeitraum. Fehlende Datumsangaben gelten als Anstellung
ab Jahresanfang bzw. bis Jahresende. Das gilt auch bei Stundenlohn; Monate ohne Lohn
benötigen dann eine geprüfte, abgeschlossene Nullabrechnung. Offene Abrechnungen werden
auch ausserhalb dieses Zeitraums berücksichtigt und müssen abgeschlossen werden.


### Abschluss
```bash
aeradex report --jahr 2026 --pdf                # Bilanz, Erfolgsrechnung, Gewinnverwendung, Anhang
aeradex allocation set 2026 --dividende 5000 --reserve 500
aeradex allocation book 2026 --datum 2027-05-20 # nach dem GV-Beschluss
aeradex allocation dividende 2026 --datum 2027-06-10  # 65 % auszahlen, 35 % Verrechnungssteuer (Formular 103)
aeradex lock 2026-12-31                         # Periode sperren (gehasht)
```

**Abschlussunterlagen** für Treuhand, Revision und Archiv — alles als ein ZIP oder jeder Teil einzeln
(Oberfläche: Abschluss → Unterlagen):
```bash
aeradex dossier --jahr 2026                     # ZIP in berichte/: alle Teile als PDF und CSV + Originalbelege
aeradex dossier --liste                         # welche Teile es gibt, Lücken in den Belegnummern
aeradex dossier --teil belege                   # nur der Belegordner (PDF)
aeradex dossier --teil journal --format csv     # nur das Journal als CSV
aeradex dossier --nur jahresrechnung,journal --format pdf
```
Im ZIP: Inhaltsverzeichnis mit Prüfprotokoll und SHA-256-Prüfsummen, Jahresrechnung (Bilanz, Erfolgsrechnung,
Anhang), Saldenliste, Journal, Kontoblätter, Belegordner, MWST-Abrechnungen und Umsatzabstimmung, offene Debitoren
und Kreditoren per 31.12., Lohnjournal und Lohnausweise, Kontenplan, `manifest.json`. Solange das Jahr nicht
gesperrt ist, tragen die Berichte den Vermerk «ENTWURF».

Der **Belegordner** stempelt jede Seite mit der Belegnummer der Buchung im Journal (`Beleg 26-001 · Datum · CHF ·
Seite 1/2`) — es gibt keine zweite Laufnummer, die sich verschieben könnte. Belegnummern werden nie wieder vergeben,
auch wenn eine Buchung storniert, ein Dokument annulliert oder ein Vorschlag verworfen wird
(`.aeradex/belegnummern.yaml`). Fehlende Nummern stehen mit Grund im Lückenverzeichnis (storniert, verworfen, aus dem
Journal entfernt in Commit …).

**Buch weitergeben**: das ganze Buch als eine `.aeradex`-Datei — für die Treuhänderin, eine Nachfolge oder einen
zweiten Computer:
```bash
aeradex export-buch "Muster GmbH.aeradex" --passwort   # mit Änderungsverlauf; Passwort optional (AES-256)
aeradex import-buch "Muster GmbH.aeradex"              # nur anzeigen, was drin ist
aeradex import-buch "Muster GmbH.aeradex" ~/buchhaltung/muster --passwort
```
Beim Import prüft aeradex jede Datei gegen ihre Prüfsumme, übernimmt den git-Verlauf und führt `aeradex check` aus.

### Berichte und Budget
Berichte für jede Periode — nicht nur zum Jahresende. Oberfläche: **Berichte** (oben), mit Diagramm, Export und
Drill-down: jede Zahl führt ins Kontoblatt genau dieser Periode und von dort zum Beleg.
```bash
aeradex bericht erfolgsrechnung --jahr 2026 --spalten monat --vergleich budget   # Monatsspalten, Budget vs. Ist
aeradex bericht bilanz --periode q3 --vergleich vorperiode                        # Bilanz per 30.09. gegen 30.06.
aeradex bericht geldfluss --jahr 2026                  # Geldflussrechnung (indirekt), abgestimmt mit den flüssigen Mitteln
aeradex bericht kennzahlen                             # Liquiditätsgrade, EK-Quote, Margen, DSO/DPO mit Beurteilung
aeradex bericht debitoren --stichtag 2026-09-30        # offene Posten nach Alter (auch: kreditoren)
aeradex bericht umsatz --nach kunde                    # Umsatz nach Kunde, Ertragskonto oder Monat
aeradex bericht erfolgsrechnung --detail --format xlsx # mit Konten, als Excel (auch pdf, csv)
aeradex bericht liste                                  # alle Berichte (inkl. Plugins) und gespeicherten Vorlagen

aeradex budget vorjahr --jahr 2027 --prozent 5         # Budget aus dem Ist 2026, Saisonverlauf bleibt
aeradex budget set --jahr 2027 --konto 6000 --betrag 24000

aeradex bericht vorlage-speichern "Monatsreport Treuhand" --bericht-typ erfolgsrechnung --spalten monat --vergleich vorjahr
aeradex bericht x --vorlage "Monatsreport Treuhand" --format pdf
aeradex bericht erfolgsrechnung --periode q3 --kommentar "Umsatz unter Budget: Auftrag X verschoben."
aeradex bericht erfolgsrechnung --periode q3 --agent   # der Agent schreibt den Kommentar (zitiert nur Zahlen des Berichts)
aeradex bericht monat --monat 2026-09 --mail           # Monatsbericht (PDF + Excel) in berichte/, optional per E-Mail
```
Jeder Bericht trägt seinen **Stand** (git-Commit, Prüfstatus), damit ein ausgedrucktes Blatt immer auf das Buch
zurückführt. Ein Kommentar merkt sich die Zahlen, zu denen er geschrieben wurde: ändern sie sich, ist er als
«veraltet» markiert. Für den automatischen Monatsbericht genügt ein Timer (siehe [docs/server.md](docs/server.md)).
Die Jahresrechnung (`aeradex report`) bleibt das massgebende Abschlussdokument; für ein ganzes Jahr zeigen die
Berichte dieselben Zahlen.

## Mit einem Agenten arbeiten

Jedes Buch enthält ein `AGENTS.md` (und `CLAUDE.md`) mit den Regeln für Agenten. Zwei Wege:

**CLI**: jeder Befehl kann `--json`, Fehler kommen als `{"ok": false, "fehler": "…"}`.

**MCP-Server**: für Claude Code, opencode und andere MCP-Clients:
```bash
claude mcp add aeradex -- aeradex --buch ~/buchhaltung/muster mcp
```
Dann z.B.: *«Verbuche die Quittungen in der Inbox»*, *«Welche Rechnungen sind über 30 Tage offen?»*, *«Mach den Lohnlauf für Januar»*.

`agent_modus` in `aeradex.yaml`:
- `vorschlag` (Standard): Agenten dürfen freie Buchungen nur vorschlagen (`vorschlaege.md`), du gibst frei.
- `direkt`: Agenten dürfen selbst buchen. Rechnungen und Lohnläufe sind in beiden Modi erlaubt, weil sie aus expliziten Eingaben deterministisch entstehen.

## Das Buch auf der Festplatte

```
muster/
├── aeradex.yaml              Firma, Bank, Systemkonten, Sperrdatum, agent_modus
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
├── .aeradex/locks.yaml       Hashes der gesperrten Periode
└── .aeradex/belegnummern.yaml höchste je vergebene Belegnummer pro Jahr
```

Das genaue Dateiformat steht in [docs/format.md](docs/format.md).

## Regeln, die `aeradex check` durchsetzt

- Jede Journalzeile: gültiges Datum in der richtigen Monatsdatei, existierende Konten, positiver Betrag mit höchstens zwei Nachkommastellen, Belegnummer.
- Jeder Beleg ist ausgeglichen (Soll = Haben), steht an einer Stelle und an einem Datum, ohne doppelte Zeilen.
- Die Eröffnungsbilanz geht auf, Erfolgskonten eröffnen bei null.
- Fremdwährungskonten nur als Bilanzkonten; jede Zeile darauf trägt Währung, FW-Betrag und Kurs, CHF = FW × Kurs.
- Zeilen mit `Quelle` (`rechnung:`, `zahlung:`, `gutschrift:`, `lohn:`, `abschluss:`, `bewertung:` …) gehören ihrem Dokument und müssen genau dazu passen.
- Ausgestellte Rechnungen und abgeschlossene Lohnabrechnungen sind unverändert (Fingerprint).
- Die gesperrte Periode ist unverändert (Hash pro Monat). Entsperren geht nur mit Grund und wird protokolliert.
- Die Kasse ist an keinem Tag negativ; abgelieferte Verrechnungssteuer wird nach Ablauf der 30 Tage angemahnt.
- Belegdateien in `belege/` gehören zu einer Buchung oder einem Dokument mit dieser Nummer (sonst Warnung).
- Hinweise: Buchungen ohne Beleg-Datei, Belegnummern ohne Buchung, unverarbeitete Inbox, Belege im Eingang,
  überfällige Rechnungen, Lohn-Entwürfe.

## Herkunft

Die Fachlogik (Saldenmotor mit Jahresverkettung, OR-Gliederung, Lohnberechnung inkl. Quellensteuer nach KS 45, Swiss QR-Bill, Lohnausweis) stammt aus der produktiv genutzten internen Buchhaltung von Thomet GmbH und wurde gegen deren echte Zahlen geprüft: Bilanz, Erfolgsrechnung und Lohnabrechnungen stimmen auf den Rappen.

## Plugins und Mitmachen

aeradex ist Community-getragen. Was nicht in den Kern gehört — eine weitere Bank, ein Kanton, ein Kontenplan, ein
Export — kommt als Plugin, ein gewöhnliches Python-Paket:

```bash
pip install aeradex-revolut && aeradex plugins ein revolut
```

Plugins schreiben nur über die Prüfung von aeradex; Buchungen, die ihnen gehören, prüft `aeradex check` wie
Rechnungen und Lohn; ein Buch ohne ein Plugin, das es braucht, ist ungültig statt still unvollständig.
Ein Katalog zeigt verfügbare Plugins mit ihren Rechten; «geprüft» vergibt ein Maintainer mit festgehaltenem
Fingerabdruck des Codes, und aeradex merkt, wenn der installierte Code davon abweicht. Lokal installiert man mit einem
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

[AGPL-3.0-or-later](LICENSE). aeradex bleibt offen, auch wenn jemand es als Dienst betreibt.
Mitgeliefert: HTMX (Zero-Clause BSD), Hanken Grotesk und IBM Plex Mono (SIL Open Font License 1.1).

---

### English summary

aeradex is an open-source, agent-first accounting and payroll system for Swiss SMEs. Books are plain Markdown/YAML files in git; a deterministic Python engine does every calculation and validation, and the LLM never adds up numbers itself. It ships a CLI (`--json` everywhere) and an MCP server, enforces Swiss rules (OR 959a/b statements, KMU chart of accounts, Swiss QR-bill, AHV/ALV/BVG/UVG/KTG, withholding tax per KS 45, salary certificate form 11), and makes closed periods, issued invoices and closed payslips tamper-evident.
