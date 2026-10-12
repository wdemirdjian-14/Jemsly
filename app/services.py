"""Logique métier : tags, rapprochement automatique, soldes, échéancier, crédits, bilans."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    FREQ_MONTHS, Account, Attachment, Category, InstallmentItem, InstallmentPlan, Insurance, Loan,
    Pending, Person, Recurrence, Rule, Tag, Transaction,
)
from .utils import (
    add_months, clean_tags, label_contains, merchant_key, month_bounds, norm, next_occurrence, shift_month,
)

DEFAULT_CATEGORIES = [
    ("Alimentation", "🛒", "depense", 60000), ("Restaurants", "🍽️", "depense", 15000),
    ("Logement", "🏠", "depense", None), ("Énergie & eau", "💡", "depense", None),
    ("Télécom & abonnements", "📱", "depense", None), ("Transport & carburant", "🚗", "depense", 25000),
    ("Santé", "🩺", "depense", None), ("Assurances", "🛡️", "depense", None),
    ("Crédits", "🏦", "depense", None), ("Enfants & école", "🎒", "depense", None),
    ("Vêtements", "👕", "depense", 10000), ("Loisirs & sorties", "🎉", "depense", 10000),
    ("Vacances", "🏖️", "depense", None), ("Maison & équipement", "🛠️", "depense", None),
    ("Impôts & taxes", "🧾", "depense", None), ("Frais bancaires", "💳", "depense", None),
    ("Cadeaux", "🎁", "depense", None), ("Frais pro Jems", "💼", "depense", None),
    ("Retraits espèces", "💶", "depense", None), ("Divers", "📦", "depense", None),
    ("Salaire", "💰", "revenu", None), ("Remboursement Jems", "↩️", "revenu", None),
    ("Aides & allocations", "🤝", "revenu", None), ("Autres revenus", "➕", "revenu", None),
    ("Virement interne / épargne", "🔁", "virement", None),
]
DEFAULT_RULES = [
    ("FRAIS|COMMISSION|AGIOS|COTISATION CARTE|INTERETS DEBITEURS", "debit", "Frais bancaires"),
    ("RETRAIT DAB|RETRAIT|RET DAB", "debit", "Retraits espèces"),
    ("CARREFOUR|LECLERC|AUCHAN|INTERMARCHE|LIDL|SUPER U|HYPER U|MONOPRIX|FRANPRIX|ALDI|CASINO|NETTO|GRAND FRAIS|PICARD", "debit", "Alimentation"),
    ("EDF|ENGIE|TOTALENERGIES ELEC|VEOLIA|SUEZ|SAUR|ENI ", "debit", "Énergie & eau"),
    ("TOTAL ACCESS|TOTALENERGIES|ESSO|SHELL|BP |AVIA|SNCF|RATP|PEAGE|VINCI|SANEF|APRR|UBER|BLABLACAR|PARKING|INDIGO",
     "debit", "Transport & carburant"),
    ("ORANGE|SFR|BOUYGUES|FREE MOBILE|FREE TELECOM|SOSH|RED BY|NETFLIX|SPOTIFY|DEEZER|DISNEY|CANAL|AMAZON PRIME|APPLE.COM|GOOGLE", "debit", "Télécom & abonnements"),
    ("PHARMACIE|PHARMA |DOCTEUR|DR |CPAM|MEDECIN|LABO|OPTIC|DENTIST", "debit", "Santé"),
    ("DGFIP|IMPOT|TRESOR PUBLIC|AMENDE", "debit", "Impôts & taxes"),
    ("CAF |CAF-|ALLOCATIONS", "credit", "Aides & allocations"),
]


def seed(db: Session):
    if db.scalar(select(func.count(Category.id))):
        return
    for name, icon, kind, budget in DEFAULT_CATEGORIES:
        db.add(Category(name=name, icon=icon, kind=kind, budget_cents=budget))
    for name, role in [("Moi", "moi"), ("Ma femme", "conjoint"), ("Enfants", "enfant"), ("Famille", "famille")]:
        db.add(Person(name=name, role=role))
    db.flush()
    cats = {c.name: c.id for c in db.scalars(select(Category))}
    for pattern, direction, cat in DEFAULT_RULES:
        db.add(Rule(pattern=pattern, direction=direction, category_id=cats[cat], priority=200))
    db.add(Account(name="Compte courant", bank="", initial_date=date(2000, 1, 1)))
    db.commit()


# ---------------------------------------------------------------- tags

def get_or_create_tags(db: Session, raw) -> list[Tag]:
    names = clean_tags(raw) if isinstance(raw, str) else clean_tags(",".join(raw))
    tags = []
    for n in names:
        t = db.scalar(select(Tag).where(Tag.name == n))
        if not t:
            t = Tag(name=n)
            db.add(t)
            db.flush()
        tags.append(t)
    return tags


def add_tags(db: Session, obj, raw):
    for t in get_or_create_tags(db, raw):
        if t not in obj.tags:
            obj.tags.append(t)


def category_by_name(db: Session, name: str, kind="depense", icon="•") -> Category:
    c = db.scalar(select(Category).where(Category.name == name))
    if not c:
        c = Category(name=name, kind=kind, icon=icon)
        db.add(c)
        db.flush()
    return c


# ---------------------------------------------------------------- rapprochement automatique

def _set_scope(tx: Transaction, scope: str | None):
    if scope:
        tx.scope = scope
        if scope == "jems" and not tx.jems_status and tx.amount_cents < 0:
            tx.jems_status = "a_declarer"


def find_pending_for(db: Session, tx: Transaction) -> Pending | None:
    cands = db.scalars(select(Pending).where(
        Pending.status == "attente",
        Pending.amount_cents.between(tx.amount_cents - 1, tx.amount_cents + 1),
        Pending.date >= tx.date - timedelta(days=45),
        Pending.date <= tx.date + timedelta(days=3),
    )).all()
    if not cands:
        return None
    kw_ok = [p for p in cands if p.keyword and label_contains(tx.label, p.keyword)]
    pool = kw_ok or [p for p in cands if not p.keyword]
    return min(pool, key=lambda p: abs((tx.date - p.date).days)) if pool else None


def link_pending(db: Session, p: Pending, tx: Transaction):
    if p.category_id:
        tx.category_id = p.category_id
    tx.person_id = p.person_id or tx.person_id
    _set_scope(tx, p.scope)
    if p.note:
        tx.note = (tx.note + "\n" + p.note).strip() if tx.note else p.note
    for t in p.tags:
        if t not in tx.tags:
            tx.tags.append(t)
    for a in p.attachments:
        a.transaction_id = tx.id
    p.status = "rapproche"
    p.transaction_id = tx.id
    tx.categorized = bool(tx.category_id)
    tx.auto_source = "achat en attente"


def match_pending_to_existing(db: Session, p: Pending) -> Transaction | None:
    """Quand on saisit un achat déjà présent sur le relevé, on le rapproche tout de suite."""
    linked = select(Pending.transaction_id).where(Pending.transaction_id.is_not(None))
    cands = db.scalars(select(Transaction).where(
        Transaction.amount_cents.between(p.amount_cents - 1, p.amount_cents + 1),
        Transaction.date >= p.date - timedelta(days=3),
        Transaction.date <= p.date + timedelta(days=45),
        Transaction.id.not_in(linked),
    )).all()
    if p.keyword:
        cands = [t for t in cands if label_contains(t.label, p.keyword)]
    if not cands:
        return None
    tx = min(cands, key=lambda t: (t.categorized, abs((t.date - p.date).days)))
    link_pending(db, p, tx)
    return tx


def _find_installment(db: Session, tx: Transaction) -> InstallmentItem | None:
    if tx.amount_cents >= 0:
        return None
    items = db.scalars(select(InstallmentItem).where(
        InstallmentItem.transaction_id.is_(None),
        InstallmentItem.amount_cents.between(-tx.amount_cents - 2, -tx.amount_cents + 2),
        InstallmentItem.due_date >= tx.date - timedelta(days=10),
        InstallmentItem.due_date <= tx.date + timedelta(days=7),
    )).all()
    items = [i for i in items if not i.plan.match_pattern or label_contains(tx.label, i.plan.match_pattern)]
    return min(items, key=lambda i: abs((i.due_date - tx.date).days)) if items else None


def _advance(obj, attr: str, tx_date: date, frequency: str):
    d = getattr(obj, attr)
    if d is None:
        return
    while d <= tx_date + timedelta(days=12):
        d = next_occurrence(d, frequency)
    setattr(obj, attr, d)


def apply_rules(db: Session, tx: Transaction) -> bool:
    for rule in db.scalars(select(Rule).order_by(Rule.priority, Rule.id)):
        if rule.direction == "debit" and tx.amount_cents > 0:
            continue
        if rule.direction == "credit" and tx.amount_cents < 0:
            continue
        if rule.max_cents and abs(tx.amount_cents) > rule.max_cents:
            continue
        if label_contains(tx.label, rule.pattern):
            if rule.category_id:
                tx.category_id = rule.category_id
            if rule.person_id:
                tx.person_id = rule.person_id
            _set_scope(tx, rule.scope)
            if rule.tags:
                add_tags(db, tx, rule.tags)
            rule.hits += 1
            tx.categorized = bool(tx.category_id)
            tx.auto_source = f"règle « {rule.pattern[:30]} »"
            return True
    return False


def apply_learning(db: Session, tx: Transaction) -> bool:
    key = merchant_key(tx.label)
    if len(key) < 3:
        return False
    prev = db.scalars(select(Transaction).where(
        Transaction.categorized.is_(True), Transaction.id != tx.id,
        Transaction.auto_source.in_(["", "manuel", "achat en attente"]),
        (Transaction.amount_cents < 0) if tx.amount_cents < 0 else (Transaction.amount_cents >= 0),
    ).order_by(Transaction.date.desc()).limit(3000)).all()
    for p in prev:
        if merchant_key(p.label) == key:
            tx.category_id = p.category_id
            tx.person_id = p.person_id
            _set_scope(tx, p.scope)
            for t in p.tags:
                if t not in tx.tags:
                    tx.tags.append(t)
            tx.categorized = bool(tx.category_id)
            tx.auto_source = "apprentissage"
            return True
    return False


def apply_auto(db: Session, tx: Transaction) -> str:
    """Catégorise une opération fraîchement importée (tx doit avoir un id)."""
    acc = db.get(Account, tx.account_id)
    if acc:
        if acc.default_scope and tx.scope in ("", "famille"):
            _set_scope(tx, acc.default_scope)
        if acc.default_person_id and not tx.person_id:
            tx.person_id = acc.default_person_id
    p = find_pending_for(db, tx)
    if p:
        link_pending(db, p, tx)
        return tx.auto_source

    item = _find_installment(db, tx)
    if item:
        plan = item.plan
        item.transaction_id = tx.id
        tx.category_id = plan.category_id
        tx.person_id = plan.person_id
        _set_scope(tx, plan.scope)
        add_tags(db, tx, ["plusieurs-fois", plan.merchant])
        n = plan.items.index(item) + 1
        tx.note = f"Échéance {n}/{plan.count} – {plan.label}"
        tx.categorized = bool(tx.category_id)
        tx.auto_source = "paiement en plusieurs fois"
        return tx.auto_source

    for loan in db.scalars(select(Loan).where(Loan.active.is_(True), Loan.match_pattern != "")):
        if tx.amount_cents < 0 and label_contains(tx.label, loan.match_pattern):
            tx.loan_id = loan.id
            tx.category_id = category_by_name(db, "Crédits", icon="🏦").id
            add_tags(db, tx, ["credit", loan.lender])
            tx.categorized = True
            tx.auto_source = "crédit"
            return tx.auto_source

    for ins in db.scalars(select(Insurance).where(Insurance.active.is_(True), Insurance.match_pattern != "")):
        if label_contains(tx.label, ins.match_pattern):
            tx.insurance_id = ins.id
            if tx.amount_cents < 0:
                tx.category_id = category_by_name(db, "Assurances", icon="🛡️").id
                _advance(ins, "next_due", tx.date, ins.frequency)
            add_tags(db, tx, ["assurance", ins.insurer] + ([ins.kind] if ins.kind != "autre" else []))
            tx.person_id = ins.person_id or tx.person_id
            tx.categorized = bool(tx.category_id)
            tx.auto_source = "assurance"
            return tx.auto_source

    for rec in db.scalars(select(Recurrence).where(Recurrence.active.is_(True), Recurrence.match_pattern != "")):
        same_sign = (rec.amount_cents < 0) == (tx.amount_cents < 0)
        if same_sign and label_contains(tx.label, rec.match_pattern):
            tx.recurrence_id = rec.id
            tx.category_id = rec.category_id or tx.category_id
            tx.person_id = rec.person_id or tx.person_id
            _set_scope(tx, rec.scope)
            _advance(rec, "next_date", tx.date, rec.frequency)
            tx.categorized = bool(tx.category_id)
            tx.auto_source = "récurrence"
            if tx.categorized:
                return tx.auto_source

    if apply_rules(db, tx):
        return tx.auto_source
    if apply_learning(db, tx):
        return tx.auto_source
    return ""


def similar_uncategorized(db: Session, tx: Transaction) -> list[Transaction]:
    key = merchant_key(tx.label)
    if len(key) < 3:
        return []
    rows = db.scalars(select(Transaction).where(
        Transaction.categorized.is_(False), Transaction.id != tx.id)).all()
    return [t for t in rows if merchant_key(t.label) == key]


# ---------------------------------------------------------------- soldes

def account_balance(db: Session, acc: Account, at: date | None = None) -> int:
    q = select(func.coalesce(func.sum(Transaction.amount_cents), 0)).where(
        Transaction.account_id == acc.id, Transaction.date >= acc.initial_date)
    if at:
        q = q.where(Transaction.date <= at)
    return acc.initial_balance_cents + db.scalar(q)


def pending_total(db: Session) -> int:
    return db.scalar(select(func.coalesce(func.sum(Pending.amount_cents), 0)).where(Pending.status == "attente"))


# ---------------------------------------------------------------- crédits

def loan_status(loan: Loan, today: date | None = None) -> dict:
    today = today or date.today()
    if loan.known_remaining_cents is not None and loan.known_remaining_at and loan.monthly_cents:
        return _loan_from_remaining(loan, today)
    if not loan.duration_months:  # durée inconnue : on suit les mensualités sans projeter la fin
        return {"paid_count": None, "count": None, "remaining_count": None, "remaining_cents": None,
                "end_date": None, "next_date": None, "progress": 0, "interest_cents": 0,
                "remaining_payments_cents": None}
    n = max(loan.duration_months, 1)
    r = (loan.rate or 0) / 100 / 12
    m = max(loan.monthly_cents - loan.insurance_cents, 0)
    k = 0
    d = loan.start_date
    while k < n and d <= today:
        k += 1
        d = add_months(loan.start_date, k)
    p = loan.principal_cents
    if r > 0:
        remaining = p * (1 + r) ** k - m * (((1 + r) ** k - 1) / r)
    else:
        remaining = p - m * k
    remaining = max(round(remaining), 0) if k < n else 0
    if not p:
        remaining = None  # montant emprunté non renseigné
    total_cost = m * n - p if p else 0
    return {
        "paid_count": k, "count": n, "remaining_count": n - k,
        "remaining_cents": remaining, "end_date": add_months(loan.start_date, n - 1),
        "next_date": d if k < n else None, "progress": round(100 * k / n),
        "interest_cents": max(total_cost, 0),
        "remaining_payments_cents": (n - k) * loan.monthly_cents,
    }


def _loan_from_remaining(loan: Loan, today: date) -> dict:
    """Projection à partir du « restant dû » connu à une date : mensualités passées depuis, mois restants,
    date de fin. Sans taux, l'estimation ignore les intérêts (fin un peu optimiste)."""
    import math

    r = (loan.rate or 0) / 100 / 12
    m = max(loan.monthly_cents - loan.insurance_cents, 1)
    day = loan.start_date.day if loan.start_date else 5
    d = loan.known_remaining_at
    first = date(d.year, d.month, min(day, 28))
    if first <= d:
        first = add_months(first, 1)
    k, nxt = 0, first
    while nxt <= today:
        k += 1
        nxt = add_months(first, k)
    rem = loan.known_remaining_cents
    rem = rem * (1 + r) ** k - m * (((1 + r) ** k - 1) / r) if r > 0 else rem - m * k
    rem = max(round(rem), 0)
    if rem == 0:
        left = 0
    elif r > 0 and r * rem < m:
        left = math.ceil(-math.log(1 - r * rem / m) / math.log(1 + r))
    else:
        left = math.ceil(rem / m)
    p = loan.principal_cents
    return {
        "paid_count": None, "count": None, "remaining_count": left, "remaining_cents": rem,
        "end_date": add_months(nxt, left - 1) if left else None, "next_date": nxt if left else None,
        "progress": round(100 * (1 - rem / p)) if p else 0,
        "interest_cents": max(left * m - rem, 0) if r > 0 else 0,
        "remaining_payments_cents": left * loan.monthly_cents, "estimated": True, "rate_known": r > 0,
    }


# ---------------------------------------------------------------- échéancier / prévisions

@dataclass
class Expected:
    date: date
    label: str
    amount_cents: int
    kind: str
    url: str
    late: bool = False


def upcoming(db: Session, start: date, end: date) -> list[Expected]:
    out: list[Expected] = []
    today = date.today()
    late_from = today - timedelta(days=15)
    for rec in db.scalars(select(Recurrence).where(Recurrence.active.is_(True))):
        d = rec.next_date
        while d <= end and (not rec.end_date or d <= rec.end_date):
            if d >= start or (rec.match_pattern and d >= late_from and start <= today):
                out.append(Expected(d, rec.label, rec.amount_cents,
                                    "provision" if rec.is_provision else "récurrence",
                                    f"/recurrences/{rec.id}", late=d < today))
            d = next_occurrence(d, rec.frequency)
    for ins in db.scalars(select(Insurance).where(Insurance.active.is_(True), Insurance.next_due.is_not(None))):
        d = ins.next_due
        while d <= end:
            if d >= start or (ins.match_pattern and d >= late_from and start <= today):
                out.append(Expected(d, f"{ins.name} ({ins.insurer})", -ins.premium_cents, "assurance",
                                    f"/insurances/{ins.id}", late=d < today))
            d = next_occurrence(d, ins.frequency)
    for loan in db.scalars(select(Loan).where(Loan.active.is_(True))):
        st = loan_status(loan)
        for k in range(loan.duration_months or 1200):
            d = add_months(loan.start_date, k)
            if st.get("end_date") and d > st["end_date"]:
                break
            if d > end:
                break
            if d >= start:
                out.append(Expected(d, f"{loan.name} ({loan.lender})", -loan.monthly_cents, "crédit",
                                    f"/loans/{loan.id}"))
    items = db.scalars(select(InstallmentItem).where(
        InstallmentItem.transaction_id.is_(None), InstallmentItem.due_date <= end,
        InstallmentItem.due_date >= min(start, late_from))).all()
    for it in items:
        n = it.plan.items.index(it) + 1
        out.append(Expected(it.due_date, f"{it.plan.label} {n}/{it.plan.count}", -it.amount_cents,
                            "plusieurs fois", f"/installments/{it.plan_id}", late=it.due_date < today))
    out.sort(key=lambda e: e.date)
    return out


def provisions(db: Session) -> list[dict]:
    """Dépenses non mensuelles : combien mettre de côté chaque mois."""
    out = []
    for rec in db.scalars(select(Recurrence).where(Recurrence.active.is_(True), Recurrence.amount_cents < 0)):
        months = FREQ_MONTHS.get(rec.frequency)
        if rec.is_provision or (months and months > 1):
            months = months or 12
            out.append(_prov(rec.label, -rec.amount_cents, months, rec.next_date, f"/recurrences/{rec.id}"))
    for ins in db.scalars(select(Insurance).where(Insurance.active.is_(True))):
        months = FREQ_MONTHS.get(ins.frequency, 1)
        if months > 1 and ins.next_due:
            out.append(_prov(f"{ins.name} ({ins.insurer})", ins.premium_cents, months, ins.next_due,
                             f"/insurances/{ins.id}"))
    out.sort(key=lambda p: p["due"])
    return out


def _prov(label, amount, months, due, url):
    today = date.today()
    monthly = round(amount / months)
    months_left = max((due.year - today.year) * 12 + due.month - today.month, 0)
    should_have = max(amount - monthly * months_left, 0)
    return {"label": label, "amount": amount, "months": months, "monthly": monthly, "due": due,
            "should_have": min(should_have, amount), "url": url}


# ---------------------------------------------------------------- bilans

def month_transactions(db: Session, month: str) -> list[Transaction]:
    start, end = month_bounds(month)
    return db.scalars(select(Transaction).where(Transaction.date.between(start, end))
                      .order_by(Transaction.date)).all()


def month_stats(db: Session, month: str) -> dict:
    txs = month_transactions(db, month)
    is_transfer = lambda t: t.category is not None and t.category.kind == "virement"  # noqa: E731
    # Le foyer : hors virements internes et hors Jems (avancé puis remboursé par l'employeur, suivi à part)
    flows = [t for t in txs if not is_transfer(t) and t.scope != "jems"]
    income = sum(t.amount_cents for t in flows if t.amount_cents > 0)
    expenses = -sum(t.amount_cents for t in flows if t.amount_cents < 0)
    by_cat, by_person, by_scope, by_merchant = defaultdict(int), defaultdict(int), defaultdict(int), defaultdict(int)
    for t in flows:
        if t.amount_cents < 0:
            by_cat[t.category_id] += -t.amount_cents
            by_person[t.person.name if t.person else "Non attribué"] += -t.amount_cents
            by_scope[t.scope] += -t.amount_cents
            by_merchant[merchant_key(t.label) or t.label[:30]] += -t.amount_cents
    cats = {c.id: c for c in db.scalars(select(Category))}
    cat_rows = []
    for cid, total in sorted(by_cat.items(), key=lambda kv: -kv[1]):
        c = cats.get(cid)
        cat_rows.append({"id": cid, "name": c.name if c else "Non catégorisé", "icon": c.icon if c else "❓",
                         "total": total, "budget": c.budget_cents if c else None})
    for c in cats.values():
        if c.budget_cents and c.id not in by_cat and c.kind == "depense":
            cat_rows.append({"id": c.id, "name": c.name, "icon": c.icon, "total": 0, "budget": c.budget_cents})
    return {
        "month": month, "count": len(txs), "income": income, "expenses": expenses, "net": income - expenses,
        "savings_rate": round(100 * (income - expenses) / income) if income else None,
        "categories": cat_rows, "persons": sorted(by_person.items(), key=lambda kv: -kv[1]),
        "scopes": sorted(by_scope.items(), key=lambda kv: -kv[1]),
        "merchants": sorted(by_merchant.items(), key=lambda kv: -kv[1])[:10],
        "uncategorized": sum(1 for t in txs if not t.categorized),
        "jems_total": -sum(t.amount_cents for t in txs if t.scope == "jems" and t.amount_cents < 0),
        "jems_reimbursed": sum(t.amount_cents for t in txs if t.scope == "jems" and t.amount_cents > 0),
        "jems_todo": sum(1 for t in txs if t.scope == "jems" and t.jems_status == "a_declarer"),
        "transactions": txs,
    }


def inspect_month(db: Session, month: str, stats: dict | None = None) -> list[dict]:
    """Contrôles automatiques du mois -> liste d'alertes {level, title, detail, url}."""
    stats = stats or month_stats(db, month)
    alerts = []
    prev = [month_stats(db, shift_month(month, -i)) for i in (1, 2, 3)]
    prev = [ps for ps in prev if _complete(ps)]  # on ne compare qu'à des mois entièrement importés
    prev_by_cat = defaultdict(list)
    for ps in prev:
        for c in ps["categories"]:
            prev_by_cat[c["id"]].append(c["total"])

    if stats["income"] and stats["net"] < 0:
        alerts.append({"level": "bad", "title": "Mois déficitaire",
                       "detail": f"Les dépenses dépassent les revenus de {-stats['net'] / 100:.2f} €."})
    for c in stats["categories"]:
        if c["budget"] and c["total"] > c["budget"]:
            alerts.append({"level": "bad", "title": f"Budget dépassé : {c['icon']} {c['name']}",
                           "detail": f"{c['total'] / 100:.2f} € pour un budget de {c['budget'] / 100:.2f} €",
                           "url": f"/transactions?month={month}&category={c['id'] or 0}"})
        hist = prev_by_cat.get(c["id"], [])
        avg = sum(hist) / len(prev) if len(prev) >= 2 else 0
        if avg > 0 and c["total"] > 1.5 * avg and c["total"] - avg > 5000:
            alerts.append({"level": "warn", "title": f"Hausse inhabituelle : {c['icon']} {c['name']}",
                           "detail": f"{c['total'] / 100:.2f} € contre {avg / 100:.2f} € en moyenne sur {len(prev)} mois",
                           "url": f"/transactions?month={month}&category={c['id'] or 0}"})
    txs = stats["transactions"]
    fees = [t for t in txs if t.amount_cents < 0 and label_contains(t.label, "FRAIS|COMMISSION|AGIOS|INTERETS DEBITEURS")]
    if fees:
        alerts.append({"level": "warn", "title": "Frais bancaires",
                       "detail": f"{len(fees)} opération(s), {-sum(t.amount_cents for t in fees) / 100:.2f} € au total.",
                       "url": f"/transactions?month={month}&q=FRAIS"})
    seen = defaultdict(list)
    for t in txs:
        seen[(t.date, t.amount_cents, merchant_key(t.label))].append(t)
    for (d, amt, key), group in seen.items():
        # ignore les petites sommes (cafés…) et les prélèvements à références différentes (2 contrats)
        if len(group) > 1 and amt <= -500 and not _distinct_refs(group):
            alerts.append({"level": "warn", "title": "Doublon possible",
                           "detail": f"{len(group)}× {-amt / 100:.2f} € chez {key or group[0].label} le {d:%d/%m}",
                           "url": f"/transactions?month={month}&q={key}"})
    _, month_end = month_bounds(month)
    at = min(month_end, date.today())
    for acc in db.scalars(select(Account).where(Account.archived.is_(False), Account.kind != "carte")):
        bal = account_balance(db, acc, at)
        if bal < 0:
            yearly = -bal * 0.166  # taux débiteur usuel ~16,6 %
            alerts.append({"level": "bad", "title": f"{acc.name} à découvert ({bal / 100:.2f} €)",
                           "detail": f"Un découvert permanent de ce niveau coûte ≈ {yearly / 100:.0f} € d'agios par an "
                                     f"(+ frais d'incidents). Un apport ponctuel de {-bal / 100:.0f} € l'éteint.",
                           "url": f"/transactions?account={acc.id}"})
    if stats["uncategorized"]:
        alerts.append({"level": "info", "title": f"{stats['uncategorized']} opération(s) à catégoriser",
                       "detail": "Plus le tri est complet, plus le bilan est juste.", "url": "/inbox"})
    if stats["jems_todo"]:
        alerts.append({"level": "info", "title": f"{stats['jems_todo']} dépense(s) Jems à déclarer",
                       "detail": "Pensez à faire la note de frais.", "url": f"/jems?month={month}"})
    old = db.scalars(select(Pending).where(Pending.status == "attente",
                                           Pending.date < date.today() - timedelta(days=40))).all()
    if old:
        alerts.append({"level": "warn", "title": f"{len(old)} achat(s) en attente depuis plus de 40 jours",
                       "detail": "Jamais vus sur le relevé : montant erroné, remboursement ou achat annulé ?",
                       "url": "/pending"})
    alerts.extend(detect_subscriptions(db, month))
    return alerts


def _complete(stats: dict) -> bool:
    """Le mois a-t-il des opérations du début à la fin (relevés importés sur tout le mois) ?"""
    days = [t.date.day for t in stats["transactions"]]
    if len(days) < 10:
        return False
    _, end = month_bounds(stats["month"])
    return min(days) <= 7 and max(days) >= end.day - 6


def _distinct_refs(group) -> bool:
    """Deux prélèvements identiques le même jour mais avec des références différentes (2 contrats) ≠ doublon."""
    return len({t.label for t in group}) == len(group)


def detect_subscriptions(db: Session, month: str) -> list[dict]:
    """Repère les prélèvements qui reviennent chaque mois sans être suivis comme récurrence."""
    keys_per_month = []
    for i in range(3):
        m = shift_month(month, -i)
        start, end = month_bounds(m)
        rows = db.scalars(select(Transaction).where(
            Transaction.date.between(start, end), Transaction.amount_cents < 0,
            Transaction.recurrence_id.is_(None), Transaction.loan_id.is_(None),
            Transaction.insurance_id.is_(None))).all()
        keys_per_month.append({merchant_key(t.label): t.amount_cents for t in rows if merchant_key(t.label)})
    tracked = [norm(r.match_pattern) for r in db.scalars(select(Recurrence)) if r.match_pattern]
    out = []
    for key, amt in keys_per_month[0].items():
        if all(key in k for k in keys_per_month[1:]) and not any(p and p in key for p in tracked):
            amounts = [k[key] for k in keys_per_month]
            if max(amounts) - min(amounts) <= max(100, abs(amt) * 3 // 100):
                out.append({"level": "info", "title": f"Récurrence détectée : {key}",
                            "detail": f"≈ {-amt / 100:.2f} € chaque mois depuis 3 mois. L'ajouter à l'échéancier ?",
                            "url": f"/recurrences/new?label={key}&amount={amt / 100:.2f}&pattern={key}"})
    return out


def save_attachment(db: Session, upload, data: bytes, **links) -> Attachment:
    import mimetypes
    import uuid
    from .config import UPLOAD_DIR

    today = date.today()
    sub = UPLOAD_DIR / f"{today:%Y}" / f"{today:%m}"
    sub.mkdir(parents=True, exist_ok=True)
    ext = "".join(c for c in (upload.filename.rsplit(".", 1)[-1] if "." in upload.filename else "bin") if c.isalnum())[:8]
    stored = f"{today:%Y}/{today:%m}/{uuid.uuid4().hex}.{ext.lower()}"
    (UPLOAD_DIR / stored).write_bytes(data)
    mime = upload.content_type or mimetypes.guess_type(upload.filename)[0] or "application/octet-stream"
    a = Attachment(filename=upload.filename[:200], stored_name=stored, mime=mime, size=len(data), **links)
    db.add(a)
    return a


def recategorize_all(db: Session) -> int:
    """Relance le rangement automatique sur tout ce qui n'a pas été rangé à la main."""
    keep = ("manuel", "achat en attente", "règlement carte", "paiement en plusieurs fois")
    n = 0
    for tx in db.scalars(select(Transaction).where(Transaction.auto_source.not_in(keep))):
        tx.category_id = tx.person_id = tx.loan_id = tx.insurance_id = tx.recurrence_id = None
        tx.scope, tx.jems_status, tx.categorized, tx.auto_source = "famille", None, False, ""
        tx.tags = []
        db.flush()
        apply_auto(db, tx)
        n += tx.categorized
    return n


# ---------------------------------------------------------------- charges fixes

PER_MONTH = {"hebdo": 52 / 12, "mensuel": 1, "trimestriel": 1 / 3, "semestriel": 1 / 6, "annuel": 1 / 12}
CATEGORY_THEMES = {
    "Logement": "Logement", "Copropriété & charges": "Logement", "Énergie & eau": "Logement",
    "Maison & équipement": "Logement", "Impôts & taxes": "Impôts", "Enfants & école": "Famille & enfants",
    "Télécom & abonnements": "Abonnements & télécom", "Transport & carburant": "Voiture & transport",
    "Santé": "Santé & prévoyance", "Crédits": "Crédits conso", "Frais bancaires": "Autre",
}


def theme_of(kind: str, obj) -> str:
    if obj.theme:
        return obj.theme
    if kind == "loan":
        return {"immo": "Logement", "travaux": "Logement", "auto": "Voiture & transport"}.get(obj.kind, "Crédits conso")
    if kind == "insurance":
        k = (obj.kind or "").lower()
        if k in ("auto", "moto"):
            return "Voiture & transport"
        if k in ("habitation", "emprunteur"):
            return "Logement"
        if k in ("mutuelle", "prévoyance", "vie"):
            return "Santé & prévoyance"
        return "Famille & enfants" if k == "scolaire" else "Autre"
    if kind == "installment":
        return "Achats en plusieurs fois"
    cat = obj.category.name if obj.category else ""
    if cat in CATEGORY_THEMES:
        return CATEGORY_THEMES[cat]
    return "Perso" if obj.scope == "perso" else "Autre"


def _paying_account(db: Session, kind: str, obj) -> int | None:
    if getattr(obj, "account_id", None):
        return obj.account_id
    col = {"loan": Transaction.loan_id, "insurance": Transaction.insurance_id,
           "recurrence": Transaction.recurrence_id}.get(kind)
    if col is not None:
        return db.scalar(select(Transaction.account_id).where(col == obj.id).order_by(Transaction.date.desc()).limit(1))
    if kind == "installment":
        ids = [i.transaction_id for i in obj.items if i.transaction_id]
        if ids:
            return db.scalar(select(Transaction.account_id).where(Transaction.id.in_(ids)).limit(1))
    return None


def fixed_items(db: Session) -> tuple[list[dict], list[dict], list[dict]]:
    """-> (charges fixes, revenus fixes, éléments exclus) ; montants en équivalent mensuel."""
    accounts = {a.id: a.name for a in db.scalars(select(Account))}
    persons = {p.id: p.name for p in db.scalars(select(Person))}
    out, income, excluded = [], [], []

    def item(kind, obj, label, monthly, url, sub="", category="", person_id=None):
        acc = _paying_account(db, kind, obj)
        return {"kind": kind, "id": obj.id, "label": label, "monthly": round(monthly), "url": url,
                "edit_url": f"/fixed/{kind}/{obj.id}", "sub": sub, "theme": theme_of(kind, obj),
                "category": category or "Autre", "account_id": acc, "account": accounts.get(acc, "Compte non défini"),
                "person": persons.get(person_id, "Famille")}

    for loan in db.scalars(select(Loan).where(Loan.active.is_(True))):
        st = loan_status(loan)
        if st["remaining_count"] == 0 or not loan.monthly_cents:
            continue
        sub = f"fin ≈ {st['end_date']:%m/%Y}" if st.get("end_date") else "durée à compléter"
        it = item("loan", loan, loan.name, loan.monthly_cents, f"/loans/{loan.id}", sub, "Crédits")
        (out if loan.is_fixed else excluded).append(it)
    for ins in db.scalars(select(Insurance).where(Insurance.active.is_(True))):
        months = FREQ_MONTHS.get(ins.frequency, 1)
        sub = "" if months == 1 else f"{ins.premium_cents / 100:.2f} € tous les {months} mois"
        it = item("insurance", ins, ins.name, ins.premium_cents / months, f"/insurances/{ins.id}", sub,
                  "Assurances", ins.person_id)
        (out if ins.is_fixed else excluded).append(it)
    for rec in db.scalars(select(Recurrence).where(Recurrence.active.is_(True))):
        if rec.end_date and rec.end_date < date.today():
            continue
        if rec.category and rec.category.kind == "virement":
            continue
        monthly = abs(rec.amount_cents) * PER_MONTH.get(rec.frequency, 1)
        sub = "" if rec.frequency == "mensuel" else f"{abs(rec.amount_cents) / 100:.2f} € ({rec.frequency})"
        it = item("recurrence", rec, rec.label, monthly, f"/recurrences/{rec.id}", sub,
                  rec.category.name if rec.category else "", rec.person_id)
        if not rec.is_fixed:
            excluded.append(it)
        elif rec.amount_cents > 0:
            income.append(it)
        else:
            out.append(it)
    for plan in db.scalars(select(InstallmentPlan)):
        left = [i for i in plan.items if i.transaction_id is None]
        if left:
            out.append(item("installment", plan, plan.label, left[0].amount_cents, f"/installments/{plan.id}",
                            f"{len(left)} échéance(s) restante(s), fin {left[-1].due_date:%m/%Y}",
                            plan.category.name if plan.category else "", plan.person_id))
    return out, income, excluded


VIEWS = {"theme": "Thématique", "person": "Pour qui", "account": "Compte", "category": "Catégorie"}
THEME_ICONS = {"Logement": "🏠", "Voiture & transport": "🚗", "Famille & enfants": "👨‍👩‍👧", "Perso": "🙋",
               "Santé & prévoyance": "🩺", "Impôts": "🧾", "Abonnements & télécom": "📱", "Crédits conso": "🏦",
               "Achats en plusieurs fois": "🧩", "Autre": "📌"}


def fixed_charges(db: Session, view: str = "theme") -> dict:
    """Vue « budget fixe » : ce qui sort (et rentre) chaque mois, regroupé selon `view`,
    et ce qu'il faut provisionner sur chaque compte."""
    out, income, excluded = fixed_items(db)
    key = view if view in VIEWS else "theme"
    groups: dict[str, dict] = {}
    for it in out:
        name = it[key]
        g = groups.setdefault(name, {"name": name, "icon": THEME_ICONS.get(name, "") if key == "theme" else "",
                                     "items": [], "total": 0})
        g["items"].append(it)
        g["total"] += it["monthly"]
    ordered = sorted(groups.values(), key=lambda g: -g["total"])
    for g in ordered:
        g["items"].sort(key=lambda i: -i["monthly"])

    # provisionnement : ce que chaque compte doit recevoir chaque mois pour couvrir ses charges fixes
    prov: dict[str, dict] = {}
    for it in out:
        p = prov.setdefault(it["account"], {"account": it["account"], "charges": 0, "income": 0, "incomes": []})
        p["charges"] += it["monthly"]
    for it in income:
        p = prov.setdefault(it["account"], {"account": it["account"], "charges": 0, "income": 0, "incomes": []})
        p["income"] += it["monthly"]
        p["incomes"].append(it)
    for p in prov.values():
        p["to_fund"] = max(p["charges"] - p["income"], 0)
    total_out = sum(it["monthly"] for it in out)
    total_in = sum(it["monthly"] for it in income)
    plans = []
    for plan in db.scalars(select(InstallmentPlan)):
        left = [i for i in plan.items if i.transaction_id is None]
        if left:
            plans.append({"plan": plan, "left_count": len(left), "left_cents": sum(i.amount_cents for i in left)})
    return {"groups": ordered, "total": total_out, "income": income, "total_in": total_in,
            "left": total_in - total_out, "excluded": excluded, "view": key,
            "provisions": sorted(prov.values(), key=lambda p: -p["charges"]), "plans": plans}


def build_installments(db: Session, plan: InstallmentPlan, total: int, count: int, first: date):
    """(Re)génère les échéances non payées d'un paiement fractionné."""
    for it in list(plan.items):
        if it.transaction_id is None:
            plan.items.remove(it)
    paid = len(plan.items)
    base, rest = divmod(total, count)
    for k in range(paid, count):
        # le 1er versement absorbe les centimes d'arrondi, comme Alma / Oney / Klarna
        plan.items.append(InstallmentItem(due_date=add_months(first, k), amount_cents=base + (rest if k == 0 else 0)))
    db.flush()


def link_installments(db: Session, plan: InstallmentPlan) -> int:
    """Rattache les échéances déjà prélevées (même montant, date proche), y compris si une règle les avait rangées
    ailleurs. Les opérations rangées à la main ne sont pas modifiées."""
    taken = set(db.scalars(select(InstallmentItem.transaction_id).where(InstallmentItem.transaction_id.is_not(None))))
    n = 0
    for item in plan.items:
        if item.transaction_id:
            continue
        cands = db.scalars(select(Transaction).where(
            Transaction.amount_cents.between(-item.amount_cents - 2, -item.amount_cents + 2),
            Transaction.date.between(item.due_date - timedelta(days=10), item.due_date + timedelta(days=7)),
            Transaction.auto_source.not_in(["manuel", "règlement carte"]),
        )).all()
        cands = [t for t in cands if t.id not in taken and
                 (not plan.match_pattern or label_contains(t.label, plan.match_pattern))]
        if not cands:
            continue
        tx = min(cands, key=lambda t: abs((t.date - item.due_date).days))
        item.transaction_id = tx.id
        taken.add(tx.id)
        tx.category_id = plan.category_id or tx.category_id
        tx.person_id = plan.person_id or tx.person_id
        _set_scope(tx, plan.scope)
        add_tags(db, tx, ["plusieurs-fois", plan.merchant])
        tx.note = f"Échéance {plan.items.index(item) + 1}/{plan.count} – {plan.label}"
        tx.categorized = bool(tx.category_id)
        tx.auto_source = "paiement en plusieurs fois"
        n += 1
    return n
