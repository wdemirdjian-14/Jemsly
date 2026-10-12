"""Bilan mensuel (inspection + analyse IA), budgets, notes de frais Jems, exports."""
import csv
import io
import zipfile
from datetime import date, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import ai
from ..config import UPLOAD_DIR
from ..db import get_db
from ..models import JEMS_STATUSES, SCOPES, Category, MonthlyReport, Transaction
from ..services import inspect_month, month_stats, provisions
from ..templating import redirect, render
from ..utils import current_month, fmt_money, month_bounds, month_label, shift_month
from .common import fcents

router = APIRouter()


def _month(request) -> str:
    m = request.query_params.get("month") or current_month()
    try:
        month_bounds(m)
        return m
    except ValueError:
        return current_month()


@router.get("/report")
def report(request: Request, db: Session = Depends(get_db)):
    month = _month(request)
    stats = month_stats(db, month)
    prev = month_stats(db, shift_month(month, -1))
    saved = db.scalar(select(MonthlyReport).where(MonthlyReport.month == month))
    history = [month_stats(db, shift_month(month, -i)) for i in range(5, -1, -1)]
    return render(request, db, "report.html", month=month, stats=stats, prev=prev,
                  alerts=inspect_month(db, month, stats), saved=saved, ai_enabled=ai.enabled(),
                  history=[(h["month"], h["income"], h["expenses"]) for h in history])


def _summary(db, month: str) -> str:
    s = month_stats(db, month)
    p = month_stats(db, shift_month(month, -1))
    lines = [f"Mois analysé : {month_label(month)}",
             f"Revenus : {fmt_money(s['income'])} (mois précédent {fmt_money(p['income'])})",
             f"Dépenses : {fmt_money(s['expenses'])} (mois précédent {fmt_money(p['expenses'])})",
             f"Solde du mois : {fmt_money(s['net'])}", "", "Dépenses par catégorie (budget) :"]
    for c in s["categories"]:
        b = f" / budget {fmt_money(c['budget'])}" if c["budget"] else ""
        lines.append(f"- {c['name']} : {fmt_money(c['total'])}{b}")
    lines += ["", "Par personne :"] + [f"- {k} : {fmt_money(v)}" for k, v in s["persons"]]
    lines += ["", "Par périmètre :"] + [f"- {SCOPES.get(k, k)} : {fmt_money(v)}" for k, v in s["scopes"]]
    lines += ["", "Contrôles automatiques :"] + [f"- {a['title']} : {a['detail']}" for a in inspect_month(db, month, s)]
    lines += ["", "Prévisions de provisions (dépenses non mensuelles) :"]
    lines += [f"- {pr['label']} : {fmt_money(pr['amount'])} le {pr['due']:%d/%m/%Y}, soit {fmt_money(pr['monthly'])}/mois"
              for pr in provisions(db)]
    lines += ["", "Opérations (date ; libellé ; montant ; catégorie ; pour qui ; périmètre) :"]
    for t in s["transactions"][:600]:
        lines.append(f"{t.date:%d/%m} ; {t.label} ; {t.amount_cents / 100:.2f} ; "
                     f"{t.category.name if t.category else '?'} ; {t.person.name if t.person else '-'} ; {t.scope}")
    return "\n".join(lines)


@router.post("/report/ai")
def report_ai(request: Request, db: Session = Depends(get_db)):
    month = _month(request)
    if not ai.enabled():
        return redirect(request, f"/report?month={month}", "Clé ANTHROPIC_API_KEY non configurée.")
    try:
        content = ai.analyse_month(_summary(db, month))
    except Exception as e:  # noqa: BLE001
        return redirect(request, f"/report?month={month}", f"Analyse IA impossible : {e}")
    rep = db.scalar(select(MonthlyReport).where(MonthlyReport.month == month)) or MonthlyReport(month=month)
    rep.content = content
    rep.created_at = datetime.now()
    db.add(rep)
    db.commit()
    return redirect(request, f"/report?month={month}#ia", "Analyse générée.")


# ---------------------------------------------------------------- budget

@router.get("/budget")
def budget(request: Request, db: Session = Depends(get_db)):
    month = _month(request)
    stats = month_stats(db, month)
    spent = {c["id"]: c["total"] for c in stats["categories"]}
    cats = db.scalars(select(Category).where(Category.kind == "depense").order_by(Category.name)).all()
    provs = provisions(db)
    total_budget = sum(c.budget_cents or 0 for c in cats)
    return render(request, db, "budget.html", month=month, cats=cats, spent=spent, provisions=provs,
                  total_budget=total_budget, total_spent=stats["expenses"],
                  provision_monthly=sum(p["monthly"] for p in provs))


@router.post("/budget")
async def budget_save(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    for c in db.scalars(select(Category).where(Category.kind == "depense")):
        v = f.get(f"b{c.id}", "").strip()
        c.budget_cents = abs(fcents(v)) if v and fcents(v) is not None else None
    db.commit()
    return redirect(request, f"/budget?month={f.get('month', '')}", "Budgets enregistrés.")


# ---------------------------------------------------------------- Jems

def _jems_rows(db, month: str, status: str):
    q = select(Transaction).where(Transaction.scope == "jems")
    if month:
        start, end = month_bounds(month)
        q = q.where(Transaction.date.between(start, end))
    if status:
        q = q.where(Transaction.jems_status == status)
    return db.scalars(q.order_by(Transaction.date)).all()


@router.get("/jems")
def jems(request: Request, db: Session = Depends(get_db)):
    month = request.query_params.get("month", "")
    status = request.query_params.get("status", "")
    rows = _jems_rows(db, month, status)
    totals = {k: -sum(t.amount_cents for t in _jems_rows(db, month, k) if t.amount_cents < 0) for k in JEMS_STATUSES}
    return render(request, db, "jems.html", rows=rows, month=month, status=status, totals=totals,
                  total=-sum(t.amount_cents for t in rows if t.amount_cents < 0),
                  missing=sum(1 for t in rows if not t.attachments))


@router.post("/jems/status")
async def jems_status(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    new = f.get("new_status")
    ids = [int(i) for i in f.getlist("ids")]
    if new in JEMS_STATUSES and ids:
        for t in db.scalars(select(Transaction).where(Transaction.id.in_(ids))):
            t.jems_status = new
        db.commit()
    return redirect(request, f"/jems?month={f.get('month', '')}&status={f.get('status', '')}",
                    f"{len(ids)} dépense(s) passée(s) en « {JEMS_STATUSES.get(new, '?')} ».")


@router.get("/jems/export")
def jems_export(request: Request, db: Session = Depends(get_db)):
    """ZIP : récapitulatif CSV (Excel) + justificatifs renommés date_montant_libellé."""
    month = request.query_params.get("month", "")
    status = request.query_params.get("status", "")
    rows = _jems_rows(db, month, status)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        out = io.StringIO()
        w = csv.writer(out, delimiter=";")
        w.writerow(["Date", "Libellé", "Montant TTC", "Catégorie", "Note", "Statut", "Justificatifs"])
        for t in rows:
            files = []
            for i, a in enumerate(t.attachments, 1):
                ext = a.stored_name.rsplit(".", 1)[-1]
                safe = "".join(c if c.isalnum() else "-" for c in t.label[:30]).strip("-")
                name = f"justificatifs/{t.date:%Y-%m-%d}_{-t.amount_cents / 100:.2f}_{safe}_{i}.{ext}"
                p = UPLOAD_DIR / a.stored_name
                if p.exists():
                    z.write(p, name)
                    files.append(name.split("/", 1)[1])
            w.writerow([t.date.strftime("%d/%m/%Y"), t.label, f"{-t.amount_cents / 100:.2f}".replace(".", ","),
                        t.category.name if t.category else "", t.note.replace("\n", " "),
                        JEMS_STATUSES.get(t.jems_status or "", ""), ", ".join(files)])
        z.writestr("note_de_frais.csv", "﻿" + out.getvalue())
    name = f"note-de-frais-jems-{month or date.today().isoformat()}.zip"
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/export.csv")
def export_csv(request: Request, db: Session = Depends(get_db)):
    month = request.query_params.get("month", "")
    q = select(Transaction).order_by(Transaction.date)
    if month:
        start, end = month_bounds(month)
        q = q.where(Transaction.date.between(start, end))
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["Date", "Compte", "Libellé", "Montant", "Catégorie", "Pour qui", "Périmètre", "Tags", "Note"])
    for t in db.scalars(q):
        w.writerow([t.date.strftime("%d/%m/%Y"), t.account.name, t.label, f"{t.amount_cents / 100:.2f}".replace(".", ","),
                    t.category.name if t.category else "", t.person.name if t.person else "",
                    SCOPES.get(t.scope, t.scope), ",".join(x.name for x in t.tags), t.note.replace("\n", " ")])
    return Response("﻿" + out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="operations-{month or "tout"}.csv"'})
