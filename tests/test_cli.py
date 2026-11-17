import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stockin.cli import app

runner = CliRunner()


def test_doctor_fresh_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path))
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    statuses = {c["name"]: c["status"] for c in report["checks"]}
    assert statuses == {"python": "ok", "data_dir": "ok", "ledger": "warn", "quotes": "warn"}
    assert list(tmp_path.iterdir()) == []  # doctor is read-only


def test_doctor_missing_data_dir_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path / "absent"))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "FAIL" in result.output
    assert not (tmp_path / "absent").exists()
