"""Official ESTV fixed-width wage tariffs (record format valid from 2025).

Source: https://www.estv.admin.ch/de/quellensteuertarife-import-in-lohnbuchhaltungssysteme
Tables are stored in the book, with source URL and checksum, never in site-packages.
"""
import hashlib
import io
import json
from decimal import Decimal
from urllib.error import URLError
from urllib.request import Request, urlopen
from zipfile import ZipFile, BadZipFile

from . import __version__
from .book import BookError

CANTONS = tuple('AG AI AR BE BL BS FR GE GL GR JU LU NE NW OW SG SH SO SZ TG TI UR VD VS ZG ZH'.split())
ANNUAL = {'FR', 'GE', 'TI', 'VD', 'VS'}
MAX_BYTES = 40 * 1024 * 1024


def parse(data: bytes, canton: str, year: int, source: str) -> dict:
    """Preserve rates and minimum taxes exactly; reject partial/mixed-year files."""
    lines = [line for line in data.decode('latin-1').splitlines() if line.strip()]
    if not lines or lines[0][:4] != '00' + canton or lines[-1][:2] != '99':
        raise BookError('ESTV: Vorlauf- oder Endrecord fehlt / falscher Kanton')
    try:
        if lines[-1][17:19] != canton or int(lines[-1][19:27]) != len(lines):
            raise ValueError('Recordanzahl')
        codes = {}
        for line in lines[1:-1]:
            if line[:2] not in {'06', '11', '12', '13'}:
                raise ValueError('Recordart')
            if len(line) < 59 or line[2:4] != '01' or line[4:6] != canton:
                raise ValueError('Record/Kanton/Transaktion')
            if line[16:24] != f'{year}0101':
                raise ValueError('Gültigkeitsdatum')
            if line[:2] != '06':
                continue  # predefined categories/commissions/median are not wage tariffs
            code = line[6:16].strip()
            from .qst import parse_code
            parse_code(code)
            low, width, minimum, pct = (int(line[a:b]) for a, b in [(24,33),(33,42),(45,54),(54,59)])
            if low < 100 or width <= 0 or minimum < 0 or not 0 <= pct <= 10000:
                raise ValueError('Tarifwerte')
            codes.setdefault(code, []).append([low, low + width - 100, minimum, pct])
        if not codes:
            raise ValueError('Keine Lohntarife')
        for code, rows in codes.items():
            rows.sort()
            if rows[0][0] != 100 or rows[-1][1] < 9999900:
                raise ValueError(f'{code}: unvollständiger Einkommensbereich')
            if any(a[1] + 100 != b[0] for a, b in zip(rows, rows[1:])):
                raise ValueError(f'{code}: Lücke/Überlappung')
    except (ValueError, IndexError) as exc:
        raise BookError(f'ESTV-Tarifdatei ungültig: {exc}') from exc
    return {'kanton': canton, 'jahr': year, 'quelle': source,
            'sha256': hashlib.sha256(data).hexdigest(), 'codes': codes}


def download(canton, year):
    canton = str(canton).upper()
    year = int(year)
    if canton not in CANTONS or not 2025 <= year <= 2099:
        raise BookError('ESTV-Import: gültigen Kanton und Jahr ab 2025 wählen')
    url = f'https://www.estv2.admin.ch/qst/{year}/loehne/tar{year % 100:02d}{canton.lower()}.zip'
    try:
        # ESTV answers the default Python-urllib User-Agent with 403 Forbidden
        request = Request(url, headers={'User-Agent': f'aeradex/{__version__}'})
        with urlopen(request, timeout=30) as response:
            payload = response.read(MAX_BYTES + 1)
        if len(payload) > MAX_BYTES:
            raise BookError('ESTV-Download zu gross')
        with ZipFile(io.BytesIO(payload)) as archive:
            members = [f for f in archive.infolist() if not f.is_dir() and f.filename.lower().endswith('.txt')]
            if len(members) != 1 or members[0].file_size > MAX_BYTES:
                raise BookError('ESTV-Archiv enthält keine eindeutige Tarifdatei')
            raw = archive.read(members[0])
        return parse(raw, canton, year, url)
    except (URLError, OSError, BadZipFile) as exc:
        raise BookError(f'ESTV-Tarife {canton} {year} konnten nicht geladen werden: {exc}') from exc


def table_path(book, canton, year):
    if canton not in CANTONS:
        raise BookError('Ungültiger Quellensteuerkanton')
    return book.root / 'lohn' / 'qst_tarife' / f'{canton}-{int(year)}.json'


def table(book, canton, year):
    path = table_path(book, canton.upper(), year)
    return json.loads(path.read_text()) if path.exists() else None


def lookup(table, code, income):
    from bisect import bisect_left
    from .qst import UnknownTariff
    code = code.strip().upper()
    rows = table['codes'].get(code)
    if not rows:
        raise UnknownTariff(f"Tarif {code} fehlt in {table['kanton']} {table['jahr']}")
    amount = Decimal(str(income)) * 100
    index = bisect_left([r[1] for r in rows], amount)
    if index >= len(rows):
        raise UnknownTariff('Einkommen über der importierten Tariftabelle; kantonale Berechnung erforderlich')
    row = rows[index]
    return Decimal(row[3]) / 10000, Decimal(row[2]) / 100
