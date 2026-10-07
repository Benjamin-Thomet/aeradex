"""Regression sources: ESTV KS 45 3.2, 6.3–6.6, 7; ESTV records 2025 3/4;
SIX SPS pain.001.001.09 credit transfer guidelines (SALA).
"""
from decimal import Decimal
from xml.etree import ElementTree as ET
import io
from zipfile import ZipFile

import pytest

from allkvitt import api, payroll, qst, qst_estv
from allkvitt.book import Book, BookError
from test_allkvitt import book


def employee(**kw):
    return dict(monatslohn=5000, pensum=100, qst={'kanton': 'BS', 'jahr': 2025, 'code': 'A0N'}, **kw)


def calc(emp, inputs=None, tariff=None):
    return payroll.calculate(emp, payroll.DEFAULT_CONFIG, 2026, 3, inputs or {}, tariff)


def test_monthly_default_current_year_and_allowances():
    # KS 45 3.2/6.3: allowances taxable; 6.4: one job needs no gross-up.
    w = calc(employee(kinderzulagen=250))
    rate = qst.rate('BS', 2026, 'A0N', 5250)
    assert w['qst_satzbestimmend'] == w['qst_basis'] == Decimal('5250')
    assert w['quellensteuer'] == payroll.money(Decimal(5250) * rate)
    assert w['ahv'] == Decimal('265')
    assert w['qst_jahr'] == 2026


def test_unknown_tariff_never_zero():
    emp = employee()
    emp['qst']['kanton'] = 'ZH'
    with pytest.raises(BookError, match='Tariftabelle'):
        calc(emp)
    emp['qst'] = {'code': 'A0N'}
    with pytest.raises(BookError, match='vollständig'):
        calc(emp)


def test_explicit_basis_and_invalid_conflicts():
    w = calc(employee(), {'qst_satzbestimmend': 8000})
    assert w['qst_satzbestimmend'] == 8000
    with pytest.raises(BookError, match='entweder'):
        calc(employee(), {'qst_satzbestimmend': 8000, 'qst_gesamtpensum': 100})
    with pytest.raises(BookError, match='positiv'):
        calc(employee(), {'qst_satzbestimmend': 0})


def test_annual_cantons_require_explicit_basis():
    emp = employee()
    emp['qst']['kanton'] = 'VD'
    with pytest.raises(BookError, match='Jahresmodell'):
        calc(emp)


def test_fractional_and_zero_workload():
    # Preserve agreed workload; don't quantize a percentage to 1% steps.
    emp = employee(); emp['pensum'] = '82.5'
    assert calc(emp)['bruttolohn'] == Decimal('4125')
    emp['pensum'] = 0
    assert calc(emp)['bruttolohn'] == 0


def records(canton='BS', year=2026, minimum=0):
    # Fixed-width positions per ESTV 2025 record specification 3.3/3.7.
    header = f'00{canton}' + ' ' * 15 + f'{year}0101' + ' ' * 83
    row = f'0601{canton}{"A0N":10}{year}0101{100:09d}{10000000:09d} 00{minimum:09d}{1000:05d}   '
    trailer = '99' + ' ' * 15 + canton + f'{3:08d}' + ' ' * 12
    return '\n'.join([header, row, trailer]).encode()


def test_estv_minimum_tax_and_exact_rate():
    table = qst_estv.parse(records(minimum=60000), 'BS', 2026, 'fixture')
    w = calc(employee(kinderzulagen=100), tariff=table)
    assert w['qst_satz'] == Decimal('.10')
    assert w['quellensteuer'] == 600  # minimum tax per ESTV 4.4
    assert w['qst_quelle'] == table['sha256']
    with pytest.raises(qst.UnknownTariff):
        qst_estv.lookup(table, 'A0Y', 5000)
    with pytest.raises(qst.UnknownTariff):
        qst_estv.lookup(table, 'A0N', 100001)


@pytest.mark.parametrize('data', [records(year=2025), records(canton='ZH'), records()[:-15],
                                    records().replace(b'00000003', b'00000004')])
def test_estv_rejects_mismatched_or_truncated_file(data):
    with pytest.raises(BookError):
        qst_estv.parse(data, 'BS', 2026, 'fixture')


def test_download_and_book_scoped_import(book, monkeypatch):
    archive = io.BytesIO()
    with ZipFile(archive, 'w') as z:
        z.writestr('tar26bs.txt', records())
    urls = []
    def fetch(request, timeout):
        assert request.get_header('User-agent', '').startswith('allkvitt/')   # ESTV blocks Python-urllib
        urls.append(request.full_url)
        return io.BytesIO(archive.getvalue())
    monkeypatch.setattr(qst_estv, 'urlopen', fetch)
    api.qst_sync(book, 'BS', 2026)
    assert urls == ['https://www.estv2.admin.ch/qst/2026/loehne/tar26bs.zip']
    api.employee_add(book, 'A', 'B', monatslohn=5000, qst=employee()['qst'])
    api.payroll_run(book, '2026-03')
    w = api.payslip_show(book, '2026-03', 'M0001')['werte']
    assert Decimal(w['quellensteuer']) == 500
    assert qst.rate('BS', 2026, 'A0N', 5000) != Decimal('.10')  # no global contamination


def add_payee(book):
    api.employee_add(book, 'Anne &', 'Muster', monatslohn=5000, iban='CH9300762011623852957',
                     strasse='Weg', nr='2', plz='3000', ort='Bern')
    api.payroll_run(book, '2026-03')


def test_payment_lifecycle_and_no_duplicate_posting(book):
    add_payee(book)
    with pytest.raises(BookError, match='abschliessen'):
        api.payroll_payment_export(book, '2026-03', '2026-03-25')
    api.payslip_close(book, '2026-03', 'M0001')
    before = list(Book(book.root).rows)
    result = api.payroll_payment_export(book, '2026-03', '2026-03-25')['zahlung']
    xml = (book.root / result['datei']).read_bytes()
    ns = {'p': 'urn:iso:std:iso:20022:tech:xsd:pain.001.001.09'}
    doc = ET.fromstring(xml)
    assert doc.find('.//p:CtgyPurp/p:Cd', ns).text == 'SALA'
    assert doc.find('.//p:Cdtr/p:Nm', ns).text == 'Anne & Muster'
    assert doc.find('.//p:ReqdExctnDt/p:Dt', ns).text == '2026-03-25'
    assert Decimal(doc.find('.//p:InstdAmt', ns).text) == Decimal(api.payslip_show(book, '2026-03', 'M0001')['werte']['nettolohn'])
    assert list(Book(book.root).rows) == before
    assert api.payroll_payment_export(book, '2026-03', '2026-03-25')['zahlung'] == result
    with pytest.raises(BookError, match='zurückziehen'):
        api.payslip_reopen(book, '2026-03', 'M0001')
    with pytest.raises(BookError, match='zurückziehen'):
        api.payroll_run(book, '2026-03')
    with pytest.raises(BookError):
        api.payroll_payment_export(book, '2026-03', '2026-03-26')
    api.payroll_payment_cancel(book, '2026-03')
    api.payslip_reopen(book, '2026-03', 'M0001')
    assert (book.root / result['datei']).read_bytes() == xml  # history preserved


def test_payment_missing_iban_and_tampering(book):
    add_payee(book)
    api.employee_update(book, 'M0001', iban='')
    api.payslip_close(book, '2026-03', 'M0001')
    with pytest.raises(BookError, match='IBAN'):
        api.payroll_payment_export(book, '2026-03', '2026-03-25')
    api.employee_update(book, 'M0001', iban='CH9300762011623852957')
    info = api.payroll_payment_export(book, '2026-03', '2026-03-25')['zahlung']
    (book.root / info['datei']).write_bytes(b'changed')
    with pytest.raises(BookError, match='verändert'):
        api.payroll_payment_export(book, '2026-03', '2026-03-25')


def test_payroll_failure_leaves_no_partial_run(book):
    api.employee_add(book, 'Good', 'Employee', monatslohn=5000)
    api.employee_add(book, 'Bad', 'Tariff', monatslohn=5000, qst={'kanton':'ZH','jahr':2026,'code':'A0N'})
    with pytest.raises(BookError):
        api.payroll_run(book, '2026-03')
    assert payroll.payslips(book) == []


def test_payment_includes_expenses(book):
    add_payee(book)
    api.expense_add(book, 'M0001', '2026-03-10', 'Zugbillett', '80', '5820')
    api.payslip_close(book, '2026-03', 'M0001')
    info = api.payroll_payment_export(book, '2026-03', '2026-03-25')['zahlung']
    slip = api.payslip_show(book, '2026-03', 'M0001')['werte']
    assert Decimal(info['total']) == Decimal(slip['nettolohn']) + 80


def test_estv_boundaries_and_single_church_variant():
    table = {'kanton': 'BS', 'jahr': 2026, 'codes': {'A0N': [[100, 500000, 0, 500], [500100, 10000000, 0, 1000]]}}
    assert qst_estv.lookup(table, 'A0N', '5000')[0] == Decimal('.05')
    assert qst_estv.lookup(table, 'A0N', '5000.01')[0] == Decimal('.10')
    with pytest.raises(qst.UnknownTariff):
        qst_estv.lookup(table, 'A0Y', 5000)


def test_input_failure_does_not_change_draft(book):
    api.employee_add(book, 'A', 'B', monatslohn=5000, qst=employee()['qst'])
    api.payroll_run(book, '2026-03')
    path = payroll.payslip_path(book, 2026, 3, 'M0001')
    before = path.read_bytes()
    with pytest.raises(BookError):
        api.payslip_inputs(book, '2026-03', 'M0001', {'qst_satzbestimmend': 0})
    assert path.read_bytes() == before


def test_all_canton_import_validates_before_writing(book, monkeypatch):
    calls = []
    def fetch(k, y):
        calls.append(k)
        if k == 'ZH':
            raise BookError('Netzwerkfehler')
        return qst_estv.parse(records(k, y), k, y, 'fixture')
    monkeypatch.setattr(qst_estv, 'download', fetch)
    with pytest.raises(BookError):
        api.qst_sync(book, 'ALLE', 2026)
    assert calls == list(qst_estv.CANTONS)
    assert not list((book.root / 'lohn' / 'qst_tarife').glob('*.json'))


def test_ks45_partial_month_endpoints():
    # KS 45 6.6: entry on last day of February/March means one tax day.
    for year, month, day in [(2026, 2, 28), (2024, 2, 29), (2026, 3, 31), (2026, 3, 30)]:
        emp = employee(eintritt=f'{year}-{month:02d}-{day:02d}')
        table = qst_estv.parse(records(year=year), 'BS', year, 'fixture')
        w = payroll.calculate(emp, payroll.DEFAULT_CONFIG, year, month, {}, table)
        assert w['qst_satzbestimmend'] == payroll.money(w['bruttolohn'] * 30)
