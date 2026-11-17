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


def test_doctor_when_data_path_is_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "afile"
    target.write_text("")
    monkeypatch.setenv("STOCKIN_DATA", str(target))
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1
    checks = {c["name"]: c for c in json.loads(result.output)["checks"]}
    assert "is a file" in checks["data_dir"]["detail"]


def test_version_json() -> None:
    result = runner.invoke(app, ["version", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["version"]
