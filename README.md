# azsqlcd

A command-line tool for end-to-end CI/CD of Azure SQL Database, run by GitHub Actions.

- Object files in git are the source of truth for one database in five environments.
- Table changes are ordered, reviewed migrations. With `table_model = true` the tool writes them
  and proves them against the object files, with no database.
- Views, procedures, functions and triggers deploy by checksum with `CREATE OR ALTER`.
- One release is promoted dev > sandbox > test > preprod > prod, with a plan and an approval gate.
- Every run ends with an exit code and a reason code that tell what happened to the database.

Azure SQL Database only. Python 3.12 or newer. The core uses the standard library only; the
database commands need the `db` extra.

## Status

Not production-ready. The unit tests use a fake at the session boundary. What ran on a real Azure
SQL Database on 2026-10-07:

- Read side, on a copy of WideWorldImporters, read-only: catalog queries, `export`, `lint`,
  `verify`, `build`, `drift --export`.
- Write path, on one disposable database, through the live scripts: the driver spike (19 items
  pass, 1 inconclusive by design, 3 manual; the five driver gates pass, `mssql-python` 1.15.0
  stays), the acceptance suite (plan, deploy in one transaction, read-back, failures, lost
  sessions, drift, resolve actions: 50 of 50 checks on the final commit; 52 of 52 on an earlier
  commit, where one scenario had 2 more checks) and the table-model script (18 of 18 checks).

- Everyday work through the real command line, on one test database: 8 releases built, 7 deployed,
  1 failed on purpose (exit 21) and rolled back; `gen`, `plan`, gated and inline `deploy`,
  `drift`, `resolve` (rebind, accept-drift, mark-applied), withdraw-and-replace.
- Onboarding of an existing database through the real command line: 162 of 162 emitted create
  statements accepted by the engine; `export`, `setup-sql`, `baseline` of 84 table-class objects
  and 49 modules; two changes deployed.
- Read-only export of Microsoft's sample database: every catalog query clean, 48 of 48 tables
  (31 table files, 17 history tables that the engine owns), 0 unmanaged, 0 fidelity differences.

A column with dynamic data masking is in the table model (not in a table type); a masked column
has not run through `deploy` on a database.

On GitHub Actions (a scratch demo repository, 2026-10-07): the workflows ran up to the Azure login
step. Green: release create, `verify` (pass, and fail as required), dispatch of an existing
release, `targets`, `record`, `incident`. Every job that needs a database stops at the login (the
demo has no real tenant): `docs/known-gaps.md`, section 12.

What did not run: any production use; any workflow step after the Azure login on GitHub Actions
(the gate job, approvals, the OIDC subject against Entra, runner groups); anything on Windows
against a database (the unit tests pass on the Windows runners of `ci.yml`), or on Python 3.14
against a database, or on the runner image; the role path of `setup-sql` for environments that
share one identity; the spike items L8 (second database), L12, L13, L14; a loss of the network
during COMMIT. The everyday work and the onboarding through the command line ran before the last
fix wave and were not repeated on the final commit: the tree holds changes made after those
runs, and has not run against a database as a whole.

`docs/known-gaps.md` is the one list of what is not built, not proven, or built in another way
than the design says: section 8 has the read-side results, section 9 the write-path results,
section 10 the open findings of the audits. `docs/comparison.md` compares the design choices of
the tool with those of other families of schema deployment tools.

Scope (owner decision, 2026-10-07): structural changes; data batches off by default. A
`-- azsqlcd:data` batch is refused (`DATA000`) unless `azsqlcd.toml` has `data_batches = true`
under `[project]`; the data rules have a low priority and less review.

Temporal tables are supported: a system-versioned temporal table is a table file like any other (period columns,
`PERIOD FOR SYSTEM_TIME`, `SYSTEM_VERSIONING = ON` with a named history table). Limits: the history
table is not an object of the repository and stays in the database after the drop of its table; a
table cannot be changed to temporal or back through generated SQL; the rename of a period column
is refused. `docs/runbook.md`, section "Temporal tables", has the rules.

## Quick start: developer

`docs/quickstart.md` walks through the demo repository `examples/demo-db` step by step, with the
output of each command. The short form:

Install from a checkout of this repository:

```
git clone https://github.com/akaalholdings/azsqlcd
cd azsqlcd
uv sync --frozen
```

Then, in the database repository (`TOOL` is the path of the checkout):

```
uv run --project "$TOOL" --no-sync azsqlcd lint
uv run --project "$TOOL" --no-sync azsqlcd gen --name add_order_status
uv run --project "$TOOL" --no-sync azsqlcd verify --base "$(git merge-base origin/main HEAD)"
```

- `lint` checks the files of the working tree: file rules, batch classifier, allow lines, tombstones.
- `gen --name` writes a migration from the difference between the table files at `origin/main` and
  in the working tree. It needs `table_model = true`. After a hand edit of a new migration, and
  after a merge of `main`, run `azsqlcd gen --resum`.
- `verify` is the check of the pull-request job: lint, chain rules, module rules and, with
  `table_model = true`, the proof. Lint is not a safety proof: `raw` batches, and `data` batches
  where they are switched on, are not modelled.

These three commands use no database and no network. Each finding has a code; `docs/runbook.md`,
section "Lint and verify findings", has one line for each. `templates/db-repo/` is the start of a
database repository; its `README.md` is the guide for the people who change a schema.

## Quick start: operator

| Document | Use |
|---|---|
| `docs/quickstart.md` | First hour: the demo repository, a change, `gen`, `verify`, `build` |
| `docs/comparison.md` | The design choices of the tool, next to those of other families of tools, and what the tool does not do |
| `docs/setup.md` | One-time setup: identities, repository rules, runners, `setup-sql`, onboarding of a database |
| `docs/triage.md` | A run failed: where its log is, `azsqlcd support-bundle`, what the log holds and what it never holds |
| `docs/porting.md` | Moving the tool to another organisation |
| `docs/agent-install.md` | Installing the tool with an AI coding agent |
| `docs/runbook.md` | On call: one row for each reason code, under its exit code, with the state of the database and the command |
| `docs/live-testing.md` | For the owner: how to run the live spike and the live acceptance on a disposable database, and the results of 2026-10-07 |
| `docs/known-gaps.md` | Not built, not proven, decisions that wait for the owner, dead ends, open review findings |
| `docs/design.md` | The design of record. Part 1, "As built", holds the module map and the command-line reference |
| `CHANGELOG.md` | What changed in each version, newest first |

## Tests

```
uv sync --extra db
uv run pytest
uv run ruff check
uv run pyright
```

No test connects to a database or to the network. Without `--extra db` the tests that read the
installed driver package are skipped. The scripts under `scripts/live_*.py` and `tests/live/`
are run by the owner only, on a disposable Azure SQL Database: `docs/live-testing.md`.

## Licence

MIT. See `LICENSE`.
