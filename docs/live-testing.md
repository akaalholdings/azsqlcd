# azsqlcd live testing

For the owner. This document tells how to run the only parts of the project that connect to a
database: `scripts/live_spike.py`, `scripts/live_acceptance.py`, `scripts/live_tables.py` and
`tests/live/`. It also holds the manual checklist for the GitHub pipeline (G1 to G7).

Status (2026-10-07): the spike, the acceptance run and the table-model script ran on one
disposable Azure SQL Database, from one machine (macOS). Section 11 holds the results, the engine
facts and what is still not proven. One read-only run of the tool itself was made on a copy of
WideWorldImporters: section 10. Not run: `tests/live`, L8 on a second database, L12, L13, L14, the
new items X6 to X10, the network case of section 7, the GitHub checklist of section 8, and every
live run on Windows, on Python 3.14 and on the runner image. A probe that stops on an error is
reported as `inconclusive`, with the error text; it is then a defect of the script, not a result.

Rules:

- The scripts never run in pytest or in CI. `pytest` collects `tests/unit` only.
- Run them only on a disposable Azure SQL Database. No SQL Server, no container (D2).
- The scripts write to the database. They refuse a database that is not disposable (section 2).

## 1. What you need

| Item | Detail |
|---|---|
| Database 1 | Azure SQL Database, provisioned (for example S0 or General Purpose). Empty. A name without `prod` |
| Database 2 | Azure SQL Database, serverless, with auto-pause. Empty. A name without `prod`. For L8 and L12 |
| Login | `az login` with an identity that is `db_owner` in both databases |
| KILL right | The login must be able to run `KILL`. The server administrator can. For another user, the administrator runs in each database: `GRANT KILL DATABASE CONNECTION TO [<user>];` |
| Network | A path from the machine to the server (firewall rule or private endpoint) |
| Machine | `uv`, `git`, the Azure CLI. Python 3.12 or newer |
| Second machines | For gate G-D5: Windows with Python 3.14, and the runner image. Run item L1 and `tests/live` there too |

What the scripts make in a database, and what they leave:

| Script | Makes | Leaves at the end |
|---|---|---|
| `live_spike.py` | Schema `azsqlcd_spike` with its objects; the user `azsqlcd_spike_user` for a moment (item X4) | Nothing. It drops both at the start and at the end |
| `live_acceptance.py` | Schema `azsqlcd` (the state of the tool); the users `azsqlcd-livetest-disposable-plan` and `-deploy`; schema `azsqlcd_accept` | All of it, so that you can read `azsqlcd.run` and `azsqlcd.step`. The next run drops the objects of `azsqlcd_accept` and the four state tables first |
| `live_tables.py` | Schema `azsqlcd` (the state of the tool) and the two users; schema `azsqlcd_tm` with two tables, a view and a procedure | All of it. The next run drops the objects of `azsqlcd_tm` and the four state tables first |
| `tests/live` | Temporary tables only | Nothing |

The two users of the acceptance run have client ids that do not exist. No identity can sign in as
them. To remove everything by hand, delete the database.

## 2. Guards

Both scripts check all of these before the first write. `tests/live` checks them too.

| Guard | Reason code |
|---|---|
| `--confirm-disposable-database` repeats `--database` exactly | `LIVE_NOT_CONFIRMED` |
| The database name does not contain `prod` | `LIVE_PROD_NAME` |
| `DB_NAME()` of the session is the confirmed name (same case) | `LIVE_DB_NAME` |
| `SERVERPROPERTY('EngineEdition')` is 5 | `LIVE_ENGINE_EDITION` |
| The database has no `azsqlcd.meta` row, or the row says `disposable` | `LIVE_BOUND_ENVIRONMENT` |

A guard that fails ends the script with exit 22 and the reason code on stderr. Nothing was
written. The first two guards need no connection. `DRIVER_MISSING` (exit 22) means that the
script was started without `--extra db`.

## 3. Commands, in order

Replace `S` with the server (`<name>.database.windows.net`), `D1` and `D2` with the databases.
All commands run in the root of the tool repository.

1. Sign in and install the driver.

   ```
   az login
   uv sync --frozen --extra db
   uv run python scripts/live_spike.py --list
   ```

2. Spike, all items, on database 1. L12, L13 and L14 report `manual` here.

   ```
   uv run --extra db python scripts/live_spike.py --server S --database D1 --confirm-disposable-database D1
   ```

   To run some items again: `--items L5,X5`. The files of the other items stay.

3. L8 on database 2, compared with database 1. `--out` names a folder outside the repository, so
   that the files of database 1 stay.

   ```
   uv run --extra db python scripts/live_spike.py --server S --database D2 --confirm-disposable-database D2 \
       --items L8 --out /tmp/azsqlcd-d2 --compare-normal-forms tests/fixtures/live/normal_forms.json
   ```

4. L12 on the serverless database. Pause it first, then start the script at once.

   ```
   az sql db pause --resource-group RG --server NAME --name D2
   az sql db show --resource-group RG --server NAME --name D2 --query status
   uv run --extra db python scripts/live_spike.py --server S --database D2 --confirm-disposable-database D2 \
       --items L12 --expect-paused
   ```

5. Driver contract. Run it also on Windows with Python 3.14 and on the runner image.

   ```
   AZSQLCD_LIVE_SERVER=S AZSQLCD_LIVE_DATABASE=D1 uv run --extra db pytest tests/live -q
   ```

   In PowerShell set the two variables first: `$env:AZSQLCD_LIVE_SERVER = 'S'` and
   `$env:AZSQLCD_LIVE_DATABASE = 'D1'`. Without them every test is skipped.

6. Acceptance on database 1.

   ```
   uv run --extra db python scripts/live_acceptance.py --server S --database D1 --confirm-disposable-database D1
   ```

   The table model (`table_model = true`) has a script of its own. Run it before the acceptance
   run when both are wanted: each of the two drops the four state tables at its start.

   ```
   uv run --extra db python scripts/live_tables.py --server S --database D1 --confirm-disposable-database D1
   ```

   One writer at a time: a second process on the same database waits behind the schema locks of
   a run, and can cause a false 24 `LOCK_TIMEOUT` in the suite (seen on 2026-10-07).

7. L14, with the deploy user that the acceptance run made (step 6 must be done).

   ```
   uv run --extra db python scripts/live_spike.py --server S --database D1 --confirm-disposable-database D1 \
       --items L14 --second-principal azsqlcd-livetest-disposable-deploy
   ```

   If the engine refuses `EXECUTE AS USER` for this user, the result is `inconclusive`. Then make
   a user without login; the item prints the statements (`observed.steps` in `spike/L14.json`).
   Run the item a second time with `--second-principal azsqlcd-livetest-disposable-plan`: the
   statements must fail and every read must pass.

8. L13, the token soak. Long. Start it right after `az login`. Do not let the machine sleep.

   ```
   az login
   uv run --extra db python scripts/live_spike.py --server S --database D1 --confirm-disposable-database D1 \
       --items L13 --soak-past-expiry-minutes 90
   ```

Exit code of both scripts: 0 = no item or check failed; 1 = at least one failed, or the cleanup
failed; 22 = a guard refused; 24 = no connection or no token.

### Expected duration

Measured on 2026-10-07 on GP_S_Gen5_1 where the row says so. The other rows are estimates from
the waits and the number of connections in the scripts.

| Step | Time |
|---|---|
| Spike, all automatic items | About 2 minutes, measured (L6 opens 40 sessions, L9 makes 500 procedures, L17 fills 300,000 rows; `--rows` changes it) |
| L8 on database 2 | 1 minute |
| L12 | Up to 3 minutes of connect, plus the pause |
| Driver contract | 2 minutes |
| Acceptance | 59 to 75 seconds, measured. About 100 connections |
| Table model (`live_tables.py`) | 37 seconds, measured |
| L13 | The life of the token (60 to 90 minutes) plus 90 minutes |
| GitHub checklist (section 8) | The blueprint estimates 8 owner hours, plus 4 to 8 hours of one-time Azure and GitHub setup |

The blueprint estimates 4 owner hours for step P0 (the spike) and 8 for step P1 (the acceptance).

## 4. Spike items

Each item writes `tests/fixtures/live/spike/<id>.json` with `result`, `observed` and `decides`.
An item file can also hold the key `note`: one line for the operator, printed as `<id> note: ...`
for every result, a pass too. `result` is `pass`, `fail`, `inconclusive` (the probe did not
finish, or needs a second run), `manual` or `not applicable` (an item about access tokens, for a
sign-in that has none).

Sign-in: the three live scripts (`live_spike.py`, `live_acceptance.py`, `live_tables.py`) sign in
as the variable `AZSQLCD_AUTH` says (`docs/setup.md`, section 2, "Three ways to sign in"). With
`AZSQLCD_AUTH=sql` there is no access token: L1 records no token life, X1 skips its token connect
to a database that does not exist, L13 is `not applicable`, and the acceptance check "a token
with too little life" is `not applicable`. The acceptance totals count `not applicable` apart: it
is not a pass and not a failure. No live script ran with `managed-identity` or `sql`. L1 to L17 are the items of the blueprint. L5b and X1 to X10 were added by the build:
28 items. X6 to X10 were added after the live run of 2026-10-07 and have not run on a database.

| Id | What it proves | When it fails | What it unlocks |
|---|---|---|---|
| L1 | The driver connects with the Azure CLI token. A closed session is gone from the server (pooling off). The session options of the runner are set and read back. It records the token life and the driver types of values | `pooling_off` false: the driver cannot be used; a closed session keeps the lock. Options not asserted: every deploy stops with `SESSION_OPTIONS`; fix the read in `runner.py` | Gate G-D5 on this machine |
| L2 | An error in statement 2 or 3 of a batch is raised, or the guard sees it. Nine batch shapes, also `RAISERROR` after a result set | Write the pyodbc adapter | Gate G-D1 |
| L3 | A syntax error, a compile error and a deferred compile error inside a transaction are seen. It records which leave the transaction open | Same as L2 | The rollback path of the runner is right for both shapes |
| L4 | Batch text with `?`, `{d ...}`, `{call ...}`, `{fn ...}` in literals, comments and bracket names arrives unchanged | Write the pyodbc adapter, or switch the escape scan off in the adapter | Gate G-D3 |
| L5 | A session that was killed while idle raises at the next batch. Two cases: with the applock, and with no session state | Write the pyodbc adapter | Gate G-D2 |
| L5b | A connection that is closed inside a transaction leaves no row | The tool cannot stop a run safely with this driver | The stop of a run by closing the connection |
| L6 | A second session with the applock reads the commit counter of a killed session with a locking read, right in every repetition (`--repetitions`, default 20). While the writer lives, the read waits | Set `RECONCILE_BY_LOCKING_READ = False`. Every lost connection in a transaction is then exit 23 | `runner.RECONCILE_BY_LOCKING_READ` stays True: a lost connection is exit 24 when the read decides |
| L7 | What `sys.sql_modules` stores after CREATE OR ALTER and after ALTER, for each module kind and eleven header shapes. Pass: the stored text is the batch with the verb `CREATE` in place of `CREATE OR ALTER` or `ALTER` (`modules.stored_text`), and `runner._module_problems` with the compare on accepts every deployed file | Set `MODULE_TEXT_READBACK = False`. Read `observed` (`not_as_stored_text`, `refused_by_the_read_back`) to see what differs | `runner.MODULE_TEXT_READBACK` stays True. Baseline can set `source_sha256` (A15) |
| L8 | How the engine writes DEFAULT, CHECK, computed and filter expressions, for a fixed list. With `--compare-normal-forms`: equal on two databases | Onboarding needs an accept list for expression text | Catalog to catalog compare with no accept list. Fixture for the table part of `catalog.py` |
| L9 | Catalog reads inside the open transaction see 500 new procedures, a table, an index and a trigger. A trigger is gone when its table is dropped (A18). It records the read times in two groups: the reads of the deploy path (modules by key, the list of objects) and the read of every module, which only export and baseline make. `observed` holds `seconds_of_the_deploy_path`, `seconds_of_the_read_of_every_module` and `read_of_every_module` (the sentence) | Wrong counts: the read-back design does not hold. A read of the deploy path over 10 s (`inconclusive`): you decide if the time is acceptable. The read of every module over 10 s does not change the result: the item prints `L9 note: the read of every module, used by export and baseline, took N s: more than the limit ...` | Read-back, A12 and A18 inside the transaction |
| L10 | Under `SET PARSEONLY ON` a syntax error raises, `SELECT 1/0` does not run, a bad module and a bad ALTER TABLE raise, nothing is created. The batch `/* azsqlcd:parseonly_off */ SELECT @@SPID;` gives no result set under PARSEONLY ON and one row after OFF | The plan has no syntax check. It must stop with `PARSEONLY_CANARY` | Plan step 9. It also shows why `FORBIDDEN_TOKEN` exists |
| L11 | (a) Which module kinds can be created before the object that they use. (b) The error when a function that a CHECK uses is changed. (c) Unbind of two chained schema-bound views, dependant first, keeps object id and grants | (a) A17 must become an error for procedures and triggers. (c) The `unbind` directive cannot be used | A17, A15, design (d) |
| L12 | The first connect to a paused serverless database succeeds inside 180 seconds. It records every error text | Add the text to `sqlerrors._KNOWN_MESSAGES` as 40613, or make `CONNECT_BUDGET_S` longer | The connect budget |
| L13 | If a session ends when its token expires. With SQL authentication: `not applicable` | Nothing fails. The result decides the rule | `min_token_minutes`; the row "Token expires on an open session" of the failure matrix |
| L14 | A member of `db_ddladmin` with the grants of `setup-sql` can run every statement kind and every read of the tool | Add the smallest grant to `state.setup_sql` for each entry of `missing` | The grants of `setup-sql` |
| L15 | The session applock: mode on the owner, test and request from a second session, kept after a rollback, free after KILL and after close. The filtered unique index of `azsqlcd.step` works | Do not deploy. The lock design does not hold | A4 |
| L16 | The catalog collation can be read and the case probe of the fence agrees with it. A failed `sp_refreshsqlmodule` in a transaction raises and rolls back | Change the probe of `catalog.fence_facts` | `FENCE_CASE_SENSITIVE`; the refresh step |
| L17 | A killed `ONLINE = ON, RESUMABLE = ON` build leaves a row in `sys.index_resumable_operations`. The index of a PRIMARY KEY has the constraint name. It records the time and the pages of ADD NOT NULL with a default | No row after the kill: `resolve --mark-not-applied` proves nothing. `inconclusive`: use more `--rows` | `resolve --mark-not-applied`; a fact for lint `LCK001` |
| X1 | The texts of errors 208, 2714, 2627, 245, 1222 and 1205 as the driver returns them; `sqlerrors.known_number` reads all six. A token connect to a database that does not exist gives the text of 18456 (fixture label `18456 connect to a database that does not exist`), not of 4060 | Change `sqlerrors._KNOWN_MESSAGES` to the captured text. Until then a lock timeout is exit 21, not 24 | `tests/fixtures/live/error_texts.json` is the fixture of `tests/unit/test_sqlerrors.py` |
| X2 | The guard values: (0, 1, new id) after a run-time error; after a batch that commits and begins again; after a second BEGIN. An error that a TRY block swallowed raises 3998 at the end of the batch and leaves `@@TRANCOUNT` 0. `(@@TRANCOUNT, XACT_STATE())` read without `CURRENT_TRANSACTION_ID()` is (0, 0) | Do not use the runner. The guard does not prove the transaction | A5 |
| X3 | After a column drop in a transaction: `catalog.broken_references` reports a module that uses the column and not a view that does not. The first error of the read is 207 (dropped column) or 208 (dropped table); 2020 alone only for a procedure that names a missing table. The failed read does not end the transaction. Three `#temp` cases record `findings_added_by_the_drop` (not run live yet). What `sp_refreshsqlmodule` repairs | A12 does not protect dependants. Do not drop, rename or retype columns with `table_model` | A12 |
| X4 | `CREATE USER ... WITH SID = 0x..., TYPE = E` is accepted, the stored SID is the 16 bytes, and the four grants of `setup-sql` are accepted | `setup-sql` must use `FROM EXTERNAL PROVIDER` | The user form of `setup-sql` |
| X5 | A session that is killed while the client waits for the batch with COMMIT raises | Write the pyodbc adapter | Gate G-D4, without the network case (section 7) |
| X6 | Fact item: what `ALTER SEQUENCE ... RESTART WITH n` does to `sys.sequences.start_value` | Nothing fails: a clear answer either way is a pass | Decides `catalog_tables.NOT_IN_DRIFT` for a sequence (RO-3) |
| X7 | `sys.numbered_procedures`, and `sys.indexes` of a view (the export facts) | The export facts of `catalog.py` need another query | `export` of numbered procedures and indexed views |
| X8 | The session-option row with `IMPLICIT_TRANSACTIONS` on and off | Every deploy stops with `SESSION_OPTIONS`, or the option is not seen | The twelve-value option row of the runner |
| X9 | A temporal table: `sys.tables.temporal_type`, `sys.periods`, the drop with versioning off. It runs in transactions that are rolled back | The temporal reader or the drop order is wrong | Temporal tables in `catalog_tables.py` |
| X10 | Masked columns: `sys.masked_columns` and the form in which the engine stores each masking function | The reader must compare with the stored form | Masked columns in the table model |

Driver gates of the design (Part 2 (k)). Any gate that fails: the pyodbc adapter is written
behind `session.py`, and the spike and `tests/live` run again with it.

| Gate | Spike item | Test in `tests/live/test_session_contract.py` |
|---|---|---|
| G-D1 | L2 (and L3) | `test_an_error_in_a_later_statement_is_raised_or_ends_the_transaction` |
| G-D2 | L5 | `test_a_killed_idle_session_is_never_replaced` |
| G-D3 | L4 | `test_batch_text_with_markers_and_escape_clauses_arrives_byte_identical` |
| G-D4 | X5 | `test_a_session_killed_while_it_waits_for_commit_raises` |
| G-D5 | L1 | `test_a_token_connect_gives_a_session_of_azure_sql_database` |

`spike/summary.json` holds the result of every item file in the folder, the five gates, the driver
decision, and which of the two constants may be set True.

Items that this script does not cover:

- The parser fixtures under `SET PARSEONLY ON` (blueprint step P4). Not built.
- Governance errors (40552 and the others). They cannot be made safely.
- The table model (`tables.py`, `catalog_tables.py`): `scripts/live_tables.py` (section 5a)
  covers a part. `docs/known-gaps.md`, section 2, lists each assumption with "no test yet".

## 5. Acceptance checks

`live_acceptance.py` builds a repository with twelve releases and runs the real modules. Each
line of its output is one check: `PASS` or `FAIL`, the name, and the exit code and reason code
that the tool gave. A check with a wrong exit code stops the run; the later scenarios are
reported as `not run`.

| Scenario | Checks | Row of the failure matrix, or amendment |
|---|---|---|
| setup-sql | The script makes the state and the users. A second run changes nothing | A2 |
| First release | A plan changes nothing. A token with too little life: 24 `TOKEN_TOO_SHORT` (with SQL authentication: `not applicable`). Deploy with the expected plan: 0. A second deploy sends no module | Plan algorithm; token row; modules by checksum |
| Release with no change | The release is recorded (run row, no step). An old plan: 22 `STALE_PLAN`, nothing sent. An older release: 0 `ALREADY_PAST` | A6; stale plan |
| Changed module | Only the changed module is sent | Modules by checksum |
| Failing batch | 21 `BATCH_FAILED`: no step row, run `failed`, batch 1 undone, stored error text redacted. Then a lock on the table: 24 `LOCK_TIMEOUT`. Then a second runner inside the first: 25 `LOCK_NOT_GRANTED`; a plan: 25 `RUN_LIVE` | Run-time error in batch N; lock timeout; applock not granted; A4 |
| Nontx, session lost | The step row says `started` before the batch is sent. Session killed after the batch: 23 `CONNECTION_LOST_NONTX`. Next deploy: 22 `STEP_UNRESOLVED`. `mark-not-applied`: 22 `NOT_PROVEN_ABSENT`. `mark-applied`, `clear-run`, then the release is recorded | Connection lost, nontx step; A16; A5 |
| Nontx, error | 23 `NONTX_FAILED`, step and run `unknown`. `mark-not-applied`, `clear-run`, then the deploy sends the migration again | Error in a nontx step, connection alive |
| Session killed in the transaction | The script follows `runner.RECONCILE_BY_LOCKING_READ`. `True` (the build since 2026-10-07): 24 `CONNECTION_LOST_ROLLED_BACK` from the deploy itself, run `failed`, no dead run in the next plan, the next deploy applies. `False`: 23 `CONNECTION_LOST_TX`, run stays `running`, batch 1 undone; a plan reports the dead run; `reconcile_by_locking_read`: 24 `CONNECTION_LOST_ROLLED_BACK`; a second kill, then the next deploy reconciles and applies. The scenario has 2 checks fewer with `True` | Connection lost, tx segment; runner killed; spike L6 on the real run row |
| Batch that ends the transaction | 23 `GUARD_FAILED`, run `unknown`. Plan: 22 `RUN_UNKNOWN`. `clear-run`. `mark-applied` without force: 22 `READBACK_REQUIRED`; with force: 0 | Guard not (1,1) |
| Data batch that changes a module | 21 `UNTOUCHED_CHANGED`, the view has its old text. Without the change: 0 | Read-back differs, or an untouched object changed |
| Drift, collision, tombstone | Drift on an untouched module is reported. New module over an unmanaged name: 22 `NAME_COLLISION`; `adopt-module`. Drift on a touched module: 22 `DRIFT_TOUCHED`; `accept-drift`. The plan lists two `OVERWRITE_MODULE` and one `DROP_MODULE`. The deploy drops the tombstoned module | Drift; collision; tombstone |
| Module that does not parse | 22 `PARSEONLY_FAILED`, no run row. The next release deploys | Compile error (caught by PARSEONLY); A7 |
| Other environment | 22 `FENCE_META_MISMATCH`; `rebind-environment`; deploy runs again | Fence; A16 |

Rows of the failure matrix that the script does not make: refresh of a dependant and blockers
(they need the table model), governance errors, transient errors at connect (L12), token expiry
(L13), loss of the network (section 7). The report lists them under `not_provoked`.

Not in the acceptance run: `export`, `baseline` and `drift` (onboarding): they have no live test
yet. The checks that need `table_model = true` are in `scripts/live_tables.py` (section 5a). The
script calls the library (`plan.compute_plan`, `runner.deploy`, the resolve actions), not the
command line. Of `cli.py`, only `build`, `plan`, `deploy --inline-plan` and `drift` ran once on
the live database by hand (section 11).

The last scenario sets `azsqlcd.meta.environment` to `sandbox` for a moment. If the script is
stopped inside that scenario, the next run refuses with `LIVE_BOUND_ENVIRONMENT`. Then run:
`UPDATE azsqlcd.meta SET environment = 'disposable';`

## 5a. Table-model checks

`live_tables.py` builds a repository with `table_model = true` and runs `gen.generate`,
`gen.verify`, `release.build`, `plan.compute_plan` and `runner.deploy` with `tables.Hooks`. It
writes `tables_report.json`. Exit codes as in section 3.

| Scenario | Checks |
|---|---|
| r2: create | `gen` writes one migration for a schema, two tables with a foreign key, defaults, a CHECK, a computed column and an index; `verify` has no error. Plan: `WORK`. Deploy with the expected plan: 0; the read-back agrees with the catalog; the tables, the schema and the view have managed rows. A second deploy sends nothing |
| r3: change | Two columns (one NOT NULL with a default) and two indexes (one filtered). The plan holds the `refresh` step of the view. Deploy: 0. The next plan: `NOOP`, no drift |
| Drift, hotfix | A column added by hand: the plan reports drift on the table. r4 holds the same column: 22 `DRIFT_TOUCHED` for plan and deploy. `resolve --mark-applied` with the table hooks reads the catalog back against the model: 0. The release is recorded; no drift stays |
| r5, r6: column drop | r5 drops a column and changes the view that uses it in the same release: the plan lists `DROP_COLUMN`; deploy 0. r6 drops a column that a managed procedure still uses: 21 `DEPENDANT_BROKEN` (finding `ERROR_207`), rolled back, run `failed` |

A view that uses the dropped column and is not changed in the release fails earlier, at its
`refresh` step: 21 `BATCH_FAILED`, failed step `refresh:<object key>`, rolled back. The release
after it cannot fix it (22 `CATCHUP_REQUIRED`): the fix is withdraw-and-replace of the migration.
This case was seen once and is not a check of the script. Since that run the runner reports a
refresh that the engine refuses as 21 `DEPENDANT_BROKEN` with the same failed step; that reason
code has not run on a database for a view.

## 6. After the runs: what to change, what to commit

Files under `tests/fixtures/live/`:

| File | From | Use |
|---|---|---|
| `spike/<id>.json`, `spike/summary.json` | Spike | The record of the P0 gate |
| `error_texts.json` | X1 and other items | Replaces `ENGINE_TEXT` in `tests/unit/test_sqlerrors.py` |
| `catalog_rows.json` | L7, X3, X9, X10 | Fixtures for `tests/unit/test_catalog.py` and `test_modules.py`. Also holds the keys `temporal_table` and `masked_columns` |
| `normal_forms.json` | L8 | Fixture for the table part of `catalog.py` |
| `acceptance_report.json` | Acceptance | The record of the P1 gate |

1. Read every file before the commit. The scripts replace the server name, the database name and
   the login name (`SERVER_NAME`, `DATABASE_NAME`, `LOGIN_NAME`). Look for what they cannot
   know: an IP address, a session tracing id, a subscription id.
2. Add the files one by one and commit on a branch:

   ```
   git checkout -b chore/live-fixtures
   git add tests/fixtures/live/spike/summary.json
   git add tests/fixtures/live/error_texts.json
   git add tests/fixtures/live/catalog_rows.json
   git add tests/fixtures/live/normal_forms.json
   git add tests/fixtures/live/acceptance_report.json
   git add tests/fixtures/live/spike/L1.json
   git commit -m "Add live spike and acceptance results"
   ```

   Repeat the `git add` line for each `spike/<id>.json`.
3. Changes that follow from the results. Each is a pull request of its own:
   - Both constants of `runner.py` are True since 2026-10-07 (section 11). L7 fails in a later
     run: set `runner.MODULE_TEXT_READBACK = False`. L6 fails, or the acceptance check
     `CONNECTION_LOST_ROLLED_BACK` fails: set `runner.RECONCILE_BY_LOCKING_READ = False`.
   - X1: put the captured texts into `tests/unit/test_sqlerrors.py`. Change
     `sqlerrors._KNOWN_MESSAGES` where a text was not recognised.
   - A gate failed: write the pyodbc adapter, add it to `ADAPTERS` in
     `tests/live/test_session_contract.py`, run sections 3.2 and 3.5 again.
   - Remove each proven row from `docs/known-gaps.md`, section 2. `docs/design.md`, Part 1,
     "As built", lists the two constants and what each flip changes.

## 7. Not automated: loss of the network during COMMIT

X5 kills the session. It does not cut the network. One case stays open: the COMMIT reaches the
server, the transaction is committed, and the client gets an error because the answer is lost.
The design covers this case with the commit counter (`azsqlcd.run.segments_committed`) and
exit 23. This build has no controlled test for it.

A coarse check is possible with the acceptance script:

1. Start the acceptance run. Wait for the line `-- session killed in the transaction; reconcile`.
2. Remove the firewall rule of the machine, or disable its network adapter, for 30 seconds.
3. The script then reports a failed check or `stopped by ...`. That is expected here.
4. Give the network back. Read the newest rows:
   `SELECT TOP (3) run_id, status, segments_committed, note FROM azsqlcd.run ORDER BY run_id DESC;`
   `SELECT run_id, migration_id, status FROM azsqlcd.step ORDER BY step_id DESC;`
5. Pass: no run row says `ok` for a run whose migration has no step row, and no step row exists
   for a run whose row says `failed`. A row that says `running` is right: the next deploy
   reconciles it.
6. Run the acceptance script again. It starts from a clean state.

The moment of the loss is not controlled, so this check can miss the COMMIT. Keep the row in
`docs/known-gaps.md` open until a controlled test exists.

## 8. GitHub checklist (G1 to G7)

Manual. On a scratch database repository. The blueprint asks for five small databases on one
disposable server, one for each environment, ten GitHub environments and six identities. Set the
repository up as `docs/setup.md` says (sections 1 to 6) before the first item. This checklist
was not run. On 2026-10-07 the workflows ran on GitHub Actions in a scratch demo repository with
no tenant, up to the Azure login step: `docs/known-gaps.md`, section 12, says what those runs
proved. `docs/known-gaps.md` also lists the wiring assumptions W1 to W15 and the bypass tests B1
to B6 that the runs of this checklist prove.

| Id | Do this | Pass when | If it fails |
|---|---|---|---|
| G1 | Tool repository private or internal, Actions access "repositories in the organization". In the scratch repository open a pull request that changes one module file | The job `verify / verify` runs the action of the tool at the tag. No token of the tool repository is configured | Copy the workflow files into the database repository and install the tool from a git URL with a read-only GitHub App token |
| G2 | Merge the pull request. Let the dev plan job run | `azure/login` succeeds with `allow-no-subscriptions` for an identity that has no Azure role. The log of the login step shows the subject `repo:<owner>/<scratch>:environment:dev-plan` | Change the federated credential subject to the one in the log; note the difference in `docs/setup.md` |
| G3 | Push a branch that adds a job with `environment: prod` to `db.yml`. Dispatch `db.yml` from that branch: `gh workflow run db.yml --ref <branch> -f release=r1`. Also name an environment that does not exist | No job of an environment starts; GitHub refuses by the deployment-branch rule before a token exists. The new environment name does not become an environment that a credential matches | Stop. Do not use the pipeline for prod. The environment rule is the only gate before the token |
| G4 | Put two targets into `[env.preprod]`. Promote a release to preprod | Record: one approval or two; all legs wait at once or one after another. Both legs deploy after approval | Keep one target for each environment (the tested shape) |
| G5 | `gh workflow run db.yml --ref main -f release=r<n>` once with a Linux runner and once with a Windows runner. Then merge two pull requests one minute apart | Both runs print the manifest digest that the release stores. The two merges give two releases `r<n>` and `r<n+1>` | A digest that differs between systems: the build is not reproducible; do not promote by dispatch |
| G6 | Follow section 3 of `uv run python scripts/setup_repo.py --repo <owner>/<scratch> --print-azure` for the prod deploy identity. Deploy to prod. Then add a job in another workflow file that names environment `prod` | The prod deploy logs in. The job of the other workflow file gets no token | Blocking for prod (A29). Keep prod off the pipeline until the subject with `job_workflow_ref` works |
| G7 | On the private runner set `AZSQLCD_OFFLINE=1` and remove the route to PyPI. Run a deploy | `uv sync --frozen` works from the filled cache. The driver loads (`libltdl7`, `libkrb5-3`, `libgssapi-krb5-2`). `gh`, `az` and Python 3.12 are present | Add the missing package to the image, or fill the uv cache for the tool tag |

Write the result of each item into `docs/known-gaps.md` (section 2, table "GitHub and Azure").

## 9. Problems

| Problem | Cause | Do this |
|---|---|---|
| An item is `inconclusive` with `stopped_by` | The probe met an error that it did not plan for | Read the text. A KILL that is refused: grant `KILL DATABASE CONNECTION` (section 1). Any other text: a defect of the script; report it |
| `CLEANUP FAILED` at the end of the spike | An object of schema `azsqlcd_spike` could not be dropped | Run the spike again with `--items L13` (no probe runs; start and end both clean up) |
| The spike waits in X1, X5 or L17 | These items start a helper process that signs in with `az` by itself. The item waits up to 4 minutes for its session | Check that `az account get-access-token --resource https://database.windows.net` works in a new shell |
| `LIVE_BOUND_ENVIRONMENT` after an acceptance run that was stopped | The last scenario was interrupted | `UPDATE azsqlcd.meta SET environment = 'disposable';` |
| Acceptance: the first check after a kill fails with another reason code | `session.py` did not classify the lost session as `SESSION_LOST` | Keep the report. The exit code is still 23 or 24. Compare with `first_execute.error` of spike item L5 |
| L17 `inconclusive` | The index build ended before it was seen | Use more rows: `--items L17 --rows 2000000` |

## 10. First live results (2026-10-07)

One read-only run, outside the scripts of this document, on a copy of WideWorldImporters on Azure
SQL Database (engine 12.0.2000.8, GP_S_Gen5_2, SQL_Latin1_General_CP1_CI_AS, driver mssql-python
1.15.0). It used the commit before the first fix wave with two patches. A wrapper let only SELECT
and SET through, and module text only inside the `SET PARSEONLY ON` window. No spike item and no
acceptance scenario of sections 4 and 5 ran, so no row of those sections is closed by it.
`docs/known-gaps.md`, section 8, has the full list of findings and engine facts.

What ran:

| Step | Result |
|---|---|
| Building blocks on a real session (session options, fence, applock test, service objective, object list, module capture, export facts, dependants, broken references, table facts, model read, blockers) | No SQL error. 396 user objects; 47 module captures; model read: 33 tables, 26 sequences, 4 table types, 9 schemas, 34 unsupported |
| Query shapes | 26 of 26 clean on the first try (24 tagged `azsqlcd:*`, the SET-options batch, the state-table probe), plus the PARSEONLY canary sequence. 0 needed a change of SQL text |
| Batches | 583 sent; 0 refused by the read-only guard; no write |
| `export` | Exit 0 on the first run. 106 files. 0 modules quarantined; 47 tables quarantined. A second export was byte-identical |
| `lint`, `verify` | Exit 0 and exit 0: 0 errors; 33 `EXA001` warnings, and `PRF000` in `verify`. Table-file check on 159 files: 0 findings |
| Fidelity (emitted file parsed back, compared with independent catalog queries) | 33 of 33 tables, 26 of 26 sequences, 4 of 4 table types: 0 differences |
| `build`; then `plan`, `drift`, `baseline --report-only` | `build` exit 0. The three database commands: exit 22 `STATE_MISSING` after 4 batches each (no state tables in this database). `drift --export`: exit 0, the file equals the export file |

What it found:

- The dependant check. `catalog.broken_references` called 18 of 47 modules of a healthy database
  broken: the engine gives `is_all_columns_found = 0` for every reference to a procedure, a scalar
  function, a sequence or a table type (RO-1; patched, 18 became 10, proven live). Of the 10, 7
  are sound procedures with `#temp` tables, 2 give error 2020 in a healthy database, and 1 name is
  truly missing (RO-2; the check is differential since then; not proven fixed live). Spike item
  X3 holds the `#temp` cases since then (a procedure with a `#temp` table joined to the altered
  table, and `UPDATE alias ... FROM #t AS alias`); they have not run on a database.
- Temporal tables were quarantined in this run: 34 of the 48 tables of the sample database are
  temporal tables or their history tables (`UNSUPPORTED`), and 13 more have a foreign key to one
  (`DEPENDS_ON_UNMANAGED`). 1 table of the sample schema was exported. Temporal tables are in the
  model since then; "Temporal tables, after the first run" below has the new numbers.
- Sequences: `start_value` equals `current_value` on 26 of 26 (RO-3). Spike item X6 is the write
  check (`CREATE SEQUENCE`, `ALTER SEQUENCE ... RESTART WITH n`, read `start_value`); it has not
  run. Until then `drift` leaves `start_value` out (`catalog_tables.NOT_IN_DRIFT`).
- `GIT_FAILED` did not name the revision (RO-4; patched). `export.md` holds two `[unmanaged]`
  blocks (RO-5; fixed since then: one block, at the end).

Temporal tables, after the first run (same day, two more runs outside the scripts):

| Step | Result |
|---|---|
| Emitted DDL of six temporal tables on a disposable database, in one schema that the run made and dropped | The engine accepted every statement: 0 refusals. 37 of 37 catalog checks agreed with the model (`sys.tables`, `sys.periods`, `sys.columns`, retention) |
| Generated statements of two fixture pairs | Column, CHECK and index added to a temporal table: ran, and the history table got the columns. Drop: `SET (SYSTEM_VERSIONING = OFF)`, `DROP TABLE` ran; the history table is left as a plain table |
| Read-only `export` of the sample database | Exit 0, 125 object files. Table files: 20 before, 39 now. Quarantined tables: 47 before, 11 unmanaged now. 17 history tables are listed as owned by the engine |
| The 11 that remain | One table with a masked column (`[Purchasing].[Suppliers]`, dynamic data masking) and the 10 tables that reference it. No reason is temporal |
| `lint` with `table_model = true` on the exported files | 0 errors, 33 `EXA001` warnings; 39 of 39 table files parse with 0 token differences |

These runs were made by hand with the library. `live_acceptance.py` has no temporal case, and no
temporal change went through `deploy`.

Still not proven (the list of this run; section 11 has the state after the write-path runs):

- `deploy`, `baseline` that writes, `resolve`, `setup-sql`, `live_spike.py`, `live_acceptance.py`:
  not in this run. The spike, the acceptance run, `setup-sql` and the resolve actions ran since
  then through the library (section 11); `baseline` and `export` on the write path did not.
- Any write experiment (sequence `RESTART WITH`; a dropped column against
  `sys.dm_sql_referenced_entities`).
- The consequence of RO-2 in the runner (exit 21 `DEPENDANT_BROKEN` for a redeployed procedure
  with a `#temp` table): read from code, not executed.
- `plan`, `drift` and `baseline` beyond `STATE_MISSING`.
- Reader branches with no object in that database: columnstore, partitioning, full-text,
  memory-optimized, XML compression, filtered index, index options, data compression, foreign key
  actions, disabled or untrusted constraints, system-named table constraints and
  `rename-constraints.sql`, alias types, synonyms, triggers and `STRING_AGG` over
  `sys.trigger_events`, numbered procedures, CLR, signed or encrypted modules, database DDL
  triggers, a non-default column collation, `NO_VIEW_DEFINITION` (the login was `dbo`).
- A definition with a leading comment before `CREATE`.
- The key-chunk paths above 500 keys (the largest call had 67).
- The unit tests of this repository with the two patches (they ran in the working copy of the
  live run only: 4260 passed).

For the next run on the disposable database, these objects are needed to reach the branches
above: a columnstore index, a partitioned table, a full-text index, a filtered index, an index
with a non-default option, a compressed table, a foreign key with an action, a disabled
constraint, a constraint with an engine-made name, an alias type, a synonym, a trigger, and a
module whose definition starts with a comment.

## 11. Write-path results (2026-10-07)

Database: a disposable database, Azure SQL Database, GP_S_Gen5_1 (serverless),
`is_read_committed_snapshot_on = 1`. Machine: macOS arm64, Python 3.12.12, mssql-python 1.15.0.
The runs used work copies of the commit of that hour plus the patches that the runs wrote. The
tree as it stands now holds those changes, and other changes made after the runs; it has not run
against a database as a whole.

### Spike (`live_spike.py`)

Three runs. The final full run: 19 pass, 1 inconclusive by design, 3 manual, exit 0, about 2
minutes. The database was left clean (0 user objects, 0 user schemas, no spike user).

- Pass: L1, L2, L3, L4, L5, L5b, L6, L7, L9, L10, L11, L15, L16, L17, X1, X2, X3, X4, X5.
- Inconclusive by design: L8 (it needs a second database).
- Manual, not run: L12, L13, L14.
- The first run had 4 fails (L4, L7, X2, X3). None was a driver failure: 5 defects of the tool
  and 5 of the script, fixed and proven in the later runs.

Driver gates. All five pass on this machine; `mssql-python` 1.15.0 stays, and no pyodbc adapter
is written.

| Gate | Result |
|---|---|
| G-D1 | Pass. L2: 9 of 9 batch shapes raise. L3: 5 of 5 compile and run-time shapes raise |
| G-D2 | Pass. L5: a killed idle session raises 08S01 at the next batch, with the applock and with no session state. No batch ran on a new session. The lock is free after the kill |
| G-D3 | Pass. L4: the batch that `sys.dm_exec_sql_text` holds is byte-identical; the literal is identical; the stored definition equals `modules.stored_text` of the batch |
| G-D4 | Pass. X5: KILL during `WAITFOR; COMMIT` raises after 3.0 s; the fence row reads 0 from another session |
| G-D5 | Pass on this machine only. L1: token connect, pooling off, session options asserted, token life 84 minutes at the start. Windows with Python 3.14 and the runner image: not run |

### Acceptance (`live_acceptance.py`)

52 of 52 checks pass on the real engine (the last run of that session, on an earlier commit than
the final one; exit 0, about 75 s). On the commit before the
patches: 14 pass, 1 fail, 8 not run. The one fail was a defect of the tool: a plan during a live
deploy ended as lock timeout 1222 after 30 s, not as 25 `RUN_LIVE`. With the lock test first in
`plan.compute_plan`: 52 pass.

What the 52 checks covered: `setup-sql` (twice, no change the second time); plan; deploy in one
transaction with the read-back and the state rows; a deploy with nothing to do; `STALE_PLAN`;
`ALREADY_PAST`; a failing batch (21 `BATCH_FAILED`, no step row, stored text redacted); a lock
timeout (24 `LOCK_TIMEOUT`); a second runner (25 `LOCK_NOT_GRANTED`) and a plan (25 `RUN_LIVE`)
during a live run; a non-transactional step with a lost session (23 `CONNECTION_LOST_NONTX`, then
22 `STEP_UNRESOLVED`, 22 `NOT_PROVEN_ABSENT`) and with an error (23 `NONTX_FAILED`); a session
killed in the transaction; a batch that ends the transaction (23 `GUARD_FAILED`, 22 `RUN_UNKNOWN`,
22 `READBACK_REQUIRED`); a data batch that changes a module (21 `UNTOUCHED_CHANGED`); drift, a
name collision (22 `NAME_COLLISION`), a tombstone, `adopt-module`, `accept-drift` (22
`DRIFT_TOUCHED` before it); a module that does not parse (22 `PARSEONLY_FAILED`); another
environment (22 `FENCE_META_MISMATCH`, `rebind-environment`); a token with too little life (24
`TOKEN_TOO_SHORT`).

One run had both constants of `runner.py` set True: 50 pass, 0 fail (the killed-session scenario
has 2 checks fewer in that mode). The tree has both constants True now, and on the final commit
the script gave 50 of 50. The killed session through `runner.deploy` itself gave 24
`CONNECTION_LOST_ROLLED_BACK`, the run row was `failed`, and the next deploy applied. Every module
deploy passed the text read-back.

By hand, through `python -m azsqlcd` on the same database: `build` 0, `plan` 0 (`already_past`),
`deploy --inline-plan` 0 (nothing sent), `drift` 0.

### Table model (`live_tables.py`)

18 of 18 checks pass (37 s): the four scenarios of section 5a. The DDL that `gen` writes was
accepted as written, and `catalog_tables.read_model` agreed with the model for: int, tinyint,
decimal(18, 2), nvarchar(100), varchar(200), datetime2(3), date, char(2); DEFAULT `((0))`, `((1))`,
`(sysutcdatetime())`; a CHECK; a computed column; PRIMARY KEY CLUSTERED; UNIQUE NONCLUSTERED; a
FOREIGN KEY with ON DELETE CASCADE added after both tables; a nonclustered index with INCLUDE; a
filtered index, stored by the engine as `([Status]>(0))`.

### Engine facts

Module text:

- `sys.sql_modules.definition` after `CREATE OR ALTER` is the batch byte for byte with the two
  words `OR` and `ALTER` deleted; the white space and the comments around them stay
  (`CREATE OR ALTER PROCEDURE` is stored as `CREATE   PROCEDURE`, three spaces). After `ALTER` the
  word is replaced by upper-case `CREATE`. After `CREATE` the text is identical. Text above the
  header, CRLF, trailing blanks, Unicode and the letter case of `CREATE` are kept. The same for
  procedure, view, the three function kinds and trigger.
- So the read-back compares with `modules.stored_text` of the file, and `export` writes
  `CREATE OR ALTER` and one space in place of the stored verb and the white space after it.

A broken dependant:

- `sys.dm_sql_referenced_entities` of a module that uses a dropped column raises 207 first; of a
  view whose table was dropped, 208 first; of a procedure that names a missing table, 2020 alone,
  with no row. A procedure that EXECs a missing procedure gives no error and one row with
  `referenced_id` NULL and `is_all_columns_found = 0`. The driver returns only the first record.
- The failed read does not end the transaction: the guard stays (1, 1) with `XACT_ABORT ON`. A
  failed `sp_refreshsqlmodule` (207) does end it.
- A view with `SELECT *` and a view that does not use the column stay sound after the column drop.

Locks and reads:

- Catalog reads wait for the schema locks of an open DDL transaction, also with
  `READ_COMMITTED_SNAPSHOT` on: `sys.tables` with `HAS_PERMS_BY_NAME` behind `ALTER TABLE`;
  `sys.objects` and the `sys.sql_modules` capture behind `CREATE SCHEMA`. `APPLOCK_TEST`, the fence
  query and the reads of the state rows did not wait.
- `LOCK_TIMEOUT` ends those waits on time: 1222 after 1.53 to 1.60 s at 1500 ms, 8 of 8.
- The locking read `WITH (READCOMMITTEDLOCK, ROWLOCK)` of the fence row waits while the writer
  lives. After KILL of the writer a new session gets the applock at once and reads the committed
  value: 40 of 40 right in each of two runs, slowest 0.21 s.

Guard values `(@@TRANCOUNT, XACT_STATE(), CURRENT_TRANSACTION_ID())`:

- In the transaction: (1, 1, id), stable over SELECT and DDL. It held after every batch of all 52
  acceptance checks.
- After a run-time error with `XACT_ABORT ON`, and outside any transaction: (0, 1, a new id for
  each statement). `XACT_STATE()` reads 1 there because `CURRENT_TRANSACTION_ID()` is in the
  statement; without it the pair is (0, 0).
- After `COMMIT; BEGIN` in one batch: (1, 1, other id). After a second `BEGIN`: (2, 1, same id).
- An error that a TRY block swallowed raises 3998 at the end of the batch; the guard then reads
  (0, 1, new id). (1, -1) never reaches the guard.
- Left open after an error (guard still 1, 1): a syntax error of the batch, `RAISERROR` severity
  16, a failed read of `sys.dm_sql_referenced_entities`. Ended: duplicate key, divide by zero,
  conversion error, `THROW`, unknown column, unknown table, failed `sp_refreshsqlmodule`.

Driver and session:

- An engine error arrives as the text of the first record, `[Microsoft][SQL Server]<message>`,
  with no number and no SQLSTATE. 25 captured texts are read with 0 misses.
- A session killed while the client waits for a batch: `General error`, text "Connection may have
  been terminated by the server", about 0.05 s after the KILL (class `SESSION_LOST` since then).
  A session killed while idle: the next batch raises 08S01. No batch ever ran on a new session.
- A token connect to a database that does not exist gives the text of 18456, in one attempt. The
  text of 4060 was not seen with token login.
- The driver does not send the application name: `sys.dm_exec_sessions` shows `MSSQL-Python`.
- Twice a session was lost with 08S01 "TCP Provider: Error code 0x274C" after a long silent
  wait; two controlled probes did not reproduce it.

Other:

- `CREATE USER ... WITH SID = 0x<16 bytes>, TYPE = E` is accepted in the form of `setup-sql`, and
  the four grants are accepted.
- `ALTER TABLE ... ADD col int NOT NULL DEFAULT (0)` on 300,000 rows: 0.03 s, the used pages did
  not change (metadata only).
- Can be created before the object that it uses: procedure, trigger, scalar function,
  multi-statement table-valued function. Cannot (208): view, inline table-valued function.
- `SET PARSEONLY ON`: a batch `SET PARSEONLY OFF; SELECT 1` runs its SELECT, so the
  `FORBIDDEN_TOKEN` guard of the plan stays.
- The normal forms of DEFAULT, CHECK, computed and filter expressions are recorded for one
  database. A CHECK with `IN (1, 2, 3)` is stored as `([c]=(3) OR [c]=(2) OR [c]=(1))`; an index
  filter keeps `IN`.

### The two constants

Both are True in `runner.py` since these runs.

| Constant | Why |
|---|---|
| `MODULE_TEXT_READBACK` | L7 passed on 32 steps over 11 header shapes and every module kind, with the compare against `modules.stored_text`; the acceptance run with the constant True passed the read-back for every module deploy (first create, change, overwrite after `adopt-module` and after `accept-drift`). With the old compare against the file text it would have failed every deploy |
| `RECONCILE_BY_LOCKING_READ` | Both conditions of the design are met. L6 passed twice (40 of 40 reads right in each run). The acceptance check gave 24 `CONNECTION_LOST_ROLLED_BACK` on the real run row 4 of 4 times, once through `runner.deploy` with the constant True |

### Still not proven

This list is the state after the runs of this section. Later that day the command line ran on
other disposable databases (`docs/known-gaps.md`, section 11). Where that section names an item
of this list as run, that section wins.

- `tests/live` (the driver contract with pytest).
- L8 on a second database; L12 (auto-resume of a paused serverless database); L13 (token
  expiry); L14 (the rights of a `db_ddladmin` member); gate G-D5 on Windows with Python 3.14 and
  on the runner image.
- X6 to X10, the L10 addition and the three `#temp` cases of X3: written after the runs, proven
  on scripted fakes only.
- Loss of the network during COMMIT (section 7). `CONNECTION_LOST_COMMITTED` was not seen live.
- The command line: only `build`, `plan`, `deploy --inline-plan` (`ALREADY_PAST`) and `drift` ran
  live. Not through `cli.py`: `setup-sql`, `resolve`, `export`, `baseline`,
  `deploy --expect-plan-file` with real work, `--ci github`.
- `drift`, `export` and `baseline` while a deploy is live: they read the catalog and can wait for
  its locks.
- Table model, outside `live_tables.py`: user-defined types, sequences, synonyms, renames,
  `ALTER COLUMN`, IDENTITY, the unbind of schema-bound modules, `TABLE_BLOCKER`, unmanaged
  dependants and `[ack]`, a non-transactional migration with table hooks, `accept-drift` of a
  table, `export` and `baseline`. A temporal change through `deploy`. Masked columns.
- A second database, Windows, Python 3.14, the runner image. WideWorldImporters and `master`
  were not written to.
- The GitHub checklist (section 8). After these runs the workflows ran on GitHub Actions up to
  the Azure login step (`docs/known-gaps.md`, section 12); every step after the login is open.
