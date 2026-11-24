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
    _categories_without_unique_name()
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


def _categories_without_unique_name():
    """Anciennes bases : le nom de catégorie était unique. Avec les sous-catégories, « Voiture › Assurance » et
    « Logement › Assurance » doivent pouvoir coexister : on reconstruit la table (recette officielle SQLite)."""
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        unique = [r for r in cur.execute("PRAGMA index_list(categories)").fetchall() if r[2]]
        if not unique:
            return
        cols = [r[1] for r in cur.execute("PRAGMA table_info(categories)").fetchall()]
        keep = [c for c in ("id", "name", "icon", "kind", "budget_cents", "parent_id") if c in cols]
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute("BEGIN")
        cur.execute("CREATE TABLE categories_new (id INTEGER NOT NULL PRIMARY KEY, name VARCHAR(80) NOT NULL, "
                    "icon VARCHAR(8) NOT NULL DEFAULT '', kind VARCHAR(10) NOT NULL DEFAULT 'depense', "
                    "budget_cents INTEGER, parent_id INTEGER REFERENCES categories (id) ON DELETE SET NULL)")
        cur.execute(f"INSERT INTO categories_new ({', '.join(keep)}) SELECT {', '.join(keep)} FROM categories")
        cur.execute("DROP TABLE categories")
        cur.execute("ALTER TABLE categories_new RENAME TO categories")
        cur.execute("COMMIT")
        cur.execute("PRAGMA foreign_keys=ON")
    finally:
        raw.close()
