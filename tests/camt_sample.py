"""Build camt.053.001.08 statements for tests (structure per Swiss Payment Standards)."""
from __future__ import annotations

from decimal import Decimal


def entry(amount: str, side: str, booked: str, ref: str = "", ref_type: str = "", e2e: str = "", party: str = "",
          ustrd: str = "", acct_ref: str = "") -> str:
    """side: CRDT (money in) or DBIT (money out)."""
    rmt = ""
    if ref:
        tp = "<Prtry>QRR</Prtry>" if ref_type == "QRR" else "<Cd>SCOR</Cd>"
        rmt = f"<RmtInf><Strd><CdtrRefInf><Tp><CdOrPrtry>{tp}</CdOrPrtry></Tp><Ref>{ref}</Ref></CdtrRefInf></Strd></RmtInf>"
    elif ustrd:
        rmt = f"<RmtInf><Ustrd>{ustrd}</Ustrd></RmtInf>"
    role = "Dbtr" if side == "CRDT" else "Cdtr"
    parties = f"<RltdPties><{role}><Pty><Nm>{party}</Nm></Pty></{role}></RltdPties>" if party else ""
    return (f"<Ntry><Amt Ccy=\"CHF\">{amount}</Amt><CdtDbtInd>{side}</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts>"
            f"<BookgDt><Dt>{booked}</Dt></BookgDt><ValDt><Dt>{booked}</Dt></ValDt><AcctSvcrRef>{acct_ref or booked + amount}</AcctSvcrRef>"
            f"<BkTxCd><Domn><Cd>PMNT</Cd><Fmly><Cd>{'RCDT' if side == 'CRDT' else 'ICDT'}</Cd><SubFmlyCd>AUTT</SubFmlyCd></Fmly></Domn></BkTxCd>"
            f"<NtryDtls><TxDtls><Refs><EndToEndId>{e2e or 'NOTPROVIDED'}</EndToEndId></Refs>"
            f"<Amt Ccy=\"CHF\">{amount}</Amt><CdtDbtInd>{side}</CdtDbtInd>{parties}{rmt}</TxDtls></NtryDtls></Ntry>")


def statement(iban: str, opening: str, entries: list[tuple[str, str]], frm: str, to: str, stmt_id: str = "STMT-1") -> bytes:
    """entries: list of (xml, signed amount) as returned by entry() with its signed value."""
    closing = Decimal(opening) + sum((Decimal(v) for _, v in entries), Decimal("0"))

    def bal(code, amount, day):
        a = Decimal(amount)
        return (f"<Bal><Tp><CdOrPrtry><Cd>{code}</Cd></CdOrPrtry></Tp><Amt Ccy=\"CHF\">{abs(a):.2f}</Amt>"
                f"<CdtDbtInd>{'CRDT' if a >= 0 else 'DBIT'}</CdtDbtInd><Dt><Dt>{day}</Dt></Dt></Bal>")
    body = "".join(x for x, _ in entries)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08">'
        f"<BkToCstmrStmt><GrpHdr><MsgId>MSG-{stmt_id}</MsgId><CreDtTm>{to}T20:00:00</CreDtTm></GrpHdr>"
        f"<Stmt><Id>{stmt_id}</Id><ElctrncSeqNb>1</ElctrncSeqNb><CreDtTm>{to}T20:00:00</CreDtTm>"
        f"<FrToDt><FrDtTm>{frm}T00:00:00</FrDtTm><ToDtTm>{to}T23:59:59</ToDtTm></FrToDt>"
        f"<Acct><Id><IBAN>{iban}</IBAN></Id><Ccy>CHF</Ccy></Acct>"
        f"{bal('OPBD', opening, frm)}{bal('CLBD', str(closing), to)}{body}</Stmt></BkToCstmrStmt></Document>"
    ).encode("utf-8")
