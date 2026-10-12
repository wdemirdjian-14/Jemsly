"""Import des relevés : dépôt du/des fichier(s) -> aperçu (doublons décochés) -> validation -> rangement auto.

Relevés PDF Crédit Agricole : lus localement (aucune donnée envoyée à l'extérieur), compte reconnu par son
numéro, détail de la carte à débit différé importé ligne à ligne sur un compte « carte » dédié.
"""
import json
from collections import Counter, defaultdict
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import ai, ca_pdf
from ..db import get_db
from ..importers import ImportErrorFR, parse_file
from ..models import Account, ImportBatch, Pending, Transaction
from ..services import account_balance, apply_auto, category_by_name
from ..templating import redirect, render
from ..utils import norm
from .common import fint, lookups

router = APIRouter()
TRANSFER_CATEGORY = "Virement interne / épargne"


def _keys(rows):
    seen = Counter()
    for r in rows:
        if r.get("ref"):
            r["key"] = f"ref:{r['ref']}"
        else:
            base = f"{r['date']}|{r['amount_cents']}|{norm(r['label'])}"
            seen[(r.get("acct"), base)] += 1
            r["key"] = f"{base}|{seen[(r.get('acct'), base)]}"
    return rows


def _resolve_account(db: Session, number: str, holder: str, kind: str, chosen: Account | None) -> Account:
    acc = db.scalar(select(Account).where(Account.number == number))
    if acc:
        return acc
    if kind != "carte" and chosen and not chosen.number:
        chosen.number = number
        return chosen
    if kind == "carte":
        name, akind = f"Carte {number[-2:].rjust(4, '•')} (débit différé)", "carte"
    elif " ou " in holder.lower():
        name, akind = f"Compte joint ••{number[-4:]}", "joint"
    else:
        name, akind = f"Compte perso ••{number[-4:]}", "courant"
    # Carte : solde de départ 0 depuis toujours (chaque relevé solde ses paiements carte → 0)
    acc = Account(name=name, bank="Crédit Agricole", number=number, kind=akind, initial_date=date(2000, 1, 1))
    db.add(acc)
    db.flush()
    return acc


def _parse_ca(db: Session, raw: bytes, chosen: Account | None):
    """-> (compte principal, lignes, solde, date solde, meta)"""
    parsed = ca_pdf.parse_ca_text(ca_pdf.pdf_to_text(raw))
    rows, main, meta = [], None, {"sections": []}
    card_acc = None
    for s in parsed["sections"]:
        kind = "carte" if s["kind"] == "card" else "compte"
        acc = _resolve_account(db, s["number"], s["holder"], kind, chosen)
        if kind == "carte":
            card_acc = acc
        elif main is None:
            main = acc
            meta.update(opening=s["opening"], opening_date=s.get("opening_date"), closing=s["closing"],
                        closing_date=s.get("closing_date"))
        meta["sections"].append({"account": acc.name, "count": len(s["rows"]), "check": s["check"],
                                 "debit": s["computed_debit"], "credit": s["computed_credit"],
                                 "expected_debit": s["expected_debit"], "expected_credit": s["expected_credit"]})
        for r in s["rows"]:
            r["acct"] = acc.id
            rows.append(r)
    if main is None:
        main = card_acc
    if card_acc:
        for r in rows:
            if r["acct"] != card_acc.id and ca_pdf.card_settlement_key(r["label"]):
                r["settle"] = card_acc.id
    bal = meta.get("closing")
    bal_date = date.fromisoformat(meta["closing_date"]) if meta.get("closing_date") else None
    return main, rows, bal, bal_date, meta


async def _create_batch(db: Session, up, chosen: Account | None) -> ImportBatch:
    raw = await up.read()
    fmt, rows, bal, bal_date = parse_file(up.filename, raw)
    meta = {}
    acc = chosen
    if fmt == "pdf":
        text = None
        try:
            text = ca_pdf.pdf_to_text(raw)
        except ca_pdf.CAParseError:
            pass
        if text and ca_pdf.is_ca_statement(text):
            acc, rows, bal, bal_date, meta = _parse_ca(db, raw, chosen)
        elif ai.enabled():
            if not chosen:
                raise ImportErrorFR("Choisissez le compte concerné par ce relevé.")
            res = ai.extract_pdf_statement(raw)
            rows, bal = res["rows"], res["balance"]
            bal_date = date.fromisoformat(res["balance_date"]) if res["balance_date"] else None
        else:
            raise ImportErrorFR("Ce PDF n'est pas un relevé Crédit Agricole reconnu. Exportez le relevé en CSV "
                                "ou OFX depuis votre banque (ou configurez ANTHROPIC_API_KEY).")
    if not acc:
        raise ImportErrorFR("Choisissez le compte concerné par ce relevé.")
    if not rows:
        raise ImportErrorFR("Aucune opération trouvée dans ce fichier.")
    for r in rows:
        r.setdefault("acct", acc.id)
    rows = _keys(rows)
    if bal is not None and bal_date is None:
        bal_date = max(date.fromisoformat(r["date"]) for r in rows)
    batch = ImportBatch(account_id=acc.id, filename=up.filename[:200], fmt=fmt, rows_json=json.dumps(rows),
                        statement_balance_cents=bal, statement_date=bal_date, meta_json=json.dumps(meta))
    db.add(batch)
    db.flush()
    return batch


def _annotate(db: Session, rows):
    """Marque les lignes déjà importées / doublons probables (par compte)."""
    by_acct = defaultdict(list)
    for r in rows:
        by_acct[r["acct"]].append(r)
    for acct, rs in by_acct.items():
        existing = db.scalars(select(Transaction).where(Transaction.account_id == acct)).all()
        keys = {t.import_key for t in existing}
        by_day_amount = Counter((t.date.isoformat(), t.amount_cents) for t in existing)
        for r in rs:
            if r["key"] in keys:
                r["status"] = "déjà importée"
            elif by_day_amount[(r["date"], r["amount_cents"])]:
                r["status"] = "doublon probable"
                by_day_amount[(r["date"], r["amount_cents"])] -= 1
            else:
                r["status"] = ""
    return rows


def _confirm(db: Session, batch: ImportBatch, selected: set[int]) -> int:
    rows = json.loads(batch.rows_json)
    meta = json.loads(batch.meta_json or "{}")
    accts = {r["acct"] for r in rows}
    empty_before = {a for a in accts
                    if not db.scalar(select(func.count(Transaction.id)).where(Transaction.account_id == a))}
    keys = defaultdict(set)
    for a in accts:
        keys[a] = set(db.scalars(select(Transaction.import_key).where(Transaction.account_id == a)))
    transfer = None
    created = dup = 0
    for i, r in enumerate(rows):
        if i not in selected or r["key"] in keys[r["acct"]]:
            dup += 1
            continue
        tx = Transaction(account_id=r["acct"], date=date.fromisoformat(r["date"]), label=r["label"][:300],
                         amount_cents=r["amount_cents"], import_key=r["key"], batch_id=batch.id)
        db.add(tx)
        db.flush()
        keys[r["acct"]].add(r["key"])
        created += 1
        if r.get("settle"):
            # prélèvement des dépenses carte : virement interne compte -> carte (les achats sont détaillés)
            transfer = transfer or category_by_name(db, TRANSFER_CATEGORY, kind="virement", icon="🔁")
            tx.category_id, tx.categorized, tx.auto_source = transfer.id, True, "règlement carte"
            counterpart = Transaction(account_id=r["settle"], date=tx.date, label=f"Règlement carte : {tx.label}"[:300],
                                      amount_cents=-tx.amount_cents, import_key=f"settle:{r['key']}",
                                      batch_id=batch.id, category_id=transfer.id, categorized=True,
                                      auto_source="règlement carte")
            if counterpart.import_key not in keys[r["settle"]]:
                db.add(counterpart)
                keys[r["settle"]].add(counterpart.import_key)
            continue
        apply_auto(db, tx)
    # 1er import d'un compte : le solde de départ est celui indiqué par la banque
    main = db.get(Account, batch.account_id)
    if batch.account_id in empty_before and meta.get("opening") is not None and meta.get("opening_date") \
            and main.kind != "carte":
        main.initial_balance_cents = meta["opening"]
        main.initial_date = date.fromisoformat(meta["opening_date"]) + timedelta(days=1)
    batch.status, batch.imported, batch.duplicates = "done", created, dup
    return created


@router.get("/import")
def import_form(request: Request, db: Session = Depends(get_db)):
    batches = db.scalars(select(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(30)).all()
    return render(request, db, "import.html", batches=batches, ai_enabled=ai.enabled(), **lookups(db))


@router.post("/import")
async def import_upload(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    uploads = [u for u in f.getlist("files") + f.getlist("file") if getattr(u, "filename", None)]
    chosen = db.get(Account, fint(f.get("account_id")) or 0)
    if not uploads:
        return redirect(request, "/import", "Choisissez un ou plusieurs fichiers.")
    if len(uploads) == 1:
        try:
            batch = await _create_batch(db, uploads[0], chosen)
        except (ImportErrorFR, ca_pdf.CAParseError) as e:
            db.rollback()
            return redirect(request, "/import", str(e))
        except Exception as e:  # noqa: BLE001 - on montre l'erreur plutôt qu'une page 500
            db.rollback()
            return redirect(request, "/import", f"Lecture impossible : {e}")
        db.commit()
        return redirect(request, f"/import/{batch.id}")

    # Plusieurs fichiers : import direct (doublons écartés), du plus ancien au plus récent
    report, total = [], 0
    for up in sorted(uploads, key=lambda u: u.filename):
        try:
            batch = await _create_batch(db, up, chosen)
            rows = _annotate(db, json.loads(batch.rows_json))
            n = _confirm(db, batch, {i for i, r in enumerate(rows) if not r["status"]})
            db.commit()
            total += n
            meta = json.loads(batch.meta_json or "{}")
            bad = [s["account"] for s in meta.get("sections", []) if not s["check"]]
            report.append(f"{up.filename[:40]} : {n}" + (f" ⚠️ totaux à vérifier ({', '.join(bad)})" if bad else ""))
        except Exception as e:  # noqa: BLE001
            db.rollback()
            report.append(f"{up.filename[:40]} : erreur – {e}")
    return redirect(request, "/import", f"{total} opération(s) importée(s). " + " · ".join(report))


@router.get("/import/{bid}")
def import_preview(bid: int, request: Request, db: Session = Depends(get_db)):
    batch = db.get(ImportBatch, bid) or _404()
    rows = json.loads(batch.rows_json)
    txs = []
    if batch.status == "preview":
        rows = _annotate(db, rows)
    else:
        txs = db.scalars(select(Transaction).where(Transaction.batch_id == batch.id,
                                                   Transaction.auto_source != "règlement carte")
                         .order_by(Transaction.date.desc())).all()
    check = None
    if batch.status == "done" and batch.statement_balance_cents is not None and batch.statement_date:
        computed = account_balance(db, batch.account, at=batch.statement_date)
        check = {"bank": batch.statement_balance_cents, "computed": computed,
                 "diff": batch.statement_balance_cents - computed}
    names = {a.id: a.name for a in db.scalars(select(Account))}
    return render(request, db, "import_preview.html", batch=batch, rows=rows, txs=txs, check=check,
                  meta=json.loads(batch.meta_json or "{}"), names=names,
                  auto=sum(1 for t in txs if t.categorized),
                  from_pending=sum(1 for t in txs if t.auto_source == "achat en attente"))


@router.post("/import/{bid}/confirm")
async def import_confirm(bid: int, request: Request, db: Session = Depends(get_db)):
    batch = db.get(ImportBatch, bid) or _404()
    if batch.status != "preview":
        return redirect(request, f"/import/{bid}")
    f = await request.form()
    created = _confirm(db, batch, {int(i) for i in f.getlist("row")})
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
