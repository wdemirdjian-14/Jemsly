"""Crédits, assurances, paiements en plusieurs fois, récurrences & provisions, échéancier."""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    Attachment, InstallmentItem, InstallmentPlan, Insurance, Loan, Recurrence, Transaction,
)
from ..services import (
    account_balance, apply_auto, build_installments, link_installments, loan_status, pending_total, provisions,
    upcoming,
)
from ..templating import redirect, render
from ..utils import label_contains
from .common import fcents, fdate, fint, lookups, save_uploads

router = APIRouter()

INSURANCE_KINDS = ["auto", "moto", "habitation", "mutuelle", "prévoyance", "emprunteur", "scolaire",
                   "vie", "animaux", "téléphone", "autre"]
LOAN_KINDS = {"immo": "Immobilier", "conso": "Consommation", "auto": "Auto / LOA / LLD",
              "travaux": "Travaux", "renouvelable": "Renouvelable", "etudiant": "Étudiant", "autre": "Autre"}


def _404():
    raise HTTPException(404, "Introuvable")


def _relink(db: Session, pattern: str, attr: str, obj_id: int):
    """Rattache les opérations passées correspondant au motif (sans écraser une catégorie manuelle)."""
    n = 0
    if not pattern:
        return 0
    for tx in db.scalars(select(Transaction).where(getattr(Transaction, attr).is_(None))):
        if not label_contains(tx.label, pattern):
            continue
        if tx.auto_source == "manuel":
            setattr(tx, attr, obj_id)
        else:
            apply_auto(db, tx)
        n += getattr(tx, attr) == obj_id
    return n


# ---------------------------------------------------------------- échéancier

@router.get("/forecast")
def forecast(request: Request, db: Session = Depends(get_db)):
    days = fint(request.query_params.get("days")) or 60
    today = date.today()
    items = upcoming(db, today - timedelta(days=15), today + timedelta(days=days))
    accounts = lookups(db)["accounts"]
    balance = sum(account_balance(db, a) for a in accounts) + pending_total(db)
    running, rows = balance, []
    for e in items:
        if e.date >= today or e.late:
            running += e.amount_cents
        rows.append((e, running))
    low = min((r for _, r in rows), default=balance)
    return render(request, db, "forecast.html", rows=rows, balance=balance, days=days, low=low,
                  provisions=provisions(db))


# ---------------------------------------------------------------- crédits

@router.get("/loans")
def loans(request: Request, db: Session = Depends(get_db)):
    rows = [(l, loan_status(l)) for l in db.scalars(select(Loan).order_by(Loan.active.desc(), Loan.name))]
    total_remaining = sum(s["remaining_cents"] or 0 for l, s in rows if l.active)
    monthly = sum(l.monthly_cents for l, s in rows if l.active and (s["remaining_count"] is None or s["remaining_count"] > 0))
    return render(request, db, "loans.html", rows=rows, total_remaining=total_remaining, monthly=monthly,
                  kinds=LOAN_KINDS)


@router.get("/loans/{lid}")
@router.get("/loans/new")
def loan_detail(request: Request, lid: int | None = None, db: Session = Depends(get_db)):
    loan = db.get(Loan, lid) or _404() if lid else None
    ctx = {}
    if loan:
        ctx["status"] = loan_status(loan)
        ctx["txs"] = db.scalars(select(Transaction).where(Transaction.loan_id == loan.id)
                                .order_by(Transaction.date.desc())).all()
        ctx["docs"] = db.scalars(select(Attachment).where(Attachment.loan_id == loan.id)).all()
    return render(request, db, "loan.html", loan=loan, kinds=LOAN_KINDS, **ctx, **lookups(db))


@router.post("/loans")
@router.post("/loans/{lid}")
async def loan_save(request: Request, lid: int | None = None, db: Session = Depends(get_db)):
    f = await request.form()
    loan = db.get(Loan, lid) or _404() if lid else Loan()
    if f.get("action") == "delete" and lid:
        db.delete(loan)
        db.commit()
        return redirect(request, "/loans", "Crédit supprimé.")
    loan.name = f.get("name", "").strip() or "Crédit"
    loan.lender = f.get("lender", "").strip()
    loan.kind = f.get("kind", "conso")
    loan.principal_cents = abs(fcents(f.get("principal"), 0))
    loan.rate = float((f.get("rate") or "0").replace(",", "."))
    loan.monthly_cents = abs(fcents(f.get("monthly"), 0))
    loan.insurance_cents = abs(fcents(f.get("insurance"), 0))
    loan.start_date = fdate(f.get("start_date"), date.today())
    loan.duration_months = fint(f.get("duration_months")) or 0
    loan.known_remaining_cents = abs(fcents(f.get("known_remaining"))) if fcents(f.get("known_remaining")) is not None else None
    loan.known_remaining_at = fdate(f.get("known_remaining_at"))
    loan.match_pattern = f.get("match_pattern", "").strip()
    loan.notes = f.get("notes", "").strip()
    loan.active = bool(f.get("active")) or not lid
    db.add(loan)
    db.flush()
    await save_uploads(db, f, loan_id=loan.id)
    n = _relink(db, loan.match_pattern, "loan_id", loan.id)
    db.commit()
    return redirect(request, f"/loans/{loan.id}", f"Crédit enregistré ({n} mensualité(s) retrouvée(s)).")


# ---------------------------------------------------------------- assurances

@router.get("/insurances")
def insurances(request: Request, db: Session = Depends(get_db)):
    rows = db.scalars(select(Insurance).order_by(Insurance.active.desc(), Insurance.kind)).all()
    yearly = sum(i.premium_cents * {"mensuel": 12, "trimestriel": 4, "semestriel": 2, "annuel": 1}.get(i.frequency, 12)
                 for i in rows if i.active)
    return render(request, db, "insurances.html", rows=rows, yearly=yearly)


@router.get("/insurances/{iid}")
@router.get("/insurances/new")
def insurance_detail(request: Request, iid: int | None = None, db: Session = Depends(get_db)):
    ins = db.get(Insurance, iid) or _404() if iid else None
    ctx = {}
    if ins:
        txs = db.scalars(select(Transaction).where(Transaction.insurance_id == ins.id)
                         .order_by(Transaction.date.desc())).all()
        ctx = {"txs": txs, "docs": db.scalars(select(Attachment).where(Attachment.insurance_id == ins.id)).all(),
               "paid_12m": -sum(t.amount_cents for t in txs if t.date >= date.today() - timedelta(days=365))}
    return render(request, db, "insurance.html", ins=ins, kinds=INSURANCE_KINDS, **ctx, **lookups(db))


@router.post("/insurances")
@router.post("/insurances/{iid}")
async def insurance_save(request: Request, iid: int | None = None, db: Session = Depends(get_db)):
    f = await request.form()
    ins = db.get(Insurance, iid) or _404() if iid else Insurance()
    if f.get("action") == "delete" and iid:
        db.delete(ins)
        db.commit()
        return redirect(request, "/insurances", "Contrat supprimé.")
    ins.name = f.get("name", "").strip() or "Assurance"
    ins.insurer = f.get("insurer", "").strip()
    ins.kind = f.get("kind", "autre")
    ins.contract_number = f.get("contract_number", "").strip()
    ins.premium_cents = abs(fcents(f.get("premium"), 0))
    ins.frequency = f.get("frequency", "mensuel")
    ins.next_due = fdate(f.get("next_due"))
    ins.renewal_date = fdate(f.get("renewal_date"))
    ins.person_id = fint(f.get("person_id"))
    ins.match_pattern = f.get("match_pattern", "").strip() or ins.insurer
    ins.notes = f.get("notes", "").strip()
    ins.active = bool(f.get("active")) or not iid
    db.add(ins)
    db.flush()
    await save_uploads(db, f, insurance_id=ins.id)
    n = _relink(db, ins.match_pattern, "insurance_id", ins.id)
    db.commit()
    return redirect(request, f"/insurances/{ins.id}", f"Contrat enregistré ({n} paiement(s) retrouvé(s)).")


# ---------------------------------------------------------------- paiements en plusieurs fois

@router.get("/installments")
def installments(request: Request, db: Session = Depends(get_db)):
    plans = db.scalars(select(InstallmentPlan).order_by(InstallmentPlan.first_date.desc())).all()
    remaining = sum(i.amount_cents for p in plans for i in p.items if i.transaction_id is None)
    return render(request, db, "installments.html", plans=plans, remaining=remaining)


@router.get("/installments/{pid}")
@router.get("/installments/new")
def installment_detail(request: Request, pid: int | None = None, db: Session = Depends(get_db)):
    plan = db.get(InstallmentPlan, pid) or _404() if pid else None
    docs = db.scalars(select(Attachment).where(Attachment.installment_id == pid)).all() if plan else []
    return render(request, db, "installment.html", plan=plan, docs=docs, **lookups(db))


@router.post("/installments")
@router.post("/installments/{pid}")
async def installment_save(request: Request, pid: int | None = None, db: Session = Depends(get_db)):
    f = await request.form()
    plan = db.get(InstallmentPlan, pid) or _404() if pid else None
    if plan and f.get("action") == "delete":
        db.delete(plan)
        db.commit()
        return redirect(request, "/installments", "Paiement supprimé.")
    total = abs(fcents(f.get("total"), 0))
    count = max(fint(f.get("count")) or 3, 1)
    first = fdate(f.get("first_date"), date.today())
    if plan is None:
        plan = InstallmentPlan(total_cents=total, count=count, first_date=first, label="")
        db.add(plan)
    regenerate = not plan.items or (plan.total_cents, plan.count, plan.first_date) != (total, count, first)
    plan.label = f.get("label", "").strip() or "Achat en plusieurs fois"
    plan.merchant = f.get("merchant", "").strip()
    plan.total_cents, plan.count, plan.first_date = total, count, first
    plan.match_pattern = f.get("match_pattern", "").strip()
    plan.category_id = fint(f.get("category_id"))
    plan.person_id = fint(f.get("person_id"))
    plan.scope = f.get("scope") or "famille"
    plan.notes = f.get("notes", "").strip()
    db.flush()
    if regenerate:
        build_installments(db, plan, total, count, first)
    n = link_installments(db, plan)
    await save_uploads(db, f, installment_id=plan.id)
    db.commit()
    return redirect(request, f"/installments/{plan.id}", f"Échéancier enregistré ({n} échéance(s) déjà payée(s)).")


@router.post("/installments/items/{item_id}/toggle")
def installment_item_toggle(item_id: int, request: Request, db: Session = Depends(get_db)):
    """Marquer une échéance comme payée sans opération rattachée (ou l'inverse)."""
    it = db.get(InstallmentItem, item_id) or _404()
    if it.transaction_id:
        it.transaction_id = None
    else:
        tx = db.scalar(select(Transaction).where(Transaction.amount_cents == -it.amount_cents,
                                                 Transaction.date.between(it.due_date - timedelta(days=10),
                                                                          it.due_date + timedelta(days=10))))
        if tx:
            it.transaction_id = tx.id
    db.commit()
    return redirect(request, f"/installments/{it.plan_id}",
                    None if it.transaction_id else "Aucune opération correspondante trouvée sur le compte.")


# ---------------------------------------------------------------- récurrences & provisions

@router.get("/recurrences")
def recurrences(request: Request, db: Session = Depends(get_db)):
    rows = db.scalars(select(Recurrence).order_by(Recurrence.active.desc(), Recurrence.next_date)).all()
    per_month = {"mensuel": 1, "trimestriel": 1 / 3, "semestriel": 1 / 6, "annuel": 1 / 12, "hebdo": 52 / 12}
    monthly_in = sum(r.amount_cents * per_month[r.frequency] for r in rows if r.active and r.amount_cents > 0)
    monthly_out = -sum(r.amount_cents * per_month[r.frequency] for r in rows if r.active and r.amount_cents < 0)
    return render(request, db, "recurrences.html", rows=rows, monthly_in=round(monthly_in),
                  monthly_out=round(monthly_out))


@router.get("/recurrences/{rid}")
@router.get("/recurrences/new")
def recurrence_detail(request: Request, rid: int | None = None, db: Session = Depends(get_db)):
    rec = db.get(Recurrence, rid) or _404() if rid else None
    txs = db.scalars(select(Transaction).where(Transaction.recurrence_id == rid)
                     .order_by(Transaction.date.desc()).limit(24)).all() if rec else []
    return render(request, db, "recurrence.html", rec=rec, txs=txs, q=request.query_params, **lookups(db))


@router.post("/recurrences")
@router.post("/recurrences/{rid}")
async def recurrence_save(request: Request, rid: int | None = None, db: Session = Depends(get_db)):
    f = await request.form()
    rec = db.get(Recurrence, rid) or _404() if rid else None
    if rec and f.get("action") == "delete":
        db.delete(rec)
        db.commit()
        return redirect(request, "/recurrences", "Récurrence supprimée.")
    cents = abs(fcents(f.get("amount"), 0))
    cents = cents if f.get("direction") == "in" else -cents
    if rec is None:
        rec = Recurrence(label="", amount_cents=cents, next_date=date.today())
        db.add(rec)
    rec.label = f.get("label", "").strip() or "Récurrence"
    rec.amount_cents = cents
    rec.frequency = f.get("frequency", "mensuel")
    rec.next_date = fdate(f.get("next_date"), date.today())
    rec.end_date = fdate(f.get("end_date"))
    rec.category_id = fint(f.get("category_id"))
    rec.person_id = fint(f.get("person_id"))
    rec.scope = f.get("scope") or "famille"
    rec.match_pattern = f.get("match_pattern", "").strip()
    rec.is_provision = bool(f.get("is_provision"))
    rec.is_fixed = bool(f.get("is_fixed"))
    rec.active = bool(f.get("active")) or not rid
    db.flush()
    n = _relink(db, rec.match_pattern, "recurrence_id", rec.id)
    db.commit()
    return redirect(request, "/recurrences", f"Récurrence enregistrée ({n} opération(s) rattachée(s)).")
