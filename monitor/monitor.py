"""Snapshot data.sf.gov dataset health: one CSV row per dataset per run.

Only appends to a file; it does no git operations (the workflow persists the file).
Exit code: 0 if at least one dataset succeeded, 1 if every dataset recorded an
error, or if the target file is unwritable or has an unexpected header.
"""

import csv
import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from config.datasets import BASE_URL, DATASETS

HEADER = [
    "run_ts_utc",
    "name",
    "dataset_id",
    "count_http_status",
    "count_latency_ms",
    "row_count",
    "meta_http_status",
    "meta_latency_ms",
    "columns_json",
    "rows_updated_at",
    "error",
]
TIMEOUT_S = 30
SHAPE_ERRORS = (KeyError, IndexError, TypeError, ValueError, OverflowError, OSError)


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch(session, url, headers, label):
    """GET url and decode JSON. Returns (status, latency_ms, body, error).

    status is "" when no response arrived; error is None on success.
    """
    start = time.perf_counter()
    status, body, error = "", None, None
    try:
        resp = session.get(url, headers=headers, timeout=TIMEOUT_S)
    except requests.RequestException as e:
        error = f"{type(e).__name__} on {label}"
    else:
        status = resp.status_code
        if status == 429:
            error = f"HTTP 429 (throttled) on {label}"
        elif not 200 <= status < 300:
            error = f"HTTP {status} on {label}"
        else:
            try:
                body = resp.json()
            except ValueError:
                error = f"malformed JSON on {label}"
    return status, round((time.perf_counter() - start) * 1000), body, error


def _extract(fn, label, errors):
    """Run one field extraction; on a shape problem record it and return ""."""
    try:
        return fn()
    except SHAPE_ERRORS as e:
        msg = f"unexpected response shape on {label}: {type(e).__name__}"
        if msg not in errors:
            errors.append(msg)
        return ""


def snapshot_dataset(session, name, dataset_id, run_ts, headers):
    """Return one row (dict keyed by HEADER). Never raises on request/response problems."""
    errors = []
    c_status, c_ms, c_body, c_err = _fetch(
        session, f"{BASE_URL}/resource/{dataset_id}.json?$select=count(*)", headers, "count"
    )
    m_status, m_ms, m_body, m_err = _fetch(
        session, f"{BASE_URL}/api/views/{dataset_id}.json", headers, "metadata"
    )

    row_count = columns_json = rows_updated_at = ""
    if c_err:
        errors.append(c_err)
    else:
        row_count = _extract(lambda: int(c_body[0]["count"]), "count", errors)
    if m_err:
        errors.append(m_err)
    else:
        # Fields are parsed independently so one missing key keeps the others.
        columns_json = _extract(
            lambda: json.dumps(
                [{"name": c["fieldName"], "type": c["dataTypeName"]} for c in m_body["columns"]],
                separators=(",", ":"),
            ),
            "metadata",
            errors,
        )
        rows_updated_at = _extract(
            lambda: _iso(datetime.fromtimestamp(m_body["rowsUpdatedAt"], timezone.utc)),
            "metadata",
            errors,
        )

    return {
        "run_ts_utc": run_ts,
        "name": name,
        "dataset_id": dataset_id,
        "count_http_status": c_status,
        "count_latency_ms": c_ms,
        "row_count": row_count,
        "meta_http_status": m_status,
        "meta_latency_ms": m_ms,
        "columns_json": columns_json,
        "rows_updated_at": rows_updated_at,
        "error": "; ".join(errors),
    }


def _check_target(path):
    """Return a problem description, or None if appending to path is safe.

    Does not open for writing or create anything.
    """
    anchor = path if path.exists() else next(p for p in path.parents if p.exists())
    mode = os.W_OK | os.X_OK if anchor.is_dir() else os.W_OK
    if not os.access(anchor, mode):
        return f"{path} is not writable (checked {anchor})"
    if path.exists() and path.stat().st_size > 0:
        try:
            with path.open(newline="", encoding="utf-8") as f:
                found = next(csv.reader(f), None)
        except (OSError, UnicodeDecodeError) as e:
            return f"cannot read {path}: {e}"
        if found != HEADER:
            return (
                f"{path} has header {found} but expected {HEADER}; "
                "refusing to append. Fix or move the file."
            )
    return None


def _render(rows, with_header):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    if with_header:
        writer.writerow(HEADER)
    for row in rows:
        writer.writerow([row[col] for col in HEADER])
    return buf.getvalue()


def _append(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write(text)


def main(session=None):
    run_dt = _utcnow()
    run_ts = _iso(run_dt)
    path = Path(os.environ.get("SNAPSHOT_PATH") or f"snapshots/{run_dt:%Y-%m}.csv")
    token = os.environ.get("SOCRATA_APP_TOKEN")
    headers = {"X-App-Token": token} if token else {}

    problem = _check_target(path)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 1

    session = session or requests.Session()
    rows = [snapshot_dataset(session, n, i, run_ts, headers) for n, i in DATASETS.items()]

    try:
        with_header = not path.exists() or path.stat().st_size == 0
        _append(path, _render(rows, with_header))
    except OSError as e:
        print(f"error: cannot write {path}: {e}", file=sys.stderr)
        return 1

    ok = sum(1 for r in rows if not r["error"])
    print(f"{ok}/{len(rows)} datasets ok; appended to {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
