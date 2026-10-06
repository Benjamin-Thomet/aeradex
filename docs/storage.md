# Textspeicher: Konsistenz, Wiederherstellung und Agenten

Die UTF-8-Dateien bleiben die massgebende Datenquelle. Git dokumentiert Änderungen;
Berichte sind daraus ableitbar. Ein Datenbankserver ist nicht erforderlich.

## Dateiformat

Neue Bücher tragen `format_version: 1` in `allkvitt.yaml`. Bücher ohne Versionsfeld
werden als Version 0 gelesen. Beim nächsten erfolgreichen API-Schreibvorgang wird
Version 1 innerhalb derselben Transaktion eingetragen; die Datenstruktur bleibt
unverändert. Unbekannte Versionen werden zurückgewiesen. Weitere Migrationen gehören
in `bookformat.py` und müssen innerhalb einer Transaktion laufen.

YAML-Dezimalzahlen werden direkt als `Decimal` geladen. Doppelte Schlüssel,
nicht endliche Zahlen und doppelte/leere Tabellenspalten sind Fehler. Eine vorhandene
Journaldatei braucht genau eine zusammenhängende Journaltabelle; abgetrennte
Tabellenzeilen dürfen nicht stillschweigend aus den Buchungen verschwinden.

Tabellenspalten werden nur nach der Überschrift gepolstert, nicht nach der längsten
Zelle. Neue lange Texte ändern bestehende Zeilen dadurch nicht. Ältere Tabellen
werden beim nächsten Schreiben einmalig umformatiert. Zusätzliche Kontenfelder und
Metadaten auf der obersten Ebene des Kontenplans bleiben beim Speichern erhalten.
Symbolische Links innerhalb des Buchs werden bei Transaktionen zurückgewiesen, damit
Schreibvorgänge die Arbeitskopie nicht verlassen.
YAML-Kommentare werden weiterhin nicht erhalten; dauerhafte Notizen gehören in
explizite Felder oder Markdown-Text.

## Schreibtransaktionen

Die mit `api._locked` registrierten Schreiboperationen benutzen `storage.transactional`:

1. Lokale Buchsperre übernehmen, unterbrochene Veröffentlichung wiederherstellen.
2. Dateien in eine private Arbeitskopie unter `.allkvitt/transaction/work/` kopieren.
3. Operation und Prüfungen in dieser Kopie ausführen.
4. Inhalt des Originalbuchs vergleichen: externe Änderungen führen zum Abbruch.
5. Änderungen und Sicherungen in einem dauerhaften Manifest festhalten.
6. Geänderte Dateien atomar ersetzen/löschen und einen Git-Commit anlegen.
7. Transaktion als abgeschlossen markieren und Arbeitskopie/Sicherung entfernen.

Die Veröffentlichung umfasst alle geänderten Buchdateien. In Git landen weiterhin
die von der Operation gemeldeten Pfade sowie alle durch sie geänderten, bereits
versionierten Dateien (etwa der alte Inbox-Pfad eines verschobenen Belegs), ergänzt
um Formatmigrationen und Idempotenzbelege. Unveränderte fremde Arbeitsänderungen
bleiben ausserhalb des Commits. Erzeugte OCR-/Kurs-Caches werden dadurch nicht versehentlich
zusätzlich versioniert.

Fehler vor der Veröffentlichung verändern das Originalbuch nicht. Fehler während
der Veröffentlichung stellen die vorherigen Dateiinhalte und den Git-Index wieder
her. Ein abgebrochener Prozess wird beim nächsten API-Zugriff ebenso behandelt.
Ein bereits erstellter Commit wird am Transaktionskennzeichen erkannt und bleibt
erhalten. Treffen Wiederherstellungsprüfungen auf fremde Änderungen, bleiben die
Sicherungen erhalten und der Zugriff stoppt mit einer Konfliktmeldung.

Die Sicherung umfasst auch uncommittete Dateien; sie setzt nicht auf `git reset`
oder `git checkout` zurück. Interne Transaktionsdateien gehören weder in Git noch
in exportierte Bücher. Einzelne YAML-/Markdown-Dateien werden zusätzlich über
`fsync` und atomaren Austausch geschrieben.

Öffentliche API-Leseoperationen teilen die Buchsperre. Ein API-Aufruf sieht so
keine halbfertige Veröffentlichung. Mehrere getrennte API-Aufrufe bilden keinen
gemeinsamen Snapshot; dafür `revision` prüfen. Direkte Zugriffe auf `Book` oder
Dateien müssen für dieselbe Garantie `storage.locked(book.root)` verwenden.

## Revisionen und Wiederholungen

`api.status(book)` liefert `revision`, einen Inhaltsfingerabdruck einschliesslich
uncommitteter Änderungen. Git, Transaktionsdateien, Idempotenzbelege, erzeugte
Berichte sowie Kurs-/OCR-Caches sind ausgenommen.

```python
stand = api.status(book)["revision"]
result = api.post_entry(
    book, "2026-01-05", "6500", "1020", "45.80", "Büromaterial",
    expected_revision=stand,
    idempotency_key="quittung-2026-001-buchen",
)
```

Alle transaktionalen API-Operationen akzeptieren die beiden optionalen Schlüssel.
Der gespeicherte Wiederholungsbeleg liegt unter `.allkvitt/requests/<sha256>.json`
und wird zusammen mit den Nutzdaten veröffentlicht. Derselbe Schlüssel und dieselben
Parameter liefern das gespeicherte Ergebnis (`wiederholt: true`); andere Parameter
zum gleichen Schlüssel sind ein Fehler. Die Wiederholung wird vor der Revisionsprüfung
erkannt, damit ein verlorenes Antwortpaket keine zweite Buchung auslöst.
Idempotenzbelege gehören zur Buchhistorie und dürfen nicht beliebig gelöscht werden.
Python-Callbacks, etwa bei `api.write`, unterstützen keinen Idempotenzschlüssel.

Die Agententools für Vorschläge, Einzel-/Sammelbuchungen, Freigabe, Storno sowie
Rechnungserstellung und Zahlung bieten beide Parameter ausdrücklich an. `status()`
liefert den nötigen Stand. Die bestehenden Freigaberegeln bleiben wirksam.

## Betriebsgrenzen

Die Sperre gilt für kooperierende lokale Prozesse. Editoren und Cloud-Synchronisation
nehmen daran nicht teil. Während einer Veröffentlichung dürfen sie das Buch nicht
ändern; auch manuelle Git-Operationen müssen ausserhalb dieses Zeitfensters bleiben. Vor der Veröffentlichung werden externe Änderungen erkannt; ein beliebiger
externer Dateischreiber kann die verbleibenden Zeitfenster nicht atomar mitprüfen.
Nextcloud ist deshalb kein Mehrrechner-Transaktionsprotokoll: für gemeinsames Arbeiten
einen zentralen Schreibdienst verwenden oder abgeschlossene Bücher kontrolliert
übergeben. Laufende Arbeitskopien/Sicherungen nicht synchronisieren.

Die erste Umsetzung kopiert und vergleicht für Schreibvorgänge das ganze Buch
(ohne Git und Laufzeitdateien). Das bevorzugt einfache Wiederherstellbarkeit, benötigt
aber zusätzlichen Platz und Zeit, besonders bei vielen grossen Belegen. Vor einer
Optimierung mit unveränderlichen Objekten oder einem rekonstruierbaren Suchindex
mit realistischen Buchgrössen messen. Netzwerk-/Mail-Nebenwirkungen und externe
Dateipfade eines Plugins liegen ausserhalb der Buchtransaktion. Plugins schreiben
über das übergebene `Book`, nicht über einen vorher gespeicherten Originalpfad.
