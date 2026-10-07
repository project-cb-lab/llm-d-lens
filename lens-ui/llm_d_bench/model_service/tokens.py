"""User model-access-token lifecycle: issue, list, reset, revoke, validate.

Reuses the auth domain's token primitives (``generate_session_token`` /
``hash_session_token``) so model tokens and login sessions share one random/hash
implementation, while living in a separate table and namespace.

Design reference: docs/design/model-service-v2-design.md section 5.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from llm_d_bench.auth.security import generate_session_token, hash_session_token
from llm_d_bench.db.dao.model_access_token import ModelAccessTokenDao
from llm_d_bench.model_service.contracts import TOKEN_PREFIX, ModelAccessToken, utcnow

#: Re-touch ``last_used_at`` at most this often, to avoid a write per request.
LAST_USED_TOUCH_SECONDS = 60


def generate_model_token() -> str:
    """Generate a new opaque ``lens-mk-…`` plaintext token (43-char body)."""
    return f"{TOKEN_PREFIX}{generate_session_token()}"


def hash_model_token(raw_token: str) -> str:
    return hash_session_token(raw_token)


def token_hint(raw_token: str) -> str:
    """Short, non-secret fingerprint: the last 4 characters of the token."""
    return raw_token[-4:]


class ModelTokenService:
    def __init__(self, dao: ModelAccessTokenDao | None = None) -> None:
        self._dao = dao or ModelAccessTokenDao()

    def list_for_user(self, user_id: str, *, include_revoked: bool = True) -> list[ModelAccessToken]:
        if include_revoked:
            return self._dao.list_for_user(user_id)
        return self._dao.list_for_user(user_id, status="active")

    def get(self, token_id: str) -> ModelAccessToken | None:
        return self._dao.get(token_id)

    def create(self, user_id: str, name: str = "default") -> tuple[ModelAccessToken, str]:
        """Issue a new token; returns the record and the one-time plaintext."""
        raw = generate_model_token()
        token = ModelAccessToken(
            user_id=user_id,
            name=name,
            token_hash=hash_model_token(raw),
            token_hint=token_hint(raw),
        )
        return self._dao.create(token), raw

    def regenerate(
        self, user_id: str, token_id: str | None = None, name: str = "default"
    ) -> tuple[ModelAccessToken, str]:
        """Revoke the user's active token(s) and issue a fresh one.

        With ``token_id`` only that token is revoked; otherwise every active
        token is revoked ("one primary token per user").
        """
        now = utcnow()
        targets = self._dao.list_for_user(user_id, status="active")
        if token_id is not None:
            targets = [token for token in targets if token.id == token_id]
        for token in targets:
            token.status = "revoked"
            token.revoked_at = now
            self._dao.save(token)
        return self.create(user_id, name=name)

    def revoke(self, user_id: str, token_id: str) -> bool:
        token = self._dao.get(token_id)
        if token is None or token.user_id != user_id:
            return False
        if token.status == "revoked":
            return True
        token.status = "revoked"
        token.revoked_at = utcnow()
        self._dao.save(token)
        return True

    def delete(self, user_id: str, token_id: str) -> bool:
        token = self._dao.get(token_id)
        if token is None or token.user_id != user_id:
            return False
        self._dao.delete(token_id)
        return True

    def validate(self, raw_token: str, *, ip: str | None = None) -> ModelAccessToken | None:
        """Return the active token for ``raw_token``, or ``None`` if unusable."""
        if not raw_token:
            return None
        token = self._dao.get_by_hash(hash_model_token(raw_token))
        if token is None or token.status != "active":
            return None
        now = utcnow()
        if token.expires_at is not None and now >= token.expires_at:
            return None
        self._touch(token, now=now, ip=ip)
        return token

    def _touch(self, token: ModelAccessToken, *, now: datetime, ip: str | None) -> None:
        stale = token.last_used_at is None or (now - token.last_used_at) > timedelta(seconds=LAST_USED_TOUCH_SECONDS)
        if not stale:
            return
        token.last_used_at = now
        if ip:
            token.last_used_ip = ip
        self._dao.save(token)
