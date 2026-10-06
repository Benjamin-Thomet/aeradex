# Prüfung Lohnmodul – 5. Oktober 2026

Geprüft: Berechnung, Arbeitgeberbeiträge, Quellensteuertarife und Import,
Abschluss/Wiederöffnung, Journal, Spesen, Lohnausweis, PDF, Weboberfläche,
CLI und Agent-Werkzeuge. Keine echten Lohnabrechnungen verändert.

## Behobene Fehler und ergänzte Funktionen

- Fehlende Bankdatei: abgeschlossene Monatsabrechnungen lassen sich als
  pain.001.001.09 mit Kategorie SALA exportieren. CHF an normale CH/LI-IBANs;
  Arbeitgeberkonto, Empfänger-IBANs und Ortsangaben werden geprüft. Auszahlung
  umfasst Nettolohn und zugeordnete effektive Spesen. Kein zusätzlicher Buchungssatz.
- Eine erneute Anforderung liefert denselben Auftrag. Andere Ausführungsdaten,
  neue Berechnung und Wiederöffnung sind bei bestehendem Auftrag gesperrt.
  Zurückziehen bewahrt die XML-Datei; ein bereits hochgeladener Bankauftrag muss
  separat bei der Bank storniert werden. Die Anwendung sendet nichts an Banken.
- Unbekannte/unvollständige QST-Tarife werden nicht mehr als Nullabzug behandelt.
- Tarifjahr folgt dem Abrechnungsjahr statt einem veralteten Mitarbeiterfeld.
- Monatliche Auszahlung bei einem Arbeitgeber verwendet ohne Sonderangabe das
  steuerbare Einkommen. Kinderzulagen gehören zur QST-Basis, nicht zur AHV-Basis.
- Untermonatige Beschäftigung rechnet die periodische QST-Satzbasis auf 30 Tage
  um. Ein expliziter geprüfter Monatswert bleibt möglich. Gesamtpensum und
  explizite Satzbasis dürfen nicht gleichzeitig gesetzt sein.
- Ein leerer QST-Pauschalsatz wird beim Speichern tatsächlich auf 0 gesetzt.
- Abrechnung und PDF zeigen QST-Bemessungsgrundlage und satzbestimmendes Einkommen
  getrennt. Die Oberfläche zeigt die Auszahlung inklusive effektiver Spesen.
- Beschäftigungsgrade wie 82.5 % werden vor der Lohnmultiplikation nicht mehr auf
  ganze Prozent gerundet; ein Pensum von 0 wird nicht durch 100 ersetzt.
- Ein fehlerhafter Mitarbeiter hinterlässt beim Monatslauf keine teilweise
  geschriebenen Entwürfe. Ungültige Formulareingaben werden vor dem Schreiben geprüft.
- ESTV-Download pro Kanton/Jahr oder für alle 26 Kantone. Importiert werden die
  progressiven Lohntarife (Recordart 06) mit Mindeststeuer, exakten ganzzahligen
  Werten, Herkunft und SHA-256. Prüfung auf Jahr, Kanton, Recordanzahl, Lücken
  und Überlappungen. Keine stillschweigende Kirchensteuer-Variante.
- Importierte Dateien liegen nachvollziehbar im Buch unter `lohn/qst_tarife/`;
  abgeschlossene Abrechnungen werden durch Aktualisierungen nicht neu berechnet.
  Es gibt keinen unbeaufsichtigten Zeitplan: Import/Aktualisierung wird per
  Schaltfläche, CLI oder Werkzeug ausgelöst. Das Abrechnungsjahr muss vorhanden sein.

## Noch offene fachliche Einschränkungen

Diese Punkte sind durch die Änderungen **nicht** vollständig behoben. Das Modul
ist damit noch keine umfassend geprüfte Schweizer Lohnsoftware.

1. **ALV-/UVG-Bemessungsgrenzen fehlen** (`payroll.calculate`,
   `employer_contributions`): feste Sätze werden auf den ganzen Bruttolohn angewendet.
   Es fehlen kalenderjährliche/unterjährige Kumulation und Höchstlohnkontrolle.
2. **Versicherungspflicht wird nicht individuell ermittelt**: Alter, Rentnerfreibetrag,
   ALV-Ausnahmen, NBU-Deckung bei tiefem Wochenpensum und abweichende
   Versicherungsverträge sind nicht vollständig abgebildet. Globale UVG-/KTG-Sätze
   sind für unterschiedliche Mitarbeitergruppen unzureichend.
3. **QST-Jahresmodell FR/GE/TI/VD/VS**: Tarifimport allein ersetzt keine
   Jahreshochrechnung und keinen Jahres-/Austrittsausgleich. Die automatische
   Monatsableitung ist dort jetzt gesperrt; ein geprüftes satzbestimmendes
   Jahreseinkommen geteilt durch 12 muss ausdrücklich eingegeben werden.
   Der Jahresausgleich bleibt extern zu prüfen.
4. **QST-Sonderfälle**: keine eigene Modellierung von aperiodischen Leistungen,
   13. Monatslohn, Mehrfachbeschäftigung mit unbekanntem Gesamtpensum, nicht
   monatlicher Stundenlohnauszahlung (180-Stunden-Regel), Auslandstagen,
   rückwirkendem Tarif-/Kantonswechsel und steuerbaren Naturalleistungen.
   Das manuelle Satzbasisfeld allein deckt nicht alle diese Fälle ab.
5. **Korrekturfeld**: verändert den Nettolohn und fliesst in den Lohnausweis,
   aber nicht in Sozialversicherungs- oder QST-Basis. Daher nicht für steuerbare
   Boni/Bruttolohnkorrekturen verwenden. Es braucht getrennte Lohnarten.
6. **Journal/Bankabgleich**: Abschluss bucht bereits gegen das Bankkonto am
   Monatsende, nicht erst am tatsächlichen Zahlungstag. Ein Export ist keine
   Zahlung. Eine vollständige Verbindlichkeits-/Zahlungsabwicklung fehlt weiterhin.
7. **Historische Einstellungen**: Kontenzuordnungen werden für Prüfungen aus der
   aktuellen Konfiguration gelesen. Nachträgliche Kontenänderungen können alte
   Buchungen als unpassend erscheinen lassen. Mitarbeiterstammdaten besitzen
   keine vollständige Gültigkeitsgeschichte.
8. **Lohnausweis**: aggregiert die angebotenen Lohnarten; weitere Formularfelder
   und steuerliche Sonderleistungen benötigen weiterhin manuelle Ergänzungen.
9. **Auslandszahlungen**: der neue Export unterstützt CHF-Zahlungen an CH/LI-IBANs.
   Fremde IBAN-Länder/BIC-Anforderungen sind nicht implementiert.
10. **Validierung externer Systeme**: Live-Download der ESTV-ZIPs und SIX-XSD-/Bank-
    Validierung konnten in der netzwerkbeschränkten Entwicklungsumgebung nicht
    durchgeführt werden. Parser und XML-Struktur werden mit lokalen Tests geprüft.
    Eine Annahmegarantie für jede Bank ist daraus nicht ableitbar.

## Bedienung

1. Unter **Mitarbeitende** IBAN und Adresse ergänzen. Für QST die offiziellen
   Tarife für Kanton und Abrechnungsjahr laden, danach Tarifcode zuweisen.
2. Lohnlauf rechnen, Abrechnungen prüfen und einzeln abschliessen.
3. Im Lohnlauf Ausführungsdatum wählen, **Bankdatei erstellen**, dann
   **Bankdatei herunterladen (pain.001)**. Einmal im E-Banking hochladen/freigeben.
4. Bestehende abgeschlossene Löhne mit alter QST-Berechnung gesondert prüfen.
   Es erfolgt keine automatische rückwirkende Korrektur.

CLI:

```sh
allkvitt payroll qst-sync BS 2026
allkvitt payroll qst-sync ALLE 2026
allkvitt payroll payment-export 2026-10 2026-10-25
allkvitt payroll payment-cancel 2026-10
```

## Fachliche Quellen

- [ESTV: offizielle Tarifdateien](https://www.estv.admin.ch/de/quellensteuertarife-import-in-lohnbuchhaltungssysteme)
- [ESTV: Recordformate ab 2025, insbesondere 3.3, 3.7 und 4.3–4.4](https://www.estv.admin.ch/dam/de/sd-web/AvojuyFCZY95/qst-tarife-recordformate-loehne-2025-de.pdf)
- [ESTV: Kreisschreiben 45, insbesondere 3.2, 6.3–6.6 und 7](https://www.estv.admin.ch/dam/de/sd-web/I3YyTvU3ThVA/dbst-ks-2019-1-045-d-de.pdf)
- [SIX: Swiss Payment Standards und XML-Schemata](https://www.six-group.com/en/products-services/banking-services/payment-standardization/downloads-faq/download-center.html)
- [AHV/IV: Beitragsmerkblätter](https://www.ahv-iv.ch/de/Merkbl%C3%A4tter/Beitr%C3%A4ge-AHV-IV-EO-ALV)
- [Suva: Voraussetzungen NBU-Deckung](https://www.suva.ch/de-ch/unfall/leistungen-der-suva/bu-nbu-beurteilung)

## Technische Prüfung

Die Regressionstests verwenden ausschliesslich Wegwerfbücher. Sie prüfen unter
anderem Tarifjahr, Zulagen, 30-Tage-Satzbasis, Tariffehler, Mindeststeuer,
Einkommensgrenzen, abgebrochene Importe, Bankdatei-Download, XML-Summen,
Spesenauszahlung, wiederholte Exporte und Wiederöffnung.

Die unveränderte Starlette-TestClient-Infrastruktur hängt in dieser Sandbox
bereits mit einer leeren Starlette-Anwendung beim Thread-Aufruf. Für die Webtests
wurde deshalb ausschliesslich im Testprozess ein periodischer Event-Loop-Timer
verwendet (`/tmp/batzen_async_tick.py`). Dieser Workaround gehört nicht zum
Produktcode. Live-ESTV-Download und Bank-/SIX-XSD-Abnahme bleiben offen.

Ergebnis: 62 Tests zu Berechnung, Import, Export, Spesen, Buchungen und
Bankabgleich bestanden; 27 bestehende/erweiterte Webtests sowie der zusätzliche
Test zum Löschen des QST-Pauschalsatzes bestanden (90 unterschiedliche Tests).
Die abschliessende Auswahl nach den letzten Anzeigeänderungen bestand ebenfalls
(6 Tests, teilweise Wiederholungen). `compileall` und `git diff --check` ohne Fehler.
