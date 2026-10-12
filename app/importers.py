"""Lecture des relevés bancaires : CSV (toutes banques FR, détection automatique), OFX/QFX.
Le PDF est délégué à l'IA (voir ai.py)."""
import csv
import io
import re

from .utils import norm, parse_amount, parse_amount_or_none, parse_date

DATE_KEYS = ["DATE OPERATION", "DATE OP", "DATEOP", "DATE DOPERATION", "DATE COMPTABLE", "DATE", "BOOKING DATE"]
LABEL_KEYS = ["LIBELLE", "LABEL", "DESCRIPTION", "INTITULE", "DETAIL", "NATURE", "OPERATION", "MOTIF"]
AMOUNT_KEYS = ["MONTANT", "AMOUNT", "SOMME"]
DEBIT_KEYS = ["DEBIT"]
CREDIT_KEYS = ["CREDIT"]


class ImportErrorFR(Exception):
    pass


def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def detect_format(filename: str, raw: bytes) -> str:
    name = filename.lower()
    head = raw[:600].lstrip()
    if name.endswith(".pdf") or head.startswith(b"%PDF"):
        return "pdf"
    if name.endswith((".ofx", ".qfx")) or b"<OFX>" in raw[:4000].upper() or b"OFXHEADER" in head[:200]:
        return "ofx"
    return "csv"


def parse_file(filename: str, raw: bytes):
    """Retourne (format, lignes, solde_relevé_centimes|None, date_solde|None)."""
    fmt = detect_format(filename, raw)
    if fmt == "ofx":
        rows, bal, bal_date = parse_ofx(decode(raw))
        return fmt, rows, bal, bal_date
    if fmt == "csv":
        return fmt, parse_csv(decode(raw)), None, None
    return fmt, None, None, None


# ---------------------------------------------------------------- OFX

def _tag(block: str, tag: str):
    m = re.search(rf"<{tag}>([^<\r\n]*)", block, re.I)
    return m.group(1).strip() if m else None


def parse_ofx(text: str):
    rows = []
    for block in re.findall(r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|(?=</BANKTRANLIST>))", text, re.S | re.I):
        amount = _tag(block, "TRNAMT")
        dt = _tag(block, "DTPOSTED") or _tag(block, "DTUSER")
        if amount is None or dt is None:
            continue
        name = _tag(block, "NAME") or ""
        memo = _tag(block, "MEMO") or ""
        label = name if not memo or memo in name else (f"{name} {memo}".strip() if name else memo)
        rows.append({
            "date": parse_date(dt).isoformat(),
            "label": re.sub(r"\s+", " ", label).strip() or "(sans libellé)",
            "amount_cents": parse_amount(amount),
            "ref": _tag(block, "FITID") or "",
        })
    if not rows:
        raise ImportErrorFR("Aucune opération trouvée dans le fichier OFX.")
    bal = bal_date = None
    m = re.search(r"<LEDGERBAL>(.*?)(?:</LEDGERBAL>|<AVAILBAL>|$)", text, re.S | re.I)
    if m:
        b = _tag(m.group(1), "BALAMT")
        d = _tag(m.group(1), "DTASOF")
        bal = parse_amount_or_none(b)
        try:
            bal_date = parse_date(d) if d else None
        except ValueError:
            bal_date = None
    return rows, bal, bal_date


# ---------------------------------------------------------------- CSV

def _find_col(headers, keys, exclude=()):
    for key in keys:
        for i, h in enumerate(headers):
            if i in exclude:
                continue
            if h == key or h.startswith(key) or (len(key) > 4 and key in h):
                return i
    return None


def _norm_header(h: str) -> str:
    h = norm(h).replace("'", "").replace("’", "")
    return re.sub(r"[^A-Z ]", " ", h).replace("  ", " ").strip()


def parse_csv(text: str):
    sample = text[:5000]
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
    except csv.Error:
        delim = ";"
    if delim == "," and sample.count(";") > sample.count(","):
        delim = ";"
    all_rows = list(csv.reader(io.StringIO(text), delimiter=delim))

    header_idx = cols = None
    for idx, row in enumerate(all_rows[:40]):
        headers = [_norm_header(c) for c in row]
        d = _find_col(headers, DATE_KEYS)
        if d is None:
            continue
        lab = _find_col(headers, LABEL_KEYS, exclude=(d,))
        amt = _find_col(headers, AMOUNT_KEYS, exclude=(d,))
        deb = _find_col(headers, DEBIT_KEYS, exclude=(d,))
        cre = _find_col(headers, CREDIT_KEYS, exclude=(d,))
        if lab is not None and (amt is not None or (deb is not None and cre is not None)):
            header_idx, cols = idx, (d, lab, amt, deb, cre)
            break
    if header_idx is None:
        raise ImportErrorFR(
            "Colonnes non reconnues : le CSV doit contenir une colonne Date, une colonne Libellé et "
            "une colonne Montant (ou Débit + Crédit).")

    d, lab, amt, deb, cre = cols
    rows = []
    for row in all_rows[header_idx + 1:]:
        if len(row) <= max(c for c in cols if c is not None):
            continue
        try:
            dt = parse_date(row[d])
        except ValueError:
            continue
        if amt is not None:
            cents = parse_amount_or_none(row[amt])
        else:
            debit = parse_amount_or_none(row[deb])
            credit = parse_amount_or_none(row[cre])
            if debit is None and credit is None:
                cents = None
            else:
                cents = (credit or 0) - abs(debit or 0)
        if cents is None:
            continue
        label = re.sub(r"\s+", " ", row[lab]).strip() or "(sans libellé)"
        rows.append({"date": dt.isoformat(), "label": label, "amount_cents": cents, "ref": ""})
    if not rows:
        raise ImportErrorFR("Aucune opération lisible dans le CSV.")
    return rows
