"""Recherche d'opérations : barre « intelligente » + filtres avancés.

Exemples de saisie dans la barre :
  axa                 -> libellé ou note contenant « axa » (sans tenir compte des accents/majuscules)
  42,50  /  42.5€     -> montant exact (débit ou crédit)
  42                  -> libellé contenant « 42 » OU montant entre 42,00 et 42,99
  >100  <20  >=50     -> montant supérieur / inférieur
  50-100              -> montant entre 50 et 100
  12/03  12/03/2026   -> date précise ;  03/2026 -> mois
  axa >50 2026        -> combinable
"""
import re
from datetime import date

from sqlalchemy import and_, func, or_

from .models import Transaction
from .utils import month_bounds, norm, parse_amount_or_none, parse_date

AMT = r"\d+(?:[.,]\d{1,2})?"


def parse_query(text: str) -> dict:
    """Décompose la saisie -> {"words": [...], "amount_ranges": [(min,max)], "fuzzy_ints": [...], "dates": [...],
    "months": [...]} (montants en centimes, valeur absolue)."""
    out = {"words": [], "amount_ranges": [], "fuzzy_ints": [], "dates": [], "months": []}
    today = date.today()
    for tok in (text or "").split():
        t = tok.strip().replace("€", "")
        if not t:
            continue
        m = re.fullmatch(rf"(>=|<=|>|<|=)({AMT})", t)
        if m:
            v = abs(parse_amount_or_none(m.group(2)) or 0)
            op = m.group(1)
            rng = {">": (v + 1, None), ">=": (v, None), "<": (None, v - 1), "<=": (None, v), "=": (v, v)}[op]
            out["amount_ranges"].append(rng)
            continue
        m = re.fullmatch(rf"({AMT})-({AMT})", t)
        if m:
            a, b = sorted(abs(parse_amount_or_none(x) or 0) for x in m.groups())
            out["amount_ranges"].append((a, b))
            continue
        if re.fullmatch(r"\d+[.,]\d{1,2}", t) or (tok.endswith("€") and re.fullmatch(AMT, t)):
            v = abs(parse_amount_or_none(t))
            out["amount_ranges"].append((v, v))
            continue
        m = re.fullmatch(r"(\d{1,2})[/.-](\d{4})", t)
        if m and 1 <= int(m.group(1)) <= 12:
            out["months"].append(f"{m.group(2)}-{int(m.group(1)):02d}")
            continue
        m = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?", t)
        if m and 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(1)) <= 31:
            try:
                if m.group(3):
                    out["dates"].append(parse_date(t.replace(".", "/")))
                else:
                    d = date(today.year, int(m.group(2)), int(m.group(1)))
                    out["dates"].append(d if d <= today else date(today.year - 1, d.month, d.day))
                continue
            except ValueError:
                pass
        if re.fullmatch(r"\d{1,6}", t) and not re.fullmatch(r"(19|20)\d{2}", t):
            out["fuzzy_ints"].append(int(t))
            continue
        if re.fullmatch(r"(19|20)\d{2}", t):
            out["months"].append(t)  # année entière
            continue
        out["words"].append(norm(t))
    return out


def apply_search(q, text: str = "", amin=None, amax=None, dfrom=None, dto=None):
    """Ajoute les conditions à une requête select(Transaction)."""
    p = parse_query(text)
    label = func.norm(Transaction.label)
    note = func.norm(Transaction.note)
    absamt = func.abs(Transaction.amount_cents)
    for w in p["words"]:
        q = q.where(or_(label.contains(w), note.contains(w)))
    for lo, hi in p["amount_ranges"]:
        if lo is not None:
            q = q.where(absamt >= lo)
        if hi is not None:
            q = q.where(absamt <= hi)
    for n in p["fuzzy_ints"]:
        q = q.where(or_(label.contains(str(n)), and_(absamt >= n * 100, absamt <= n * 100 + 99)))
    if p["dates"]:
        q = q.where(Transaction.date.in_(p["dates"]))
    if p["months"]:
        conds = []
        for m in p["months"]:
            if len(m) == 4:
                conds.append(Transaction.date.between(date(int(m), 1, 1), date(int(m), 12, 31)))
            else:
                start, end = month_bounds(m)
                conds.append(Transaction.date.between(start, end))
        q = q.where(or_(*conds))
    if amin is not None:
        q = q.where(absamt >= amin)
    if amax is not None:
        q = q.where(absamt <= amax)
    if dfrom:
        q = q.where(Transaction.date >= dfrom)
    if dto:
        q = q.where(Transaction.date <= dto)
    return q


SORTS = {
    "": (Transaction.date.desc(), Transaction.id.desc()),
    "date_asc": (Transaction.date.asc(), Transaction.id.asc()),
    "amount_desc": (func.abs(Transaction.amount_cents).desc(),),
    "amount_asc": (func.abs(Transaction.amount_cents).asc(),),
}
