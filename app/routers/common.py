from datetime import date

from sqlalchemy import select

from ..config import MAX_UPLOAD_MB
from ..models import Account, Category, Person
from ..services import save_attachment
from ..utils import parse_amount_or_none, parse_date


def fint(v):
    try:
        return int(v) if v not in (None, "", "None") else None
    except (TypeError, ValueError):
        return None


def fdate(v, default=None):
    if not v:
        return default
    try:
        return parse_date(v)
    except ValueError:
        return default


def fcents(v, default=None):
    c = parse_amount_or_none(v)
    return default if c is None else c


def lookups(db):
    return {
        "categories": db.scalars(select(Category).order_by(Category.kind, Category.name)).all(),
        "persons": db.scalars(select(Person).order_by(Person.id)).all(),
        "accounts": db.scalars(select(Account).where(Account.archived.is_(False)).order_by(Account.id)).all(),
        "today": date.today(),
    }


async def save_uploads(db, form, field="files", **links) -> int:
    n = 0
    for up in form.getlist(field):
        if not getattr(up, "filename", None):
            continue
        data = await up.read()
        if not data or len(data) > MAX_UPLOAD_MB * 1024 * 1024:
            continue
        save_attachment(db, up, data, **links)
        n += 1
    return n
