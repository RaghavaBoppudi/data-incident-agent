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
- Dataset: Olist Brazilian e-commerce (multi-table, real-world messy)

## Layout (planned)
- warehouse/   dbt project and raw data loading
- faults/      fault-injection scripts and the fault catalog
- mcp_server/  MCP tools exposed to the agent
- agent/       agent loop, prompts, incident report schema
- evals/       eval harness and results
- docs/        interfaces.md, decisions.md, scoping docs

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

## Definition of done
- make test passes
- make eval runs without errors
- If a design decision changed, docs/decisions.md is updated