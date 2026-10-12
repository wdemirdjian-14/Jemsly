"""Relevé Crédit Agricole FICTIF (noms, numéros et montants inventés) au format `pdftotext -layout`."""
import json
from datetime import date

from sqlalchemy import select

from app import ca_pdf
from app.models import Account, Category, Rule, Transaction
from app.search import parse_query

L = " " * 40  # marge gauche de la 1re page

CA_TEXT = f"""                                                                                  RELEVE DE COMPTES EN EUROS N° 004
                                                              ILE DE FRANCE                       Date d'arrêté : 08 Avril 2026
{L}M. Dupont Jean Ou Mme Martin Claire - Compte Chèque n° 11112222333
{L}IBAN : FR76 0000 0000 0000 0000 0000 000   BIC : AGRIFRPP000
{L} Date
{L} opé.
{L}           Date
{L}          valeur
{L}                   Libellé des opérations                                            Débit               Crédit    þ
{L}                                                       Ancien solde débiteur au 09.03.2026        1 000,00

{L} 10.03    10.03    Prlv      Ecole Exemple                                           700,00                         ¨
{L}                   MANDAT-0001 FR30ZZZ000000 Core
{L} 13.03    13.03    Virement    De M.  Dupont Jean                                                        1 500,00
{L} 20.03    20.03    Prlv      Dépenses Carte X1234 Au 18/03/26                         150,50
                                                                         Page 1 / 3
                                              CA Ile-De-France Credit Agricole Ile De France 26 Quai De La Rapee 75596 PARIS CEDEX 12 775 665 615 RCS PARIS
                                                                    RELEVE DE COMPTES EN EUROS N° 004
                                                                                 Date d'arrêté : 08 Avril 2026
M. Dupont Jean Ou Mme Martin Claire - Compte Chèque n° 11112222333 (suite)
 Date
 opé.
        Date
        valeur
                 Libellé des opérations                                                Débit            Crédit þ
05.04   05.04    Ech Prêt   00009999999 Echéance 05/04/26                              1 200,00                  ¨
07.04   07.04    Virement   Jems                                                                        3 000,00 ¨
              7000-1Salaire De 2026-03
                                                 Total des opérations                   2 050,50         4 500,00
                                   Nouveau solde créditeur au 08.04.2026                                 1 449,50
Information préalable sur les frais bancaires
 10.03   Commission d'intervention                                                                         8,00
                                                                       Page 2 / 3
{L}Mr Jean Dupont - Gold Dual Dd Sociétaire n° 000000xxxxxxxx99
{L} Date
{L} ope.
{L}        Date
{L}        valeur
{L}                 Libellé des opérations                                                  Débit               Crédit
{L}02.03            Carrefour Market     75 Paris                                          4,50                        ¨
12345
{L}05.03            Brasserie Exemple     75 Paris                                         96,00
{L}15.03            Station Exemple       75 Paris                                         50,00
{L}                                                           Montant prélevé au 20.03.2026         150,50
"""


def test_parse_ca_text():
    res = ca_pdf.parse_ca_text(CA_TEXT)
    acc, card = res["sections"]
    assert acc["kind"] == "account" and acc["number"] == "11112222333" and acc["check"]
    assert [r["amount_cents"] for r in acc["rows"]] == [-70000, 150000, -15050, -120000, 300000]
    assert acc["rows"][0]["date"] == "2026-03-10"
    assert acc["rows"][0]["label"] == "Prlv Ecole Exemple MANDAT-0001"  # référence SEPA nettoyée
    assert "Salaire De 2026-03" in acc["rows"][4]["label"]  # ligne de complément rattachée
    assert acc["opening"] == -100000 and acc["closing"] == 144950
    assert card["kind"] == "card" and card["check"] and len(card["rows"]) == 3
    assert ca_pdf.card_settlement_key(acc["rows"][2]["label"]) == "X1234"


def test_import_ca_pdf_card_and_balance(client, db, monkeypatch):
    monkeypatch.setattr(ca_pdf, "pdf_to_text", lambda raw: CA_TEXT)
    r = client.post("/import", data={"account_id": ""}, files={"file": ("releve.pdf", b"%PDF-1.4 fake")},
                    follow_redirects=False)
    bid = r.headers["location"].rsplit("/", 1)[1]
    page = client.get(f"/import/{bid}").text
    assert "totaux identiques" in page
    import re
    rows = re.findall(r'name="row" value="(\d+)" checked', page)
    assert len(rows) == 8
    client.post(f"/import/{bid}/confirm", data={"row": rows})
    db.expire_all()
    joint = db.scalar(select(Account).where(Account.number == "11112222333"))
    card = db.scalar(select(Account).where(Account.kind == "carte"))
    from app.services import account_balance
    assert account_balance(db, joint) == 144950  # solde de départ repris du relevé
    assert account_balance(db, card) == 0  # paiements carte + règlement s'annulent
    boul = db.scalar(select(Transaction).where(Transaction.label.contains("Carrefour")))
    assert boul.account_id == card.id and boul.category.name == "Alimentation"
    settle = db.scalar(select(Transaction).where(Transaction.label.contains("Dépenses Carte")))
    assert settle.category.kind == "virement"
    # réimport du même relevé : rien de nouveau
    r = client.post("/import", data={"account_id": ""}, files=[("files", ("a.pdf", b"%PDF")), ("files", ("b.pdf", b"%PDF"))])
    db.expire_all()
    assert len(db.scalars(select(Transaction)).all()) == 9


def test_search_parse():
    p = parse_query("axa 42,50 >100 50-60 12/03/2026 03/2026 92")
    assert p["words"] == ["AXA"]
    assert (4250, 4250) in p["amount_ranges"] and (10001, None) in p["amount_ranges"] and (5000, 6000) in p["amount_ranges"]
    assert p["dates"] == [date(2026, 3, 12)] and p["months"] == ["2026-03"] and p["fuzzy_ints"] == [92]


def test_search_endpoint(client, db):
    d = date.today().strftime("%d/%m/%Y")
    content = f"Date;Libellé;Montant\n{d};PRLV TotalEnergies Électricité;-116,00\n{d};CB LIBRAIRIE;-42,50\n".encode()
    client.post("/import", data={"account_id": "1"}, files={"file": ("r.csv", content)}, follow_redirects=False)
    bid = db.scalar(select(func_max_batch()))
    client.post(f"/import/{bid}/confirm", data={"row": ["0", "1"]})
    assert "LIBRAIRIE" in client.get("/transactions?q=42,50").text
    assert "LIBRAIRIE" not in client.get("/transactions?q=>100").text
    assert "TotalEnergies" in client.get("/transactions?q=electricite").text  # sans accent
    assert "TotalEnergies" in client.get("/transactions?amin=100&amax=200").text
    assert "LIBRAIRIE" not in client.get("/transactions?amin=100").text


def func_max_batch():
    from sqlalchemy import func
    from app.models import ImportBatch
    return func.max(ImportBatch.id)


def test_profile_import(client, db):
    profile = {
        "rename_persons": {"Moi": "Jean"},
        "persons": [{"name": "Jean", "role": "moi"}, {"name": "Léa", "role": "enfant"}],
        "categories": [{"name": "Copropriété", "icon": "🏢", "kind": "depense", "budget": 300}],
        "accounts": [{"number": "999", "name": "Compte joint", "kind": "joint", "default_scope": "famille",
                      "default_person": "Jean"}],
        "rules": [{"pattern": "SYNDIC", "category": "Copropriété", "tags": "copro"},
                  {"pattern": "WERO VERS", "category": "Cadeaux", "max_amount": 50}],
        "loans": [{"name": "Prêt", "lender": "Banque", "match_pattern": "00001", "monthly": 900}],
        "insurances": [{"name": "Auto", "insurer": "AXA", "match_pattern": "CONTRAT42", "premium": 50}],
        "recurrences": [{"label": "Salaire", "amount": 3000, "next_date": "2026-11-01", "category": "Salaire"}],
    }
    r = client.post("/settings/profile", files={"file": ("p.json", json.dumps(profile).encode())}, follow_redirects=False)
    assert r.status_code == 303
    client.post("/settings/profile", files={"file": ("p.json", json.dumps(profile).encode())})  # idempotent
    db.expire_all()
    assert db.scalar(select(Category).where(Category.name == "Copropriété")).budget_cents == 30000
    assert len(db.scalars(select(Rule).where(Rule.pattern == "SYNDIC")).all()) == 1
    assert db.scalar(select(Rule).where(Rule.pattern == "WERO VERS")).max_cents == 5000
    acc = db.scalar(select(Account).where(Account.number == "999"))
    assert acc.default_scope == "famille" and acc.default_person_id
    for url in ["/loans", "/insurances", "/recurrences", "/forecast", "/settings"]:
        assert client.get(url).status_code == 200
    d = date.today().strftime("%d/%m/%Y")
    content = f"Date;Libellé;Montant\n{d};VIR WERO VERS AMI;-30,00\n{d};VIR WERO VERS NOTAIRE;-3000,00\n".encode()
    r = client.post("/import", data={"account_id": str(acc.id)}, files={"file": ("r.csv", content)}, follow_redirects=False)
    bid = r.headers["location"].rsplit("/", 1)[1]
    client.post(f"/import/{bid}/confirm", data={"row": ["0", "1"]})
    db.expire_all()
    small = db.scalar(select(Transaction).where(Transaction.label.contains("AMI")))
    big = db.scalar(select(Transaction).where(Transaction.label.contains("NOTAIRE")))
    assert small.category.name == "Cadeaux" and not big.categorized  # max_amount respecté
    assert small.person.name == "Jean" and small.scope == "famille"  # valeurs par défaut du compte
