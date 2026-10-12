import os
import sys
import tempfile
from pathlib import Path

import pytest

_tmp = tempfile.mkdtemp(prefix="wzx-test-")
os.environ["DATA_DIR"] = _tmp
os.environ["APP_USERNAME"] = "Warren"
os.environ["APP_PASSWORD"] = "secret"
os.environ.pop("ANTHROPIC_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.auth import ensure_admin  # noqa: E402
from app.services import seed  # noqa: E402


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed(db)
        ensure_admin(db)
    c = TestClient(app)
    r = c.post("/login", data={"username": "warren", "password": "secret"}, follow_redirects=False)
    assert r.status_code == 303
    return c


@pytest.fixture()
def db():
    with SessionLocal() as s:
        yield s
