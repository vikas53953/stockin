"""A fake Telegram Bot API on 127.0.0.1 for verifying `stockin alerts`.

    python3 .claude/skills/verify/fake_telegram.py PORT LOG.jsonl [--chat ID] [--fail-after N]

Point Stockin at it with STOCKIN_TELEGRAM_API=http://127.0.0.1:PORT. Any
token of the right shape is accepted. getUpdates returns one private message
from chat ID (default 555). Every sendMessage is appended to LOG.jsonl, and
after N messages each further send answers 429. Never use a real bot token
in a verification run.
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

ap = argparse.ArgumentParser()
ap.add_argument("port", type=int)
ap.add_argument("log")
ap.add_argument("--chat", type=int, default=555)
ap.add_argument("--fail-after", type=int, default=None)
args = ap.parse_args()
sent = 0


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        global sent
        n = int(self.headers.get("Content-Length", "0"))
        params = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
        m = re.fullmatch(r"/bot[0-9]{5,15}:[A-Za-z0-9_-]{30,60}/(\w+)", self.path)
        if m is None:
            return self.reply(401, {"ok": False, "description": "Unauthorized"})
        if m[1] == "getUpdates":
            msg = {"chat": {"id": args.chat, "type": "private"}, "text": "hi"}
            return self.reply(200, {"ok": True, "result": [{"update_id": 1, "message": msg}]})
        if args.fail_after is not None and sent >= args.fail_after:
            return self.reply(429, {"ok": False, "description": "Too Many Requests"})
        sent += 1
        with open(args.log, "a", encoding="utf-8") as f:
            f.write(json.dumps(params, ensure_ascii=False) + "\n")
        return self.reply(200, {"ok": True, "result": {"message_id": sent}})

    def reply(self, code: int, body: dict[str, object]) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *a: object) -> None:
        pass


ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
