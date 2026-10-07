"""Salary payment exports: SPS pain.001.001.09, domestic CHF, category SALA.

Exports do not book payments: closing payroll already owns the bank journal rows.
An existing export is downloaded unchanged; cancellation is explicit and audited.
"""
from datetime import datetime
from decimal import Decimal
import hashlib
import uuid
from xml.etree import ElementTree as ET

from . import payroll, qrbill_ch as qr
from .book import BookError
from .files import parse_date, read_yaml, write_yaml
from .kreditoren import debtor_account

NS = 'urn:iso:std:iso:20022:tech:xsd:pain.001.001.09'


def manifest_path(book, year, month):
    return book.root / 'lohn' / str(year) / f'{month:02d}' / 'zahlung.yaml'


def active(book, year, month):
    path = manifest_path(book, year, month)
    data = read_yaml(path) if path.exists() else {}
    return data if data.get('status') == 'erstellt' else None


def iban(value):
    value = qr.normalize_iban(value)
    if qr.iban_problem(value) or qr.is_qr_iban(value):
        raise BookError('Für Lohnzahlungen ist eine gültige normale CH/LI-IBAN erforderlich')
    return value


def create(book, year, month, execution):
    d = parse_date(execution, 'Ausführungsdatum')
    previous = active(book, year, month)
    if previous:
        if previous['ausfuehrung'] != d.isoformat():
            raise BookError('Zahlungsdatei besteht bereits. Vor einer Änderung zuerst zurückziehen.')
        path = book.root / previous['datei']
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != previous['sha256']:
            raise BookError('Lohnzahlungsdatei fehlt oder wurde verändert')
        return previous, []
    slips = [p for p in payroll.payslips(book, year) if int(p['monat']) == month]
    if not slips or any(p.get('status') != 'abgeschlossen' for p in slips):
        raise BookError('Zuerst alle Abrechnungen dieses Lohnlaufs abschliessen')
    items = []
    for slip in slips:
        if not payroll.payslip_fingerprint_ok(slip):
            raise BookError('Lohnabrechnung wurde nach Abschluss verändert')
        amount = payroll.D(slip['werte'].get('auszahlung', slip['werte']['nettolohn']))
        if amount < 0:
            raise BookError(f"{slip['mitarbeiter']}: negative Auszahlung")
        if amount == 0:
            continue
        emp = payroll.employee(book, slip['mitarbeiter'])
        try:
            account = iban(emp.get('iban'))
        except BookError as exc:
            raise BookError(f"{payroll.display_name(emp)}: {exc}") from exc
        items.append((slip, emp, account, amount))
    if not items:
        raise BookError('Keine positiven Lohnauszahlungen vorhanden')
    account = iban(debtor_account(book))
    total = sum((i[3] for i in items), Decimal(0))
    msg = 'LOHN-' + uuid.uuid4().hex[:28].upper()
    root = ET.Element('Document', xmlns=NS)
    def el(parent, tag, value=None):
        node = ET.SubElement(parent, tag)
        if value is not None:
            node.text = str(value)
        return node
    def party(parent, tag, name, address):
        if not name or not address.get('ort'):
            raise BookError(f'{name or tag}: Name und Ort für die Lohnzahlung ergänzen')
        p = el(parent, tag)
        el(p, 'Nm', name[:140])
        a = el(p, 'PstlAdr')
        for xml, key, limit in [('StrtNm', 'strasse', 70), ('BldgNb', 'nr', 16),
                                ('PstCd', 'plz', 16), ('TwnNm', 'ort', 35)]:
            if address.get(key):
                el(a, xml, str(address[key])[:limit])
        country = str(address.get('land') or 'CH').upper()
        if len(country) != 2 or not country.isalpha():
            raise BookError(f'{name}: ungültiger Ländercode')
        el(a, 'Ctry', country)
    def acct(parent, tag, value):
        el(el(el(parent, tag), 'Id'), 'IBAN', value)
    init = el(root, 'CstmrCdtTrfInitn')
    hdr = el(init, 'GrpHdr')
    el(hdr, 'MsgId', msg)
    el(hdr, 'CreDtTm', datetime.now().replace(microsecond=0).isoformat())
    el(hdr, 'NbOfTxs', len(items)); el(hdr, 'CtrlSum', f'{total:.2f}')
    el(el(hdr, 'InitgPty'), 'Nm', book.settings.firma[:140])
    p = el(init, 'PmtInf')
    el(p, 'PmtInfId', msg); el(p, 'PmtMtd', 'TRF'); el(p, 'BtchBookg', 'true')
    el(p, 'NbOfTxs', len(items)); el(p, 'CtrlSum', f'{total:.2f}')
    el(el(el(p, 'PmtTpInf'), 'CtgyPurp'), 'Cd', 'SALA')
    el(el(p, 'ReqdExctnDt'), 'Dt', d.isoformat())
    party(p, 'Dbtr', book.settings.firma, book.settings.adresse)
    acct(p, 'DbtrAcct', account)
    clearing = el(el(el(p, 'DbtrAgt'), 'FinInstnId'), 'ClrSysMmbId')
    el(el(clearing, 'ClrSysId'), 'Cd', 'CHBCC'); el(clearing, 'MmbId', account[4:9])
    for slip, emp, recipient, amount in items:
        tx = el(p, 'CdtTrfTxInf')
        ident = el(tx, 'PmtId')
        el(ident, 'EndToEndId', f"L-{year}-{month:02d}-{slip['mitarbeiter']}")
        el(el(tx, 'Amt'), 'InstdAmt', f'{amount:.2f}').set('Ccy', 'CHF')
        party(tx, 'Cdtr', payroll.display_name(emp), emp.get('adresse') or {})
        acct(tx, 'CdtrAcct', recipient)
        el(el(tx, 'RmtInf'), 'Ustrd', f'Lohn {month:02d}/{year}')
    data = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    path = manifest_path(book, year, month).with_name(f'{msg.lower()}.xml')
    info = {'status': 'erstellt', 'datei': book.rel(path), 'ausfuehrung': d.isoformat(),
            'anzahl': len(items), 'total': str(total), 'sha256': hashlib.sha256(data).hexdigest(), 'msg_id': msg}
    path.write_bytes(data)
    manifest = manifest_path(book, year, month)
    write_yaml(manifest, info)
    return info, [path, manifest]


def cancel(book, year, month):
    info = active(book, year, month)
    if not info:
        raise BookError('Keine aktive Lohnzahlungsdatei vorhanden')
    info['status'] = 'zurueckgezogen'
    path = manifest_path(book, year, month)
    write_yaml(path, info)
    return [path]
