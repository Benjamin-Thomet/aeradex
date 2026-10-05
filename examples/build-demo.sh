#!/bin/sh
# Rebuilds examples/muster-gmbh — a fictional company, no real data.
set -e
cd "$(dirname "$0")"
rm -rf muster-gmbh
export ALLKVITT_NO_COMMIT=1
b() { allkvitt --buch muster-gmbh "$@" >/dev/null; }
allkvitt init muster-gmbh --firma "Muster GmbH" --jahr 2026 --strasse Bahnhofstrasse --nr 1 --plz 3000 \
  --ort Bern --iban "CH93 0076 2011 6238 5295 7" --uid CHE-123.456.789 --ohne-git >/dev/null
python3 - <<'PY'
p = "muster-gmbh/kontenplan.yaml"
s = open(p).read()
for nr, val in (("1020", "30000"), ("2800", "-20000"), ("2970", "-10000")):
    s = s.replace(f'{{nr: "{nr}", name:', f'{{nr: "{nr}", eroeffnung: {val}, name:')
open(p, "w").write(s)
PY
b book --datum 2026-01-05 --soll 6500 --haben 1020 --betrag 45.80 --text "Büromaterial Papeterie"
b book --datum 2026-01-31 --soll 6000 --haben 1020 --betrag 1800 --text "Miete Büro Januar"
b book-split --datum 2026-02-03 --text "Kundenapéro und Büromaterial" \
  --zeilen '[{"soll":"6641","betrag":"64.50"},{"soll":"6500","betrag":"18.20"},{"haben":"1000","betrag":"82.70"}]'
b customer add --name "Anna Beispiel" --firma "Beispiel AG" --strasse Marktgasse --nr 5 --plz 3011 --ort Bern
b customer add --name "Peter Privat" --strasse Seeweg --nr 12 --plz 3600 --ort Thun --rechnung-an person
b invoice create --kunde K0001 --pos "Beratung Buchhaltungsprozesse;12 h;160" --pos "Spesenpauschale;1;80;3600" \
  --datum 2026-02-10 --text "Gemäss Offerte vom 15. Januar 2026."
b invoice create --kunde K0002 --pos "Steuererklärung 2025;1;450" --datum 2026-03-02
b invoice pay R-2026-0001 --datum 2026-03-05
b employee add --vorname Lea --nachname Muster --strasse Weg --nr 2 --plz 3000 --ort Bern \
  --ahv-nr 756.1234.5678.97 --geburtsdatum 1990-04-01 --eintritt 2026-01-01 --monatslohn 6500 --pensum 80 --bvg-betrag 220
b employee add --vorname Tom --nachname Stunde --lohnart stunde --stundenlohn 32 --standard-stunden 40 \
  --ferienzuschlag-satz 0.0833 --eintritt 2026-01-01 --qst-code A0N --qst-kanton BS --qst-jahr 2026
for m in 01 02; do
  b payroll run 2026-$m
  b payroll run 2026-$m --mitarbeiter M0002 --qst-satzbestimmend 3000
  b payroll close 2026-$m M0001
  b payroll close 2026-$m M0002
done
b lieferant add --name "Swisscom (Schweiz) AG" --strasse "Alte Tiefenaustrasse" --nr 6 --plz 3048 --ort Worblaufen \
  --iban "CH56 0483 5012 3456 7800 9" --konto 6510
b lieferant add --name "Immobilien Muster AG" --strasse Bundesgasse --nr 20 --plz 3011 --ort Bern \
  --iban "CH93 0076 2011 6238 5295 7" --konto 6000
b kreditor add --lieferant L0002 --betrag 1800 --datum 2026-02-25 --faellig 2026-03-01 --rechnungsnr "Miete März"
b kreditor add --lieferant L0001 --betrag 89.90 --datum 2026-03-15 --referenz RF18539007547034
b kreditor pay E-2026-0001 --datum 2026-03-01
b payroll run 2026-03
b propose --datum 2026-03-10 --soll 6510 --haben 1020 --betrag 59.00 --text "Swisscom März" \
  --begruendung "Telefon/Internet-Rechnung, Lastschrift"
printf 'Papeterie Muster\nDatum: 12.03.2026\nDruckerpapier A4, 5 Pakete\nTotal CHF 32.50\nbar bezahlt\n' > muster-gmbh/inbox/quittung-papeterie.txt
allkvitt --buch muster-gmbh check
