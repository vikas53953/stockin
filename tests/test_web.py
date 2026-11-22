"""The local web app, driven through HTTP with synthetic statements."""

from __future__ import annotations

from pathlib import Path

import cas_fixtures as fx
import pytest
from fastapi.testclient import TestClient

from stockin.web import create_app

PORT = 8765
HOST = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "data" / "ledger.db", PORT), base_url=HOST)


def _upload(client: TestClient, path: Path, password: str = "") -> str:
    with path.open("rb") as fh:
        r = client.post(
            "/import",
            files={"statement": (path.name, fh, "application/json")},
            data={"password": password},
        )
    assert r.status_code == 200
    return r.text


def test_empty_ledger_invites_an_import(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "No statements yet" in r.text
    assert 'type="file"' in r.text
    assert "No statements yet" in client.get("/reconcile").text


def test_upload_shows_holdings_and_reconcile(client: TestClient, tmp_path: Path) -> None:
    files = fx.write_all(tmp_path / "in")
    first = _upload(client, files[0])
    assert "5 new facts" in first and "15,000.75" in first

    for f in files[1:]:
        _upload(client, f)
    again = _upload(client, files[0])
    assert "Already imported; nothing changed." in again

    home = client.get("/").text
    assert "INFOSYS LIMITED" in home and "TATA CONSULTANCY" not in home
    assert "13,010.60" in home and "unknown" in home
    assert "✓ reconciled" in home

    rec = client.get("/reconcile").text
    assert "OK: no mismatches" in rec
    assert rec.count('class="status-inferred"') == 4


def test_bad_statement_is_rejected_and_nothing_written(client: TestClient, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    text = _upload(client, bad)
    assert "Not imported" in text and "Nothing was imported" in text
    assert not (tmp_path / "data" / "ledger.db").exists()
    assert not list((tmp_path / "data").glob(".upload-*"))  # temp upload removed


def test_pdf_error_names_the_uploaded_file_not_the_temp_one(
    client: TestClient, tmp_path: Path
) -> None:
    junk = tmp_path / "my-cas.pdf"
    junk.write_bytes(b"%PDF-1.4 not really")
    text = _upload(client, junk, password="s3cret-pw")
    assert "my-cas.pdf" in text and "upload.pdf" not in text
    assert "s3cret-pw" not in text


def test_other_hosts_and_cross_site_posts_are_refused(client: TestClient, tmp_path: Path) -> None:
    assert client.get("/", headers={"host": "evil.example:8765"}).status_code == 403
    (path,) = fx.write_all(tmp_path / "in")[:1]
    with path.open("rb") as fh:
        r = client.post(
            "/import",
            files={"statement": (path.name, fh, "application/json")},
            headers={"origin": "https://evil.example"},
        )
    assert r.status_code == 403
    assert not (tmp_path / "data" / "ledger.db").exists()


def test_no_order_routes_exist(client: TestClient) -> None:
    routes = {getattr(r, "path", "") for r in client.app.routes}  # type: ignore[attr-defined]
    assert routes <= {"/", "/reconcile", "/import", "/thesis/{isin}", "/holding/{isin}", "/static"}
    # The static mount only reads files: it refuses every write.
    assert client.post("/static/ui.css").status_code == 405


def test_an_outdated_ledger_says_how_to_rebuild(tmp_path: Path, client: TestClient) -> None:
    import sqlite3

    from stockin.sources import cas
    from stockin.store import LedgerWriter

    db = tmp_path / "data" / "ledger.db"
    rows = [
        fx.txn("2026-01-25", "DIVIDEND_REINVEST", "1", "80"),
    ]
    data = fx.rta(
        "01-Jan-2026",
        "31-Jan-2026",
        [fx.folio("1/1", "AMC", [fx.scheme(fx.HDFC_MF, "F", "0", "1", rows)])],
    )
    with LedgerWriter.open(db) as w:
        w.ingest(cas.from_casparser(data, "old"))
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    r = client.get("/")
    assert r.status_code == 409
    assert "import your statements again" in r.text
    assert "Traceback" not in r.text
