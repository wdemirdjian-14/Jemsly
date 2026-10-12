"""Accueil, opérations, boîte « à trier », achats en attente, tags, pièces jointes."""
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import UPLOAD_DIR
from ..db import get_db
from ..models import Account, Attachment, Pending, Rule, Tag, Transaction, transaction_tags
from ..services import (
    account_balance, add_tags, apply_auto, inspect_month, link_pending, match_pending_to_existing, month_stats,
    pending_total, similar_uncategorized, upcoming,
)
from ..search import SORTS, apply_search
from ..templating import redirect, render
from ..utils import current_month, merchant_key, month_bounds
from .common import fcents, fdate, fint, lookups, save_uploads

router = APIRouter()


# ---------------------------------------------------------------- accueil

@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    month = current_month()
    start, _ = month_bounds(month)
    if not db.scalar(select(Transaction.id).where(Transaction.date >= start).limit(1)):
        last = db.scalar(select(func.max(Transaction.date)))  # relevés pas encore importés : dernier mois connu
        if last:
            month = last.strftime("%Y-%m")
    accounts = db.scalars(select(Account).where(Account.archived.is_(False))).all()
    balances = [(a, account_balance(db, a)) for a in accounts]
    total = sum(b for _, b in balances)
    pend = pending_total(db)
    today = date.today()
    _, month_end = month_bounds(current_month())
    to_month_end = upcoming(db, today, month_end)
    next30 = upcoming(db, today, today + timedelta(days=30))
    stats = month_stats(db, month)
    alerts = [a for a in inspect_month(db, month, stats) if a["level"] != "info"][:5]
    budgets = [c for c in stats["categories"] if c["budget"]]
    budgets.sort(key=lambda c: -(c["total"] / c["budget"]))
    return render(request, db, "dashboard.html", month=month, balances=balances, total=total, pending=pend,
                  available=total + pend, end_of_month=total + pend + sum(e.amount_cents for e in to_month_end),
                  upcoming=next30[:8], stats=stats, alerts=alerts, budgets=budgets[:6])


# ---------------------------------------------------------------- opérations

@router.get("/transactions")
def transactions(request: Request, db: Session = Depends(get_db)):
    p = request.query_params
    q = select(Transaction)
    month = p.get("month", "")
    if month:
        start, end = month_bounds(month)
        q = q.where(Transaction.date.between(start, end))
    q = apply_search(q, p.get("q", ""), amin=_abs_cents(p.get("amin")), amax=_abs_cents(p.get("amax")),
                     dfrom=fdate(p.get("from")), dto=fdate(p.get("to")))
    cat = p.get("category")
    if cat == "0":
        q = q.where(Transaction.category_id.is_(None))
    elif fint(cat):
        q = q.where(Transaction.category_id == int(cat))
    for field, col in (("person", Transaction.person_id), ("account", Transaction.account_id),
                       ("loan", Transaction.loan_id), ("insurance", Transaction.insurance_id)):
        if fint(p.get(field)):
            q = q.where(col == int(p[field]))
    if p.get("scope"):
        q = q.where(Transaction.scope == p["scope"])
    if p.get("tag"):
        q = q.join(transaction_tags).join(Tag).where(Tag.name == p["tag"])
    if p.get("dir") == "out":
        q = q.where(Transaction.amount_cents < 0)
    elif p.get("dir") == "in":
        q = q.where(Transaction.amount_cents > 0)
    rows = db.scalars(q.order_by(*SORTS.get(p.get("sort", ""), SORTS[""]))).all()
    out = -sum(t.amount_cents for t in rows if t.amount_cents < 0)
    inc = sum(t.amount_cents for t in rows if t.amount_cents > 0)
    limit = fint(p.get("limit")) or 200
    filters = {k: v for k, v in p.items() if v and k != "limit"}
    return render(request, db, "transactions.html", rows=rows[:limit], total_count=len(rows), out=out, inc=inc,
                  filters=filters, limit=limit, month=month, **lookups(db))


def _abs_cents(v):
    c = fcents(v)
    return abs(c) if c is not None else None


@router.get("/transactions/{tx_id}")
def transaction_detail(tx_id: int, request: Request, db: Session = Depends(get_db)):
    tx = db.get(Transaction, tx_id) or _404()
    pend = db.scalar(select(Pending).where(Pending.transaction_id == tx.id))
    candidates = []
    if not pend:
        candidates = db.scalars(select(Pending).where(Pending.status == "attente")
                                .order_by(func.abs(Pending.amount_cents - tx.amount_cents)).limit(5)).all()
    return render(request, db, "transaction.html", tx=tx, pend=pend, candidates=candidates,
                  similar=similar_uncategorized(db, tx), suggested=merchant_key(tx.label),
                  back=request.query_params.get("back", "/transactions"), **lookups(db))


@router.post("/transactions/{tx_id}")
async def transaction_update(tx_id: int, request: Request, db: Session = Depends(get_db)):
    tx = db.get(Transaction, tx_id) or _404()
    f = await request.form()
    tx.category_id = fint(f.get("category_id"))
    tx.person_id = fint(f.get("person_id"))
    tx.scope = f.get("scope") or "famille"
    tx.jems_status = (f.get("jems_status") or "a_declarer") if tx.scope == "jems" else None
    if "note" in f:
        tx.note = f.get("note", "").strip()
    tx.tags = []
    add_tags(db, tx, f.get("tags", ""))
    tx.categorized = tx.category_id is not None
    tx.auto_source = "manuel"
    await save_uploads(db, f, transaction_id=tx.id)
    msg = "Opération enregistrée."
    if f.get("make_rule") and f.get("rule_pattern", "").strip() and tx.category_id:
        db.add(Rule(pattern=f["rule_pattern"].strip(), category_id=tx.category_id, person_id=tx.person_id,
                    scope=tx.scope, tags=",".join(t.name for t in tx.tags),
                    direction="debit" if tx.amount_cents < 0 else "credit"))
        msg += " Règle créée."
    if f.get("apply_similar"):
        n = 0
        for other in similar_uncategorized(db, tx):
            other.category_id, other.person_id, other.scope = tx.category_id, tx.person_id, tx.scope
            other.jems_status = tx.jems_status
            other.tags = list(tx.tags)
            other.categorized = tx.categorized
            other.auto_source = "manuel"
            n += 1
        if n:
            msg += f" {n} opération(s) similaire(s) rangée(s)."
    db.commit()
    return redirect(request, f.get("back") or "/transactions", msg)


@router.post("/transactions/{tx_id}/link-pending")
async def transaction_link_pending(tx_id: int, request: Request, db: Session = Depends(get_db)):
    tx = db.get(Transaction, tx_id) or _404()
    f = await request.form()
    p = db.get(Pending, fint(f.get("pending_id")) or 0) or _404()
    link_pending(db, p, tx)
    db.commit()
    return redirect(request, f"/transactions/{tx.id}", "Achat en attente rapproché.")


@router.post("/transactions/{tx_id}/delete")
def transaction_delete(tx_id: int, request: Request, db: Session = Depends(get_db)):
    tx = db.get(Transaction, tx_id) or _404()
    for p in db.scalars(select(Pending).where(Pending.transaction_id == tx.id)):
        p.status, p.transaction_id = "attente", None
    db.delete(tx)
    db.commit()
    return redirect(request, "/transactions", "Opération supprimée.")


@router.get("/manual")
def manual_form(request: Request, db: Session = Depends(get_db)):
    return render(request, db, "manual.html", **lookups(db))


@router.post("/manual")
async def manual_create(request: Request, db: Session = Depends(get_db)):
    """Saisie manuelle d'une opération (compte sans export, espèces…)."""
    f = await request.form()
    cents = fcents(f.get("amount"))
    if cents is None:
        return redirect(request, "/manual", "Montant invalide.")
    if f.get("direction") != "in":
        cents = -abs(cents)
    tx = Transaction(account_id=int(f["account_id"]), date=fdate(f.get("date"), date.today()),
                     label=f.get("label", "").strip() or "Saisie manuelle", amount_cents=cents,
                     import_key=f"manual:{uuid.uuid4().hex}")
    db.add(tx)
    db.flush()
    apply_auto(db, tx)
    db.commit()
    return redirect(request, f"/transactions/{tx.id}", "Opération ajoutée.")


# ---------------------------------------------------------------- à trier

@router.get("/inbox")
def inbox(request: Request, db: Session = Depends(get_db)):
    rows = db.scalars(select(Transaction).where(Transaction.categorized.is_(False))
                      .order_by(Transaction.date.desc())).all()
    # une carte par marchand : les opérations similaires sont rangées en même temps
    groups: dict[str, list[Transaction]] = {}
    for t in rows:
        groups.setdefault(merchant_key(t.label) or f"#{t.id}", []).append(t)
    return render(request, db, "inbox.html", rows=rows, groups=list(groups.values()), **lookups(db))


# ---------------------------------------------------------------- achats en attente

@router.get("/pending")
def pending_list(request: Request, db: Session = Depends(get_db)):
    waiting = db.scalars(select(Pending).where(Pending.status == "attente").order_by(Pending.date.desc())).all()
    done = db.scalars(select(Pending).where(Pending.status != "attente")
                      .order_by(Pending.date.desc()).limit(30)).all()
    return render(request, db, "pending.html", waiting=waiting, done=done,
                  total=sum(p.amount_cents for p in waiting))


@router.get("/pending/new")
def pending_new(request: Request, db: Session = Depends(get_db)):
    return render(request, db, "pending_form.html", p=None, **lookups(db))


@router.get("/pending/{pid}")
def pending_edit(pid: int, request: Request, db: Session = Depends(get_db)):
    return render(request, db, "pending_form.html", p=db.get(Pending, pid) or _404(), **lookups(db))


@router.post("/pending")
@router.post("/pending/{pid}")
async def pending_save(request: Request, pid: int | None = None, db: Session = Depends(get_db)):
    f = await request.form()
    cents = fcents(f.get("amount"))
    if cents is None:
        return redirect(request, "/pending/new", "Montant invalide.")
    cents = abs(cents) if f.get("direction") == "in" else -abs(cents)
    p = db.get(Pending, pid) if pid else Pending(status="attente")
    if p is None:
        _404()
    p.date = fdate(f.get("date"), date.today())
    p.label = f.get("label", "").strip() or "Achat"
    p.amount_cents = cents
    p.keyword = f.get("keyword", "").strip()
    p.category_id = fint(f.get("category_id"))
    p.person_id = fint(f.get("person_id"))
    p.scope = f.get("scope") or "famille"
    p.note = f.get("note", "").strip()
    if not pid:
        db.add(p)
    p.tags = []
    add_tags(db, p, f.get("tags", ""))
    db.flush()
    await save_uploads(db, f, pending_id=p.id)
    msg = "Achat enregistré : il sera rangé automatiquement à son passage sur le compte."
    if p.status == "attente":
        tx = match_pending_to_existing(db, p)
        if tx:
            msg = f"Achat déjà présent sur le compte ({tx.date:%d/%m}) : opération rangée."
    db.commit()
    return redirect(request, "/pending", msg)


@router.post("/pending/{pid}/status")
async def pending_status(pid: int, request: Request, db: Session = Depends(get_db)):
    p = db.get(Pending, pid) or _404()
    f = await request.form()
    action = f.get("action")
    if action == "cancel":
        p.status = "annule"
    elif action == "reopen":
        p.status, p.transaction_id = "attente", None
    elif action == "delete":
        db.delete(p)
    db.commit()
    return redirect(request, "/pending", "Mis à jour.")


# ---------------------------------------------------------------- tags

@router.get("/tags")
def tags(request: Request, db: Session = Depends(get_db)):
    rows = db.execute(
        select(Tag, func.count(Transaction.id), func.coalesce(func.sum(Transaction.amount_cents), 0))
        .join(transaction_tags, transaction_tags.c.tag_id == Tag.id, isouter=True)
        .join(Transaction, Transaction.id == transaction_tags.c.transaction_id, isouter=True)
        .group_by(Tag.id).order_by(Tag.name)).all()
    return render(request, db, "tags.html", rows=rows)


@router.post("/tags/{tag_id}/delete")
def tag_delete(tag_id: int, request: Request, db: Session = Depends(get_db)):
    t = db.get(Tag, tag_id) or _404()
    db.delete(t)
    db.commit()
    return redirect(request, "/tags", "Tag supprimé.")


# ---------------------------------------------------------------- pièces jointes

@router.get("/attachments/{aid}")
def attachment(aid: int, db: Session = Depends(get_db)):
    a = db.get(Attachment, aid) or _404()
    path = (UPLOAD_DIR / a.stored_name).resolve()
    if not path.is_relative_to(UPLOAD_DIR.resolve()) or not path.exists():
        _404()
    return FileResponse(path, media_type=a.mime, filename=a.filename, content_disposition_type="inline")


@router.post("/attachments/{aid}/delete")
async def attachment_delete(aid: int, request: Request, db: Session = Depends(get_db)):
    a = db.get(Attachment, aid) or _404()
    f = await request.form()
    (UPLOAD_DIR / a.stored_name).unlink(missing_ok=True)
    db.delete(a)
    db.commit()
    return redirect(request, f.get("back") or "/", "Pièce jointe supprimée.")


@router.get("/attachments")
def attachments(request: Request, db: Session = Depends(get_db)):
    rows = db.scalars(select(Attachment).order_by(Attachment.uploaded_at.desc()).limit(300)).all()
    return render(request, db, "attachments.html", rows=rows)


def _404():
    raise HTTPException(404, "Introuvable")

