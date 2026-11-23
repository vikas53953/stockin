"""Telegram delivery: at-least-once and never twice once accepted (as a
property), the token never shown or sent elsewhere, and the CLI against a
fake Telegram on 127.0.0.1."""

from __future__ import annotations

import json
import os
import stat
import threading
from collections.abc import Generator
from datetime import UTC, date, datetime, timedelta, timezone
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


# ------------------------------------------------ a price in doubt (#39 round 3)
# An add or trim kept before a price was held (or turned 8 days old), and not
# yet sent, must not go out while that price is in doubt: the true call may
# be hold. EXIT on a broken thesis does not depend on a price and still goes.

_IST = timezone(timedelta(hours=5, minutes=30))
_NIFTYBEES = parse_isin("INF204KB14I2")
_FRI_TRIM = (
    "INFOSYS is 61.0% of your priced money, more than 5.0 points above your target of 50.0%."
)


def _at(monkeypatch: pytest.MonkeyPatch, when: str) -> None:
    at = datetime.fromisoformat(when).replace(tzinfo=_IST)
    monkeypatch.setattr("stockin.prices._now", lambda: at)


def _eod(tmp: Path, name: str, day: str, infy: str = "1500.55") -> Path:
    """The repo's bhavcopy sample moved to `day`, INFY at `infy` (synthetic)."""
    text = (FIX / "eod" / "bhavcopy-sample.csv").read_text()
    path = tmp / name
    path.write_text(
        text.replace("2026-11-14", day).replace(
            "1500.55,1500.55,1,,1500.55", f"{infy},{infy},1,,{infy}"
        )
    )
    return path


@pytest.fixture
def friday(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """#39 verify-3 flowP, value for value: INFY 12 and NIFTYBEES 40 with
    Friday 2026-11-13's closes (INFY ₹1,500.55, 61.0% of priced money), a
    50% target, and one TRIM kept at 20:00 but not sent."""
    import cas_fixtures as fx

    data = tmp_path / "data"
    monkeypatch.setenv("STOCKIN_DATA", str(data))
    monkeypatch.delenv("STOCKIN_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("STOCKIN_TELEGRAM_CHAT", raising=False)
    _at(monkeypatch, "2026-11-13T20:00:00")
    demat = fx.write(
        fx.demat("01-Feb-2026", "28-Feb-2026", {fx.INFY: "12", _NIFTYBEES: "40"}),
        tmp_path / "demat-etf-synthetic.json",
    )
    for path in (FIX / "cas" / "mf-2026-01.json", demat):
        r = runner.invoke(cli, ["import", "cas", str(path)])
        assert r.exit_code == 0, r.output
    nav = tmp_path / "NAVAll-2026-11-13-synthetic.txt"
    nav.write_text(
        (FIX / "eod" / "NAVAll-sample.txt").read_text().replace("14-Nov-2026", "13-Nov-2026")
    )
    for path in (_eod(tmp_path, "bhavcopy-2026-11-13.csv", "2026-11-13"), nav):
        r = runner.invoke(cli, ["prices", "import", str(path)])
        assert r.exit_code == 0, r.output
    r = runner.invoke(
        cli, ["thesis", "set", INFY, "--why", "synthetic: cash machine", "--target", "50"]
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli, ["alerts", "check"])
    assert r.exit_code == 0 and f"TRIM  {_FRI_TRIM}  [pending]" in r.output, r.output
    notify.save(data, Bot(token=TOKEN, chat=555))
    return tmp_path


def _monday_crash_is_held(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mon 2026-11-16 20:00: a real fall to ₹1,125.00 (-25%) is held."""
    _at(monkeypatch, "2026-11-16T20:00:00")
    crash = _eod(tmp, "bhavcopy-crash-2026-11-16-synthetic.csv", "2026-11-16", "1125.00")
    r = runner.invoke(cli, ["prices", "import", str(crash)])
    assert r.exit_code == 1 and "this file says 1,125.00 on 2026-11-16" in r.output, r.output
    r = runner.invoke(cli, ["verdicts"])
    assert r.output.startswith("REVIEW INFOSYS's newest price, ₹1,125.00 on 2026-11-16, was not")


@pytest.mark.parametrize("tuesday", [["alerts", "check", "--send"], ["alerts", "send"]])
def test_a_trim_kept_before_a_price_was_held_is_not_sent_while_it_is_held(
    friday: Path, telegram: type[Fake], monkeypatch: pytest.MonkeyPatch, tuesday: list[str]
) -> None:
    """#39 verify-3 R-1 (53-pending-trim-sent-while-held.txt). Friday's TRIM
    was kept but not sent; Monday's close is held; on Tuesday at 09:20 a
    send must send nothing (at ₹1,125.00 INFY is 54.0%: hold against 50%)."""
    _monday_crash_is_held(friday, monkeypatch)
    _at(monkeypatch, "2026-11-17T09:20:00")
    r = runner.invoke(cli, tuesday)
    assert r.exit_code == 0, r.output
    assert "sent 0 alerts to Telegram" in r.output
    assert telegram.sent == []
    # once the fall is accepted, the true call is hold: nothing ever goes out
    _at(monkeypatch, "2026-11-17T20:00:00")
    crash = friday / "bhavcopy-crash-2026-11-16-synthetic.csv"
    r = runner.invoke(cli, ["prices", "import", str(crash), "--accept-jumps"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli, ["alerts", "check", "--send"])
    assert r.exit_code == 0 and "sent 0 alerts" in r.output, r.output
    assert telegram.sent == []
    assert "TRIM" not in runner.invoke(cli, ["alerts", "list"]).output


def test_after_the_doubt_is_settled_a_true_trim_is_sent_once(
    friday: Path, telegram: type[Fake], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The TRIM withdrawn while in doubt is not lost: the holding's call is
    forgotten with it, so when the price is accepted and the weight is
    still above the band, the TRIM is made again and sent once."""
    _monday_crash_is_held(friday, monkeypatch)
    r = runner.invoke(cli, ["alerts", "check", "--send"])
    assert r.exit_code == 0 and telegram.sent == [], r.output
    _at(monkeypatch, "2026-11-17T20:00:00")
    r = runner.invoke(
        cli, ["thesis", "set", INFY, "--why", "synthetic: cash machine", "--target", "40"]
    )
    assert r.exit_code == 0, r.output
    crash = friday / "bhavcopy-crash-2026-11-16-synthetic.csv"
    assert runner.invoke(cli, ["prices", "import", str(crash), "--accept-jumps"]).exit_code == 0
    for _ in range(2):
        r = runner.invoke(cli, ["alerts", "check", "--send"])
        assert r.exit_code == 0, r.output
    (msg,) = telegram.sent
    assert msg["text"].startswith(
        "TRIM INFOSYS\nINFOSYS is 54.0% of your priced money, more than 5.0 points above "
        "your target of 40.0%."
    )


def test_a_trim_kept_from_a_close_that_turns_8_days_old_is_not_sent(
    friday: Path, telegram: type[Fake], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#39 verify-3 R-1, the same for a price more than 7 days old (India's
    day): at 2026-11-21 00:01 IST Friday's close is 8 days old."""
    _at(monkeypatch, "2026-11-21T00:01:00")
    for args in (["alerts", "send"], ["alerts", "check", "--send"]):
        r = runner.invoke(cli, args)
        assert r.exit_code == 0 and "sent 0 alerts" in r.output, r.output
    assert telegram.sent == []


@pytest.mark.parametrize("broken", [INFY, _NIFTYBEES])
def test_exit_on_a_broken_thesis_is_given_and_sent_while_a_price_is_held(
    friday: Path, telegram: type[Fake], monkeypatch: pytest.MonkeyPatch, broken: Isin
) -> None:
    """#39 verify-3 V6: a broken thesis says exit whatever the price, so a
    held price never hides it, on the held holding or on another one."""
    _monday_crash_is_held(friday, monkeypatch)
    if broken != INFY:
        r = runner.invoke(cli, ["thesis", "set", broken, "--why", "synthetic: the index"])
        assert r.exit_code == 0, r.output
    assert runner.invoke(cli, ["thesis", "broken", broken]).exit_code == 0
    v = json.loads(runner.invoke(cli, ["verdicts", "--json"]).output)["verdicts"]
    calls = {x["isin"]: x["call"] for x in v}
    assert calls[broken] == "exit"
    assert all(c == "review" for i, c in calls.items() if i != broken)
    r = runner.invoke(cli, ["alerts", "check", "--send"])
    assert r.exit_code == 0 and "sent 1 alert to Telegram" in r.output, r.output
    (msg,) = telegram.sent
    assert msg["text"].startswith("EXIT ")


def test_a_check_between_a_senders_list_and_its_send_withdraws_in_time(tmp_path: Path) -> None:
    """#39 verify-3 R-1 across processes: a sender lists the pending TRIM,
    then a check that finds the price in doubt withdraws it, then the
    sender comes to it. It must not go out."""
    path = tmp_path / "alerts.db"
    with AlertStore.open(path) as store:
        store.record([v(INFY, "trim")], made=T0)
    unsure = Verdict(
        isin=INFY,
        name=str(INFY),
        call="review",
        because="in doubt",
        weight=None,
        target=None,
        unsure=True,
    )
    got: list[str] = []

    def ok(method: str, params: dict[str, str]) -> Reply:
        got.append(params["text"])
        return Reply(ok=True)

    with AlertStore.open(path) as store:
        listed = store.outbox(pending_only=True)
        with AlertStore.open(path) as check:
            check.record([unsure], made=T0)
        mp = pytest.MonkeyPatch()
        mp.setattr(store, "outbox", lambda pending_only=False: listed)
        try:
            deliver(store, ok, 1, lambda: T0)
        finally:
            mp.undo()
    assert got == []


def test_a_held_price_the_price_in_use_now_agrees_with_is_not_in_doubt(
    tmp_path: Path,
) -> None:
    """#39 verify-3 C-1: 11-16 and 11-17 split closes both held, then the
    11-16 file accepted. The 11-17 row is still held, but it is within the
    band of the price now in use; it must not say "₹750.30 ... too far
    from ₹750.30"."""
    from stockin.money import Price
    from stockin.prices import PriceFile, PriceStore, Quote

    def f(day: int, price: str) -> PriceFile:
        q = Quote(isin=INFY, on=date(2026, 11, day), price=Price.rupees(price), source="bhavcopy")
        return PriceFile(source="bhavcopy", day=q.on, quotes=(q,))

    with PriceStore.open(tmp_path / "prices.db", write=True) as store:
        store.replace(f(13, "1500.55"))
        assert store.take(f(16, "750.30"))[1] and store.take(f(17, "750.30"))[1]
        store.take(f(16, "750.30"), accept_jumps=True)
        book = store.book([INFY])
    assert book.get(INFY) == f(16, "750.30").quotes[0]
    assert book.doubted(INFY) is None


def test_the_review_sentence_says_what_accept_jumps_accepts() -> None:
    """#39 verify-3 C-6: `prices fetch --accept-jumps` stores every price
    held in today's files, not only the one named; the sentence says so
    and names the narrower way."""
    from stockin.money import Price
    from stockin.prices import PriceBook, Quote
    from stockin.thesis import doubts

    used = Quote(isin=INFY, on=date(2026, 11, 13), price=Price.rupees("1500.55"), source="bhavcopy")
    new = used.model_copy(update={"on": date(2026, 11, 16), "price": Price.rupees("1125.00")})
    (said,) = doubts(
        {INFY: "INFOSYS"}, PriceBook(quotes={INFY: used}, held={INFY: new}), date(2026, 11, 17)
    ).values()
    assert "stockin prices import FILE --accept-jumps" in said
    assert "every price held in today's files" in said


def test_two_senders_at_once_send_each_alert_once(tmp_path: Path) -> None:
    """#32 verifier R-1 (from #39): each message goes out under alerts.db's
    write lock, so a second sender waits, then sees it is no longer pending."""
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
    """#32 verifier R-2 (from #39): a check that would withdraw the alert
    waits for the send to finish; the sent alert stays in the history."""
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
