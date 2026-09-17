from cryptography.fernet import Fernet

from .config import config


def _fernet() -> Fernet:
    if not config.secret_key:
        raise RuntimeError("SECRET_KEY не задан в .env")
    return Fernet(config.secret_key.encode())


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
