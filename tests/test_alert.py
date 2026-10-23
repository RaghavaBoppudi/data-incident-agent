"""Alert tests. No network; each test runs in a tmp dir standing in for the data checkout."""

import csv

import pytest

from config.datasets import DATASETS
from monitor import alert
from monitor.monitor import HEADER

RUN = "2026-10-22T14:00:00Z"
FRESH = "2026-10-22T10:00:00Z"  # 4h before RUN
NAMES = list(DATASETS)


def row(name, run=RUN, updated=FRESH, error=""):
    r = dict.fromkeys(HEADER, "")
    r.update(run_ts_utc=run, name=name, dataset_id=DATASETS[name], rows_updated_at=updated, error=error)
    return [r[c] for c in HEADER]


def run_rows(run=RUN, **by_name):
    """One row per dataset; by_name maps dataset -> dict of row() overrides."""
    return [row(n, run=run, **by_name.get(n, {})) for n in NAMES]


def write_csv(rows, name="2026-10.csv", header=HEADER):
    d = alert.Path("snapshots")
    d.mkdir(exist_ok=True)
    with (d / name).open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STALE_AFTER_HOURS", "48")
    monkeypatch.delenv("STALE_EXEMPT_DATASETS", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)


def test_healthy_run(capsys):
    write_csv(run_rows())
    assert alert.main() == 0
    assert f"All {len(NAMES)} healthy" in capsys.readouterr().out


def test_error_row_flagged(capsys):
    write_csv(run_rows(**{"311_cases": {"error": "HTTP 429 (throttled) on count"}}))
    assert alert.main() == 1
    out = capsys.readouterr().out
    assert "311_cases" in out and "HTTP 429" in out


def test_stale_beyond_threshold_flagged(capsys):
    write_csv(run_rows(fire_calls={"updated": "2026-10-20T13:59:59Z"}))  # 48h + 1s
    assert alert.main() == 1
    assert "fire_calls" in capsys.readouterr().out


def test_exactly_at_threshold_not_stale():
    write_csv(run_rows(fire_calls={"updated": "2026-10-20T14:00:00Z"}))  # exactly 48h
    assert alert.main() == 0


def test_staleness_is_relative_to_run_time_not_wall_clock():
    write_csv(run_rows(run="2020-01-01T00:00:00Z", updated="2019-12-31T00:00:00Z"))
    assert alert.main() == 0


def test_exempt_stale_ignored(monkeypatch):
    monkeypatch.setenv("STALE_EXEMPT_DATASETS", " mobile_food_permits , ")
    write_csv(run_rows(mobile_food_permits={"updated": "2026-09-10T00:00:00Z"}))
    assert alert.main() == 0


def test_exempt_missing_updated_at_ignored(monkeypatch):
    monkeypatch.setenv("STALE_EXEMPT_DATASETS", "mobile_food_permits")
    write_csv(run_rows(mobile_food_permits={"updated": ""}))
    assert alert.main() == 0


def test_exempt_with_error_still_flagged(monkeypatch, capsys):
    monkeypatch.setenv("STALE_EXEMPT_DATASETS", "mobile_food_permits")
    write_csv(run_rows(mobile_food_permits={"error": "HTTP 500 on count"}))
    assert alert.main() == 1
    assert "mobile_food_permits" in capsys.readouterr().out


def test_only_latest_run_evaluated():
    old = run_rows(run="2026-10-22T13:00:00Z", building_permits={"error": "boom"})
    write_csv(old + run_rows())
    assert alert.main() == 0


def test_latest_run_chosen_by_max_not_file_order(capsys):
    write_csv(run_rows(building_permits={"error": "boom"}) + run_rows(run="2026-10-22T13:00:00Z"))
    assert alert.main() == 1
    assert "building_permits" in capsys.readouterr().out


def test_single_run_file():
    write_csv(run_rows())
    assert alert.main() == 0


def test_newest_monthly_file_used():
    write_csv(run_rows(building_permits={"error": "old month"}), name="2026-09.csv")
    write_csv(run_rows(), name="2026-10.csv")
    assert alert.main() == 0


def test_missing_updated_at_without_error_flagged(capsys):
    write_csv(run_rows(fire_calls={"updated": ""}))
    assert alert.main() == 1
    assert "fire_calls" in capsys.readouterr().out


def test_unparseable_updated_at_flagged():
    write_csv(run_rows(fire_calls={"updated": "yesterday"}))
    assert alert.main() == 1


def test_no_snapshots_dir():
    assert alert.main() == 1


def test_no_csv_file():
    alert.Path("snapshots").mkdir()
    assert alert.main() == 1


def test_header_only_file():
    write_csv([])
    assert alert.main() == 1


def test_header_mismatch(capsys):
    write_csv(run_rows(), header=HEADER[::-1])
    assert alert.main() == 1
    assert "expected" in capsys.readouterr().out


def test_bad_run_ts():
    write_csv(run_rows(run="not-a-time"))
    assert alert.main() == 1


@pytest.mark.parametrize("value", [None, "", "abc", "nan", "-1"])
def test_bad_threshold_is_config_error(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("STALE_AFTER_HOURS")
    else:
        monkeypatch.setenv("STALE_AFTER_HOURS", value)
    write_csv(run_rows())
    assert alert.main() == 2


def test_unknown_exempt_name_is_config_error(monkeypatch, capsys):
    monkeypatch.setenv("STALE_EXEMPT_DATASETS", "mobile_food_permit")
    write_csv(run_rows())
    assert alert.main() == 2
    assert "mobile_food_permit" in capsys.readouterr().err


def test_summary_appended_to_step_summary_file(tmp_path, monkeypatch):
    target = tmp_path / "summary.md"
    target.write_text("earlier step\n")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
    write_csv(run_rows(fire_calls={"error": "x | y"}))
    assert alert.main() == 1
    text = target.read_text()
    assert text.startswith("earlier step\n")
    assert "| fire_calls | error | x \\| y |" in text
