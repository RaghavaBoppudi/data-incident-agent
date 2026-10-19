# data-incident-agent

An AI agent that investigates dbt test failures and data incidents:
finds the root cause using lineage, data, and git history, then opens
a GitHub issue and a draft PR with a fix and a regression test.
A human approves every merge.

## Stack
- Python 3.12, managed with uv
- DuckDB as the warehouse, dbt-core + dbt-duckdb for models and tests
- MCP Python SDK for the tool server
- pytest for tests
- Data: San Francisco open data via the Socrata SODA API at data.sf.gov
  - Building Permits: i98e-djp9 (~1.3M rows; ingest a bounded window)
  - Registered Business Locations: g8m3-pdis (~368k rows)
  - Eviction Notices: 5cei-gny5 (~49k rows)
  - Mobile Food Facility Permit: rqzj-sfat (500 rows; fast test table)
  - All four are public domain (PDDL license)

## Layout (planned)
- warehouse/   dbt project and raw data loading
- faults/      fault-injection scripts and the fault catalog
- mcp_server/  MCP tools exposed to the agent
- agent/       agent loop, prompts, incident report schema
- evals/       eval harness and results
- docs/        interfaces.md, decisions.md, scoping docs
- ingest/      SODA API fetch scripts (data files are gitignored, never committed)

## Commands
These don't exist yet; Day 1 creates them.
- make setup         install deps and build the warehouse
- make test          run pytest and dbt tests
- make inject-faults inject a fault from the catalog
- make eval          run the full eval suite

## Hard rules
- The agent has READ-ONLY warehouse access. Never write to the warehouse
  from agent or MCP code.
- Never commit secrets. Credentials come from .env, which is gitignored.
- Every MCP tool and every fault needs a test.
- Follow docs/interfaces.md exactly. If an interface needs to change,
  stop and ask instead of changing it.
- Don't add dependencies without saying why.
- Use data.sf.gov, never data.sfgov.org. The old domain returns an HTML
  redirect page instead of JSON.
- Dataset IDs above were verified against the portal. Verify any new ID
  with a metadata call before using it; never take IDs from third-party pages.
- Never commit data files. Commit the fetch script; data goes in a
  gitignored data/ folder as dated Parquet snapshots.
- Join datasets at neighborhood or supervisor-district level only, never by
  address matching. Verify those columns exist when profiling.

## Definition of done
- make test passes
- make eval runs without errors
- If a design decision changed, docs/decisions.md is updated