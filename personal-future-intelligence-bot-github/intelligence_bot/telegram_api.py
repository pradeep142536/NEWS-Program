from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class TelegramError(RuntimeError):
    def __init__(self, message: str, *, error_code: int = 0, retry_after: int | None = None, permanent: bool = False):
        super().__init__(message)
        self.error_code = error_code
        self.retry_after = retry_after
        self.permanent = permanent


class TelegramClient:
    def __init__(self, token: str, timeout: float = 40):
        self.token = token
        self.timeout = timeout
        self.base_url = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(params or {}).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + method,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "PersonalFutureIntelligenceBot/0.1"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                payload = json.loads(response.read(2_000_001))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read(64_000))
            except Exception:
                payload = {}
            raise self._api_error(payload, exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Do not include the request URL: it contains the bot token.
            raise TelegramError(f"Telegram request failed: {type(exc).__name__}") from None
        if not payload.get("ok"):
            raise self._api_error(payload)
        return payload.get("result", {})

    @staticmethod
    def _api_error(payload: dict[str, Any], fallback_code: int = 0) -> TelegramError:
        code = int(payload.get("error_code", fallback_code) or 0)
        parameters = payload.get("parameters") or {}
        retry_after = parameters.get("retry_after")
        message = str(payload.get("description", "Telegram API error"))[:500]
        return TelegramError(message, error_code=code, retry_after=int(retry_after) if retry_after else None, permanent=code in (400, 403))

    def get_me(self) -> dict[str, Any]:
        return self.call("getMe")

    def get_webhook_info(self) -> dict[str, Any]:
        return self.call("getWebhookInfo")

    def get_chat(self, chat_id: str | int) -> dict[str, Any]:
        return self.call("getChat", {"chat_id": chat_id})

    def get_chat_member(self, chat_id: str | int, user_id: int) -> dict[str, Any]:
        return self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id})

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["channel_post"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", params)

    def send_message(self, chat_id: int, text: str, parse_mode: str = "HTML") -> dict[str, Any]:
        return self.call("sendMessage", {
            "chat_id": chat_id,
            "text": text[:4000],
            "parse_mode": parse_mode,
            "link_preview_options": {"is_disabled": True},
        })

    def send_photo(self, chat_id: int, image: bytes, filename: str, caption: str) -> dict[str, Any]:
        boundary = "----PFIBot" + secrets.token_hex(16)
        fields = {"chat_id": str(chat_id), "parse_mode": "HTML", "caption": caption[:1000]}
        chunks: list[bytes] = []
        for key, value in fields.items():
            chunks.extend([
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            ])
        mime = "image/png" if filename.lower().endswith(".png") else "image/gif" if filename.lower().endswith(".gif") else "image/webp" if filename.lower().endswith(".webp") else "image/jpeg"
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {mime}\r\n\r\n".encode(),
            image,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ])
        req = urllib.request.Request(
            self.base_url + "sendPhoto",
            data=b"".join(chunks),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": "PersonalFutureIntelligenceBot/0.1"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                payload = json.loads(response.read(2_000_001))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read(64_000))
            except Exception:
                payload = {}
            raise self._api_error(payload, exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TelegramError(f"Telegram photo request failed: {type(exc).__name__}") from None
        if not payload.get("ok"):
            raise self._api_error(payload)
        return payload.get("result", {})
