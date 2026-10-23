# Decisions

## Snapshot CSVs persist on the `data` branch
The hourly workflow (`.github/workflows/snapshot.yml`) appends to `snapshots/YYYY-MM.csv` and pushes it to the `data` branch, so `main` stays free of generated data and the monitor stays git-free. The workflow checks out `data` into its own directory. If the branch is missing (`git ls-remote` exit 2) it bootstraps an orphan branch; any other probe failure fails the job instead of bootstrapping. Pushing needs `permissions: contents: write`.

## No retries; a lost push is lost by design
Each run does one plain `git push origin HEAD:data`, with no retry and no force. If it fails (non-fast-forward, permissions, protection added later) that run's rows exist only on the runner and are dropped. The next hourly run checks out the remote branch and appends normally, and the gap shows as a missing `run_ts_utc`. Runs are serialized by a `snapshot-data` concurrency group, so a lost race is unlikely.

## Exit codes versus alerts
`monitor.monitor` exits 1 only when every dataset errored or the file is unwritable or has a bad header; it has already written its rows by then. The workflow records the exit code, still commits and pushes the evidence, then runs `monitor.alert` and finally fails the job on a non-zero monitor exit. `monitor.alert` judges only the latest run in the newest monthly file: it exits 1 on any dataset error, stale or missing `rows_updated_at`, or no usable data, and 2 on bad config. It cannot detect that the schedule itself stopped firing; that needs an external dead-man's switch.

## One 48h staleness threshold, one exemption
`STALE_AFTER_HOURS=48` is the single threshold, set in the workflow `env` with no default in code. It is measured against the run's `run_ts_utc`, not the wall clock, and is strictly greater-than. `mobile_food_permits` last updated 6 weeks before the monitor was built, so it would fail every run; `STALE_EXEMPT_DATASETS=mobile_food_permits` skips staleness (not errors) for it. The other six datasets were 11-18h old when checked. Add names to the exemption only after observing a real slow dataset.
