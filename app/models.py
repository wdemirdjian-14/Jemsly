"""Modèle de données. Tous les montants sont stockés en centimes (entiers, signés :
négatif = sortie d'argent, positif = entrée)."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey, Integer, String, Table, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

SCOPES = {"perso": "Perso", "famille": "Famille", "jems": "Jems (note de frais)"}
JEMS_STATUSES = {"a_declarer": "À déclarer", "declaree": "Déclarée", "remboursee": "Remboursée"}
FREQUENCIES = {"hebdo": "Hebdomadaire", "mensuel": "Mensuelle", "trimestriel": "Trimestrielle",
               "semestriel": "Semestrielle", "annuel": "Annuelle"}
FREQ_MONTHS = {"mensuel": 1, "trimestriel": 3, "semestriel": 6, "annuel": 12}

transaction_tags = Table(
    "transaction_tags", Base.metadata,
    Column("transaction_id", ForeignKey("transactions.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)
pending_tags = Table(
    "pending_tags", Base.metadata,
    Column("pending_id", ForeignKey("pending.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    bank: Mapped[str] = mapped_column(String(80), default="")
    initial_balance_cents: Mapped[int] = mapped_column(Integer, default=0)
    initial_date: Mapped[date] = mapped_column(Date, default=date.today)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    number: Mapped[str] = mapped_column(String(40), default="")  # n° de compte / de carte (détection à l'import)
    kind: Mapped[str] = mapped_column(String(10), default="courant")  # courant | joint | carte | epargne
    default_scope: Mapped[str] = mapped_column(String(10), default="")  # périmètre par défaut des opérations
    default_person_id: Mapped[int | None] = mapped_column(ForeignKey("persons.id", ondelete="SET NULL"), nullable=True)


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    icon: Mapped[str] = mapped_column(String(8), default="•")
    kind: Mapped[str] = mapped_column(String(10), default="depense")  # depense | revenu | virement
    budget_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)  # budget mensuel (positif)


class Person(Base):
    __tablename__ = "persons"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    role: Mapped[str] = mapped_column(String(20), default="moi")  # moi | conjoint | enfant | famille


class Tag(Base):
    __tablename__ = "tags"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)


class Loan(Base):
    """Crédit (immo, conso, auto, LOA, renouvelable…)."""
    __tablename__ = "loans"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    lender: Mapped[str] = mapped_column(String(100), default="")
    kind: Mapped[str] = mapped_column(String(20), default="conso")
    principal_cents: Mapped[int] = mapped_column(Integer, default=0)
    rate: Mapped[float] = mapped_column(default=0.0)  # taux annuel nominal en %
    monthly_cents: Mapped[int] = mapped_column(Integer, default=0)  # mensualité (positif), assurance comprise
    insurance_cents: Mapped[int] = mapped_column(Integer, default=0)  # dont assurance emprunteur
    start_date: Mapped[date] = mapped_column(Date, default=date.today)  # 1re échéance
    duration_months: Mapped[int] = mapped_column(Integer, default=12)
    match_pattern: Mapped[str] = mapped_column(String(120), default="")
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # « Restant dû » relevé dans l'appli de la banque (quand durée/taux ne sont pas connus)
    known_remaining_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    known_remaining_at: Mapped[date | None] = mapped_column(Date, nullable=True)


class Insurance(Base):
    __tablename__ = "insurances"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    insurer: Mapped[str] = mapped_column(String(100), default="")
    kind: Mapped[str] = mapped_column(String(30), default="autre")
    contract_number: Mapped[str] = mapped_column(String(60), default="")
    premium_cents: Mapped[int] = mapped_column(Integer, default=0)  # montant par échéance (positif)
    frequency: Mapped[str] = mapped_column(String(15), default="mensuel")
    next_due: Mapped[date | None] = mapped_column(Date, nullable=True)
    renewal_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # échéance annuelle / résiliation
    person_id: Mapped[int | None] = mapped_column(ForeignKey("persons.id"), nullable=True)
    match_pattern: Mapped[str] = mapped_column(String(120), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    person: Mapped[Person | None] = relationship()


class Recurrence(Base):
    """Flux récurrent attendu (salaire, loyer, abonnements…) ou provision (taxe foncière…)."""
    __tablename__ = "recurrences"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(120))
    amount_cents: Mapped[int] = mapped_column(Integer)  # signé
    frequency: Mapped[str] = mapped_column(String(15), default="mensuel")
    next_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("persons.id"), nullable=True)
    scope: Mapped[str] = mapped_column(String(10), default="famille")
    match_pattern: Mapped[str] = mapped_column(String(120), default="")
    is_provision: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    category: Mapped[Category | None] = relationship()


class InstallmentPlan(Base):
    """Achat payé en plusieurs fois (3x, 4x, 10x…)."""
    __tablename__ = "installment_plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(120))
    merchant: Mapped[str] = mapped_column(String(100), default="")
    total_cents: Mapped[int] = mapped_column(Integer)  # positif, frais inclus
    count: Mapped[int] = mapped_column(Integer, default=3)
    first_date: Mapped[date] = mapped_column(Date)
    match_pattern: Mapped[str] = mapped_column(String(120), default="")
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("persons.id"), nullable=True)
    scope: Mapped[str] = mapped_column(String(10), default="famille")
    notes: Mapped[str] = mapped_column(Text, default="")
    items: Mapped[list[InstallmentItem]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="InstallmentItem.due_date")
    category: Mapped[Category | None] = relationship()
    person: Mapped[Person | None] = relationship()


class InstallmentItem(Base):
    __tablename__ = "installment_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("installment_plans.id", ondelete="CASCADE"))
    due_date: Mapped[date] = mapped_column(Date)
    amount_cents: Mapped[int] = mapped_column(Integer)  # positif
    transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True)
    plan: Mapped[InstallmentPlan] = relationship(back_populates="items")


class ImportBatch(Base):
    __tablename__ = "import_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    filename: Mapped[str] = mapped_column(String(200))
    fmt: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    status: Mapped[str] = mapped_column(String(10), default="preview")  # preview | done
    rows_json: Mapped[str] = mapped_column(Text, default="[]")
    statement_balance_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    statement_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    imported: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    meta_json: Mapped[str] = mapped_column(Text, default="{}")  # contrôles du relevé (totaux, sections)
    account: Mapped[Account] = relationship()


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (UniqueConstraint("account_id", "import_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    date: Mapped[date] = mapped_column(Date, index=True)
    label: Mapped[str] = mapped_column(String(300))
    amount_cents: Mapped[int] = mapped_column(Integer)
    import_key: Mapped[str] = mapped_column(String(200))
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id", ondelete="SET NULL"), nullable=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("persons.id", ondelete="SET NULL"), nullable=True)
    scope: Mapped[str] = mapped_column(String(10), default="famille")
    jems_status: Mapped[str | None] = mapped_column(String(12), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    categorized: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_source: Mapped[str] = mapped_column(String(30), default="")  # règle, achat en attente, apprentissage…
    loan_id: Mapped[int | None] = mapped_column(ForeignKey("loans.id", ondelete="SET NULL"), nullable=True)
    insurance_id: Mapped[int | None] = mapped_column(ForeignKey("insurances.id", ondelete="SET NULL"), nullable=True)
    recurrence_id: Mapped[int | None] = mapped_column(ForeignKey("recurrences.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    account: Mapped[Account] = relationship()
    category: Mapped[Category | None] = relationship()
    person: Mapped[Person | None] = relationship()
    tags: Mapped[list[Tag]] = relationship(secondary=transaction_tags, order_by="Tag.name")
    loan: Mapped[Loan | None] = relationship()
    insurance: Mapped[Insurance | None] = relationship()
    attachments: Mapped[list[Attachment]] = relationship(back_populates="transaction")


class Pending(Base):
    """Achat saisi à la volée, en attente d'apparaître sur le relevé bancaire."""
    __tablename__ = "pending"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date)
    label: Mapped[str] = mapped_column(String(200))
    amount_cents: Mapped[int] = mapped_column(Integer)  # signé (négatif = dépense)
    keyword: Mapped[str] = mapped_column(String(80), default="")  # mot attendu dans le libellé bancaire
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("persons.id"), nullable=True)
    scope: Mapped[str] = mapped_column(String(10), default="famille")
    note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(12), default="attente")  # attente | rapproche | annule
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    category: Mapped[Category | None] = relationship()
    person: Mapped[Person | None] = relationship()
    tags: Mapped[list[Tag]] = relationship(secondary=pending_tags, order_by="Tag.name")
    attachments: Mapped[list[Attachment]] = relationship(back_populates="pending")


class Rule(Base):
    """Règle de catégorisation automatique : si le libellé contient `pattern`…"""
    __tablename__ = "rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    pattern: Mapped[str] = mapped_column(String(120))
    direction: Mapped[str] = mapped_column(String(8), default="tous")  # tous | debit | credit
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"), nullable=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("persons.id", ondelete="SET NULL"), nullable=True)
    scope: Mapped[str | None] = mapped_column(String(10), nullable=True)
    tags: Mapped[str] = mapped_column(String(200), default="")  # séparés par des virgules
    priority: Mapped[int] = mapped_column(Integer, default=100)
    max_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)  # seulement si |montant| ≤ max
    hits: Mapped[int] = mapped_column(Integer, default=0)
    category: Mapped[Category | None] = relationship()
    person: Mapped[Person | None] = relationship()


class Attachment(Base):
    __tablename__ = "attachments"
    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(200))
    stored_name: Mapped[str] = mapped_column(String(200))
    mime: Mapped[str] = mapped_column(String(100), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True)
    pending_id: Mapped[int | None] = mapped_column(ForeignKey("pending.id", ondelete="SET NULL"), nullable=True)
    loan_id: Mapped[int | None] = mapped_column(ForeignKey("loans.id", ondelete="SET NULL"), nullable=True)
    insurance_id: Mapped[int | None] = mapped_column(ForeignKey("insurances.id", ondelete="SET NULL"), nullable=True)
    transaction: Mapped[Transaction | None] = relationship(back_populates="attachments")
    pending: Mapped[Pending | None] = relationship(back_populates="attachments")


class MonthlyReport(Base):
    __tablename__ = "monthly_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String(7), unique=True)  # AAAA-MM
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    env_fingerprint: Mapped[str] = mapped_column(String(200), default="")  # dernier APP_PASSWORD appliqué
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
