"""Lecture locale (sans IA) des relevés PDF Crédit Agricole.

Le PDF est converti en texte par `pdftotext -layout` (poppler) puis analysé :
- sections « Compte … n° XXXXX » : opérations du compte, débit/crédit déduits de la colonne ;
- sections carte à débit différé (« … Sociétaire n° 5137xxxx ») : détail des paiements carte,
  réglés en une ligne « Dépenses Carte X1234 Au jj/mm/aa » sur le compte ;
- contrôle : les totaux calculés sont comparés aux « Total des opérations » / « Montant prélevé ».
"""
from __future__ import annotations

import re
import shutil
import subprocess
from datetime import date

from .utils import parse_amount

AMOUNT = r"\d{1,3}(?:[ .]\d{3})*,\d{2}"
MONTHS = {"janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
          "aout": 8, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12, "décembre": 12}

OP_LINE = re.compile(r"^(\s*)(\d{2})\.(\d{2})\s+(?:(\d{2})\.(\d{2})\s+)?(.*?)\s+(" + AMOUNT + r")\s*[^\d\s]?\s*$")
ACCOUNT_HDR = re.compile(r"^\s*(.+?)\s+-\s+(Compte .+?|Livret .+?|Plan .+?)\s+n°\s*(\d{6,})(\s+\(suite\))?\s*$")
CARD_HDR = re.compile(r"^\s*(.+?)\s+-\s+(.+?)\s+Sociétaire n°\s*([0-9x]+)(\s+\(suite\))?\s*$", re.I)
COLS_HDR = re.compile(r"Libellé des opérations\s+Débit\s+Crédit")
NOISE = re.compile(r"^\s*(\d[\d ]*|Page \d+ / \d+|.*Credit Agricole.*RCS.*|.*RELEVE DE COMPTES.*|.*Date d'arrêté.*|"
                   r"Date|opé\.?|ope\.?|valeur|Date\s+valeur|[¨þ�\s]*)\s*$")


class CAParseError(Exception):
    pass


def is_ca_statement(text: str) -> bool:
    return "RELEVE DE COMPTES" in text and ("Credit Agricole" in text or "Crédit Agricole" in text)


def pdf_to_text(raw: bytes) -> str:
    if not shutil.which("pdftotext"):
        raise CAParseError("pdftotext (poppler-utils) n'est pas installé sur le serveur.")
    res = subprocess.run(["pdftotext", "-layout", "-", "-"], input=raw, capture_output=True, timeout=60)
    if res.returncode != 0:
        raise CAParseError("PDF illisible.")
    return res.stdout.decode("utf-8", errors="replace")


def _stmt_date(text: str) -> date:
    m = re.search(r"Date d'arrêté\s*:\s*(\d{1,2})\s+([A-Za-zéûÉ]+)\s+(\d{4})", text)
    if not m:
        raise CAParseError("Date d'arrêté introuvable.")
    return date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))


def _op_date(dd: str, mm: str, ref: date) -> date:
    d, m = int(dd), int(mm)
    year = ref.year - 1 if m > ref.month + 1 else ref.year
    return date(year, m, d)


def _signed_balance(kind: str, amount: str) -> int:
    v = parse_amount(amount)
    return -v if "débiteur" in kind.lower() else v


def parse_ca_text(text: str) -> dict:
    """Retourne {"statement_date", "sections": [{kind, number, holder, label, rows, opening, closing,
    expected_debit, expected_credit, check}]}"""
    ref = _stmt_date(text)
    sections: dict[str, dict] = {}
    order: list[str] = []
    cur = None
    debit_end = credit_end = None
    last_row = None
    label_col = None
    row_indent = 0

    def section(kind, number, holder, label):
        key = f"{kind}:{number}"
        if key not in sections:
            sections[key] = {"kind": kind, "number": number, "holder": holder.strip(), "label": label.strip(),
                             "rows": [], "opening": None, "closing": None, "closing_date": None,
                             "expected_debit": None, "expected_credit": None}
            order.append(key)
        return sections[key]

    for line in text.splitlines():
        m = ACCOUNT_HDR.match(line)
        if m and "IBAN" not in line:
            cur = section("account", m.group(3), m.group(1), m.group(2))
            last_row, debit_end = None, None
            continue
        m = CARD_HDR.match(line)
        if m:
            cur = section("card", m.group(3), m.group(1), m.group(2))
            last_row, debit_end = None, None
            continue
        if cur is None:
            continue
        if COLS_HDR.search(line):
            d = line.index("Débit")
            c = line.index("Crédit")
            debit_end, credit_end = d + len("Débit"), c + len("Crédit")
            label_col = line.index("Libellé")
            continue
        m = re.search(r"(Ancien|Nouveau) solde (débiteur|créditeur) au (\d{2})\.(\d{2})\.(\d{4})\s+(" + AMOUNT + ")", line)
        if m:
            val = _signed_balance(m.group(2), m.group(6))
            if m.group(1) == "Ancien":
                cur["opening"] = val
                cur["opening_date"] = date(int(m.group(5)), int(m.group(4)), int(m.group(3))).isoformat()
            else:
                cur["closing"] = val
                cur["closing_date"] = date(int(m.group(5)), int(m.group(4)), int(m.group(3))).isoformat()
                debit_end = None  # fin du tableau : la suite (frais à venir, infos) n'est pas une opération
            last_row = None
            continue
        m = re.search(r"Total des opérations\s+(" + AMOUNT + r")\s+(" + AMOUNT + ")", line)
        if m:
            cur["expected_debit"], cur["expected_credit"] = parse_amount(m.group(1)), parse_amount(m.group(2))
            last_row = None
            continue
        m = re.search(r"Montant prélevé au (\d{2})\.(\d{2})\.(\d{4})\s+(" + AMOUNT + ")", line)
        if m:
            cur["expected_debit"] = parse_amount(m.group(4))
            cur["settled_on"] = date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
            debit_end = None
            last_row = None
            continue
        if debit_end is None:
            continue
        m = OP_LINE.match(line)
        if m:
            amt = m.group(7)
            end = m.end(7)
            is_debit = end <= (debit_end + credit_end) / 2
            cents = parse_amount(amt)
            label = re.sub(r"\s{2,}", " ", m.group(6)).strip()
            row = {"date": _op_date(m.group(2), m.group(3), ref).isoformat(), "label": label,
                   "amount_cents": -cents if is_debit else cents, "ref": ""}
            cur["rows"].append(row)
            last_row = row
            row_indent = len(m.group(1))
            continue
        # ligne de complément de libellé (référence, motif de virement…)
        if last_row is not None and not NOISE.match(line) and label_col is not None:
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())
            if indent >= row_indent + 8 and not re.search(AMOUNT + r"\s*$", stripped) and len(last_row["label"]) < 250:
                last_row["label"] += " " + re.sub(r"\s{2,}", " ", stripped)
        elif NOISE.match(line) is None:
            last_row = None

    out = []
    for key in order:
        s = sections[key]
        deb = -sum(r["amount_cents"] for r in s["rows"] if r["amount_cents"] < 0)
        cre = sum(r["amount_cents"] for r in s["rows"] if r["amount_cents"] > 0)
        ok = (s["expected_debit"] is None or s["expected_debit"] == deb - (cre if s["kind"] == "card" else 0)) and \
             (s["expected_credit"] is None or s["expected_credit"] == cre)
        if s["kind"] == "account" and s["opening"] is not None and s["closing"] is not None:
            ok = ok and s["opening"] + cre - deb == s["closing"]
        s["check"] = ok
        s["computed_debit"], s["computed_credit"] = deb, cre
        for r in s["rows"]:
            r["label"] = _clean_label(r["label"])
        out.append(s)
    if not any(s["rows"] for s in out):
        raise CAParseError("Aucune opération trouvée dans ce relevé.")
    return {"statement_date": ref.isoformat(), "sections": out}


def _clean_label(label: str) -> str:
    label = re.sub(r"\bCore\b", "", label)
    label = re.sub(r"\s+FR\d{2}ZZZ\w+", "", label)  # identifiant créancier SEPA
    return re.sub(r"\s{2,}", " ", label).strip()


def card_settlement_key(label: str) -> str | None:
    """'Prlv Dépenses Carte X1234 Au 16/04/26' -> 'X1234'"""
    m = re.search(r"Dépenses Carte\s+(X\d{4})", label, re.I)
    return m.group(1).upper() if m else None
