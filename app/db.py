from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DB_PATH

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()
    from .utils import norm  # recherche insensible aux accents et à la casse : WHERE norm(label) LIKE …
    dbapi_conn.create_function("norm", 1, lambda s: norm(s) if s else "", deterministic=True)


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def migrate():
    """Crée les tables et ajoute les colonnes manquantes (évolutions du modèle sans perdre les données)."""
    from sqlalchemy import inspect, text

    Base.metadata.create_all(engine)
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = col.type.compile(engine.dialect)
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                if isinstance(default, bool):
                    default = int(default)
                sql = f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl}'
                if default is not None:
                    sql += f" DEFAULT {default!r}" if not isinstance(default, str) else f" DEFAULT '{default}'"
                conn.execute(text(sql))
