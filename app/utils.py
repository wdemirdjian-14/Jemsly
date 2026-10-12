import re
import unicodedata
from calendar import monthrange
from datetime import date, datetime, timedelta


def parse_amount(value) -> int:
    """'1 234,56' / '-12.5' / '1.234,56 €' -> centimes (int). Lève ValueError si illisible."""
    if value is None:
        raise ValueError("montant vide")
    if isinstance(value, (int, float)):
        return round(float(value) * 100)
    s = str(value).strip().replace(" ", "").replace(" ", "").replace(" ", "")
    s = s.replace("€", "").replace("EUR", "").replace("+", "")
    if not s:
        raise ValueError("montant vide")
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    if s.endswith("-"):
        neg, s = True, s[:-1]
    if s.startswith("-"):
        neg, s = not neg, s[1:]
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    cents = round(float(s) * 100)
    return -cents if neg else cents


def parse_amount_or_none(value):
    try:
        return parse_amount(value)
    except (ValueError, TypeError):
        return None


def parse_date(value) -> date:
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%Y", "%d.%m.%Y", "%Y%m%d", "%d-%m-%y", "%Y/%m/%d"):
        try:
            d = datetime.strptime(s[:10] if fmt != "%Y%m%d" else s[:8], fmt).date()
        except ValueError:
            continue
        if d.year >= 1990:  # évite que '12/10/26' soit lu comme l'an 26 avec %Y
            return d
    raise ValueError(f"date illisible : {value!r}")


def fmt_money(cents, signed=False) -> str:
    if cents is None:
        return "—"
    v = cents / 100
    s = f"{abs(v):,.2f}".replace(",", " ").replace(".", ",")
    sign = "-" if v < 0 else ("+" if signed and v > 0 else "")
    return f"{sign}{s} €"


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", strip_accents(s or "").upper()).strip()


_NOISE = re.compile(
    r"\b(PAIEMENT|PAIEMT|CARTE|CB|PAR|CARTE\s*X?\d+|X\d{4}|FACTURE|PRLV|PRELEVEMENT|SEPA|VIR|VIREMENT|"
    r"INST|INSTANTANE|RECU|EMIS|DE|DU|LA|LE|DES|ACHAT|RETRAIT|DAB|ECH|ECHEANCE|REF|MANDAT|ICS|"
    r"NUM|N[O°]|FR\d*|EUR|TPE|CONTACTLESS|SANS\s+CONTACT)\b"
)


def merchant_key(label: str) -> str:
    """Clé « marchand » d'un libellé bancaire : 'CB AMAZON PAYMENTS 12/10 X1234' -> 'AMAZON PAYMENTS'."""
    s = norm(label)
    s = re.sub(r"\d{1,2}[/.-]\d{1,2}([/.-]\d{2,4})?", " ", s)  # dates
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = _NOISE.sub(" ", s)
    words = [w for w in s.split() if not re.fullmatch(r"[A-Z]?\d+[A-Z]?\d*", w) and len(w) > 1]
    return " ".join(words[:3])


def label_contains(label: str, pattern: str) -> bool:
    if not pattern:
        return False
    pats = [p for p in (norm(x) for x in pattern.split("|")) if p]
    lab = norm(label)
    return any(p in lab for p in pats)


def month_bounds(month: str):
    y, m = map(int, month.split("-"))
    return date(y, m, 1), date(y, m, monthrange(y, m)[1])


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    return date(y, m, min(d.day, monthrange(y, m)[1]))


def shift_month(month: str, n: int) -> str:
    d = add_months(date.fromisoformat(month + "-01"), n)
    return f"{d.year:04d}-{d.month:02d}"


def current_month() -> str:
    return date.today().strftime("%Y-%m")


MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
             "septembre", "octobre", "novembre", "décembre"]


def month_label(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{MONTHS_FR[m - 1].capitalize()} {y}"


def fmt_date(d) -> str:
    if not d:
        return ""
    return d.strftime("%d/%m/%Y")


def next_occurrence(d: date, frequency: str) -> date:
    if frequency == "hebdo":
        return d + timedelta(days=7)
    from .models import FREQ_MONTHS
    return add_months(d, FREQ_MONTHS.get(frequency, 1))


def clean_tags(raw: str) -> list[str]:
    out = []
    for t in re.split(r"[,;#]", raw or ""):
        t = strip_accents(t).strip().lower()
        t = re.sub(r"[^a-z0-9 _-]", "", t).strip().replace(" ", "-")
        if t and t not in out:
            out.append(t)
    return out
