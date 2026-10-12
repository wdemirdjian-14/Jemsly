"""Comptes utilisateurs : identifiant + mot de passe (haché PBKDF2-SHA256, sel aléatoire)."""
import hashlib
import hmac
import secrets

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import APP_PASSWORD, APP_USERNAME
from .models import User

ITERATIONS = 600_000
MIN_LENGTH = 10


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS).hex()
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
    except ValueError:
        return False
    test = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
    return hmac.compare_digest(test, digest)


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == normalize_username(username)))
    if user and verify_password(password, user.password_hash):
        return user
    verify_password(password, hash_password("leurre"))  # même durée que l'identifiant existe ou non
    return None


def ensure_admin(db: Session):
    """Crée le compte défini dans .env (APP_USERNAME / APP_PASSWORD).
    Mot de passe oublié : changez APP_PASSWORD dans .env et redémarrez, il remplace l'ancien. Tant que la valeur
    du .env ne change pas, un mot de passe modifié dans l'appli est conservé."""
    if not APP_PASSWORD:
        return
    name = normalize_username(APP_USERNAME)
    user = db.scalar(select(User).where(User.username == name))
    if user is None:
        db.add(User(username=name, password_hash=hash_password(APP_PASSWORD), env_fingerprint=hash_password(APP_PASSWORD)))
    elif not verify_password(APP_PASSWORD, user.env_fingerprint or ""):
        user.password_hash = hash_password(APP_PASSWORD)
        user.env_fingerprint = hash_password(APP_PASSWORD)
    db.commit()


def has_users(db: Session) -> bool:
    return bool(db.scalar(select(func.count(User.id))))
