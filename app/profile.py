"""Import d'un « profil » JSON : personnes, comptes, catégories, règles, crédits, assurances, récurrences.

Le profil est propre à chaque foyer et ne fait pas partie du code. Chaque élément est mis à jour s'il existe
déjà (clé : nom, numéro ou motif), sinon créé : importer deux fois le même profil ne crée pas de doublons.
Montants en euros, dates AAAA-MM-JJ.
"""
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Account, Category, Insurance, Loan, Person, Recurrence, Rule
from .services import category_by_name, recategorize_all
from .utils import parse_amount


def _cents(v):
    return None if v in (None, "") else parse_amount(v)


def _date(v):
    return date.fromisoformat(v) if v else None


def apply_profile(db: Session, data: dict) -> dict:
    stats = {}

    # personnes (renommage possible des personnes par défaut)
    for old, new in (data.get("rename_persons") or {}).items():
        p = db.scalar(select(Person).where(Person.name == old))
        if p and not db.scalar(select(Person).where(Person.name == new)):
            p.name = new
    db.flush()
    persons = {}
    for item in data.get("persons", []):
        p = db.scalar(select(Person).where(Person.name == item["name"])) or Person(name=item["name"])
        p.role = item.get("role", p.role or "moi")
        db.add(p)
        persons[p.name] = p
    db.flush()
    persons = {p.name: p for p in db.scalars(select(Person))}
    stats["personnes"] = len(data.get("persons", []))

    for item in data.get("categories", []):
        c = db.scalar(select(Category).where(Category.name == item["name"])) or Category(name=item["name"])
        c.icon = item.get("icon", c.icon or "•")
        c.kind = item.get("kind", c.kind or "depense")
        if "budget" in item:
            c.budget_cents = _cents(item["budget"])
        db.add(c)
    db.flush()
    stats["catégories"] = len(data.get("categories", []))

    def cat(name):
        return category_by_name(db, name).id if name else None

    def person(name):
        return persons[name].id if name in persons else None

    def account(number):
        a = db.scalar(select(Account).where(Account.number == number)) if number else None
        return a.id if a else None

    for item in data.get("accounts", []):
        a = db.scalar(select(Account).where(Account.number == item["number"])) if item.get("number") else None
        a = a or db.scalar(select(Account).where(Account.name == item["name"])) or Account(initial_date=date(2000, 1, 1))
        a.name, a.number = item["name"], item.get("number", a.number or "")
        a.bank = item.get("bank", a.bank or "")
        a.kind = item.get("kind", a.kind or "courant")
        a.default_scope = item.get("default_scope", a.default_scope or "")
        a.default_person_id = person(item.get("default_person"))
        db.add(a)
    # le compte d'exemple créé à l'installation disparaît s'il est resté vide
    if data.get("accounts"):
        demo = db.scalar(select(Account).where(Account.name == "Compte courant", Account.number == ""))
        if demo and not demo.archived:
            from .models import Transaction
            if not db.scalar(select(Transaction.id).where(Transaction.account_id == demo.id).limit(1)):
                demo.archived = True
    db.flush()
    stats["comptes"] = len(data.get("accounts", []))

    for item in data.get("rules", []):
        r = db.scalar(select(Rule).where(Rule.pattern == item["pattern"])) or Rule(pattern=item["pattern"])
        r.direction = item.get("direction", "tous")
        r.category_id = cat(item.get("category"))
        r.person_id = person(item.get("person"))
        r.scope = item.get("scope") or None
        r.tags = item.get("tags", "")
        r.priority = item.get("priority", 80)
        r.max_cents = abs(_cents(item["max_amount"])) if item.get("max_amount") else None
        db.add(r)
    stats["règles"] = len(data.get("rules", []))

    for item in data.get("loans", []):
        l = db.scalar(select(Loan).where(Loan.match_pattern == item["match_pattern"])) or Loan(name=item["name"])
        l.name, l.lender, l.kind = item["name"], item.get("lender", ""), item.get("kind", "conso")
        l.match_pattern = item["match_pattern"]
        l.monthly_cents = abs(_cents(item.get("monthly")) or 0)
        l.insurance_cents = abs(_cents(item.get("insurance")) or 0)
        l.principal_cents = abs(_cents(item.get("principal")) or 0)
        l.rate = float(item.get("rate") or 0)
        l.duration_months = int(item.get("duration_months") or 0)
        l.start_date = _date(item.get("start_date")) or date.today()
        l.notes = item.get("notes", l.notes or "")
        l.known_remaining_cents = abs(_cents(item["remaining"])) if item.get("remaining") is not None else None
        l.known_remaining_at = _date(item.get("remaining_at"))
        l.active = item.get("active", True)
        l.theme = item.get("theme", l.theme or "")
        l.is_fixed = item.get("is_fixed", True)
        if item.get("account"):
            l.account_id = account(item["account"])
        db.add(l)
    stats["crédits"] = len(data.get("loans", []))

    for item in data.get("insurances", []):
        i = db.scalar(select(Insurance).where(Insurance.match_pattern == item["match_pattern"])) \
            or Insurance(name=item["name"])
        i.name, i.insurer, i.kind = item["name"], item.get("insurer", ""), item.get("kind", "autre")
        i.match_pattern = item["match_pattern"]
        i.contract_number = item.get("contract_number", "")
        i.premium_cents = abs(_cents(item.get("premium")) or 0)
        i.frequency = item.get("frequency", "mensuel")
        i.next_due = _date(item.get("next_due"))
        i.person_id = person(item.get("person"))
        i.notes = item.get("notes", i.notes or "")
        i.theme = item.get("theme", i.theme or "")
        i.is_fixed = item.get("is_fixed", True)
        if item.get("account"):
            i.account_id = account(item["account"])
        db.add(i)
    stats["assurances"] = len(data.get("insurances", []))

    for item in data.get("recurrences", []):
        r = db.scalar(select(Recurrence).where(Recurrence.label == item["label"])) \
            or Recurrence(label=item["label"], amount_cents=0, next_date=date.today())
        r.amount_cents = _cents(item["amount"])
        r.frequency = item.get("frequency", "mensuel")
        r.next_date = _date(item.get("next_date")) or date.today()
        r.match_pattern = item.get("match_pattern", "")
        r.category_id = cat(item.get("category"))
        r.person_id = person(item.get("person"))
        r.scope = item.get("scope", "famille")
        r.is_provision = bool(item.get("is_provision"))
        r.is_fixed = item.get("is_fixed", True)
        r.theme = item.get("theme", r.theme or "")
        if item.get("account"):
            r.account_id = account(item["account"])
        db.add(r)
    stats["récurrences"] = len(data.get("recurrences", []))

    db.flush()
    stats["opérations rangées"] = recategorize_all(db)
    return stats
