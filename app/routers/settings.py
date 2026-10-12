"""Paramètres : comptes, catégories, personnes, règles, sauvegarde."""
import io
import json
import re
import sqlite3
import tempfile
import zipfile
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..auth import MIN_LENGTH, hash_password, normalize_username, verify_password
from ..config import DB_PATH, UPLOAD_DIR
from ..db import get_db
from ..models import (
    Account, Category, InstallmentPlan, Insurance, Pending, Person, Recurrence, Rule, Transaction, User,
)
from ..profile import apply_profile
from ..services import account_balance, apply_auto, recategorize_all
from ..templating import redirect, render
from .common import fcents, fdate, fint, lookups

router = APIRouter()


def _404():
    raise HTTPException(404, "Introuvable")


def _detach(db: Session, column: str, value: int):
    """Retire la référence (catégorie/personne) partout avant suppression."""
    for model in (Transaction, Pending, Recurrence, InstallmentPlan, Insurance, Rule):
        if hasattr(model, column):
            db.execute(update(model).where(getattr(model, column) == value).values({column: None}))


@router.get("/more")
def more(request: Request, db: Session = Depends(get_db)):
    return render(request, db, "more.html")


@router.get("/settings")
def settings(request: Request, db: Session = Depends(get_db)):
    lk = lookups(db)
    all_accounts = db.scalars(select(Account).order_by(Account.archived, Account.id)).all()
    return render(request, db, "settings.html", all_accounts=[(a, account_balance(db, a)) for a in all_accounts],
                  rules=db.scalars(select(Rule).order_by(Rule.priority, Rule.pattern)).all(),
                  users=db.scalars(select(User).order_by(User.username)).all(), me=request.session.get("uid"), **lk)


@router.post("/settings/accounts")
async def account_save(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    acc = db.get(Account, fint(f.get("id")) or 0) or Account()
    acc.name = f.get("name", "").strip() or "Compte"
    acc.bank = f.get("bank", "").strip()
    acc.initial_balance_cents = fcents(f.get("initial_balance"), 0)
    acc.initial_date = fdate(f.get("initial_date"), date(2000, 1, 1))
    acc.archived = bool(f.get("archived"))
    acc.number = f.get("number", acc.number or "").strip()
    acc.kind = f.get("kind") or acc.kind or "courant"
    acc.default_scope = f.get("default_scope", "")
    acc.default_person_id = fint(f.get("default_person_id"))
    db.add(acc)
    db.commit()
    return redirect(request, "/settings#comptes", "Compte enregistré.")


@router.post("/settings/categories")
async def category_save(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    cat = db.get(Category, fint(f.get("id")) or 0)
    if cat and f.get("action") == "delete":
        _detach(db, "category_id", cat.id)
        db.delete(cat)
        db.commit()
        return redirect(request, "/settings#categories", "Catégorie supprimée.")
    cat = cat or Category()
    cat.name = f.get("name", "").strip() or "Catégorie"
    cat.icon = (f.get("icon", "").strip() or "•")[:8]
    cat.kind = f.get("kind", "depense")
    db.add(cat)
    db.commit()
    return redirect(request, "/settings#categories", "Catégorie enregistrée.")


@router.post("/settings/persons")
async def person_save(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    p = db.get(Person, fint(f.get("id")) or 0)
    if p and f.get("action") == "delete":
        _detach(db, "person_id", p.id)
        db.delete(p)
        db.commit()
        return redirect(request, "/settings#personnes", "Personne supprimée.")
    p = p or Person()
    p.name = f.get("name", "").strip() or "Personne"
    p.role = f.get("role", "moi")
    db.add(p)
    db.commit()
    return redirect(request, "/settings#personnes", "Personne enregistrée.")


@router.post("/settings/rules")
async def rule_save(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    r = db.get(Rule, fint(f.get("id")) or 0)
    if r and f.get("action") == "delete":
        db.delete(r)
        db.commit()
        return redirect(request, "/settings#regles", "Règle supprimée.")
    r = r or Rule()
    r.pattern = f.get("pattern", "").strip()
    if not r.pattern:
        return redirect(request, "/settings#regles", "Le motif est obligatoire.")
    r.direction = f.get("direction", "tous")
    r.category_id = fint(f.get("category_id"))
    r.person_id = fint(f.get("person_id"))
    r.scope = f.get("scope") or None
    r.tags = f.get("tags", "").strip()
    r.priority = fint(f.get("priority")) or 100
    r.max_cents = abs(fcents(f.get("max_amount"))) if fcents(f.get("max_amount")) else None
    db.add(r)
    db.commit()
    return redirect(request, "/settings#regles", "Règle enregistrée.")


@router.post("/settings/users")
async def user_save(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    action = f.get("action", "create")
    me = db.get(User, request.session.get("uid"))
    if action == "delete":
        u = db.get(User, fint(f.get("id")) or 0)
        if u and u.id != me.id:
            db.delete(u)
            db.commit()
            return redirect(request, "/settings#utilisateurs", f"Accès de « {u.username} » supprimé.")
        return redirect(request, "/settings#utilisateurs", "Vous ne pouvez pas supprimer votre propre compte.")
    password = f.get("password", "")
    if len(password) < MIN_LENGTH:
        return redirect(request, "/settings#utilisateurs",
                        f"Mot de passe trop court ({MIN_LENGTH} caractères minimum).")
    if password != f.get("password2", ""):
        return redirect(request, "/settings#utilisateurs", "Les deux mots de passe ne correspondent pas.")
    if action == "password":
        if not verify_password(f.get("current", ""), me.password_hash):
            return redirect(request, "/settings#utilisateurs", "Mot de passe actuel incorrect.")
        me.password_hash = hash_password(password)
        db.commit()
        return redirect(request, "/settings#utilisateurs", "Mot de passe modifié.")
    name = normalize_username(f.get("username", ""))
    if not re.fullmatch(r"[a-z0-9._-]{3,40}", name):
        return redirect(request, "/settings#utilisateurs",
                        "Identifiant invalide (3 à 40 caractères : lettres, chiffres, . _ -).")
    if db.scalar(select(User).where(User.username == name)):
        return redirect(request, "/settings#utilisateurs", "Cet identifiant existe déjà.")
    db.add(User(username=name, password_hash=hash_password(password)))
    db.commit()
    return redirect(request, "/settings#utilisateurs", f"Accès créé pour « {name} ».")


@router.post("/settings/profile")
async def profile_import(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    up = f.get("file")
    if not getattr(up, "filename", None):
        return redirect(request, "/settings#profil", "Choisissez le fichier profil (.json).")
    try:
        data = json.loads((await up.read()).decode("utf-8"))
        stats = apply_profile(db, data)
    except (ValueError, KeyError, TypeError) as e:
        db.rollback()
        return redirect(request, "/settings#profil", f"Profil invalide : {e}")
    db.commit()
    return redirect(request, "/settings#profil", "Profil appliqué : " + ", ".join(f"{v} {k}" for k, v in stats.items()))


@router.post("/settings/recategorize")
def recategorize(request: Request, db: Session = Depends(get_db)):
    n = recategorize_all(db)
    db.commit()
    return redirect(request, "/inbox", f"{n} opération(s) rangée(s) automatiquement.")


@router.post("/settings/reapply")
def reapply(request: Request, db: Session = Depends(get_db)):
    n = 0
    for tx in db.scalars(select(Transaction).where(Transaction.categorized.is_(False))):
        apply_auto(db, tx)
        n += tx.categorized
    db.commit()
    return redirect(request, "/inbox", f"{n} opération(s) rangée(s) automatiquement.")


@router.get("/settings/backup")
def backup(request: Request):
    """Sauvegarde complète : base SQLite (copie cohérente) + justificatifs."""
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "wazzzxpense.db"
        src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(copy)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(copy, "wazzzxpense.db")
            for p in UPLOAD_DIR.rglob("*"):
                if p.is_file():
                    z.write(p, f"uploads/{p.relative_to(UPLOAD_DIR)}")
    return Response(buf.getvalue(), media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="wazzzxpense-{date.today().isoformat()}.zip"'})
