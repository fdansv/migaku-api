"""Read optional local login credentials without exposing their contents."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path


class CredentialsError(Exception):
    pass


@dataclass(repr=False)
class Credentials:
    email: str
    password: str


def load_credentials(path: Path) -> Credentials | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise CredentialsError("credentials_unreadable") from None
    try:
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise CredentialsError("credentials_permissions")
            raw = stream.read(16_385)
    except OSError:
        raise CredentialsError("credentials_unreadable") from None
    if len(raw) > 16_384:
        raise CredentialsError("credentials_invalid")
    try:
        values = {}
        for line in raw.decode("utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, separator, value = line.partition("=")
            key = key.strip().lower()
            if not separator or key not in {"username", "email", "password"} or key in values:
                raise CredentialsError("credentials_invalid")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            values[key] = value
        email = values.get("email") or values.get("username")
        password = values.get("password")
        if not email or not password or ("username" in values and "email" in values):
            raise CredentialsError("credentials_invalid")
        return Credentials(email=email, password=password)
    except UnicodeError:
        raise CredentialsError("credentials_invalid") from None
