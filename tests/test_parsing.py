from datetime import date

import pytest

from app.importers import ImportErrorFR, parse_csv, parse_file, parse_ofx
from app.utils import add_months, merchant_key, parse_amount, parse_date


@pytest.mark.parametrize("raw,cents", [
    ("1 234,56", 123456), ("-12,50", -1250), ("12.5", 1250), ("1.234,56 €", 123456),
    ("1,234.56", 123456), ("-0,99", -99), ("+45,00", 4500), ("45,00-", -4500), (12.3, 1230),
])
def test_parse_amount(raw, cents):
    assert parse_amount(raw) == cents


def test_parse_date():
    assert parse_date("12/10/2026") == date(2026, 10, 12)
    assert parse_date("12/10/26") == date(2026, 10, 12)
    assert parse_date("2026-10-12") == date(2026, 10, 12)
    assert parse_date("20261012120000[0:GMT]") == date(2026, 10, 12)


def test_add_months_clamps_day():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)


def test_merchant_key():
    assert merchant_key("PAIEMENT PAR CARTE X1234 AMAZON PAYMENTS 12/10") == "AMAZON PAYMENTS"
    assert merchant_key("PRLV SEPA AXA FRANCE IARD ECH/101026 ICS FR12ZZZ") .startswith("AXA FRANCE")


CA_CSV = """Compte courant;;;
Solde au 12/10/2026;1 520,30 €;;

Date;Libellé;Débit euros;Crédit euros;
02/10/2026;"PAIEMENT PAR CARTE
X1234 CARREFOUR 01/10";54,20;;
03/10/2026;VIREMENT SALAIRE JEMS;;3 200,00;
05/10/2026;PRLV SEPA AXA FRANCE;48,90;;
"""

BOURSO_CSV = """dateOp;dateVal;label;category;categoryParent;supplierFound;amount;comment;accountNum;accountLabel;accountbalance
2026-10-02;2026-10-02;"CARTE 01/10/26 NETFLIX";"Abonnements";"Loisirs";"netflix";-13,49;;123;CC;1000
2026-10-03;2026-10-03;"VIR SALAIRE";"Salaire";"Revenus";"";2500,00;;123;CC;1000
"""

ENGLISH_CSV = "Date,Description,Amount\n2026-10-02,Coffee shop,-3.50\n2026-10-03,Refund,10.00\n"


def test_csv_credit_agricole_style():
    rows = parse_csv(CA_CSV)
    assert [r["amount_cents"] for r in rows] == [-5420, 320000, -4890]
    assert "CARREFOUR" in rows[0]["label"]
    assert rows[0]["date"] == "2026-10-02"


def test_csv_boursorama_style():
    rows = parse_csv(BOURSO_CSV)
    assert [r["amount_cents"] for r in rows] == [-1349, 250000]
    assert rows[0]["label"].endswith("NETFLIX")


def test_csv_english():
    rows = parse_csv(ENGLISH_CSV)
    assert [r["amount_cents"] for r in rows] == [-350, 1000]


def test_csv_unrecognised():
    with pytest.raises(ImportErrorFR):
        parse_csv("foo;bar\n1;2\n")


OFX = """OFXHEADER:100
DATA:OFXSGML
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261005<TRNAMT>-48.90<FITID>A1<NAME>PRLV AXA FRANCE<MEMO>Assurance auto
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20261003<TRNAMT>3200.00<FITID>A2<NAME>VIR SALAIRE
</BANKTRANLIST><LEDGERBAL><BALAMT>1520.30<DTASOF>20261012</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"""


def test_ofx_sgml():
    rows, bal, bal_date = parse_ofx(OFX)
    assert [(r["ref"], r["amount_cents"]) for r in rows] == [("A1", -4890), ("A2", 320000)]
    assert rows[0]["label"] == "PRLV AXA FRANCE Assurance auto"
    assert bal == 152030 and bal_date == date(2026, 10, 12)


def test_detect_format():
    assert parse_file("releve.ofx", OFX.encode())[0] == "ofx"
    assert parse_file("x.csv", CA_CSV.encode("cp1252"))[0] == "csv"
    assert parse_file("x.pdf", b"%PDF-1.4 ...")[0] == "pdf"
