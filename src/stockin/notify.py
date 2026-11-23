"""Telegram: send the alerts in the outbox to your phone. Outbound only.

Stockin talks to Telegram's Bot API over HTTPS and never listens on a port.
The bot can only send you messages; nothing a message says can reach your
holdings, and Stockin places no orders anywhere.

Where the bot's token lives (it is a password: anyone with it can post as
your bot):
- $STOCKIN_TELEGRAM_TOKEN and $STOCKIN_TELEGRAM_CHAT, if both are set;
- else telegram.json in the data folder, written by `stockin alerts
  connect` with owner-only permissions (0600), never in the repository,
  never in a database, never printed. Errors say "the bot token", not it.

Delivery is at least once: an alert is marked sent only after Telegram
accepts it, so a failure leaves it pending for the next run, and one that
Telegram accepted is never sent again, even by two runs at once (each
message is sent under alerts.db's write lock). (A crash between Telegram accepting
a message and the mark is the one case that sends it twice; Telegram has
no way to refuse a repeat.)
"""

from __future__ import annotations

import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from .alerts import FROM_PRICE, AlertStore, Sent, text
from .facts import Isin
from .money import Value

API = "https://api.telegram.org"
_TOKEN = re.compile(r"^[0-9]{5,15}:[A-Za-z0-9_-]{30,60}$")
"""What @BotFather hands out: the bot's number, a colon, a secret."""


class TelegramError(Exception):
    """Telegram could not be reached or said no. The message never holds the token."""


class Bot(Value):
    token: str = Field(repr=False)
    chat: int

    def __str__(self) -> str:  # never the token
        return f"bot {self.token.split(':', 1)[0]} to chat {self.chat}"


def parse_token(raw: str) -> str:
    token = raw.strip()
    if not _TOKEN.match(token):
        raise ValueError(
            "that does not look like a bot token (digits, a colon, then about 35 letters)"
        )
    return token


def config_path(data: Path) -> Path:
    return data / "telegram.json"


def load(data: Path) -> Bot | None:
    """The configured bot, or None. Raises ValueError (with no token in it)
    when the environment or telegram.json is set but unusable."""
    env_token = os.environ.get("STOCKIN_TELEGRAM_TOKEN", "")
    env_chat = os.environ.get("STOCKIN_TELEGRAM_CHAT", "")
    if env_token or env_chat:
        if not (env_token and env_chat):
            raise ValueError(
                "set both STOCKIN_TELEGRAM_TOKEN and STOCKIN_TELEGRAM_CHAT, or neither"
            )
        if not re.fullmatch(r"-?[0-9]{1,20}", env_chat.strip()):
            raise ValueError("STOCKIN_TELEGRAM_CHAT is not a chat number")
        return Bot(token=parse_token(env_token), chat=int(env_chat.strip()))
    path = config_path(data)
    if not path.exists():
        return None
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
        return Bot(token=parse_token(str(raw["token"])), chat=raw["chat"])
    except (OSError, ValueError, KeyError, TypeError, ValidationError):
        raise ValueError(f"{path} is damaged; run `stockin alerts connect` again") from None


def save(data: Path, bot: Bot) -> Path:
    """Write telegram.json with owner-only permissions, replacing any old one."""
    path = config_path(data)
    data.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"token": bot.token, "chat": bot.chat}, f)
    os.replace(tmp, path)
    return path


class _Loose(BaseModel):
    """Telegram's replies carry many fields; read only the ones used."""

    model_config = ConfigDict(extra="ignore")


class _Chat(_Loose):
    id: int
    type: str


class _Message(_Loose):
    chat: _Chat


class _Update(_Loose):
    message: _Message | None = None


class Reply(_Loose):
    ok: bool
    description: str = "no reason given"
    result: Any = None


Transport = Callable[[str, dict[str, str]], Reply]
"""(method, params) -> Telegram's reply, already checked ok. Tests pass a fake."""


def api_base() -> str:
    """Telegram, unless STOCKIN_TELEGRAM_API points at a server on this
    computer (used by the verify skill's fake Telegram). Anything else is
    refused, so the token is never sent to another host."""
    base = os.environ.get("STOCKIN_TELEGRAM_API", "").rstrip("/")
    if not base:
        return API
    host = urllib.parse.urlsplit(base).hostname
    if base.startswith("http://") and host in ("127.0.0.1", "localhost"):
        return base
    raise ValueError("STOCKIN_TELEGRAM_API may only point at http://127.0.0.1")


def http_transport(token: str, timeout: float = 20) -> Transport:
    base = api_base()

    def call(method: str, params: dict[str, str]) -> Reply:
        body = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(f"{base}/bot{token}/{method}", data=body, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except (OSError, http.client.HTTPException):
                raise TelegramError(f"Telegram answered HTTP {exc.code}") from None
        except (OSError, http.client.HTTPException, ValueError) as exc:
            reason = str(getattr(exc, "reason", "") or type(exc).__name__)
            hidden = reason.replace(token, "***")
            raise TelegramError(f"could not reach Telegram ({hidden})") from None
        try:
            reply = Reply.model_validate_json(raw)
        except ValidationError:
            raise TelegramError("Telegram's answer could not be read") from None
        if not reply.ok:
            raise TelegramError(f"Telegram said: {reply.description.replace(token, '***')}")
        return reply

    return call


def send_one(call: Transport, chat: int, message: str) -> None:
    call("sendMessage", {"chat_id": str(chat), "text": message})


def deliver(
    store: AlertStore,
    call: Transport,
    chat: int,
    now: Callable[[], datetime],
    unsure: Iterable[Isin] = (),
) -> tuple[tuple[Sent, ...], TelegramError | None]:
    """Send every pending alert, oldest first, marking each sent as soon as
    Telegram accepts it. Stops at the first failure, which is returned, so
    the rest stay pending in order.

    `unsure` are the holdings whose price is in doubt now (Verdict.unsure):
    their add and trim alerts are held back, still pending, so nothing
    worked out from a price Stockin is not sure of reaches the phone even
    when no check has run since the doubt arose (#39 verify-3 R-1). Each
    alert is sent under alerts.db's write lock and only if it is still
    pending then, so one a check withdrew meanwhile is not sent."""
    doubt = frozenset(unsure)
    done: list[Sent] = []
    for s in store.outbox(pending_only=True):
        if s.alert.call in FROM_PRICE and s.alert.isin in doubt:
            continue
        try:
            with store.sending(s.id) as mark:
                if mark is None:  # sent by another run, or withdrawn
                    continue
                send_one(call, chat, text(s.alert))
                when = now()
                mark(when)
        except TelegramError as exc:
            return tuple(done), exc
        done.append(s.model_copy(update={"sent": when}))
    return tuple(done), None


def find_chat(call: Transport) -> int | None:
    """The chat of the newest private message sent to the bot, if any."""
    reply = call("getUpdates", {"allowed_updates": '["message"]'})
    try:
        updates = TypeAdapter(list[_Update]).validate_python(reply.result or [])
    except ValidationError:
        raise TelegramError("Telegram's list of messages could not be read") from None
    chats = [u.message.chat.id for u in updates if u.message and u.message.chat.type == "private"]
    return chats[-1] if chats else None
