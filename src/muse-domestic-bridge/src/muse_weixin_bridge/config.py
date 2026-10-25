# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat


class ConfigurationError(RuntimeError):
    """A safe error that never contains configuration values."""


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(path, 0o700)


def write_private_json(path: Path, value: dict) -> None:
    private_directory(path.parent)
    temporary = path.with_name(path.name + ".new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        # Preserve any previous credential file on failure.
        raise ConfigurationError("private_configuration_write_failed") from None


@dataclass(frozen=True)
class Config:
    path: Path
    app_id: str
    secrets_file: Path
    data_dir: Path
    history_limit: int = 500
    max_message_chars: int = 20000
    outbound_chunk_chars: int = 2500

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        path = Path(path).resolve()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            base = path.parent
            resolve = lambda value: (base / value).resolve()
            config = cls(
                path=path, app_id=str(raw.get("app_id", "")),
                secrets_file=resolve(raw.get("secrets_file", "secrets.json")),
                data_dir=resolve(raw.get("data_dir", "state")),
                history_limit=int(raw.get("history_limit", 500)),
                max_message_chars=int(raw.get("max_message_chars", 20000)),
                outbound_chunk_chars=int(raw.get("outbound_chunk_chars", 2500)),
            )
        except (OSError, ValueError, TypeError, KeyError):
            raise ConfigurationError("configuration_missing_or_invalid") from None
        if not 20 <= config.history_limit <= 1000 or not 100 <= config.outbound_chunk_chars <= 3000 or not 1 <= config.max_message_chars <= 20000:
            raise ConfigurationError("configuration_limits_invalid")
        return config


class Secrets:
    def __init__(self, path: Path):
        self.path = path

    def read(self) -> dict:
        try:
            if os.name != "nt" and stat.S_IMODE(self.path.stat().st_mode) & 0o077:
                raise ConfigurationError("secrets_permissions_must_be_0600")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except ConfigurationError:
            raise
        except (OSError, ValueError):
            raise ConfigurationError("secrets_missing_or_invalid") from None

    def cookie(self) -> str:
        cookie = self.read().get("muse_cookie", "")
        if not isinstance(cookie, str) or "hatch_sess=" not in cookie:
            raise ConfigurationError("muse_cookie_not_configured")
        return cookie

    def app_secret(self) -> str:
        value = self.read().get("feishu_app_secret", "")
        if not isinstance(value, str) or not value:
            raise ConfigurationError("feishu_secret_not_configured")
        return value

