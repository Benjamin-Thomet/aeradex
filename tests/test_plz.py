"""Postcode → canton from the official locality directory (swisstopo AMTOVZ)."""
from aeradex import plz


def test_postcodes_of_one_canton():
    assert plz.kanton("3600", "Thun") == "BE"
    assert plz.kanton("8001") == "ZH"
    assert plz.kanton("1201", "Genève") == "GE"
    assert plz.kanton("6500", "Bellinzona") == "TI"


def test_postcode_across_cantons_is_resolved_by_the_place():
    assert plz.choices("1290") == ["GE", "VD"]
    assert plz.kanton("1290", "Versoix") == "GE"        # municipality in GE, a locality name in VD as well
    assert plz.kanton("1290", "Mies") == "VD"
    assert plz.kanton("1290") is None


def test_non_address_postcode_falls_back_to_the_place_and_unknown_is_none():
    assert plz.kanton("3000", "Bern") == "BE"            # PO-box postcode, not in the directory
    assert plz.kanton("9490", "Vaduz") is None           # Liechtenstein: not a canton
    assert plz.kanton("", "") is None
