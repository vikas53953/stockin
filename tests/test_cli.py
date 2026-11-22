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
    assert statuses == {
        "python": "ok",
        "data_dir": "ok",
        "ledger": "warn",
        "quotes": "warn",
        "telegram": "warn",
    }
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


@pytest.mark.parametrize("command", [["insights"], ["insights", "--json"], ["holdings"]])
def test_output_survives_a_console_without_the_rupee_sign(
    tmp_path: Path, command: list[str]
) -> None:
    """Windows gives piped or redirected output the cp1252 code page, which
    has no ₹: `stockin insights` raised UnicodeEncodeError there (the drop 2
    Windows CI run). The installed entry point writes UTF-8 instead."""
    import os
    import subprocess
    import sys

    env = {**os.environ, "STOCKIN_DATA": str(tmp_path), "PYTHONIOENCODING": "cp1252"}
    fix = Path(__file__).parent / "fixtures"
    run = [sys.executable, "-c", "from stockin.cli import main; main()"]
    for name in ("mf-2026-01.json", "demat-2026-02.json"):
        done = subprocess.run(
            [*run, "import", "cas", str(fix / "cas" / name)], env=env, capture_output=True
        )
        assert done.returncode == 0, done.stderr
    subprocess.run(
        [*run, "prices", "import", str(fix / "eod" / "bhavcopy-sample.csv")],
        env=env,
        check=True,
        capture_output=True,
    )
    out = subprocess.run([*run, *command], env=env, capture_output=True)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
    text = out.stdout.decode("utf-8")  # valid UTF-8, not cp1252
    assert "Traceback" not in text
    if command == ["insights"]:
        assert "₹" in text
