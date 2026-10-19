"""Account names with commas: the shipped charts quote them, and books written from older charts are repaired."""
from pathlib import Path

import pytest

from aeradex import plugins
from aeradex.book import load_accounts


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
