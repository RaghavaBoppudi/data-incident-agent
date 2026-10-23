"""Check the latest snapshot run for problems. Run from the data checkout (cwd has snapshots/).

Reads the newest snapshots/*.csv and evaluates only the rows of its latest run_ts_utc.
A dataset is a problem if its error is set, or its rows_updated_at is missing/unparseable
or older than STALE_AFTER_HOURS at run time. STALE_EXEMPT_DATASETS (comma-separated
names) skips the staleness checks only; errors are never skipped.

Exit code: 0 healthy; 1 problems or no usable data; 2 config error.
Writes a markdown summary to $GITHUB_STEP_SUMMARY, or stdout when unset.
"""

import csv
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config.datasets import DATASETS
from monitor.monitor import HEADER

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class ConfigError(Exception):
    pass


class NoDataError(Exception):
    pass


def _parse_ts(value):
    return datetime.strptime(value, TS_FORMAT).replace(tzinfo=timezone.utc)


def _config():
    """Return (threshold timedelta, exempt names). The threshold has no default on purpose."""
    raw = os.environ.get("STALE_AFTER_HOURS", "").strip()
    try:
        hours = float(raw)
    except ValueError:
        hours = math.nan
    if not math.isfinite(hours) or hours < 0:
        raise ConfigError(f"STALE_AFTER_HOURS must be a non-negative number, got {raw!r}")
    exempt = {n.strip() for n in os.environ.get("STALE_EXEMPT_DATASETS", "").split(",")}
    exempt.discard("")
    unknown = sorted(exempt - DATASETS.keys())
    if unknown:
        raise ConfigError(f"STALE_EXEMPT_DATASETS has unknown dataset(s): {', '.join(unknown)}")
    return timedelta(hours=hours), exempt


def _latest_run(snapshots):
    """Return (file, run_ts, rows of the latest run) from the newest CSV by filename."""
    files = sorted(snapshots.glob("*.csv"))
    if not files:
        raise NoDataError(f"no snapshot file in {snapshots}/")
    path = files[-1]
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        found = next(reader, None)
        if found != HEADER:
            raise NoDataError(f"{path} has header {found} but expected {HEADER}")
        rows = [dict(zip(HEADER, r)) for r in reader if r]
    if not rows:
        raise NoDataError(f"{path} has no data rows")
    run_ts = max(r["run_ts_utc"] for r in rows)
    return path, run_ts, [r for r in rows if r["run_ts_utc"] == run_ts]


def _problems(rows, run_dt, threshold, exempt):
    """Return [(dataset, problem, detail)] for one run's rows."""
    found = []
    for r in rows:
        name = r["name"]
        if r["error"]:
            found.append((name, "error", r["error"]))
        elif name not in exempt:
            try:
                age = run_dt - _parse_ts(r["rows_updated_at"])
            except ValueError:
                found.append((name, "no rows_updated_at", repr(r["rows_updated_at"])))
                continue
            if age > threshold:
                found.append((name, "stale", f"updated {r['rows_updated_at']}, {age} old"))
    return found


def _cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def _summary(path, run_ts, rows, problems, threshold):
    lines = ["## Snapshot health", "", f"Run `{run_ts}` from `{path}`: {len(rows)} datasets checked."]
    if not problems:
        lines.append(f"All {len(rows)} healthy (stale threshold {threshold.total_seconds() / 3600:g}h).")
    else:
        lines += ["", "| dataset | problem | detail |", "|---|---|---|"]
        lines += [f"| {_cell(n)} | {_cell(p)} | {_cell(d)} |" for n, p, d in problems]
    return "\n".join(lines) + "\n"


def _emit(text):
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)  # also in the step log; the summary tab is easy to miss


def main():
    try:
        threshold, exempt = _config()
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        path, run_ts, rows = _latest_run(Path("snapshots"))
    except NoDataError as e:
        _emit(f"## Snapshot health\n\n**No usable snapshot data:** {_cell(e)}\n")
        return 1

    try:
        run_dt = _parse_ts(run_ts)
    except ValueError:
        _emit(f"## Snapshot health\n\n**No usable snapshot data:** bad run_ts_utc {_cell(run_ts)!r} in {path}\n")
        return 1

    problems = _problems(rows, run_dt, threshold, exempt)
    _emit(_summary(path, run_ts, rows, problems, threshold))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
