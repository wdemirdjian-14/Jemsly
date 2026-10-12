import re
from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy import func, select

from .config import APP_NAME
from .models import FREQUENCIES, JEMS_STATUSES, SCOPES, Pending, Transaction
from .utils import fmt_date, fmt_money, merchant_key, month_label, shift_month


def markdown_lite(text: str) -> Markup:
    """Rendu minimal et sûr (tout est échappé) : titres, listes, gras."""
    out, in_list = [], False
    for line in (text or "").splitlines():
        t = str(escape(line.strip()))
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        if re.match(r"^[-*] ", t) or re.match(r"^\d+\. ", t):
            if not in_list:
                out.append("<ul>")
                in_list = True
            item = re.sub(r"^([-*]|\d+\.) ", "", t)
            out.append(f"<li>{item}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if t.startswith("#"):
            out.append(f"<h3>{t.lstrip('#').strip()}</h3>")
        elif t:
            out.append(f"<p>{t}</p>")
    if in_list:
        out.append("</ul>")
    return Markup("\n".join(out))


templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
env = templates.env
env.filters["money"] = fmt_money
env.filters["smoney"] = lambda c: fmt_money(c, signed=True)
env.filters["d"] = fmt_date
env.filters["eur"] = lambda c: "" if c is None else f"{c / 100:.2f}"
env.filters["month_label"] = month_label
env.filters["mkey"] = merchant_key
env.filters["md"] = markdown_lite
env.globals.update(APP_NAME=APP_NAME, SCOPES=SCOPES, JEMS_STATUSES=JEMS_STATUSES, FREQUENCIES=FREQUENCIES,
                   shift_month=shift_month)


def render(request: Request, db, name: str, **ctx):
    ctx.setdefault("flash", request.session.pop("flash", None))
    ctx["nav_inbox"] = db.scalar(select(func.count(Transaction.id)).where(Transaction.categorized.is_(False)))
    ctx["nav_pending"] = db.scalar(select(func.count(Pending.id)).where(Pending.status == "attente"))
    ctx["path"] = request.url.path
    return templates.TemplateResponse(request, name, ctx)


def redirect(request: Request, url: str, flash: str | None = None):
    if flash:
        request.session["flash"] = flash
    return RedirectResponse(url, status_code=303)
