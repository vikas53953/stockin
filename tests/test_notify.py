"""Telegram delivery: at-least-once and never twice once accepted (as a
property), the token never shown or sent elsewhere, and the CLI against a
fake Telegram on 127.0.0.1."""

from __future__ import annotations

import json
import os
import stat
import threading
from collections.abc import Generator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs

import pytest
from hypothesis import given
from hypothesis import strategies as st
from typer.testing import CliRunner

from stockin import notify
from stockin.alerts import AlertStore
from stockin.cli import app as cli
from stockin.facts import Isin, parse_isin
from stockin.notify import Bot, Reply, TelegramError, deliver
from stockin.thesis import Call, Verdict

FIX = Path(__file__).parent / "fixtures"
INFY = parse_isin("INE009A01021")
ISINS = [parse_isin(i) for i in ("INE009A01021", "INF179K01BE2", "INE002A01018", "INE040A01034")]
TOKEN = "123456789:AAH" + "x" * 32
T0 = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
runner = CliRunner()


def v(isin: Isin, call: Call) -> Verdict:
    return Verdict(
        isin=isin, name=str(isin), call=call, because=f"{isin} {call}", weight=None, target=None
    )


# ------------------------------------------------------------ the property


@given(
    st.lists(
        st.lists(st.sampled_from(["add", "trim", "exit", "hold"]), min_size=4, max_size=4),
        max_size=6,
    ),
    st.lists(st.booleans(), max_size=60),
)
def test_every_alert_arrives_once_in_order_however_sends_fail(
    checks: list[list[Call]], fails: list[bool]
) -> None:
    """Checks and sends interleave; any send may fail (before Telegram
    accepts it). After the network recovers, Telegram has received every
    alert exactly once, in the order they were made."""
    got: list[str] = []
    plan = iter(fails)

    def flaky(method: str, params: dict[str, str]) -> Reply:
        if next(plan, False):
            raise TelegramError("network down")
        got.append(params["text"])
        return Reply(ok=True)

    def ok(method: str, params: dict[str, str]) -> Reply:
        got.append(params["text"])
        return Reply(ok=True)

    import tempfile

    with tempfile.TemporaryDirectory() as d, AlertStore.open(Path(d) / "alerts.db") as store:
        for calls in checks:
            store.record([v(i, c) for i, c in zip(ISINS, calls, strict=True)], made=T0)
            deliver(store, flaky, 1, lambda: T0)
        _, err = deliver(store, ok, 1, lambda: T0)
        assert err is None
        made = [notify.text(s.alert) for s in store.outbox()]
        assert got == made  # each once, oldest first
        assert store.outbox(pending_only=True) == ()


def test_a_failure_stops_the_run_and_keeps_the_rest_pending(tmp_path: Path) -> None:
    sent: list[str] = []

    def second_fails(method: str, params: dict[str, str]) -> Reply:
        if len(sent) == 1:
            raise TelegramError("down")
        sent.append(params["text"])
        return Reply(ok=True)

    with AlertStore.open(tmp_path / "alerts.db") as store:
        store.record([v(i, "trim") for i in ISINS[:3]], made=T0)
        done, err = deliver(store, second_fails, 1, lambda: T0)
        assert len(done) == 1 and isinstance(err, TelegramError)
        assert [s.alert.isin for s in store.outbox(pending_only=True)] == sorted(ISINS[:3])[1:]


# ------------------------------------------------------------ config


def test_token_is_checked_and_never_printed(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        notify.parse_token("not a token")
    bot = Bot(token=TOKEN, chat=42)
    assert TOKEN not in str(bot) and TOKEN not in repr(bot)
    path = notify.save(tmp_path, bot)
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert notify.load(tmp_path) == bot
    notify.save(tmp_path, Bot(token=TOKEN, chat=7))  # replaces
    assert notify.load(tmp_path) == Bot(token=TOKEN, chat=7)


@pytest.mark.parametrize(
    "body",
    ["{", '{"token": "x", "chat": 1}', '{"chat": 1}', f'{{"token": "{TOKEN}", "chat": "1"}}'],
)
def test_a_damaged_config_is_said_without_the_token(tmp_path: Path, body: str) -> None:
    notify.config_path(tmp_path).write_text(body)
    with pytest.raises(ValueError, match="alerts connect") as e:
        notify.load(tmp_path)
    assert TOKEN not in str(e.value)


def test_environment_wins_and_must_be_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notify.save(tmp_path, Bot(token=TOKEN, chat=7))
    monkeypatch.setenv("STOCKIN_TELEGRAM_TOKEN", TOKEN)
    with pytest.raises(ValueError, match="both"):
        notify.load(tmp_path)
    monkeypatch.setenv("STOCKIN_TELEGRAM_CHAT", "-100200")
    assert notify.load(tmp_path) == Bot(token=TOKEN, chat=-100200)
    monkeypatch.setenv("STOCKIN_TELEGRAM_CHAT", "me")
    with pytest.raises(ValueError, match="chat number"):
        notify.load(tmp_path)


@pytest.mark.parametrize(
    "base",
    [
        "https://evil.example",
        "http://10.0.0.5:9",
        "http://127.0.0.1.evil.example",
        "ftp://127.0.0.1",
    ],
)
def test_the_token_only_goes_to_telegram_or_this_computer(
    monkeypatch: pytest.MonkeyPatch, base: str
) -> None:
    monkeypatch.setenv("STOCKIN_TELEGRAM_API", base)
    with pytest.raises(ValueError, match=r"127\.0\.0\.1"):
        notify.http_transport(TOKEN)


def test_default_is_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STOCKIN_TELEGRAM_API", raising=False)
    assert notify.api_base() == "https://api.telegram.org"


# ------------------------------------------------------------ fake Telegram


class Fake(BaseHTTPRequestHandler):
    sent: ClassVar[list[dict[str, Any]]] = []
    fail_after: ClassVar[int | None] = None
    updates: ClassVar[list[dict[str, Any]]] = []
    raw: ClassVar[bytes | None] = None

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        params = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        _, bot, method = self.path.split("/", 2)
        if bot != f"bot{TOKEN}":
            return self._reply(401, {"ok": False, "description": f"Unauthorized {bot}"})
        if Fake.raw is not None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(Fake.raw)
            return None
        if method == "getUpdates":
            return self._reply(200, {"ok": True, "result": Fake.updates})
        if Fake.fail_after is not None and len(Fake.sent) >= Fake.fail_after:
            return self._reply(429, {"ok": False, "description": "Too Many Requests"})
        Fake.sent.append(params)
        return self._reply(200, {"ok": True, "result": {"message_id": len(Fake.sent)}})

    def _reply(self, code: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch) -> Generator[type[Fake]]:
    Fake.sent, Fake.fail_after, Fake.updates, Fake.raw = [], None, [], None
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("STOCKIN_TELEGRAM_API", f"http://127.0.0.1:{server.server_port}")
    yield Fake
    server.shutdown()


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path))
    monkeypatch.delenv("STOCKIN_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("STOCKIN_TELEGRAM_CHAT", raising=False)
    for name in ("mf-2026-01.json", "demat-2026-02.json"):
        assert runner.invoke(cli, ["import", "cas", str(FIX / "cas" / name)]).exit_code == 0
    r = runner.invoke(cli, ["prices", "import", str(FIX / "eod" / "bhavcopy-sample.csv")])
    assert r.exit_code in (0, 1), r.output
    r = runner.invoke(cli, ["thesis", "set", INFY, "--why", "cash machine", "--target", "30"])
    assert r.exit_code == 0, r.output
    return tmp_path


def _private(chat: int) -> dict[str, Any]:
    return {"update_id": 1, "message": {"chat": {"id": chat, "type": "private"}, "text": "hi"}}


def test_cli_connect_finds_your_chat_and_saves_privately(data: Path, telegram: type[Fake]) -> None:
    telegram.updates = [
        _private(555),
        {"update_id": 2, "message": {"chat": {"id": -9, "type": "group"}}},
    ]
    r = runner.invoke(cli, ["alerts", "connect"], input=f"{TOKEN}\n\n")
    assert r.exit_code == 0, r.output
    assert TOKEN not in r.output and "chat 555" in r.output
    assert telegram.sent[0]["chat_id"] == "555"
    assert notify.load(data) == Bot(token=TOKEN, chat=555)
    doc = runner.invoke(cli, ["doctor", "--json"]).output
    assert TOKEN not in doc and '"connected: bot 123456789 to chat 555"' in doc


def test_connect_takes_the_newest_private_chat(data: Path, telegram: type[Fake]) -> None:
    telegram.updates = [_private(111), _private(222)]
    r = runner.invoke(cli, ["alerts", "connect"], input=f"{TOKEN}\n\n")
    assert r.exit_code == 0 and notify.load(data) == Bot(token=TOKEN, chat=222)


def test_cli_connect_without_a_message_saves_nothing(data: Path, telegram: type[Fake]) -> None:
    r = runner.invoke(cli, ["alerts", "connect"], input=f"{TOKEN}\n\n")
    assert r.exit_code == 2 and "no message from you yet" in r.output
    assert not notify.config_path(data).exists()


def test_cli_connect_with_a_wrong_token_says_so_without_it(
    data: Path, telegram: type[Fake]
) -> None:
    wrong = "987654321:BBB" + "y" * 32
    r = runner.invoke(cli, ["alerts", "connect"], input=f"{wrong}\n\n")
    assert r.exit_code == 2 and "Unauthorized" in r.output
    assert wrong not in r.output and "***" in r.output


def test_cli_check_send_delivers_once(data: Path, telegram: type[Fake]) -> None:
    notify.save(data, Bot(token=TOKEN, chat=555))
    r = runner.invoke(cli, ["alerts", "check", "--send"])
    assert r.exit_code == 0, r.output
    assert "sent 1 alert to Telegram" in r.output
    (msg,) = telegram.sent
    assert msg["chat_id"] == "555" and msg["text"].startswith("TRIM INFOSYS\nINFOSYS is 100.0%")
    r = runner.invoke(cli, ["alerts", "check", "--send"])
    assert r.exit_code == 0 and "sent 0 alerts" in r.output and len(telegram.sent) == 1
    assert "[sent]" in runner.invoke(cli, ["alerts", "list"]).output


def test_cli_check_json_send_keeps_stdout_json(data: Path, telegram: type[Fake]) -> None:
    notify.save(data, Bot(token=TOKEN, chat=555))
    r = runner.invoke(cli, ["alerts", "check", "--json", "--send"])
    assert r.exit_code == 0
    assert len(json.loads(r.stdout)["alerts"]) == 1


def test_cli_send_failure_exits_1_and_retries_later(data: Path, telegram: type[Fake]) -> None:
    notify.save(data, Bot(token=TOKEN, chat=555))
    assert runner.invoke(cli, ["alerts", "check"]).exit_code == 0
    telegram.fail_after = 0
    r = runner.invoke(cli, ["alerts", "send"])
    assert r.exit_code == 1 and "Too Many Requests" in r.output and "1 still pending" in r.output
    telegram.fail_after = None
    r = runner.invoke(cli, ["alerts", "send"])
    assert r.exit_code == 0 and len(telegram.sent) == 1


def test_cli_unreadable_answer_is_said(data: Path, telegram: type[Fake]) -> None:
    notify.save(data, Bot(token=TOKEN, chat=555))
    telegram.raw = b"<html>proxy page</html>"
    r = runner.invoke(cli, ["alerts", "test"])
    assert r.exit_code == 2 and "could not be read" in r.output


def test_cli_unreachable_telegram_is_said_without_the_token(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notify.save(data, Bot(token=TOKEN, chat=555))
    monkeypatch.setenv("STOCKIN_TELEGRAM_API", "http://127.0.0.1:9")  # nothing listens
    r = runner.invoke(cli, ["alerts", "test"])
    assert r.exit_code == 2 and "could not reach Telegram" in r.output
    assert TOKEN not in r.output


def test_cli_send_before_connect_says_how(data: Path) -> None:
    r = runner.invoke(cli, ["alerts", "send"])
    assert r.exit_code == 2 and "stockin alerts connect" in r.output
    doc = json.loads(runner.invoke(cli, ["doctor", "--json"]).output)
    (tg,) = [c for c in doc["checks"] if c["name"] == "telegram"]
    assert tg["status"] == "warn"


# ------------------------------------------------------------ #32 verifier notes


def test_two_senders_at_once_send_each_alert_once(tmp_path: Path) -> None:
    """R-1: each message goes out under alerts.db's write lock, so a second
    sender waits, then sees it is no longer pending."""
    import time

    path = tmp_path / "alerts.db"
    with AlertStore.open(path) as store:
        store.record([v(INFY, "trim")], made=T0)
    got: list[str] = []
    lock = threading.Lock()

    def slow(method: str, params: dict[str, str]) -> Reply:
        time.sleep(0.4)
        with lock:
            got.append(params["text"])
        return Reply(ok=True)

    def run() -> None:
        with AlertStore.open(path) as s:
            deliver(s, slow, 1, lambda: T0)

    threads = [threading.Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(got) == 1
    with AlertStore.open(path) as store:
        (kept,) = store.outbox()
        assert kept.sent == T0


def test_a_check_during_a_send_does_not_withdraw_what_went_out(tmp_path: Path) -> None:
    """R-2: a check that would withdraw the alert waits for the send to
    finish; the sent alert stays in the history."""
    path = tmp_path / "alerts.db"
    with AlertStore.open(path) as store:
        store.record([v(INFY, "trim")], made=T0)
    checker: list[threading.Thread] = []

    def during(method: str, params: dict[str, str]) -> Reply:
        def check() -> None:
            with AlertStore.open(path) as s:
                s.record([v(INFY, "hold")], made=T0)

        t = threading.Thread(target=check)
        t.start()
        checker.append(t)
        t.join(0.5)  # the check is blocked by the send's lock
        assert t.is_alive()
        return Reply(ok=True)

    with AlertStore.open(path) as store:
        done, err = deliver(store, during, 1, lambda: T0)
    checker[0].join()
    assert err is None and len(done) == 1
    with AlertStore.open(path) as store:
        (kept,) = store.outbox()
        assert kept.sent == T0 and store.calls() == {INFY: "hold"}


def test_a_config_others_can_read_is_a_doctor_warning(data: Path) -> None:
    """C-1."""
    if os.name != "posix":
        pytest.skip("POSIX file modes")
    path = notify.save(data, Bot(token=TOKEN, chat=555))
    assert not notify.others_can_read(data)
    path.chmod(0o644)
    doc = json.loads(runner.invoke(cli, ["doctor", "--json"]).output)
    (tg,) = [c for c in doc["checks"] if c["name"] == "telegram"]
    assert tg["status"] == "warn" and "chmod 600" in tg["hint"] and TOKEN not in str(tg)


def test_a_dangling_config_link_is_damage_not_absence(tmp_path: Path) -> None:
    """C-2: the same answer alerts.db gives."""
    notify.config_path(tmp_path).symlink_to(tmp_path / "gone.json")
    with pytest.raises(ValueError, match="not there"):
        notify.load(tmp_path)


def test_the_refusal_names_both_local_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """C-3."""
    monkeypatch.setenv("STOCKIN_TELEGRAM_API", "https://evil.example")
    with pytest.raises(ValueError, match="localhost"):
        notify.api_base()


def test_a_network_error_that_names_the_url_hides_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Author survivor T3: if an error ever carries the URL, the token in it
    is replaced."""
    import urllib.error
    import urllib.request

    def fails(req: urllib.request.Request, timeout: float) -> object:
        raise urllib.error.URLError(f"cannot open {req.full_url}")

    monkeypatch.delenv("STOCKIN_TELEGRAM_API", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", fails)
    with pytest.raises(TelegramError) as e:
        notify.send_one(notify.http_transport(TOKEN), 1, "hi")
    assert TOKEN not in str(e.value) and "***" in str(e.value)
