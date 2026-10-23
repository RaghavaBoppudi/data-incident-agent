"""Monitor tests. All HTTP is faked; the autouse fixture blocks any real request."""

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

from config.datasets import BASE_URL, DATASETS
from monitor import monitor as mon

NOW = datetime(2026, 10, 22, 14, 0, 0, tzinfo=timezone.utc)
NEXT_MONTH = datetime(2026, 11, 1, 0, 5, 0, tzinfo=timezone.utc)
EPOCH = 1792665709
EXPECTED_UPDATED_AT = datetime.fromtimestamp(EPOCH, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
RAW_COLUMNS = [
    {"id": 1, "fieldName": "permit_number", "dataTypeName": "text", "name": "Permit Number"},
    {"id": 2, "fieldName": "issued_date", "dataTypeName": "calendar_date", "name": "Issued"},
]
EXPECTED_COLUMNS = [
    {"name": "permit_number", "type": "text"},
    {"name": "issued_date", "type": "calendar_date"},
]
IDS = list(DATASETS.values())
FIRST = IDS[0]
ID_RE = re.compile(r"([a-z0-9]{4}-[a-z0-9]{4})\.json")


class FakeResp:
    def __init__(self, body=None, status=200, raw=None):
        self.status_code = status
        self._raw = json.dumps(body) if raw is None else raw

    def json(self):
        return json.loads(self._raw)


def count_resp(count="123"):
    return FakeResp([{"count": count}])


def meta_resp(drop=(), **over):
    body = {"columns": RAW_COLUMNS, "rowsUpdatedAt": EPOCH, **over}
    for key in drop:
        del body[key]
    return FakeResp(body)


class FakeSession:
    """overrides: {(dataset_id | None, "count" | "meta"): FakeResp | exception instance}."""

    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {}), "timeout": timeout})
        kind = "count" if "$select" in url else "meta"
        dataset_id = ID_RE.search(url).group(1)
        for key in ((dataset_id, kind), (None, kind)):
            if key in self.overrides:
                out = self.overrides[key]
                if isinstance(out, BaseException):
                    raise out
                return out
        return count_resp() if kind == "count" else meta_resp()


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    def blocked(*args, **kwargs):
        raise AssertionError("test attempted a real network request")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    monkeypatch.delenv("SNAPSHOT_PATH", raising=False)
    monkeypatch.delenv("SOCRATA_APP_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def out_csv(monkeypatch, tmp_path):
    path = tmp_path / "out" / "snap.csv"
    monkeypatch.setenv("SNAPSHOT_PATH", str(path))
    return path


@pytest.fixture
def run(monkeypatch):
    def _run(session=None, now=NOW):
        monkeypatch.setattr(mon, "_utcnow", lambda: now)
        session = session or FakeSession()
        return mon.main(session=session), session

    return _run


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def by_id(path):
    return {r["dataset_id"]: r for r in read_rows(path)}


def all_fail(count=FakeResp([], 500), meta=FakeResp({}, 500)):
    return {(None, "count"): count, (None, "meta"): meta}


# --- config ---


def test_dataset_config():
    assert BASE_URL == "https://data.sf.gov"
    assert DATASETS == {
        "building_permits": "i98e-djp9",
        "business_locations": "g8m3-pdis",
        "eviction_notices": "5cei-gny5",
        "mobile_food_permits": "rqzj-sfat",
        "311_cases": "vw6y-z8j6",
        "fire_calls": "nuek-vuh3",
        "police_incidents": "wg3w-h783",
    }


# --- core snapshot behavior ---


def test_successful_snapshot(run, out_csv):
    code, session = run()
    assert code == 0
    rows = read_rows(out_csv)
    assert len(rows) == len(DATASETS)
    assert list(rows[0]) == mon.HEADER
    assert {r["run_ts_utc"] for r in rows} == {"2026-10-22T14:00:00Z"}
    row = by_id(out_csv)[FIRST]
    assert row["name"] == "building_permits"
    assert (row["count_http_status"], row["meta_http_status"]) == ("200", "200")
    assert row["count_latency_ms"].isdigit() and row["meta_latency_ms"].isdigit()
    assert row["row_count"] == "123"
    assert json.loads(row["columns_json"]) == EXPECTED_COLUMNS
    assert row["rows_updated_at"] == EXPECTED_UPDATED_AT
    assert row["error"] == ""
    urls = [c["url"] for c in session.calls]
    assert f"{BASE_URL}/resource/{FIRST}.json?$select=count(*)" in urls
    assert f"{BASE_URL}/api/views/{FIRST}.json" in urls
    assert all(u.startswith("https://data.sf.gov/") for u in urls)
    assert {c["timeout"] for c in session.calls} == {30}


def test_row_count_is_stored_as_integer():
    row = mon.snapshot_dataset(FakeSession({(None, "count"): count_resp("9011562")}), "n", FIRST, "ts", {})
    assert row["row_count"] == 9011562 and isinstance(row["row_count"], int)


def test_http_error_row_does_not_stop_other_datasets(run, out_csv):
    code, _ = run(FakeSession({(FIRST, "count"): FakeResp([], 500)}))
    assert code == 0
    rows = by_id(out_csv)
    bad = rows[FIRST]
    assert bad["count_http_status"] == "500"
    assert bad["row_count"] == ""
    assert bad["error"] == "HTTP 500 on count"
    assert json.loads(bad["columns_json"]) == EXPECTED_COLUMNS  # metadata side survived
    assert all(r["error"] == "" for i, r in rows.items() if i != FIRST)


@pytest.mark.parametrize("kind", ["count", "meta"])
def test_malformed_json(run, out_csv, kind):
    code, _ = run(FakeSession({(FIRST, kind): FakeResp(raw="<html>not json</html>")}))
    assert code == 0
    label = "count" if kind == "count" else "metadata"
    row = by_id(out_csv)[FIRST]
    assert row["error"] == f"malformed JSON on {label}"
    assert row[f"{'count' if kind == 'count' else 'meta'}_http_status"] == "200"


def test_header_written_once(run, out_csv):
    run()
    run()
    lines = out_csv.read_text().splitlines()
    assert lines.count(",".join(mon.HEADER)) == 1
    assert len(lines) == 1 + 2 * len(DATASETS)


# --- persistence ---


def test_new_month_creates_new_file_with_own_header(run):
    run(now=NOW)
    oct_file = Path("snapshots/2026-10.csv")
    before = oct_file.read_bytes()
    run(now=NEXT_MONTH)
    nov_file = Path("snapshots/2026-11.csv")
    assert oct_file.read_bytes() == before
    assert nov_file.read_text().splitlines()[0] == ",".join(mon.HEADER)
    assert len(read_rows(nov_file)) == len(DATASETS)


def test_explicit_snapshot_path_used_as_is_and_parent_created(run, out_csv, tmp_path):
    assert not out_csv.parent.exists()
    run()
    assert out_csv.exists()
    assert not (tmp_path / "snapshots").exists()


def test_header_mismatch_writes_nothing_and_fetches_nothing(run, out_csv, capsys):
    out_csv.parent.mkdir()
    out_csv.write_text("a,b,c\n1,2,3\n")
    before = out_csv.read_bytes()
    code, session = run()
    assert code == 1
    assert session.calls == []
    assert out_csv.read_bytes() == before
    assert str(out_csv) in capsys.readouterr().err


def test_header_mismatch_is_per_file(run):
    Path("snapshots").mkdir()
    Path("snapshots/2026-10.csv").write_text("bad,header\n")
    assert run(now=NOW)[0] == 1
    assert run(now=NEXT_MONTH)[0] == 0
    assert len(read_rows("snapshots/2026-11.csv")) == len(DATASETS)


def test_zero_byte_file_gets_header(run, out_csv):
    out_csv.parent.mkdir()
    out_csv.touch()
    assert run()[0] == 0
    assert out_csv.read_text().splitlines()[0] == ",".join(mon.HEADER)


def test_unwritable_existing_file_exits_before_any_request(monkeypatch, run, out_csv, capsys):
    out_csv.parent.mkdir()
    out_csv.write_text(",".join(mon.HEADER) + "\n")
    checked = []
    monkeypatch.setattr(mon.os, "access", lambda p, mode: checked.append(Path(p)) or False)
    code, session = run()
    assert code == 1
    assert session.calls == []
    assert checked == [out_csv]
    assert "not writable" in capsys.readouterr().err


def test_unwritable_parent_checked_without_creating_anything(monkeypatch, run, out_csv, tmp_path, capsys):
    checked = []
    monkeypatch.setattr(mon.os, "access", lambda p, mode: checked.append(Path(p)) or False)
    code, session = run()
    assert code == 1
    assert session.calls == []
    assert checked == [tmp_path]  # nearest existing ancestor of the missing target
    assert not out_csv.parent.exists() and not out_csv.exists()
    assert "not writable" in capsys.readouterr().err


def test_all_rows_written_in_a_single_write(monkeypatch, run, out_csv):
    writes = []
    real = mon._append
    monkeypatch.setattr(mon, "_append", lambda path, text: (writes.append(text), real(path, text)))
    run()
    assert len(writes) == 1
    assert writes[0].count("\n") == 1 + len(DATASETS)


def test_append_does_one_write_call_on_the_file_handle(monkeypatch, run, out_csv):
    class SpyFile:
        def __init__(self, f):
            self.f, self.writes = f, []

        def write(self, text):
            self.writes.append(text)
            return self.f.write(text)

        def __enter__(self):
            self.f.__enter__()
            return self

        def __exit__(self, *exc):
            return self.f.__exit__(*exc)

    spies = []
    real_open = Path.open

    def spy_open(self, mode="r", *args, **kwargs):
        f = real_open(self, mode, *args, **kwargs)
        if mode != "a":
            return f
        spies.append(SpyFile(f))
        return spies[-1]

    monkeypatch.setattr(Path, "open", spy_open)
    run()
    assert len(spies) == 1
    assert len(spies[0].writes) == 1
    assert spies[0].writes[0].count("\n") == 1 + len(DATASETS)
    assert out_csv.read_text() == spies[0].writes[0]


def test_crash_mid_run_leaves_no_partial_rows(run, out_csv, tmp_path):
    assert not out_csv.parent.exists()
    with pytest.raises(KeyboardInterrupt):
        run(FakeSession({(IDS[3], "count"): KeyboardInterrupt()}))
    assert not out_csv.exists()
    assert not out_csv.parent.exists()
    assert list(tmp_path.iterdir()) == []


# --- exit codes ---


def test_all_datasets_failing_writes_rows_and_exits_1(run, out_csv):
    code, _ = run(FakeSession(all_fail()))
    assert code == 1
    rows = read_rows(out_csv)
    assert len(rows) == len(DATASETS)
    assert {r["error"] for r in rows} == {"HTTP 500 on count; HTTP 500 on metadata"}


def test_every_dataset_with_partial_failure_still_counts_as_all_failed(run, out_csv):
    code, _ = run(FakeSession({(None, "meta"): FakeResp({}, 500)}))
    assert code == 1
    assert all(r["row_count"] == "123" for r in read_rows(out_csv))


def test_some_datasets_succeeding_exits_0(run, out_csv):
    overrides = {(i, k): FakeResp([], 500) for i in IDS[1:] for k in ("count", "meta")}
    code, _ = run(FakeSession(overrides))
    assert code == 0
    rows = by_id(out_csv)
    assert rows[FIRST]["error"] == ""
    assert all(rows[i]["error"] for i in IDS[1:])


# --- app token and rate limiting ---


def test_token_sent_on_every_request_and_never_leaked(monkeypatch, run, out_csv, capsys, caplog):
    secret = "s3cret-token-abc"
    monkeypatch.setenv("SOCRATA_APP_TOKEN", secret)
    code, session = run(FakeSession({(FIRST, "count"): FakeResp([], 429)}))
    assert code == 0
    assert len(session.calls) == 2 * len(DATASETS)
    assert all(c["headers"] == {"X-App-Token": secret} for c in session.calls)
    captured = capsys.readouterr()
    assert secret not in out_csv.read_text() + captured.out + captured.err + caplog.text


@pytest.mark.parametrize("value", [None, ""])
def test_no_token_header_when_unset_or_empty(monkeypatch, run, out_csv, value):
    if value is not None:
        monkeypatch.setenv("SOCRATA_APP_TOKEN", value)
    _, session = run()
    assert len(session.calls) == 2 * len(DATASETS)
    assert all("X-App-Token" not in c["headers"] for c in session.calls)


@pytest.mark.parametrize(
    "kind, label", [("count", "count"), ("meta", "metadata")]
)
def test_429_is_distinguishable(run, out_csv, kind, label):
    run(FakeSession({(FIRST, kind): FakeResp({}, 429)}))
    assert by_id(out_csv)[FIRST]["error"] == f"HTTP 429 (throttled) on {label}"


# --- partial failures and unexpected shapes ---


def test_metadata_failure_keeps_count_fields(run, out_csv):
    run(FakeSession({(FIRST, "meta"): FakeResp({}, 503)}))
    row = by_id(out_csv)[FIRST]
    assert row["row_count"] == "123" and row["count_http_status"] == "200"
    assert (row["meta_http_status"], row["columns_json"], row["rows_updated_at"]) == ("503", "", "")
    assert row["meta_latency_ms"].isdigit()
    assert row["error"] == "HTTP 503 on metadata"


def test_count_failure_keeps_metadata_fields(run, out_csv):
    run(FakeSession({(FIRST, "count"): FakeResp([], 503)}))
    row = by_id(out_csv)[FIRST]
    assert (row["count_http_status"], row["row_count"]) == ("503", "")
    assert json.loads(row["columns_json"]) == EXPECTED_COLUMNS
    assert row["rows_updated_at"] == EXPECTED_UPDATED_AT
    assert row["error"] == "HTTP 503 on count"


def test_both_failures_are_joined(run, out_csv):
    overrides = {(FIRST, "count"): FakeResp([], 500), (FIRST, "meta"): FakeResp({}, 429)}
    run(FakeSession(overrides))
    assert by_id(out_csv)[FIRST]["error"] == "HTTP 500 on count; HTTP 429 (throttled) on metadata"


def test_missing_rows_updated_at_keeps_columns(run, out_csv):
    run(FakeSession({(FIRST, "meta"): meta_resp(drop=["rowsUpdatedAt"])}))
    row = by_id(out_csv)[FIRST]
    assert json.loads(row["columns_json"]) == EXPECTED_COLUMNS
    assert row["rows_updated_at"] == ""
    assert row["error"] == "unexpected response shape on metadata: KeyError"
    assert row["meta_http_status"] == "200"


def test_missing_columns_keeps_rows_updated_at(run, out_csv):
    run(FakeSession({(FIRST, "meta"): meta_resp(drop=["columns"])}))
    row = by_id(out_csv)[FIRST]
    assert row["columns_json"] == ""
    assert row["rows_updated_at"] == EXPECTED_UPDATED_AT
    assert row["error"] == "unexpected response shape on metadata: KeyError"


@pytest.mark.parametrize(
    "body, exc",
    [([], "IndexError"), ([{"n": 1}], "KeyError"), ({"count": "1"}, "KeyError"), ([{"count": "abc"}], "ValueError")],
)
def test_unexpected_count_shape(run, out_csv, body, exc):
    run(FakeSession({(FIRST, "count"): FakeResp(body)}))
    row = by_id(out_csv)[FIRST]
    assert row["row_count"] == ""
    assert row["error"] == f"unexpected response shape on count: {exc}"


@pytest.mark.parametrize("body", [[1, 2], "just a string"])
def test_non_dict_metadata_body(run, out_csv, body):
    run(FakeSession({(FIRST, "meta"): FakeResp(body)}))
    row = by_id(out_csv)[FIRST]
    assert (row["columns_json"], row["rows_updated_at"]) == ("", "")
    assert row["error"] == "unexpected response shape on metadata: TypeError"


@pytest.mark.parametrize("exc", [requests.Timeout("slow"), requests.ConnectionError("down")])
def test_transport_errors_leave_statuses_empty_but_record_latency(run, out_csv, exc):
    code, _ = run(FakeSession({(FIRST, "count"): exc, (FIRST, "meta"): exc}))
    assert code == 0
    row = by_id(out_csv)[FIRST]
    assert (row["count_http_status"], row["meta_http_status"]) == ("", "")
    assert row["count_latency_ms"].isdigit() and row["meta_latency_ms"].isdigit()
    name = type(exc).__name__
    assert row["error"] == f"{name} on count; {name} on metadata"


# --- encoding ---


def test_csv_round_trip_preserves_columns_json(run, out_csv):
    tricky = [
        {"fieldName": "a,b", "dataTypeName": 'te"xt'},
        {"fieldName": "plain", "dataTypeName": "number"},
    ]
    run(FakeSession({(None, "meta"): meta_resp(columns=tricky)}))
    with open(out_csv, newline="", encoding="utf-8") as f:
        raw_rows = list(csv.reader(f))
    assert all(len(r) == len(mon.HEADER) for r in raw_rows)
    col = mon.HEADER.index("columns_json")
    expected = [{"name": "a,b", "type": 'te"xt'}, {"name": "plain", "type": "number"}]
    assert all(json.loads(r[col]) == expected for r in raw_rows[1:])
