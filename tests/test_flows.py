import io
import re
import zipfile
from datetime import date, timedelta

from sqlalchemy import select

from app.models import (
    Account, Category, InstallmentPlan, Insurance, Loan, Pending, Recurrence, Rule, Transaction,
)
from app.services import loan_status
from tests.test_parsing import OFX


def _csv(lines):
    return ("Date;Libellé;Montant\n" + "\n".join(lines) + "\n").encode()


def _import(client, content, name="releve.csv", select_all=True):
    r = client.post("/import", data={"account_id": "1"}, files={"file": (name, content)}, follow_redirects=False)
    assert r.status_code == 303, r.text
    url = r.headers["location"]
    bid = int(url.rsplit("/", 1)[1])
    page = client.get(url).text
    rows = re.findall(r'name="row" value="(\d+)" (checked)?', page)
    chosen = [i for i, c in rows if c or select_all]
    r = client.post(f"/import/{bid}/confirm", data={"row": chosen}, follow_redirects=False)
    assert r.status_code == 303
    return bid


def test_login_required():
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"
    assert c.post("/login", data={"password": "nope"}).status_code == 401
    assert c.get("/healthz").text == "ok"


def test_all_pages_render(client):
    for url in ["/", "/transactions", "/inbox", "/pending", "/pending/new", "/import", "/report", "/budget",
                "/forecast", "/loans", "/loans/new", "/insurances", "/insurances/new", "/installments",
                "/installments/new", "/recurrences", "/recurrences/new?label=X&amount=-9.99&pattern=X",
                "/jems", "/tags", "/attachments", "/settings", "/more", "/manual"]:
        r = client.get(url)
        assert r.status_code == 200, (url, r.text[:500])


def test_import_rules_and_dedup(client, db):
    d = date.today().strftime("%d/%m/%Y")
    content = _csv([f"{d};CB CARREFOUR MARKET;-54,20", f"{d};VIR SALAIRE;3200,00", f"{d};MAGASIN INCONNU;-10,00"])
    _import(client, content)
    txs = db.scalars(select(Transaction)).all()
    assert len(txs) == 3
    carrefour = next(t for t in txs if "CARREFOUR" in t.label)
    assert carrefour.category.name == "Alimentation" and carrefour.categorized
    # Réimport du même fichier : tout est détecté comme doublon et décoché
    _import(client, content, select_all=False)
    db.expire_all()
    assert len(db.scalars(select(Transaction)).all()) == 3


def test_ofx_import_and_balance_check(client, db):
    bid = _import(client, OFX.encode(), name="releve.ofx")
    page = client.get(f"/import/{bid}").text
    assert "Contrôle du solde" in page
    client.post(f"/import/{bid}/align-balance")
    page = client.get(f"/import/{bid}").text
    assert "Solde cohérent" in page
    db.expire_all()
    ins = db.scalars(select(Transaction).where(Transaction.import_key == "ref:A1")).one()
    assert ins.amount_cents == -4890


def test_pending_purchase_is_matched_on_import(client, db):
    today = date.today()
    cat = db.scalar(select(Category).where(Category.name == "Vêtements"))
    r = client.post("/pending", data={
        "amount": "79,99", "direction": "out", "label": "Chaussures Léo", "date": today.isoformat(),
        "category_id": str(cat.id), "person_id": "3", "scope": "famille", "tags": "rentrée, Léo",
    }, files={"files": ("ticket.jpg", b"\xff\xd8fakejpeg", "image/jpeg")}, follow_redirects=False)
    assert r.status_code == 303
    later = (today + timedelta(days=2)).strftime("%d/%m/%Y")
    _import(client, _csv([f"{later};CB DECATHLON 1234;-79,99"]))
    db.expire_all()
    tx = db.scalars(select(Transaction).where(Transaction.label.contains("DECATHLON"))).one()
    assert tx.category_id == cat.id and tx.person_id == 3 and tx.auto_source == "achat en attente"
    assert sorted(t.name for t in tx.tags) == ["leo", "rentree"]
    assert len(tx.attachments) == 1
    assert db.scalars(select(Pending)).one().status == "rapproche"
    # le justificatif est téléchargeable
    assert client.get(f"/attachments/{tx.attachments[0].id}").content == b"\xff\xd8fakejpeg"


def test_pending_created_after_import_matches_existing(client, db):
    today = date.today()
    _import(client, _csv([f"{today:%d/%m/%Y};CB FNAC PARIS;-120,00"]))
    client.post("/pending", data={"amount": "120", "label": "Cadeau", "date": today.isoformat(),
                                  "scope": "jems"})
    db.expire_all()
    tx = db.scalars(select(Transaction)).one()
    assert tx.scope == "jems" and tx.jems_status == "a_declarer"


def test_manual_categorisation_creates_rule_and_learns(client, db):
    d = date.today().strftime("%d/%m/%Y")
    _import(client, _csv([f"{d};CB BOULANGERIE DUPONT 12/10;-4,50", f"{d};CB BOULANGERIE DUPONT 11/10;-3,20"]))
    tx = db.scalars(select(Transaction)).first()
    cat = db.scalar(select(Category).where(Category.name == "Alimentation"))
    client.post(f"/transactions/{tx.id}", data={
        "category_id": str(cat.id), "person_id": "1", "scope": "famille", "tags": "pain",
        "make_rule": "1", "rule_pattern": "BOULANGERIE DUPONT", "apply_similar": "1"})
    db.expire_all()
    assert all(t.category_id == cat.id for t in db.scalars(select(Transaction)))
    assert db.scalar(select(Rule).where(Rule.pattern == "BOULANGERIE DUPONT")) is not None
    _import(client, _csv([f"{d};CB BOULANGERIE DUPONT 13/10;-2,10"]))
    db.expire_all()
    new = db.scalars(select(Transaction).where(Transaction.label.contains("13/10"))).one()
    assert new.category_id == cat.id and [t.name for t in new.tags] == ["pain"]


def test_tag_search(client, db):
    d = date.today().strftime("%d/%m/%Y")
    _import(client, _csv([f"{d};PRLV AXA ASSURANCE MINI;-35,00"]))
    tx = db.scalars(select(Transaction)).one()
    client.post(f"/transactions/{tx.id}", data={"scope": "famille", "tags": "AXA, assurance-mini"})
    page = client.get("/transactions?tag=axa").text
    assert "PRLV AXA ASSURANCE MINI" in page
    assert "axa" in client.get("/tags").text


def test_insurance_links_past_and_future(client, db):
    d = date.today()
    _import(client, _csv([f"{d:%d/%m/%Y};PRLV SEPA MAAF ASSURANCES;-62,00"]))
    r = client.post("/insurances", data={"name": "Auto", "insurer": "MAAF", "kind": "auto", "premium": "62",
                                         "frequency": "mensuel", "next_due": (d + timedelta(days=1)).isoformat()},
                    follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    ins = db.scalars(select(Insurance)).one()
    tx = db.scalars(select(Transaction)).one()
    assert tx.insurance_id == ins.id and tx.category.name == "Assurances"
    assert "maaf" in [t.name for t in tx.tags]
    assert "Auto (MAAF)" in client.get("/forecast").text


def test_loan_and_installments(client, db):
    start = date.today().replace(day=1) - timedelta(days=60)
    client.post("/loans", data={"name": "Voiture", "lender": "Cetelem", "kind": "auto", "principal": "12000",
                                "rate": "4,5", "monthly": "280", "insurance": "0",
                                "start_date": start.isoformat(), "duration_months": "48",
                                "match_pattern": "CETELEM"})
    loan = db.scalars(select(Loan)).one()
    st = loan_status(loan)
    assert st["paid_count"] in (2, 3) and 0 < st["remaining_cents"] < 1_200_000
    assert "Voiture" in client.get("/loans").text

    first = date.today()
    client.post("/installments", data={"label": "Canapé", "merchant": "Alma", "total": "600,01", "count": "3",
                                       "first_date": first.isoformat(), "scope": "famille"})
    plan = db.scalars(select(InstallmentPlan)).one()
    assert [i.amount_cents for i in plan.items] == [20001, 20000, 20000]
    _import(client, _csv([f"{first:%d/%m/%Y};PRLV ALMA;-200,01"]))
    db.expire_all()
    plan = db.scalars(select(InstallmentPlan)).one()
    assert plan.items[0].transaction_id is not None
    assert "Canapé 2/3" in client.get("/forecast?days=90").text


def test_recurrence_and_provisions(client, db):
    client.post("/recurrences", data={"label": "Taxe foncière", "amount": "1200", "direction": "out",
                                      "frequency": "annuel", "next_date": (date.today() + timedelta(days=200)).isoformat(),
                                      "is_provision": "1"})
    rec = db.scalars(select(Recurrence)).one()
    assert rec.amount_cents == -120000 and rec.is_provision
    page = client.get("/budget").text
    assert "Taxe foncière" in page and "100,00" in page


def test_report_and_jems_export(client, db):
    d = date.today().strftime("%d/%m/%Y")
    _import(client, _csv([f"{d};HOTEL IBIS LYON;-89,00", f"{d};VIR SALAIRE;2000,00"]))
    tx = db.scalars(select(Transaction).where(Transaction.label.contains("IBIS"))).one()
    client.post(f"/transactions/{tx.id}", data={"scope": "jems", "jems_status": "a_declarer"},
                files={"files": ("facture.pdf", b"%PDF-1.4 facture", "application/pdf")})
    page = client.get("/report").text
    assert "dépense(s) Jems à déclarer" in page
    r = client.get(f"/jems/export?month={date.today():%Y-%m}")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert "note_de_frais.csv" in names and any(n.startswith("justificatifs/") for n in names)
    assert "HOTEL IBIS" in z.read("note_de_frais.csv").decode("utf-8-sig")
    client.post("/jems/status", data={"ids": [str(tx.id)], "new_status": "remboursee"})
    db.expire_all()
    assert db.get(Transaction, tx.id).jems_status == "remboursee"


def test_undo_import_restores_pending(client, db):
    today = date.today()
    client.post("/pending", data={"amount": "15", "label": "Ciné", "date": today.isoformat()})
    bid = _import(client, _csv([f"{today:%d/%m/%Y};CB PATHE;-15,00"]))
    db.expire_all()
    assert db.scalars(select(Pending)).one().status == "rapproche"
    client.post(f"/import/{bid}/undo")
    db.expire_all()
    assert db.scalars(select(Transaction)).all() == []
    assert db.scalars(select(Pending)).one().status == "attente"


def test_backup(client):
    r = client.get("/settings/backup")
    assert "wazzzxpense.db" in zipfile.ZipFile(io.BytesIO(r.content)).namelist()


def test_settings_crud(client, db):
    client.post("/settings/accounts", data={"name": "Livret A", "initial_balance": "1000", "initial_date": "2026-01-01"})
    assert db.scalars(select(Account).where(Account.name == "Livret A")).one().initial_balance_cents == 100000
    cat = db.scalar(select(Category).where(Category.name == "Divers"))
    client.post("/settings/categories", data={"id": str(cat.id), "action": "delete"})
    db.expire_all()
    assert db.scalar(select(Category).where(Category.name == "Divers")) is None


def test_loan_from_known_remaining():
    from app.models import Loan
    loan = Loan(name="P", monthly_cents=50000, insurance_cents=0, rate=0, start_date=date(2026, 1, 5),
                duration_months=0, principal_cents=3000000, known_remaining_cents=1000000,
                known_remaining_at=date(2026, 10, 1))
    st = loan_status(loan, today=date(2026, 12, 10))  # 2 mensualités (5/10, 5/11, 5/12 -> 3) depuis
    assert st["estimated"] and st["remaining_cents"] == 1000000 - 3 * 50000
    assert st["remaining_count"] == 17 and st["end_date"] == date(2028, 5, 5)
    loan.rate = 3.0
    st = loan_status(loan, today=date(2026, 12, 10))
    assert st["remaining_count"] >= 17 and st["interest_cents"] > 0


def test_users_login_and_access(client, db):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.models import User
    anon = TestClient(app)
    assert anon.post("/login", data={"username": "inconnu", "password": "secret"}).status_code == 401
    assert anon.post("/login", data={"username": "warren", "password": "mauvais"}).status_code == 401
    assert anon.get("/transactions", follow_redirects=False).headers["location"] == "/login"
    # création d'un accès pour Laura
    client.post("/settings/users", data={"action": "create", "username": "Laura", "password": "court",
                                         "password2": "court"})
    assert db.scalar(select(User).where(User.username == "laura")) is None  # trop court
    client.post("/settings/users", data={"action": "create", "username": "Laura", "password": "tres-long-mdp",
                                         "password2": "tres-long-mdp"})
    laura = TestClient(app)
    assert laura.post("/login", data={"username": "laura", "password": "tres-long-mdp"},
                      follow_redirects=False).status_code == 303
    assert laura.get("/").status_code == 200
    # suppression : la session de Laura est immédiatement révoquée
    uid = db.scalar(select(User).where(User.username == "laura")).id
    client.post("/settings/users", data={"action": "delete", "id": str(uid)})
    assert laura.get("/", follow_redirects=False).headers["location"] == "/login"
    # changement de mot de passe
    client.post("/settings/users", data={"action": "password", "current": "secret", "password": "nouveau-mdp-123",
                                         "password2": "nouveau-mdp-123"})
    assert anon.post("/login", data={"username": "warren", "password": "nouveau-mdp-123"},
                     follow_redirects=False).status_code == 303


def test_fixed_charges_and_purchase_in_installments(client, db):
    today = date.today()
    client.post("/recurrences", data={"label": "Salaire", "amount": "3000", "direction": "in", "frequency": "mensuel",
                                      "next_date": today.isoformat(), "is_fixed": "1"})
    client.post("/recurrences", data={"label": "Netflix", "amount": "13,49", "direction": "out", "frequency": "mensuel",
                                      "next_date": today.isoformat(), "is_fixed": "1"})
    client.post("/recurrences", data={"label": "Taxe foncière", "amount": "1200", "direction": "out",
                                      "frequency": "annuel", "next_date": today.isoformat(), "is_fixed": "1"})
    client.post("/recurrences", data={"label": "Carte (variable)", "amount": "2000", "direction": "out",
                                      "frequency": "mensuel", "next_date": today.isoformat()})  # non fixe
    client.post("/insurances", data={"name": "Auto", "insurer": "AXA", "premium": "50", "frequency": "mensuel"})
    # achat en 3 fois saisi depuis « + Achat » ; la 1re échéance est déjà sur le compte, rangée par une règle
    _import(client, _csv([f"{today:%d/%m/%Y};PRLV AMAZON PAYMENTS;-100,01"]))
    r = client.post("/pending", data={"amount": "300,01", "direction": "out", "installments": "3", "label": "Télé",
                                      "keyword": "AMAZON", "date": today.isoformat()},
                    files={"files": ("facture.pdf", b"%PDF-1", "application/pdf")}, follow_redirects=False)
    assert r.headers["location"].startswith("/installments/")
    plan = db.scalars(select(InstallmentPlan)).one()
    assert [i.amount_cents for i in plan.items] == [10001, 10000, 10000] and plan.items[0].transaction_id
    assert db.scalars(select(Pending)).all() == []
    page = client.get("/fixed").text
    # 13,49 + 100 (1200/12) + 50 + 100 (prochaine échéance) = 263,49 ; la carte variable n'y est pas
    assert "263,49" in page.replace(" ", "")
    assert page.index("Carte (variable)") > page.index("Retirés des charges fixes")  # listée à part
    assert "2736,51" in page.replace(" ", "")  # reste = 3000 - 263,49
    home = client.get("/").text
    assert "Charges fixes" in home and "1 en cours" in home



def test_fixed_list_is_editable(client, db):
    from app.services import fixed_charges
    client.post("/fixed/new", data={"label": "Nounou", "amount": "600", "direction": "out", "frequency": "mensuel",
                                    "theme": "Famille & enfants", "account_id": "1"})
    client.post("/fixed/new", data={"label": "Apport Laura", "amount": "1000", "direction": "in",
                                    "frequency": "mensuel", "account_id": "1"})
    client.post("/insurances", data={"name": "AXA Tesla", "insurer": "AXA", "kind": "auto", "premium": "100,90"})
    ins = db.scalars(select(Insurance)).one()
    f = fixed_charges(db)
    assert {g["name"] for g in f["groups"]} == {"Famille & enfants", "Voiture & transport"}  # thème déduit
    prov = {p["account"]: p for p in f["provisions"]}
    assert prov["Compte courant"]["charges"] == 60000 and prov["Compte courant"]["to_fund"] == 0
    # modification depuis la liste : thème + compte
    client.post(f"/fixed/insurance/{ins.id}", data={"label": "AXA Tesla", "amount": "120", "frequency": "mensuel",
                                                   "theme": "Perso", "account_id": "1"})
    db.expire_all()
    f = fixed_charges(db, "theme")
    assert "Perso" in {g["name"] for g in f["groups"]} and f["total"] == 72000
    assert {p["account"]: p for p in f["provisions"]}["Compte courant"]["to_fund"] == 0  # 720 - 1000 couverts
    # retirer puis remettre
    client.post(f"/fixed/insurance/{ins.id}", data={"action": "exclude"})
    db.expire_all()
    assert fixed_charges(db)["total"] == 60000 and len(fixed_charges(db)["excluded"]) == 1
    client.post(f"/fixed/insurance/{ins.id}", data={"action": "include"})
    db.expire_all()
    assert fixed_charges(db)["total"] == 72000
    for view in ("theme", "person", "account", "category"):
        assert client.get(f"/fixed?view={view}").status_code == 200
    assert client.get(f"/fixed/insurance/{ins.id}").status_code == 200
