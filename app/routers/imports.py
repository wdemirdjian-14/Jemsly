"""Import des relevés : dépôt du fichier -> aperçu (doublons décochés) -> validation -> rangement auto."""
import json
from collections import Counter
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import ai
from ..db import get_db
from ..importers import ImportErrorFR, parse_file
from ..models import Account, ImportBatch, Pending, Transaction
from ..services import account_balance, apply_auto
from ..templating import redirect, render
from ..utils import norm
from .common import fint, lookups

router = APIRouter()


def _keys(rows):
    seen = Counter()
    for r in rows:
        if r.get("ref"):
            r["key"] = f"ref:{r['ref']}"
        else:
            base = f"{r['date']}|{r['amount_cents']}|{norm(r['label'])}"
            seen[base] += 1
            r["key"] = f"{base}|{seen[base]}"
    return rows


@router.get("/import")
def import_form(request: Request, db: Session = Depends(get_db)):
    batches = db.scalars(select(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(24)).all()
    return render(request, db, "import.html", batches=batches, ai_enabled=ai.enabled(), **lookups(db))


@router.post("/import")
async def import_upload(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    up = f.get("file")
    acc = db.get(Account, fint(f.get("account_id")) or 0)
    if not acc or not getattr(up, "filename", None):
        return redirect(request, "/import", "Choisissez un compte et un fichier.")
    raw = await up.read()
    try:
        fmt, rows, bal, bal_date = parse_file(up.filename, raw)
        if fmt == "pdf":
            if not ai.enabled():
                return redirect(request, "/import", "Les relevés PDF nécessitent la clé ANTHROPIC_API_KEY "
                                                    "(sinon, exportez en CSV ou OFX depuis votre banque).")
            res = ai.extract_pdf_statement(raw)
            rows, bal = res["rows"], res["balance"]
            bal_date = date.fromisoformat(res["balance_date"]) if res["balance_date"] else None
    except ImportErrorFR as e:
        return redirect(request, "/import", str(e))
    except Exception as e:  # noqa: BLE001 - on montre l'erreur à l'utilisateur plutôt qu'une page 500
        return redirect(request, "/import", f"Lecture impossible : {e}")
    if not rows:
        return redirect(request, "/import", "Aucune opération trouvée dans ce fichier.")
    rows = _keys(rows)
    if bal is not None and bal_date is None:
        bal_date = max(date.fromisoformat(r["date"]) for r in rows)
    batch = ImportBatch(account_id=acc.id, filename=up.filename[:200], fmt=fmt, rows_json=json.dumps(rows),
                        statement_balance_cents=bal, statement_date=bal_date)
    db.add(batch)
    db.commit()
    return redirect(request, f"/import/{batch.id}")


@router.get("/import/{bid}")
def import_preview(bid: int, request: Request, db: Session = Depends(get_db)):
    batch = db.get(ImportBatch, bid) or _404()
    rows = json.loads(batch.rows_json)
    if batch.status == "preview":
        existing = db.scalars(select(Transaction).where(Transaction.account_id == batch.account_id)).all()
        keys = {t.import_key for t in existing}
        by_day_amount = Counter((t.date.isoformat(), t.amount_cents) for t in existing)
        for r in rows:
            if r["key"] in keys:
                r["status"] = "déjà importée"
            elif by_day_amount[(r["date"], r["amount_cents"])]:
                r["status"] = "doublon probable"
                by_day_amount[(r["date"], r["amount_cents"])] -= 1
            else:
                r["status"] = ""
        txs = []
    else:
        txs = db.scalars(select(Transaction).where(Transaction.batch_id == batch.id)
                         .order_by(Transaction.date.desc())).all()
    check = None
    if batch.status == "done" and batch.statement_balance_cents is not None and batch.statement_date:
        computed = account_balance(db, batch.account, at=batch.statement_date)
        check = {"bank": batch.statement_balance_cents, "computed": computed,
                 "diff": batch.statement_balance_cents - computed}
    return render(request, db, "import_preview.html", batch=batch, rows=rows, txs=txs, check=check,
                  auto=sum(1 for t in txs if t.categorized),
                  from_pending=sum(1 for t in txs if t.auto_source == "achat en attente"))


@router.post("/import/{bid}/confirm")
async def import_confirm(bid: int, request: Request, db: Session = Depends(get_db)):
    batch = db.get(ImportBatch, bid) or _404()
    if batch.status != "preview":
        return redirect(request, f"/import/{bid}")
    f = await request.form()
    selected = {int(i) for i in f.getlist("row")}
    rows = json.loads(batch.rows_json)
    keys = set(db.scalars(select(Transaction.import_key).where(Transaction.account_id == batch.account_id)))
    created = dup = 0
    for i, r in enumerate(rows):
        if i not in selected or r["key"] in keys:
            dup += 1
            continue
        tx = Transaction(account_id=batch.account_id, date=date.fromisoformat(r["date"]), label=r["label"][:300],
                         amount_cents=r["amount_cents"], import_key=r["key"], batch_id=batch.id)
        db.add(tx)
        db.flush()
        apply_auto(db, tx)
        keys.add(r["key"])
        created += 1
    batch.status, batch.imported, batch.duplicates = "done", created, dup
    db.commit()
    return redirect(request, f"/import/{bid}", f"{created} opération(s) importée(s).")


@router.post("/import/{bid}/align-balance")
def import_align_balance(bid: int, request: Request, db: Session = Depends(get_db)):
    """Ajuste le solde initial du compte pour que le solde calculé égale celui du relevé."""
    batch = db.get(ImportBatch, bid) or _404()
    if batch.statement_balance_cents is None or not batch.statement_date:
        return redirect(request, f"/import/{bid}")
    acc = batch.account
    diff = batch.statement_balance_cents - account_balance(db, acc, at=batch.statement_date)
    acc.initial_balance_cents += diff
    db.commit()
    return redirect(request, f"/import/{bid}", "Solde du compte recalé sur le relevé.")


@router.post("/import/{bid}/undo")
def import_undo(bid: int, request: Request, db: Session = Depends(get_db)):
    batch = db.get(ImportBatch, bid) or _404()
    n = 0
    for tx in db.scalars(select(Transaction).where(Transaction.batch_id == batch.id)):
        for p in db.scalars(select(Pending).where(Pending.transaction_id == tx.id)):
            p.status, p.transaction_id = "attente", None
            for a in p.attachments:
                a.transaction_id = None
        db.delete(tx)
        n += 1
    db.delete(batch)
    db.commit()
    return redirect(request, "/import", f"Import annulé ({n} opération(s) supprimée(s)).")


def _404():
    raise HTTPException(404, "Introuvable")
