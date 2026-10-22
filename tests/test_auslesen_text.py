"""The booking text a receipt reading proposes: the sender (not a label like «Original») and what was bought."""
from aeradex import erfassung


INVOICE = """Rechnung
Original
Rechnungsnummer: 0239682276
Rechnungsdatum: 2026-10-16
Muster Druck B.V.
Hafenweg 8
5928 LW
Venlo
Niederlande
Artikel Stk. MwSt. Nettobetrag
Matte Visitenkarten 250 8.1% CHF 30.66
Zwischensumme (exkl. MwSt.) CHF 30.66
Lieferkosten (exkl. MwSt.) CHF 8.78
8.1% Gesamtbetrag MwSt. CHF 3.19
Gesamt CHF 42.63
Seite 1/1"""


def test_sender_with_a_dutch_legal_form_and_no_label():
    assert erfassung.parse_text(INVOICE)["name"] == "Muster Druck B.V."
    assert erfassung.parse_text("Original\nBäckerei Beispiel\nGipfeli 2.40\nTotal 2.40")["name"] == "Bäckerei Beispiel"


def test_items_name_what_was_bought():
    assert erfassung.items(INVOICE) == "Matte Visitenkarten"
    assert erfassung.items("Coop Thun\nBananen 1.2 kg 3.40\nMilch 1.60\nBrot 2.80\nTotal CHF 7.80\nBar 10.00") \
        == "Bananen, Milch …"
    assert erfassung.items("Total 10.00\nMwSt 8.1% 0.75") == ""
