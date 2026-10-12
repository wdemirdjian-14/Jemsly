"""Intégration Claude (optionnelle, active si ANTHROPIC_API_KEY est défini) :
- lecture des relevés PDF -> opérations structurées
- analyse mensuelle du budget en langage naturel."""
import base64
import json

from .config import ANTHROPIC_API_KEY, CLAUDE_MODEL

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def enabled() -> bool:
    return bool(ANTHROPIC_API_KEY)


def _client():
    import anthropic
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _text(response) -> str:
    if response.stop_reason == "refusal":
        raise RuntimeError("L'IA a refusé de traiter ce document.")
    return "".join(b.text for b in response.content if b.type == "text")


TX_SCHEMA = {
    "type": "object",
    "properties": {
        "statement_balance": {"type": ["number", "null"],
                              "description": "Solde final du relevé (nouveau solde), en euros, ou null"},
        "statement_date": {"type": ["string", "null"], "description": "Date du solde final AAAA-MM-JJ, ou null"},
        "transactions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date d'opération AAAA-MM-JJ"},
                    "label": {"type": "string", "description": "Libellé complet de l'opération"},
                    "amount": {"type": "number", "description": "Montant en euros : négatif pour un débit, positif pour un crédit"},
                },
                "required": ["date", "label", "amount"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["statement_balance", "statement_date", "transactions"],
    "additionalProperties": False,
}


def extract_pdf_statement(pdf_bytes: bytes) -> dict:
    """Retourne {"rows": [...], "balance": centimes|None, "balance_date": str|None}."""
    with _client().beta.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=64000,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": TX_SCHEMA}},
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.standard_b64encode(pdf_bytes).decode()}},
                {"type": "text", "text": (
                    "Voici un relevé de compte bancaire français. Extrais TOUTES les opérations listées, "
                    "dans l'ordre, sans en oublier ni en inventer. N'inclus pas les lignes de solde, de "
                    "report ou de total. Les montants de la colonne Débit sont négatifs, ceux de la colonne "
                    "Crédit positifs. Si le relevé contient plusieurs comptes, ne prends que le compte courant "
                    "principal. Indique aussi le nouveau solde de fin de relevé s'il figure.")},
            ],
        }],
    ) as stream:
        response = stream.get_final_message()
    data = json.loads(_text(response))
    rows = []
    for t in data.get("transactions", []):
        rows.append({"date": t["date"], "label": " ".join(str(t["label"]).split()),
                     "amount_cents": round(float(t["amount"]) * 100), "ref": ""})
    bal = data.get("statement_balance")
    return {"rows": rows, "balance": round(bal * 100) if bal is not None else None,
            "balance_date": data.get("statement_date")}


ANALYSIS_SYSTEM = (
    "Tu es un conseiller en gestion de budget familial, bienveillant et concret. Tu analyses les comptes "
    "d'un foyer français (un couple et leurs enfants). Certaines dépenses sont professionnelles "
    "(périmètre « jems ») et seront remboursées via note de frais : ne les compte pas comme des dépenses "
    "du foyer, mais signale celles qui restent à déclarer. Réponds en français, en Markdown simple "
    "(titres ###, listes), en 400 mots maximum, avec : 1) le verdict du mois en 2 phrases, 2) les points "
    "d'attention chiffrés, 3) trois actions concrètes pour le mois prochain. Ne pose pas de question."
)


def analyse_month(summary: str) -> str:
    with _client().beta.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        output_config={"effort": "medium"},
        system=ANALYSIS_SYSTEM,
        messages=[{"role": "user", "content": summary}],
    ) as stream:
        response = stream.get_final_message()
    return _text(response).strip()
