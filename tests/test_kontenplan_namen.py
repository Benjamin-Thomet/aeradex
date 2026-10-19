"""Account names with commas: the shipped charts quote them, and books written from older charts are repaired."""
from pathlib import Path

import pytest

from aeradex import plugins
from aeradex.book import load_accounts, save_accounts


@pytest.mark.parametrize("name", sorted(plugins.kontenplaene()))
def test_shipped_charts_keep_names_whole(name):
    path = Path(plugins.kontenplaene()[name])
    for acct in load_accounts(path).values():
        assert not any(v is None for v in acct.extra.values()), (name, acct.nr, acct.extra)


def test_a_split_name_is_joined_back(tmp_path):
    p = tmp_path / "kontenplan.yaml"
    p.write_text('konten:\n- {nr: "1520", name: Büromaschinen, Informatik, Kommunikationstechnologie, klasse: aktiv}\n'
                 '- {nr: "6800", name: Abschreibungen, klasse: aufwand, notiz: ~}\n')
    accts = load_accounts(p)
    assert accts["1520"].name == "Büromaschinen, Informatik, Kommunikationstechnologie" and accts["1520"].extra == {}
    assert accts["6800"].name == "Abschreibungen" and accts["6800"].extra == {"notiz": None}


def test_a_book_saved_with_split_names_is_repaired(tmp_path):
    """save_accounts wrote the fragments first, as extra keys: Informatik: null, …, nr, name, klasse."""
    p = tmp_path / "kontenplan.yaml"
    p.write_text("konten:\n- Informatik: null\n  Kommunikationstechnologie: null\n  nr: '1520'\n"
                 "  name: Büromaschinen\n  klasse: aktiv\n")
    accts = load_accounts(p)
    assert accts["1520"].name == "Büromaschinen, Informatik, Kommunikationstechnologie" and accts["1520"].extra == {}
    save_accounts(p, accts)
    assert "Informatik: null" not in p.read_text() and load_accounts(p)["1520"].name.endswith("Kommunikationstechnologie")
