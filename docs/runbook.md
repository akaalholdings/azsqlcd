# azsqlcd runbook

A run failed and you want help: `docs/triage.md` says which log to send and how to pack it
(`azsqlcd support-bundle`). Every non-zero exit of a database command prints `log: <path>`.

For the person on call. Not proven in production. On 2026-10-07 these paths ran on a test server
of Azure SQL Database, through the real command line: deploy, plan, drift, resolve, export,
baseline, a failed deploy that rolled back (exit 21), withdraw-and-replace, and the refusals
`STALE_PLAN`, `DRIFT_TOUCHED`, `NAME_COLLISION`, `INLINE_PLAN_GATED`, `FENCE_META_MISMATCH`,
`STATE_MISSING`, `CONSTRAINT_NAMES`, `ALREADY_BASELINED`, `TABLE_BLOCKER`, `TOKEN_TOO_SHORT` and
`CONNECTION_LOST`. Through GitHub Actions no row that needs a database was proven: every such
job stopped at the Azure login (`docs/known-gaps.md`, section 12). `docs/known-gaps.md` lists what
is not verified. Every row comes from the source of the tool: the place where the tool raises the
reason code.

## How to read a failure

1. Open the failed job. For test, preprod and prod the issue with the label `azsqlcd-incident` links
   to it. All open incidents: `gh search issues --owner akaalholdings --label azsqlcd-incident --state open`.
2. Read the last lines that the tool printed (stderr):

   ```
   REASON_CODE: message
     <one line for each object, blocker or failed batch: names, properties, hashes, redacted text>
   azsqlcd <command>: exit N
   ```

   The same exit code and reason code are in the head line of the job summary
   (`azsqlcd <command>: exit N REASON_CODE`), in the step outputs `exit_code` and `reason_code`, in
   the incident issue, and for `deploy` in `report.json` (artefact `report-<env>-<target>`, also an
   asset of the release).
   Exit 0 has the reason code `OK`; a deploy can answer `ALREADY_PAST`.
3. Go to the section of the exit code. Find the row of the reason code. One reason code can have a
   row under two exit codes: the exit code tells what happened to the database.
4. A reason code with no row: the first paragraph of the section gives the state of the database.
   Open an issue in the tool repository; a test keeps the rows complete.

| Exit | Meaning | State of the database |
|---|---|---|
| 0 | Done, or nothing to do | At the release, or newer |
| 21 | Failed and rolled back | As before the unit of work |
| 22 | Refused | Untouched: no batch of the release was sent |
| 23 | Outcome unknown | Unknown until a human inspects it |
| 24 | Clean stop | As before the unit of work. Safe to start again |
| 25 | Another run holds the lock | Untouched by this run |
| 30 | Drift found (`drift` only) | Untouched: `drift` only reads |
| other | The tool did not start | Untouched |

Engine messages are printed and stored redacted: every quoted or parenthesised part is
`<redacted>`. Nine messages that hold only names of objects and columns can keep their names.
Four of them name objects that exist in the database and always keep the names (1913, 2714, 3726,
5074), for example `The index 'IX_Order_Cust' is dependent on column 'CustId'.`. Five print a name
as the statement wrote it (207, 208, 2705, 3701, 4902), for example
`Invalid column name 'Status'.`. They keep the name only when the batch that was sent holds it as
an identifier. A statement that dynamic SQL builds from data, an `sp_refreshsqlmodule` step and a
read of the dependants do not: there the message reads `Invalid column name <redacted>.`. The
column `error_text` of `azsqlcd.run` never keeps a name of these five. `--show-error-text` prints
the full message on stderr. The tool refuses the flag for `--env prod` and together with `--rebind-environment`
(`SHOW_ERROR_TEXT_REFUSED`). No workflow passes it.

## Rules with no exception

- Never change a table of schema `azsqlcd` by hand. `resolve` is the only write path outside a deploy.
- Never run a migration file by hand. Never send a batch again. The tool sends no batch twice.
- Never edit a merged migration. Withdraw it and add a replacement by pull request.
- The tool never moves a database backward. Azure SQL Database has no in-place restore. Fix forward.
  `azsqlcd.run.started_utc` of a run is the point-in-time-restore reference; the job summary prints it.
- Engine messages are stored redacted. Do not paste full error text or SQL into an issue.
- prod prevents self-review: the person who starts a run cannot approve it. A prod deploy, a prod
  re-run and a prod `resolve` need a second DBA.

## Commands

Replace `<...>`. All commands run in the database repository.

- **RERUN** Run the failed jobs of a run again. The plan artefact is used again; the deploy job
  computes the plan again under the lock and stops with `STALE_PLAN` if anything changed. A gated
  environment asks for a new approval.
  `gh run rerun <run-id> --failed`
- **PROMOTE** Promote an existing release again, with a new plan. Each stage before `from_stage`
  only proves that it holds the release (plan job, no deploy, no approval).
  `gh workflow run db.yml --ref main -f release=r<n> -f from_stage=<stage>`
- **RESOLVE** One recovery action on one target, behind the approval of the deploy environment.
  `<n>` is the release that the failed run used.
  `gh workflow run resolve.yml --ref main -f environment=<env> -f target=<target> -f release=r<n> -f action=<action> -f subject=<subject> -f confirm_database=<database> -f reason="<why>"`
  `confirm_database` must be the name exactly as `SELECT DB_NAME();` prints it.
- **LOOK** Read the history of a target (plan identity or DBA, read only):
  `SELECT TOP (10) run_id, command, status, release_seq, segments_committed, failed_step, error_number, error_text, note, started_utc, finished_utc FROM azsqlcd.run ORDER BY run_id DESC;`
  `SELECT step_id, run_id, kind, migration_id, status, applied_utc, note FROM azsqlcd.step WHERE run_id = <run_id>;`
  `azsqlcd.run.note` of a run that did not end `ok` holds its reason code, or `reconciled` for a
  dead run that a later deploy closed.

`resolve` actions (A16). Each one takes the lock and writes a run row and a `resolve` step. The run
row copies the recorded release; a `resolve` never moves it.

| action | subject | Use |
|---|---|---|
| `mark-applied` | migration file | (a) A non-transactional step with the status `started` or `unknown` that did finish. The tool does no catalog check for this case: the check of the DBA is the only one. (b) The next pending transactional migration of the release, when its change is in the database already. With `table_model = true` the catalog must equal the model after the migration; if the tool cannot compare, add `-f force_no_readback=true` |
| `mark-not-applied` | migration file | A non-transactional step that left nothing: the index is absent and `sys.index_resumable_operations` has no row for it. Only for a batch that is `CREATE INDEX` or `ADD CONSTRAINT ... PRIMARY KEY` or `UNIQUE`. The next deploy sends the migration again |
| `accept-drift` | object key | Keep the live definition of a managed module that exists (the next deploy lists it as `OVERWRITE_MODULE` and sends the file). Tables: only with `table_model = true` and only when the live table equals the files |
| `adopt-module` | object key | Make an unmanaged module managed. The key must spell kind, schema and name exactly as the catalog does. Use it only for a module that has a file in the release; else the next plan stops with `MODULE_ORPHAN` |
| `clear-run` | run id | A run with the status `unknown`, after inspection. The run row keeps the status `unknown`; plans run again |
| `rebind-environment` | environment name | After a refresh of the database from another environment. The subject must equal the `environment` input |

After a failed non-transactional step two actions are needed: first `mark-applied` or
`mark-not-applied` for the step, then `clear-run` for the run. See "Unknown run".

## Exit 0

Done. The database holds the release, or a newer one.

| Reason code | Meaning | State of the database | What to do |
|---|---|---|---|
| `OK` | The release was applied, or it is recorded and nothing was pending. A deploy that finds nothing pending at a newer release number writes a run row with no step (A6) | At the release | Nothing |
| `ALREADY_PAST` | The database records a newer release and holds every migration of this one; or a deploy of a newer release committed work in this database and did not end `ok`. No batch was sent; older module text is never deployed again | Unchanged | Nothing. Reject pending approvals of older releases in the Actions page. If the newer deploy did not end `ok`, follow the row of its own exit code |

## Exit 21

The unit of work failed and was rolled back. The tool proved it: `@@TRANCOUNT` is 0 and the commit
counter of the run (`azsqlcd.run.segments_committed`) is what the tool knows. Schema and data are
as before the unit. Identity and sequence values that the run used are gone. The run row is
`failed`, with the step in `failed_step` and the reason code in `note`; the unit has no step row.
On a first converge (no migration pending, every module recorded with no source) modules run in
chunks, each chunk its own transaction: the chunks before the failed one are committed and stay.
Later stages did not run. The cause is in the release, so starting again gives the same result: fix
forward by pull request. The new release starts at dev; each target applies only what it lacks. A
merged migration is withdrawn and replaced (`withdrawn` on its chain line, a new file with
`replaces=`).

A reason code of exit 22 that the tool raises inside the transaction leaves as exit 21 with the
same code. The rows below are the ones that the runner raises there.

| Reason code | Meaning | State of the database | What to do |
|---|---|---|---|
| `BATCH_FAILED` | A batch raised an engine error that is not a lock timeout, a deadlock or a service limit: compile error, run-time error, constraint violation. The message names the step: `<migration file>#<batch number>`, `module:<key>`, `drop:<key>` or `refresh:<key>`. In a `resolve` or `baseline` run the step is the action, and the failed batch is a state write | Rolled back | Deploy: fix the SQL by pull request. A merged migration: withdraw and replace it; the steps are in `docs/setup.md`, section 7, "Withdraw and replace a merged migration". A refresh step that the engine refuses is not this code: see `DEPENDANT_BROKEN`. If the stored `error_text` (LOOK) reads like a lock timeout or a deadlock, the tool did not recognise the text: treat it as "Lock timeout in prod at night". `resolve` or `baseline`: start the workflow again; if it repeats, open an issue in the tool repository |
| `GOVERNANCE_LIMIT` | A batch reached a resource limit of the service and the session stayed alive (the tool knows the texts of 40552, 9002, 40549, 40550, 40551, 40544) | Rolled back | Never start again unchanged. By pull request: split the data batch, or move the work to a non-transactional migration with `ONLINE = ON, RESUMABLE = ON` |
| `READBACK_MISMATCH` | Before COMMIT the tool read what the unit made, and it is not what the files say. Module: it is absent, or its kind, `ANSI_NULLS`, `QUOTED_IDENTIFIER` or `SCHEMABINDING` differs. Table-class object (`table_model = true`): a property differs from the model of the release. The detail lines hold object, properties and hashes | Rolled back | A defect in the migration or in the tool. Compare the migration with the object file and fix by pull request. If the files are right, open an issue in the tool repository with `report.json` |
| `UNTOUCHED_CHANGED` | A `data` or `raw` batch of this unit changed a managed object that the release does not touch. The tool compared every managed object before and after the batches | Rolled back | By pull request: take the change out of the batch and put it in the file of the object (module) or in a model batch of a migration (table) |
| `DEPENDANT_BROKEN` | `table_model = true`: after the change, a managed view, function, procedure or trigger that uses an altered table has a finding that it did not have before the change. The check is differential: the plan records the findings of each dependant before the change (`dependant_findings` of `plan.json`, shown in the summary), and the run fails only for a new one. The detail `findings` holds only the new findings: `ERROR_207` (a column is gone), `ERROR_208` (an object is gone), `ERROR_2020`, `COLUMNS_NOT_FOUND`, `UNRESOLVED`, `COLUMN_GONE [schema].[table].[column]`. `COLUMN_GONE`: the dependant had a finding before the change that names no column (it did not bind, or it uses the table with a `#temp` table), and its text names a column that the release drops or renames. The check is a scan of the text: if the name is a column of another table, change the module file in the same release (a comment is enough) so that the module is sent again. A dependant that the release changes is checked by the rule for sent modules. A dependant with `ERROR_2020` before the change is read again, and another error number is a new finding. A view or a table-valued function that the release does not change is refreshed before that check, and a refresh that the engine refuses is the same reason code: the failed step is `refresh:<key>` and the finding is `ERROR_207`, `ERROR_208` or `REFRESH_FAILED`. With `table_model = false` the tool does not make this check | Rolled back | In one pull request: change the dependant (or tombstone it), withdraw the migration, and add its statements again as a replacement (`allow REPLACEMENT_EDGE`). A release that only changes the module is refused with `CATCHUP_REQUIRED`. Then merge; the new release starts at dev |
| `DRIFT_TOUCHED` | At an `unbind` step the live module differs from what the tool recorded. The plan checked the same under this lock, so an earlier batch of this unit changed the module, or someone changed it during the run | Rolled back | PROMOTE: `gh workflow run db.yml --ref main -f release=r<n> -f from_stage=<stage>`. If the plan now stops with `DRIFT_TOUCHED` (exit 22): "Drift on a touched object". If the deploy fails here again: the migration changes the module before it unbinds it; fix the migration by pull request |
| `UNBIND_HEADER` | At an `unbind` step the module has no managed row, its definition cannot be read, or its header has no `SCHEMABINDING` | Rolled back | As the `UNBIND_HEADER` row of exit 22 |
| `SESSION_OPTIONS` | A migration batch changed a session option: a `SET` statement, for example `SET LANGUAGE`, `SET NOCOUNT OFF`, `SET LOCK_TIMEOUT`, `SET ANSI_NULLS OFF` or `SET IMPLICIT_TRANSACTIONS ON`, also inside a procedure that the batch calls. The tool reads the options again before it sends a module, because the engine stores two of them with the module, and after the last batch of every unit, also when no module step follows. The detail `found` and `expected` are the two rows of twelve values; `IMPLICIT_TRANSACTIONS` is one of the twelve | Rolled back. After `SET IMPLICIT_TRANSACTIONS ON` the write of the status `failed` can be lost: the run row then stays `running` and the next deploy closes it as `failed` | By pull request: remove the `SET` statement from the batch (withdraw and replace). Lint refuses these `SET` statements in repository text (`FORBIDDEN_TOKEN`); a procedure that the batch calls is not read by lint |
| `INDEXED_VIEW` | A batch of the unit created an index on a view that a later module step or unbind of the same unit alters. `ALTER VIEW` drops every index of a view | Rolled back | By pull request: do not create the index and alter the view in one release |
| `NO_VIEW_DEFINITION` | A catalog read inside the transaction found a definition that the deploy identity cannot read | Rolled back | As the `NO_VIEW_DEFINITION` row of exit 22, for the deploy user `azsqlcd-<project>-<env>-deploy` |

## Exit 22

Refused. No batch of the release was sent. Schema and data are untouched. The offline commands
(`lint`, `verify`, `gen`, `build`, `targets`, `setup-sql`) use no database. A deploy that refuses
after it took the lock can have closed the row of a dead run (reconcile), or left a run row of its
own with the status `failed`. It changes nothing else. Remove the cause, then start again from the
plan job (PROMOTE), or, when the row says so, RERUN.

### Pull request and workstation: lint, verify, gen

| Reason code | Meaning | State of the database | What to do |
|---|---|---|---|
| `OUT_NOT_WRITABLE` | `build`: `--out` is a file, lies below a file, or is in a directory that cannot be written; or the release files could not be written there. The message names the path | No database is used | Give `--out` a directory that can be made, for example `--out dist`, and run `build` again |
| `LOG_NOT_FOUND` | `show-log` or `support-bundle`: `--log` names no file, or the directory of `--log-dir` holds no `azsqlcd-*.jsonl` | No database is used | Take the path from the line `log: <path>` of the failed run and give `--log <path>`; `docs/triage.md`, section 1, lists the directories |
| `BUNDLE_REFUSED` | `support-bundle`: the log is not a regular file or is over 5 MB, or the zip of `--out` could not be written. No zip was written | No database is used | Give the log of one run (`--log <path>`), and an `--out` path that can be written |
| `LINT_FAILED` | `lint` or `build`: the files hold a finding with the severity `error`. Every finding is printed as `path:line: severity CODE: message`. `build` writes no release | No database is used | Fix each finding: section "Lint and verify findings". Check with `azsqlcd lint` |
| `VERIFY_FAILED` | `verify`: a finding of lint, of the chain rules, of the module rules or of the model proof has the severity `error` | No database is used | Fix each finding: section "Lint and verify findings". Check with `azsqlcd verify --base "$(git merge-base origin/main HEAD)"` |
| `GEN_NEEDS_TABLE_MODEL` | `gen --name`: `azsqlcd.toml` has `table_model = false`, so there is no model to write a migration from | No database is used | Write the migration by hand, add its line to `migrations/migrations.sum`, then `azsqlcd gen --resum` numbers and hashes it |
| `GEN_REFUSED` | `gen` does not write this change. Each refusal is printed with code, object, message and hint: `ORD001`, `ORD002`, `IDENTITY_CHANGE`, `COMPUTED_CHANGE`, `COLLATION_CHANGE`, `ALTER_COLUMN_DEPENDANTS`, `USER_TYPE_CHANGE`, `SEQUENCE_CHANGE`, `SCHEMA_OWNER_CHANGE`, `ORDER_BLOCKED`, `TEMPORAL_CHANGE` (section "Temporal tables") | No database is used | Follow the hint. Hand-write the migration, then `azsqlcd gen --resum`; `azsqlcd verify --base "$(git merge-base origin/main HEAD)"` proves it |
| `GEN_INVALID` | `gen`: the name is not letters, digits and `_` (at most 180); the migration file exists already; a new migration of the branch does not replay on the base revision; `--resum` ran before the base was merged into the branch; or a generated statement is one that lint refuses (the message names the statement class and the finding code of lint) | No database is used | Follow the message. For `--resum`: `git merge origin/main`, keep the lines of both sides in `migrations.sum`, then `azsqlcd gen --resum` |
| `RENAME_INVALID` | `gen`: a `--rename` value cannot be read | No database is used | Write `--rename "column:[schema].[table].[old]=[new]"`. Kinds: `table`, `column`, `index`, `constraint`; a table and a constraint have no `[table]` part |
| `MODEL_INVALID` | Table-class files do not parse, or do not fit together, so there is no model. `gen`: an object is in a schema that has no schema file; the message names the file to add (`schema/schemas/<name>.sql` with `CREATE SCHEMA [name];`). `gen` and `verify`; with `table_model = true` also `plan`, `deploy`, `baseline` and `resolve` | Untouched | Fix the files that the message and `azsqlcd lint` name, by pull request |
| `FILE_INVALID` | A file is not valid UTF-8 | No database is used | Save the file as UTF-8 |

### Release and files: build, targets, and every command that reads a release

| Reason code | Meaning | State of the database | What to do |
|---|---|---|---|
| `NOT_ON_MAIN` | `build`: the commit is not on the first-parent chain of `refs/remotes/origin/main` | Untouched | Releases come only from `main`. Merge by pull request. A tag `r<n>` that points off `main` was created by hand: see "A release is missing" |
| `MAIN_REF_INVALID` | `build`: the ref of main is not a full ref name, or is a tag. `verify --base` (as a reason code): the base is a short name such as `origin/main`, and the repository has a tag of that name; git reads the tag first. Any other revision that git reads (`origin/main`, `HEAD`, a short commit id) is resolved by the command line to its full commit id. As a finding of the proof: the ref is not a full commit id and not a full ref name | Untouched | `build`: the tool default is `refs/remotes/origin/main`; a caller that passes `main_ref` must pass a full name such as `refs/heads/main`. `verify` and the proof of a withdrawal: give the commit, `azsqlcd verify --base "$(git merge-base origin/main HEAD)"`; the workflow passes the base commit of the pull request |
| `SHALLOW_REPOSITORY` | The checkout has no full history, so the release number cannot be computed | Untouched | Workflow: the checkout needs `fetch-depth: 0`. Workstation: `git fetch --unshallow` |
| `GIT_FAILED` | git could not read the commit, a blob or the history. The message names the git command and the revision or the ref of main (detail `revision`). Also: the `git` program that was found lies inside the repository or the current directory, and the tool does not start it. When the root is a bare repository the tool names it to git with `--git-dir`, so the git setting `safe.bareRepository=explicit` does not stop a build. A bare repository inside a working tree is not named: git refuses it under that setting, and the message holds the text of git. A long error text of git is shown as its first line, ` [...] `, and its last 300 characters | Untouched | `gh run rerun <run-id> --failed`. If it repeats, check that the checkout step has `fetch-depth: 0` and that `origin/main` exists in the checkout |
| `TREE_INVALID` | The commit, or the working tree, holds a path that a release cannot hold: a symbolic link, a submodule, a backslash, a path that is not UTF-8, a root folder in another letter case (`Schema/` for `schema/`), or a path that Windows cannot check out (a device name such as `aux`, a colon, a question mark, an asterisk, a double quote, an angle bracket or a vertical bar, a dot or a space at the end of a name, a file name above 255 bytes). The message names it | Untouched | Rename or remove the path by pull request. A folder in the wrong letter case: `git mv` |
| `BUNDLE_INCOMPLETE` | `build`: the commit holds no `azsqlcd.toml` or no `migrations/migrations.sum` | Untouched | Add the file by pull request. A repository with no migration holds `migrations/migrations.sum` with the one line `azsqlcd-sum 1` |
| `BUNDLE_INVALID` | `bundle.tar` holds a member that the manifest does not list, lists twice, or whose bytes differ; `manifest.json` is not a manifest; the manifest does not name the release that added a pending migration; or a member has a path that Windows cannot check out | Untouched | Same as `DIGEST_MISMATCH` |
| `DIGEST_MISMATCH` | `manifest.json` of the downloaded release does not have the digest that the release job computed from the tag | Untouched | A release asset was replaced. Treat as a security event: check the audit log of the repository for `release` events. Restore: build the tag on a clean clone (`azsqlcd build --commit <sha of r<n>> --out dist`), `gh release upload r<n> dist/manifest.json dist/bundle.tar --clobber`, then PROMOTE |
| `CONFIG_INVALID` | `azsqlcd.toml` is missing, is not TOML, or has an unknown key, a missing key or a bad value. A key name that holds `key`, `secret` or `password` is refused. The message names the key path, never a value. A required key that is missing is named with its table and one example line, for example `azsqlcd.toml: project.min_token_minutes: is missing. Add the key min_token_minutes to the table [project], for example: min_token_minutes = 20`. `env.<name>.auth` must be `oidc` or `managed-identity`. `[project] module_chunk` can be left out (100) | Untouched | Fix the file by pull request. For a missing key, add the example line to the table that the message names and set the value |
| `ENV_NOT_CONFIGURED` | `azsqlcd.toml` has no `[env.<name>]` for the stage | Untouched | Add the environment by pull request, or remove the stage from `db.yml` |
| `TARGET_NOT_CONFIGURED` | `[env.<name>]` has no target with the id that the command names | Untouched | Give the `target` input the id from `azsqlcd.toml`, or add the target by pull request |
| `CHAIN_INVALID` | `migrations/migrations.sum` breaks a chain rule (format, order, checksum, `replaces`), or a migration file of the release does not have the sha256 or the mode of its chain line | Untouched | In a pull request: merge `main`, run `azsqlcd gen --resum`, push. On `main`: fix by pull request |
| `MIGRATION_INVALID` | A migration file breaks a file rule (header, mode, batch kind, directive). `targets`: a non-transactional migration states no `expected-minutes`. `gen`: `migrations.sum` lists a new migration whose file is gone: run `azsqlcd gen --resum` (it drops that chain line), then `gen` again. With `table_model = true`: a pending model batch is outside the grammar | Untouched | Fix the file in the pull request. A merged migration: withdraw and replace |
| `MODULE_INVALID` | A module file breaks a file rule: path, header, kind, more than one batch, not `CREATE OR ALTER`, not UTF-8; or two module files name one object when case is ignored | Untouched | Fix the file by pull request. The message names path and line |
| `TOMBSTONE_INVALID` | `schema/_tombstones.toml` is malformed or names a key that is not an object key | Untouched | Fix the file by pull request |
| `ORD004` | The modules have a dependency cycle through a view or a function | Untouched | Change a module, or add `-- azsqlcd:ignore-dep [schema].[name]` to one module file, by pull request |

### Connection, fence and state: every database command

| Reason code | Meaning | State of the database | What to do |
|---|---|---|---|
| `DRIVER_MISSING` | A database command ran without the driver | Untouched | Workflow: the action call needs `db: true`. Workstation: `uv sync --frozen --extra db` |
| `AUTH_INVALID` | The sign-in of a database command cannot be used: the variable `AZSQLCD_AUTH` is not `entra`, `managed-identity` or `sql` (exact, lower case; not set or empty means `entra`); or `AZSQLCD_MANAGED_IDENTITY_CLIENT_ID` is set and is not a GUID; or SQL authentication in GitHub Actions (the variable `GITHUB_ACTIONS` is `true` or `1` in any letter case, the variable `GITHUB_RUN_ID` is set, or the command has `--ci github`); or the login or the password of SQL authentication holds a control character; or the password has fewer than 8 characters. The message names the variable, never its value | Untouched: no connection was tried | Workstation: set the variable to one of the three values, or remove it to sign in with the Azure CLI login (`az login`). Workflow: SQL authentication is refused there and has no override; set `auth = "oidc"` or `auth = "managed-identity"` for the environment in `azsqlcd.toml` by pull request, and remove `AZSQLCD_AUTH` from the variables of the runner machine. `docs/setup.md`, section 2, "Three ways to sign in" |
| `SQL_AUTH_MISSING` | `AZSQLCD_AUTH=sql`, and the login (`AZSQLCD_SQL_USER`) or the password (`AZSQLCD_SQL_PASSWORD`) is not set or is empty. The message names the variable | Untouched: no connection was tried | Set both variables in the shell that runs the command (`docs/setup.md`, section 2, "Three ways to sign in", shows how to do it with no password on a command line). The password is read only from the variable: no argument, no key of `azsqlcd.toml` and no file takes it. For the Azure CLI login, remove `AZSQLCD_AUTH` |
| `SESSION_OPTIONS` | At the start of a session the options that the tool set are not the options that it reads back (`XACT_ABORT`, `NOCOUNT`, the ANSI options, `LOCK_TIMEOUT`, `IMPLICIT_TRANSACTIONS` off, language `us_english`). The message holds both rows of twelve values | Untouched | `gh run rerun <run-id> --failed` once. If it repeats, the read of the options does not fit the driver: open an issue in the tool repository with the message. No deploy runs until it is fixed |
| `FENCE_ENGINE_EDITION` | The server is not Azure SQL Database (engine edition is not 5). The tool runs nowhere else | Untouched | Wrong `server` in `azsqlcd.toml`: fix by pull request. There is no override |
| `FENCE_READ_ONLY` | The database is not `READ_WRITE`: a read-only replica, or a database in a read-only state | Untouched | Secondary of a failover group: use the listener name as `server` in `azsqlcd.toml`. Else a DBA makes the database writable |
| `FENCE_DB_NAME` | The session is in another database than `azsqlcd.toml` names for the target (compared without case) | Untouched | Fix `database` of the target by pull request |
| `FENCE_CASE_SENSITIVE` | The catalog collation of the database is case-sensitive. The tool compares object names without case | Untouched | This build cannot manage the database. There is no override |
| `FENCE_META_MISMATCH` | `azsqlcd.meta` binds the database to another project or another environment than the command names | Untouched | Another project, or the wrong database: fix `azsqlcd.toml` by pull request. After a refresh from another environment: "Refresh of an environment from prod" |
| `STATE_MISSING` | Schema `azsqlcd`, one of its tables, the right to read one, or the `meta` row is missing. `deploy` and `baseline` never create them | Untouched | An administrator runs the script of `azsqlcd setup-sql --env <env> --target <target>` once on the target. Then RERUN: `gh run rerun <run-id> --failed` |
| `STATE_VERSION_UNSUPPORTED` | `azsqlcd.meta` holds a state version that this tool does not read | Untouched | Use the tool version that wrote the state (`tool_version` in LOOK): set that tag in the workflows of the database repository. This build has no upgrade command |
| `STATE_INVALID` | A row of `azsqlcd.object` holds a capture that is not a JSON object: the row was changed by hand | Untouched | Stop. Every command reads the state first, so no command runs. Find who changed the row (Azure SQL auditing). A DBA restores `catalog_capture` of that row from a point-in-time copy. Open an issue in the tool repository |
| `NO_VIEW_DEFINITION` | The definition of a module, or of an expression of a table, cannot be read: the principal has no VIEW DEFINITION on it. The message names the first object | Untouched | An administrator runs the `setup-sql` script again (it grants VIEW DEFINITION), or removes the DENY on the object. Then `gh run rerun <run-id> --failed` |
| `SQL_ERROR` | `deploy`, `baseline` or `resolve`: the database refused a read or a state write of the tool before any batch of the unit of work. The message holds the redacted engine text | Untouched | `gh run rerun <run-id> --failed` once. A missing right: an administrator runs the `setup-sql` script again. Else open an issue in the tool repository with `report.json` |
| `READ_FAILED` | A read-only command (`plan`, `drift`, `export`, `baseline --report-only`) got an engine error in a read. A lock timeout or a deadlock in a read also ends here. The usual cause of a lock timeout: a deploy started while the command read the catalog, and a catalog read waits for the schema locks of an open DDL transaction (measured live; the lock test of `plan` runs first, so a deploy that is already live gives 25 `RUN_LIVE`) | Untouched | `gh run rerun <run-id> --failed`. If it repeats and names a missing right: an administrator runs the `setup-sql` script again |
| `SHOW_ERROR_TEXT_REFUSED` | `--show-error-text` was given for `--env prod`, or together with `--rebind-environment` | Untouched | Run the command without the flag |
| `TOOL_DEFECT` | An exception that the tool did not classify, before the first batch of the unit of work was sent | Untouched | `gh run rerun <run-id> --failed` once. If it repeats, open an issue in the tool repository with `report.json`. Do not work around it |

### Plan: the plan job, and the deploy job under the lock

| Reason code | Meaning | State of the database | What to do |
|---|---|---|---|
| `STEP_UNRESOLVED` | A step has the status `started` or `unknown`: the outcome of a non-transactional migration is not known. A deploy that finds a dead run in such a step sets the step and the run to `unknown` and stops with this code | As the step left it | "Unknown run", steps 4 to 6 |
| `RUN_UNKNOWN` | An earlier run has the status `unknown` and no `clear-run` step after it | As the earlier run left it | "Unknown run" |
| `BASELINE_REQUIRED` | (a) The chain of the release starts with `baseline` and the database has no baseline step. (b) `table_model = true`: an object of the table model has no managed row and no pending migration creates it, so the database was never compared with the model | Untouched | Onboard the target: `docs/setup.md`, section 7. For (b), after the switch to `table_model = true`: run `baseline` again for the target: `gh workflow run onboard.yml --ref main -f environment=<env> -f target=<target> -f action=baseline -f release=r<n> -f confirm_database=<database>` |
| `CHAIN_DIVERGED` | The database holds an applied migration that the release chain does not hold at that position, or with another checksum; or a migration of the release is not applied and the database is past the release; a late replacement (a new line with `replaces=` for a withdrawn migration that this database never applied) is not this case: the plan has the note `late replacement: ...` and runs it after the migrations that the database applied already | Untouched | Stop. Someone deployed from another history, or changed a merged migration. Run LOOK; compare `azsqlcd.step` with `migrations/migrations.sum` of the release. Do not resolve until the cause is known; restore the chain on `main` by pull request |
| `CATCHUP_REQUIRED` | The database has pending migrations that earlier releases added. A release applies only the migrations that it added. The message says "promote r<k> first" and gives the two cases. Not raised for the first deploy to a database that records no release, no migration, no baseline and no committed deploy: the whole chain then runs with this release, and the plan has the note `first deploy: ...` (see "First deploy to an empty database") | Untouched | Case 1, r<k> can still be deployed: `gh workflow run db.yml --ref main -f release=r<k> -f from_stage=<stage>`; repeat with the next release that the message names; then promote the release that was refused. Case 2, r<k> cannot be deployed because its migration fails, or a module fails after it: withdraw the migration by pull request (`docs/setup.md`, section 7, "Withdraw and replace a merged migration"), then promote the release that holds the withdrawal; it carries the pending migrations of the later releases in one transaction (plan note `catch-up in one release: ...`) and the withdrawn migration never runs: "Catch-up after a withdraw-and-replace". Put the corrected change into the pull request of the withdrawal as a replacement (`replaces=`): a database that applied the migration keeps what it has, and every other database runs the replacement. A withdrawal with no replacement, with the corrected change as a new migration later, works only when no database applied the migration and `table_model = true`. If release r<k> does not exist: "A release is missing" |
| `NONTX_NOT_ALONE` | A non-transactional migration is pending together with other work. It must be the only change of its release. The message lists the other pending items | Untouched | The other items are module changes of releases that this database skipped: promote the release before it, `gh workflow run db.yml --ref main -f release=r<n-1> -f from_stage=<stage>`, then the refused release. In a catch-up over a withdrawn migration there is no tool path: `docs/known-gaps.md`, section 4 |
| `MODULE_ORPHAN` | A module is managed in this database and the release has no file and no tombstone for it. The tool never drops a module without a tombstone | Untouched | By pull request: add a `[[drop]]` entry for the key to `schema/_tombstones.toml`, or add the module file |
| `TOMBSTONE_CONFLICT` | An object has a module file and a tombstone, also when the two keys differ only by case | Untouched | By pull request: remove the file or the tombstone |
| `DIRECTIVE_UNRESOLVED` | `-- azsqlcd:unbind` names no managed module of this database, or more than one; or `-- azsqlcd:deploy-module` names no module file of the release | Untouched | `unbind` of a module that is unmanaged here and has a file in the release: `-f action=adopt-module -f subject=<object key>`. Else fix the migration by pull request (withdraw and replace) |
| `NAME_COLLISION` | A pending create names an object, an index or a constraint that exists in the database and is not recorded | Untouched | If the live object is the change of the migration (made by hand before): `-f action=mark-applied -f subject=<migration file>`. If it is another object: rename one of them (pull request, or DBA). A module that has a file in the release: `-f action=adopt-module -f subject=<object key>` |
| `DRIFT_TOUCHED` | A managed object that this release changes differs from what the tool recorded, or is gone. The detail lines hold object, property and two hashes | Untouched | "Drift on a touched object" |
| `DRIFT_BLOCK` | A managed object that the release does not change differs from what the tool recorded, and `[env.<env>]` has `drift = "block"` | Untouched | "Drift on a touched object"; or a DBA reverts the object by hand |
| `LEGACY_FLAGS` | A module to deploy is stored with `ANSI_NULLS` or `QUOTED_IDENTIFIER` off. A deploy would change the flags | Untouched | A DBA creates the module again with both options on; then `-f action=accept-drift -f subject=<object key>`; then PROMOTE |
| `INDEXED_VIEW` | A managed view that the release alters or unbinds has an index. `ALTER VIEW` would drop every index of it. Detail `objects` lists the keys | Untouched | Take the change of the view out of the release; or a DBA drops the indexes of the view by hand, PROMOTE, and creates them again. A view with an index that needs no change stays unmanaged (`[unmanaged] objects`) |
| `UNBIND_HEADER` | The header of a schema-bound module in the database cannot be rewritten: the definition cannot be read, or the header has no `SCHEMABINDING` | Untouched | A DBA alters the module in the database without `SCHEMABINDING`; then `-f action=accept-drift -f subject=<object key>`; then PROMOTE. The deploy binds it again from the file |
| `TABLE_BLOCKER` | `table_model = true`: a column or a table that the release drops, renames or retypes has a blocker that the files do not hold (index, user statistic, foreign key, schema-bound module), or an unmanaged module uses it. Each blocker is a detail line | Untouched | A DBA drops the blocker, or a pull request adds it to the files so that the migration handles it. An unmanaged dependant: fix it (DBA), or accept the break: add the line that the tool prints to `[ack] unmanaged_dependants` of `azsqlcd.toml` by pull request. Then PROMOTE |
| `RENAME_NOT_RESOLVED` | `table_model = true`: a pending `sp_rename` names an object that the model of the release cannot follow | Untouched | Fix the migration by pull request (withdraw and replace): the rename and the object files must agree |
| `FORBIDDEN_TOKEN` | A pending batch or module holds the word `PARSEONLY`. The text could switch off the syntax check and run, so it is not sent | Untouched | Fix the file by pull request. For a name, write `[PARSEONLY]` |
| `PARSEONLY_CANARY` | `SET PARSEONLY ON` did not stop a statement, or a syntax error raised no error, so the syntax check cannot be trusted on this target. No text was sent. In `export` also: `SET PARSEONLY OFF` did not give the session back | Untouched | `gh run rerun <run-id> --failed` once. If it repeats, open an issue in the tool repository. There is no override |
| `PARSEONLY_FAILED` | A pending batch or module does not parse on the target. The message names the file and the first line of the batch; every failing batch is a detail line with the redacted engine text | Untouched | Fix the SQL by pull request (a module: change the file; a merged migration: withdraw and replace) |

### Deploy

| Reason code | Meaning | State of the database | What to do |
|---|---|---|---|
| `STALE_PLAN` | The deploy job computed the plan again under the lock and it differs from the approved plan: state, release, target or tool changed after the plan job | Untouched | See "Stale plan" below. `gh workflow run db.yml --ref main -f release=r<n> -f from_stage=<stage>` |
| `PLAN_INVALID` | The plan file cannot be read, is not a plan of this format, or was changed (its hash is not the hash of its content) | Untouched | PROMOTE, for a new plan job. If it repeats, the plan artefact was changed between the jobs: treat as a security event |
| `INLINE_PLAN_GATED` | `deploy --inline-plan` for an environment that `azsqlcd.toml` marks `gated = true`. A gated deploy needs the approved plan | Untouched | By pull request: make `gated` of the stage in `.github/workflows/db.yml` equal to `azsqlcd.toml` |

### Resolve

| Reason code | Meaning | State of the database | What to do |
|---|---|---|---|
| `CONFIRM_MISMATCH` | `--confirm-database` is not the name of the database of the session. Also `baseline` | Untouched | Start the workflow again with `confirm_database` exactly as `SELECT DB_NAME();` prints it |
| `REASON_REQUIRED` | The reason is empty | Untouched | Start the workflow again with `-f reason="<why>"` |
| `RESOLVE_NOT_APPLICABLE` | The action does not fit the recorded state. `mark-applied`: the migration is not an open non-transactional step and not the next pending transactional migration. `mark-not-applied`: not an open non-transactional step. `accept-drift`: not a managed object, or the object is gone. `adopt-module`: not a module key, managed already, or no module with exactly this name and kind. `clear-run`: the run is not `unknown`. `rebind-environment`: bound to this environment already. `--force-no-readback` without `mark-applied` | Untouched | Read the message. Run LOOK. Choose the action that fits. `clear-run` on a run that is `running`: nothing to do, the next deploy closes the row |
| `READBACK_REQUIRED` | `mark-applied` of a pending transactional migration with `table_model = false`: the tool cannot prove that the migration is applied | Untouched | Check the database by hand against the migration file. Then RESOLVE again with `-f force_no_readback=true` |
| `READBACK_NOT_COMPUTABLE` | `mark-applied` with `table_model = true`: a later migration of the release changes the same object again, or the release does not hold the files to tell, so the model after the migration is not known | Untouched | RESOLVE with `-f release=r<k>`, the release that added the migration, when that release holds no later change of the object. Else check by hand and add `-f force_no_readback=true` |
| `READBACK_MISMATCH` | `mark-applied`, `accept-drift` of a table, or `baseline`: the database is not what the model of the release says. Nothing was executed, so this is a refusal here. The detail lines hold object, properties and hashes | Untouched | `mark-applied`: make the hand-made change equal to the migration, then RESOLVE again. `accept-drift`: revert the table by hand, or ship a migration. `baseline`: `docs/setup.md`, section 7: refresh the environment from prod, or a DBA aligns it |
| `NOT_PROVEN_ABSENT` | `mark-not-applied`: the index exists, or `sys.index_resumable_operations` holds a build of it; or the tool cannot read which index the migration builds | Untouched | A build row exists: a DBA runs `ALTER INDEX <index> ON <table> RESUME;` or `ALTER INDEX <index> ON <table> ABORT;`. The index exists: `-f action=mark-applied -f subject=<migration file>`. Another kind of batch: a DBA finishes the change by hand, then `mark-applied` |
| `TABLE_MODEL_REQUIRED` | `accept-drift` of a table-class object with a release that has `table_model = false` | Untouched | RESOLVE with a release that has `table_model = true` |
| `REBIND_ENV_MISMATCH` | `--rebind-environment` names another environment than `--env` | Untouched | Start the workflow again with `subject` equal to `environment` |
| `REBIND_PROD_SERVER` | `rebind-environment` to prod on a server that is not the prod server of `azsqlcd.toml` | Untouched | A database is bound to prod only on the configured prod server. Check the target; do not work around it |

### Onboarding: export, baseline, drift --export

| Reason code | Meaning | State of the database | What to do |
|---|---|---|---|
| `ALREADY_BASELINED` | The database has a baseline step. A second baseline is allowed only once, for the switch to `table_model = true`, while the state holds no row of a table-class object | Untouched | An object that changed since is drift: `-f action=accept-drift -f subject=<object key>`. A module that the tool does not manage: `-f action=adopt-module -f subject=<object key>` |
| `BASELINE_ACK_REQUIRED` | A live module differs from its file, the first deploy will overwrite it, and `onboarding/<env>/overwrite-ack.toml` of the release does not list its key | Untouched | "Baseline acknowledgement file" |
| `BASELINE_ACK_INVALID` | `onboarding/<env>/overwrite-ack.toml` does not hold exactly one key `modules` with a list of module keys | Untouched | Fix the file by pull request: "Baseline acknowledgement file" |
| `EXPORT_INCOMPLETE` | `export`: a file under `--out` could not be written. The message names the file and the error of the operating system. On Windows without long paths the usual cause is a full path above 259 characters | Untouched: the database was only read. A part of the files is under `--out` | Remove the directory of `--out`, fix the cause (a shorter `--out` path; on Windows `git config --global core.longpaths true` and the system setting `LongPathsEnabled`), and run `export` again |
| `SNAPSHOT_INVALID` | `baseline --report-only`: `onboarding/prod/snapshot.json` of the release is not a table snapshot that `export` wrote | Untouched | Export from prod again and commit the new `snapshot.json` by pull request |
| `CONSTRAINT_NAMES` | `baseline` (write mode, `table_model = true`): a constraint of the table files has a name that the engine made in this database (for example `PK__Customer__3214EC07A1B2C3D4`). Recorded under the name of the file it would be a name that the database does not have. Detail `constraints` lists them | Untouched: nothing was written | The tool wrote `onboarding/<env>/rename-constraints.sql` in the working directory of the run (the same text is `rename_constraints_sql` of the refusal). It holds `EXEC sys.sp_rename` only. A DBA reviews and runs it; then `gh workflow run onboard.yml --ref main -f environment=<env> -f target=<target> -f action=baseline -f release=r<n> -f confirm_database=<name>` again. `baseline --report-only` lists the same constraints before any write |
| `SECRET_LITERAL` | `export`: a table-class file would hold a `PASSWORD =` or `SECRET =` literal. Nothing was written. `drift --export`: the module text holds such a literal | Untouched | A DBA removes the literal from the object, or a pull request lists the object under `[unmanaged] objects` in `azsqlcd.toml`. Then export again |
| `MODULE_NOT_EXPORTABLE` | `drift --export`: the key is not a module key, the database has no module with this name and kind, the definition is encrypted, or the text cannot be a module file | Untouched | Give the module key as the drift report prints it. An encrypted module cannot be managed |

## Exit 23

The outcome is unknown. Part of the work can be in the database. Do not start again and do not
approve another run for this target. A human inspects the database and records the result with
RESOLVE. See "Unknown run" below. When the session stayed alive the tool rolled back what was still
open and set the run row to `unknown`: every plan then stops with `RUN_UNKNOWN`. When the
connection was lost the run row stays `running`; the next deploy closes it under the lock.

| Reason code | Meaning | State of the database | What to do |
|---|---|---|---|
| `GUARD_FAILED` | After a step the session was not in the transaction of the unit of work: a batch, or a procedure that it called, ended or began a transaction. Also when the guard query failed or gave no row | The part before the batch ended the transaction is committed. Run row `unknown` | "Unknown run" |
| `FENCE_MISMATCH` | A step failed and the tool rolled back, but the commit counter of the run is not the value that the tool knows: a transaction of this run did commit | Committed, in whole or in part. Run row `unknown` | "Unknown run". Expect that the release is applied |
| `ROLLBACK_UNVERIFIED` | A step failed and the tool could not prove the rollback: `@@TRANCOUNT` is not 0, or the proof queries failed | Unknown. Run row `unknown` | "Unknown run" |
| `CONNECTION_LOST_TX` | The connection was lost inside the transaction, and the locking read on a new session could not decide what happened (no session, the lock was not granted, or the read failed). When the read decides, the run ends with exit 24 (`CONNECTION_LOST_ROLLED_BACK` or `CONNECTION_LOST_COMMITTED`). A service limit that ends the session (for example 40552) also ends here | The engine rolled the unit back, or committed it whole with its state rows if COMMIT reached the server. Run row `running` | "Unknown run", step 3, first case. No `resolve` is needed |
| `CONNECTION_LOST_NONTX` | The connection was lost in a non-transactional migration, after its step row was set to `started` | The index build can run, be paused, or be done. Step `started`, run row `running`: no `clear-run` is needed, the next deploy closes the run row under the lock and stops with `STEP_UNRESOLVED` | "Unknown run", step 4 |
| `SESSION_OPTIONS` | In a non-transactional migration: the batch ran, and after it a session option is not what the tool set (a `SET` statement in the batch or in a procedure that it calls, `SET IMPLICIT_TRANSACTIONS ON` included) | The batch is applied and cannot be rolled back. Step and run `unknown` | "Unknown run", step 4 (`mark-applied` or `mark-not-applied`), then `clear-run`. Then remove the `SET` statement by pull request |
| `NONTX_FAILED` | An engine error stopped a non-transactional migration; the session stayed alive. Nothing is retried | A part can be applied. Step and run `unknown` | "Unknown run", step 4 |
| `GOVERNANCE_LIMIT` | A resource limit of the service stopped a non-transactional migration; the session stayed alive | A part can be applied. Step and run `unknown` | "Unknown run", step 4. Never start again unchanged: a DBA decides how the build can stay under the limit |
| `READBACK_MISMATCH` | In a non-transactional migration: the batch ran, and the read-back after it differs from the files | The batch is applied and cannot be rolled back. Step and run `unknown` | "Unknown run", step 4. Then fix the cause by pull request, as the row of exit 21 says |
| `UNTOUCHED_CHANGED` | In a non-transactional migration: the batch ran and changed a managed object that the release does not touch | The batch is applied. Step and run `unknown` | "Unknown run", step 4. Then fix by pull request, as the row of exit 21 says |
| `DEPENDANT_BROKEN` | In a non-transactional migration: the batch ran, and a managed dependant no longer binds | The batch is applied. Step and run `unknown` | "Unknown run", step 4. Then fix the dependant by pull request |
| `TOOL_DEFECT_AFTER_DISPATCH` | An exception that the tool did not classify, after a batch of the unit of work was sent. Also: `deploy`, `baseline` or `resolve` ended on an exception that the runner did not report, after a session was asked for; then there is no `report.json`, and the message says "it has no report of the run" | Unknown. Run row `unknown` when the session stayed alive; `running` or absent when the command line raised the code | "Unknown run". Open an issue in the tool repository with `report.json`, or with the log of the step when there is none |

## Exit 24

A clean stop. Nothing of this unit of work is in the database. The same run can start again: RERUN.

| Reason code | Meaning | State of the database | What to do |
|---|---|---|---|
| `TOKEN_UNAVAILABLE` | No access token for the database. With the Azure CLI sign-in (`AZSQLCD_AUTH` not set, or `entra`): `azure/login` did not run, or the federated credential does not match the subject of the job. With `AZSQLCD_AUTH=managed-identity` the message starts "no access token for the database from the managed identity": the machine has no managed identity, the identity endpoint gave no answer, or `AZSQLCD_MANAGED_IDENTITY_CLIENT_ID` is not the client id of a user-assigned identity of this machine. The message holds `<client id>` in place of the client id (with or without its dashes), and `<token>` in place of a token that the identity library put into its error text. Not raised with SQL authentication: it has no token | Untouched | `gh run rerun <run-id> --failed`. If it repeats, with OIDC: compare the subject in the `azure/login` log with the federated credentials (`scripts/setup_repo.py --repo <owner/name> --print-azure`). With a managed identity: check that the job ran on a self-hosted runner in Azure, that the identity of `[identities]` is assigned to that runner machine, and that the client id in `azsqlcd.toml` is the client id of that identity (`docs/setup.md`, section 2, "Three ways to sign in") |
| `TOKEN_TOO_SHORT` | The token expires sooner than the command needs. A command that writes needs `min_token_minutes`. A read-only command (`plan`, `drift`, `export`, `baseline --report-only`) needs 2 minutes. The message gives the minutes that are left and the minutes that are needed. Not applicable with SQL authentication (`AZSQLCD_AUTH=sql`): there is no token, the check does not run, and the plan prints `token minutes left: not applicable (SQL authentication has no access token)`. With a managed identity the check runs, and the message still speaks of the Azure CLI and `az login`: ignore that part | Untouched | With a managed identity: start the command again; the tool asks the identity for a token at each connect. In a workflow with OIDC: start the job again, `gh run rerun <run-id> --failed` (a new login gives a new token). On a workstation the Azure CLI gives out its cached token until a few minutes before the end of that token (seen in both live runs of 2026-10-07: 5 and 6 minutes left, 20 needed): wait the minutes that the message names and run the command again, or sign in again with `az login` |
| `CONNECT_FAILED` | No connection inside the 180-second budget, or an error at connect that is not transient. The message holds the redacted engine text, and the number when the tool knows the text. With SQL authentication (`AZSQLCD_AUTH=sql`) a failed login reads `error 18456, class OTHER; the driver message is not shown for SQL authentication`: the text of the driver can hold the login, so it is never printed, also not with `--show-error-text`. Any other connect error of SQL authentication keeps its redacted text, with the login and the password replaced by `<hidden>`, also in the form that a connection string holds them (in braces, `}` doubled). A driver text that names a connection keyword of the sign-in (`UID=`, `PWD=`) ends there with `<hidden>`: the driver put the connection string into its message | Untouched | `gh run rerun <run-id> --failed`. If it repeats it is not transient. "Cannot open database" (4060): the database name is wrong, or the identity is not a user of the database (run the `setup-sql` script; check `azsqlcd.toml`). "Login failed" (18456): the identity is not a user of this database. With SQL authentication: check `AZSQLCD_SQL_USER` and `AZSQLCD_SQL_PASSWORD`, and that the login has a user in the database (the `setup-sql` script creates no user for a SQL login; a DBA creates it). A timeout or a denied connection (47073): no network path from the runner (`docs/setup.md`, section 5) |
| `CONNECTION_LOST` | The connection was lost before any batch of the unit of work was sent, or in a read-only command (`plan`, `drift`, `export`, `baseline --report-only`) | Untouched | `gh run rerun <run-id> --failed` |
| `LOCK_TIMEOUT` | A batch waited longer than `lock_timeout_ms` for a lock (1222). The tool rolled back and proved it. Also a read or a state write before the unit of work | Rolled back | "Lock timeout in prod at night" below |
| `DEADLOCK` | The session was the victim of a deadlock (1205). The tool rolled back and proved it | Rolled back | `gh run rerun <run-id> --failed`. If it repeats: "Lock timeout in prod at night", step 4 |
| `RUN_NOT_CLOSED` | The unit of work is committed, with its step rows, and the write that closes the run row failed, or that write is still in an open transaction (`@@TRANCOUNT` is not 0 after it; detail `trancount`), so it is lost when the session ends. The release is applied and not yet recorded | At the release. Run row `running` | "The run row was not closed" below |
| `CONNECTION_LOST_ROLLED_BACK` | The connection was lost in the transaction, and a new session took the deploy lock and read with a locking read that the unit was rolled back (`runner.RECONCILE_BY_LOCKING_READ`, on since the live runs of 2026-10-07) | Rolled back. Run row `failed` | `gh run rerun <run-id> --failed` |
| `CONNECTION_LOST_COMMITTED` | The connection was lost in the transaction, and a new session took the deploy lock and read with a locking read that the unit was committed (`runner.RECONCILE_BY_LOCKING_READ`, on since the live runs of 2026-10-07). Not seen live: no test cuts the network during COMMIT | The unit is applied, with its step rows. Run row `failed` | PROMOTE: `gh workflow run db.yml --ref main -f release=r<n> -f from_stage=<stage>`. The deploy records the release |

## Exit 25

Another run holds the deploy lock of this database. This run changed nothing.

| Reason code | Meaning | State of the database | What to do |
|---|---|---|---|
| `RUN_LIVE` | `plan`: a deploy, a baseline or a `resolve` runs on this target now. The lock test is the first statement of a plan, before any catalog read. A run that starts after the test can still make a read of the plan wait: that ends as 22 `READ_FAILED` | Being changed by the other run | Find the other run in the Actions page. Wait for it. Then `gh run rerun <run-id> --failed` |
| `LOCK_NOT_GRANTED` | `deploy`, `baseline` or `resolve`: the lock was not granted inside `applock_wait_s` | Being changed by the other run | Same. If no other run exists in any workflow, a human session holds the lock: a DBA finds it with `SELECT request_session_id FROM sys.dm_tran_locks WHERE resource_type = 'APPLICATION' AND resource_description LIKE '%azsqlcd%';` and decides |

## Exit 30

`drift` only. A managed object in the database differs from what the tool recorded at its last
deploy, or is gone. The report prints class, object, property and two hashes; never definition
text. An object with no managed row is printed with the class `unmanaged`; it is not drift.
Nothing was changed. A later deploy that touches a drifted object stops with exit 22.

| Reason code | Meaning | State of the database | What to do |
|---|---|---|---|
| `DRIFT_FOUND` | One or more managed objects were changed or dropped outside the pipeline. Each object is a detail line | Untouched by the tool | "Out-of-band change by a DBA" below. Do it before the next release touches the object |

## Any other exit code: the tool did not start

The action prints "tool did not start". 1 and 2 belong to Python, argparse and uv, not to the
tool: a wrong argument in a workflow (exit 2), `--ci github` without `GITHUB_OUTPUT` and
`GITHUB_STEP_SUMMARY` (exit 2), a failed `uv sync` (no route to PyPI and no offline cache), a
missing Python 3.12 or a missing system library on the runner image. The database is untouched.
Read the lines above the message. Fix the runner image or the workflow. Then RERUN.

## Lint and verify findings

`lint` and `verify` print one line for each finding: `path:line: severity CODE: message`. A finding
with the severity `error` fails the command (`LINT_FAILED`, `VERIFY_FAILED`); a warning does not.
No database is used. The job summary states that lint is not a safety proof.

A statement that needs an allow line and has none. The finding code is the allow code; for
`LONG_LOCK` it is `LCK001`. Fix for each: write the line that the message prints above the
statement, in the same batch, with a real reason:
`-- azsqlcd:allow <CODE> <object> reason: <why this is safe>`.

| Code | Severity | Meaning | Fix |
|---|---|---|---|
| `DROP_TABLE` | error | `DROP TABLE` | Allow line with `[schema].[table]` |
| `DROP_COLUMN` | error | `ALTER TABLE ... DROP COLUMN` | Allow line with `[schema].[table].[column]` |
| `ALTER_COLUMN_LOSSY` | error | Every `ALTER TABLE ... ALTER COLUMN`: the lexer cannot tell a widening from a narrowing | Allow line with `[schema].[table].[column]` |
| `DROP_SEQUENCE` | error | `DROP SEQUENCE` | Allow line with `[schema].[name]` |
| `DROP_TYPE` | error | `DROP TYPE` | Allow line with `[schema].[name]` |
| `DROP_SCHEMA` | error | `DROP SCHEMA` | Allow line with `[schema]` |
| `RENAME` | error | `EXEC sp_rename` | Allow line with the old name, bracketed |
| `TEMPORAL_OFF` | error | `ALTER TABLE ... SET (SYSTEM_VERSIONING = OFF)`: the engine stops writing history, and the history table becomes a plain table that the tool does not manage. `gen` writes the statement before the `DROP TABLE` of a temporal table | Allow line with `[schema].[table]` of the temporal table. Say in the reason what happens to the history table |
| `OLD_MODULE` | allow code | Not a finding of its own. It answers `DAT001`: a `data` or `raw` batch that must run the old text of a module that the same pull request changes | `-- azsqlcd:allow OLD_MODULE [schema].[module] reason: <text>` above the batch |
| `UNMASK` | error | `ALTER TABLE ... ALTER COLUMN ... DROP MASKED`: every reader of the column sees the real values after it. `ADD MASKED WITH (FUNCTION = '...')` needs no allow line, and neither form needs `ALTER_COLUMN_LOSSY` | Allow line with `[schema].[table].[column]` |
| `LCK001` | error | A statement that holds a long lock, in a transactional migration, on a table that the migration did not create: `CREATE INDEX`, `ADD PRIMARY KEY` or `UNIQUE`, `ADD CHECK` or `FOREIGN KEY` without `WITH NOCHECK` | `allow LONG_LOCK [schema].[table]`; or move the statement to a non-transactional migration with `ONLINE = ON` |
| `DATA_NO_WHERE` | error | `UPDATE` or `DELETE` with no `WHERE` in a data batch | Add the `WHERE`, or an allow line with the target as written |
| `TRUNCATE` | error | `TRUNCATE TABLE` in a data batch | Allow line with the table |
| `DYNAMIC_SQL` | error | `EXEC (...)`, `EXEC @variable` or `sp_executesql` in a data batch | `allow DYNAMIC_SQL batch` |
| `EXEC_PROC` | error | `EXEC` of a procedure in a data batch | Allow line with the procedure as written |
| `RAW` | error | Every `raw` batch | `allow RAW <object key of the raw directive>` |

Allow lines and migration batches.

| Code | Severity | Meaning | Fix |
|---|---|---|---|
| `ALLOW_UNUSED` | error | An allow line matches no statement of its batch, or its code is not an allow code | Remove the line, or correct code and object |
| `ALLOW_REASON` | error | The reason of an allow line starts with `TODO` | Write the reason, then `azsqlcd gen --resum` |
| `ALLOW_FORMAT` | error | A comment looks like a directive and is not one: a directive starts with `-- azsqlcd:` exactly (two hyphens, one space, lower case) in column 0. The line is a plain comment and does nothing | Write the line in the exact form, then `azsqlcd gen --resum` |
| `NNL001` | warning | `ALTER TABLE ... ADD` of a `NOT NULL` column with no `DEFAULT`, on a table that the migration did not create. The engine refuses the statement when the table has a row: it passes in an empty database and fails in the first one with data (seen live: exit 21 `BATCH_FAILED`) | Add a `DEFAULT`; or add the column `NULL`, fill it, and alter it to `NOT NULL` |
| `FORBIDDEN_TOKEN` | error | Migration batch: `BEGIN TRANSACTION`, `COMMIT`, `ROLLBACK`, `SAVE TRANSACTION`, `RAISERROR`, `RETURN`, `GOTO`, `SET NOEXEC`, `SET PARSEONLY`, `SET XACT_ABORT`, `USE`, or a line that starts with `:`. Module file: the word `PARSEONLY`, or `SET NOEXEC ON` or `OFF`. The rule also applies to `raw` batches | Remove the statement. To stop with an error use `THROW`. For a name write `[PARSEONLY]` |
| `THREE_PART_NAME` | error | A name with three or more parts. `DROP INDEX schema.table.index` (the old form) and `DROP STATISTICS schema.table.statistics` are not findings | Use two-part names; for a column use a table alias |
| `SECURITY_STATEMENT` | error | `GRANT`, `DENY`, `REVOKE`, `REVERT`, `EXECUTE AS`, `ALTER ROLE`, `ALTER AUTHORIZATION` or `CREATE USER` outside a `raw` batch | Remove it: users, roles and permissions are not managed |
| `DATA_DDL` | error | `CREATE`, `ALTER`, `DROP` or `sp_rename` in a data batch, temporary tables included | Move the statement to a model batch; a data batch is DML only |
| `DATA_FORBIDDEN` | error | `ENABLE TRIGGER`, `DISABLE TRIGGER`, `DBCC`, or `SELECT ... INTO` a table that is not temporary, in a data batch | Remove the statement; create the table in a model batch |
| `DATA000` | error | A `data` batch, and `[project] data_batches` of `azsqlcd.toml` is not `true`. Data batches are switched off by default: the tool is for structural changes | Take the data change out of the migration and run it outside the tool. Only when the owner of the repository decides to use data batches: set `data_batches = true` under `[project]` by pull request |
| `MODEL_STATEMENT` | error | A model batch is not one statement of the closed list: `CREATE` or `DROP TABLE`, `ALTER TABLE ADD`, `ALTER COLUMN` or `DROP`, `CREATE` or `DROP INDEX`, `CREATE`, `ALTER` or `DROP SEQUENCE`, `CREATE` or `DROP SCHEMA`, `TYPE`, `SYNONYM`, `EXEC sys.sp_rename`. A second message, "X has no place in the statement before it, so it starts a second statement", names a word that the statement cannot hold | Put `GO` between statements. Put a name that is a keyword in brackets. DML goes in a `-- azsqlcd:data` batch, other DDL in a `-- azsqlcd:raw` batch |
| `STATEMENT_UNREADABLE` | error | The lexer cannot read the object of the statement, so no allow line can match | Write the name as `[schema].[name]` |
| `DROP_INDEX` | warning | `DROP INDEX` | Check that no query needs the index |
| `DROP_CONSTRAINT` | warning | `ALTER TABLE ... DROP CONSTRAINT`: the rule is no longer enforced. Also for `ALTER TABLE ... DROP PERIOD FOR SYSTEM_TIME` | Check that the drop is meant |
| `NTX004` | warning | `ONLINE = ON` without `WAIT_AT_LOW_PRIORITY` in a non-transactional migration: every later statement on the table waits behind the build. Each statement of a batch is read alone. An online `ALTER COLUMN` and a columnstore build take no such clause and are not findings | Add `WAIT_AT_LOW_PRIORITY (...)` |
| `NTX005` | error | A non-transactional migration states no `expected-minutes`; the job timeout is computed from it | Write `-- azsqlcd:mode nontx expected-minutes: <N>` |
| `NTX006` | error | `RESUMABLE = ON` in a transactional migration: a resumable build cannot run in a transaction | Move the statement to a migration with `-- azsqlcd:mode nontx expected-minutes: <N>`, or remove `RESUMABLE = ON` |
| `PAR001` | error | The parentheses of a migration batch (model, raw or data; `tx` or `nontx`) or of a module file under `schema/` do not balance: a `)` closes nothing, or a `(` is never closed. The message names the line of the first such parenthesis. A parenthesis in a string, a comment or a quoted name is not counted. Without the finding only the engine refuses the text, at the deploy | Go to the line that the message names: remove the `)` that closes nothing, or add the `)` that is missing. No allow line exists for this finding. A merged migration cannot be corrected (`CHN004`): withdraw and replace it by pull request (`docs/setup.md`, section 7, "Withdraw and replace a merged migration"). A migration whose line of `migrations/migrations.sum` says `withdrawn` is not reported, so that pull request and every later one pass. A repository that held such a merged migration before the tool had this rule gets the finding from `lint`, `verify` and `build` until the migration is withdrawn |

Files of one revision.

| Code | Severity | Meaning | Fix |
|---|---|---|---|
| `SECRET_LITERAL` | error | A file holds a `PASSWORD =` or `SECRET =` literal | Remove the literal; a secret is never written in the repository |
| `UNKNOWN_PATH` | error | A file under `schema/` or `migrations/` is not a file of the layout. A local `verify` also reports an untracked file such as `.DS_Store` | Remove the file, or move it to `schema/<kind directory>/<schema>.<name>.sql` or `migrations/<NNNN__name>.sql` |
| `FILE_INVALID` | error | A file is not UTF-8, or a table-class file does not lex | Save as UTF-8; close the string, comment or bracket |
| `MODULE_INVALID` | error | A module file breaks a file rule: path, header, kind, more than one batch, not `CREATE OR ALTER` | Fix the file; the message names the line |
| `MIGRATION_INVALID` | error | A migration file breaks a file rule: header, mode, batch kind, directive | Fix the file; the message names the line |
| `CHAIN_INVALID` | error | `migrations/migrations.sum` breaks a format rule | Merge `main`, then `azsqlcd gen --resum` |
| `TOMBSTONE_INVALID` | error | `schema/_tombstones.toml` is malformed. The message names the line and the fix | Fix the file |
| `CONFIG_INVALID` | error | `azsqlcd.toml` is missing or breaks a rule | Fix the key that the message names |
| `CHN002` | error | A migration file has no line in `migrations.sum`. The message names the fix | `azsqlcd gen --resum` |
| `CHN003` | error | A chain line names a file that does not exist | Restore the file, or remove the line if it is not merged |
| `CHN004` | error | The sha256 of a chain line is not the sha256 of its file. The message ends with the fix: "If the migration is not merged yet, run azsqlcd gen --resum; a merged migration never changes" | New migration: `azsqlcd gen --resum`. Merged migration: undo the edit of the file |
| `CHN005` | error | The mode of a chain line differs from the mode in its file | `azsqlcd gen --resum` |
| `ORD004` | error | A dependency cycle through a view or a function | Change a module, or add `-- azsqlcd:ignore-dep [schema].[name]` |
| `CYCLE_BROKEN` | warning | A dependency cycle of procedures and triggers only; the tool breaks it in name order | None needed |
| `EXA001` | warning | A module header has `EXECUTE AS` other than `CALLER`: the module runs with the permissions of another principal | The reviewer checks that this is meant |
| `MODULE_DUPLICATE` | error | Two module files name one object when case and kind are ignored | Remove one file |
| `DIR001` | error | A module file holds a directive that does nothing there (it reads only `after` and `ignore-dep`), or a directive that does not start in column 0 | Correct or remove the line |
| `DIR002` | warning | `-- azsqlcd:after` or `-- azsqlcd:ignore-dep` names no module file of the repository | Correct the name, or remove the line |
| `TMB002` | error | A tombstone for a module that still has its file | Remove the file or the tombstone |
| `MODULE_CASE` | warning | A module file is renamed and the new name differs in letter case only. The database compares names without case, so it is the same object | Nothing. Do not add a tombstone for the old name: that is `TMB002` |

A change against the base revision (`verify` only).

| Code | Severity | Meaning | Fix |
|---|---|---|---|
| `CHN001` | error | A chain line of the base revision changed (only the word `withdrawn` may be added), new lines are not at the end, or `replaces` names no withdrawn line | Undo the change; merge `main`, then `azsqlcd gen --resum` |
| `CHN006` | error | `table_model = false`: a merged migration is withdrawn (the word `withdrawn` on its chain line) and the same pull request adds no replacement: a database that did not run it would skip it for good. With `table_model = true` the proof holds this rule (`WDR004`) | Add a migration whose chain line holds `replaces=<file>` in the same pull request |
| `KND001` | error | A removed module file and a new module file give one name two kinds | Drop the old object in one release and add the new one in a later release, or use a new name |
| `TMB001` | error | A module file is removed and `schema/_tombstones.toml` has no `[[drop]]` entry for it. The message prints the three lines to add: `[[drop]]`, `object = "<KIND>:[schema].[name]"`, `reason = "<why the module is dropped>"` | Add the entry, with a reason |
| `NTX003` | error | A non-transactional migration is not the only change of its pull request (A8) | Put it in a pull request of its own |
| `DAT001` | error | A `data` or `raw` batch of a new migration uses a module that the same pull request adds or changes; the batch would run the old text | Write `-- azsqlcd:deploy-module [schema].[name]` above the batch, or `-- azsqlcd:allow OLD_MODULE [schema].[name] reason: <text>` |
| `REN001` | warning | A new migration renames an object and a module file still uses the old name | Change the module file. With `table_model = true` the deploy fails with `DEPENDANT_BROKEN` if the module no longer binds; with `false` nothing checks it |
| `DRP002` | warning | A module has a new tombstone and another module file still uses its name | Change the other module file |
| `DRP003` | warning | A new migration drops a table or a column and a module file still uses the name | Change the module file |

Table model and proof (`table_model = true`; `PRF000` also without it).

| Code | Severity | Meaning | Fix |
|---|---|---|---|
| `NF000` | error | A table-class file differs from its canonical form: the file says something that the model does not hold, or spells it another way. The message names the line and the tokens | Write the file as the message says. `docs/known-gaps.md`, section 3, lists the canonical form |
| `MDL001` | error | The model of the revision does not fit together: for example an index on a missing column, a foreign key with no matching key, two files for one object | Fix the object files |
| `MODEL_INVALID` | error | Table-class files do not parse, so there is no model and no proof | Fix the first file that the message names; `azsqlcd lint` lists all |
| `PRF000` | warning | No proof: `table_model = false`, or this pull request sets it to `true` | None. After the switch run `baseline` on every target |
| `PRF001` | error | The new migrations, replayed on the base model, do not give the model of the files. The message names object and property; or the model refuses a statement of the migration | Make the migration and the object file agree |
| `PRF002` | error | A table-class file changed and the pull request adds no migration. The message holds what `gen` would write | `azsqlcd gen --base origin/main --name <name>` |
| `PRF003` | error | `allow SET_NOT_NULL` is missing on an `ALTER COLUMN` that makes a column `NOT NULL`; or an allow line `SET_NOT_NULL` or `REPLACEMENT_EDGE` matches nothing | Add or remove the allow line |
| `PRF004` | error | The pull request that sets `table_model = true` adds a migration: nothing can prove it | Add the migration in a pull request of its own |
| `PRF005` | error | The pull request sets `table_model` from `true` to `false`. It is checked as with `table_model = true` | Do not switch the model off by pull request: revert the line |
| `PRF006` | error | The replay accepts the new migrations, and `plan` could not follow one of their renames: a constraint is renamed and then goes with `DROP TABLE` of its table | Drop the table without the rename |
| `PRF007` | error | A new migration alters, renames or drops a column or a table that a schema-bound module of the base revision names, and has no `-- azsqlcd:unbind` line for that module (or for a schema-bound module on top of it). `gen` writes the line; a hand-written migration needs it too. For a column, the module must name the table and the column. Without the line the plan refuses the merged release with `TABLE_BLOCKER`, and a merged migration cannot change (seen live at onboarding, before this check existed) | Write the line that the message prints above the first statement, then `azsqlcd gen --resum` |
| `TMB003` | error | A tombstone that this pull request adds names a function, and a `CHECK`, a `DEFAULT` or a computed column of a table-class file still uses the function. The engine refuses `DROP FUNCTION` while such an expression uses it. Only with `table_model = true` | Take the use out of the table-class file (with its migration) in the same pull request, or keep the function file and remove the tombstone |
| `RAW001` | error | A `raw` batch for an object that `[unmanaged] objects` of `azsqlcd.toml` does not list | A managed object changes through a model batch; or list the object as unmanaged |
| `RAW002` | error | The text of a `raw` batch names a table-class object or a module that the repository manages. A raw batch is not replayed and not read back, so what it does to that object has no proof. A name of one part is read as an object of schema `dbo`; strings and comments do not count | Change a table-class object in a model batch and a module in its file; a raw batch names unmanaged objects only |
| `WDR001` | error | A withdraw-and-replace in a pull request that also changes a table-class file | Split the pull request |
| `WDR002` | error | The replacement gives another model than the withdrawn migration, or the model before the withdrawn migration cannot be read | Correct the replacement |
| `WDR003` | error | The replacement has no `-- azsqlcd:allow REPLACEMENT_EDGE <withdrawn id> reason: ...` | Add the line |
| `WDR004` | error | A merged migration with a model batch is withdrawn with no replacement, and the table-class files do not hold the model without it, or that cannot be proven offline | Add the replacement in the same pull request (a new line with `replaces=<file>`), or change the table files to the model without the withdrawn migration. A database that did run the withdrawn migration then differs from the model. `drift` does not report it (open finding N2-F5 of `docs/known-gaps.md`): the recorded capture of the table is the one of the withdrawn migration. A DBA compares that database with the table files and repairs it by hand |
| `WDR005` | error | A `replaces=` line names a migration that an earlier pull request withdrew | Add the change as a normal migration without `replaces=`. A replacement is added only by the pull request that withdraws |

The parser gives these codes for a table-class file and, with `table_model = true`, for a model
batch of a new migration. All are errors.

| Code | Meaning | Fix |
|---|---|---|
| `SYNTAX` | Text outside the grammar, or a file rule: the path does not match the name, a second object in the file, `GO` with a count | Correct the text at the line |
| `UNSUPPORTED` | A feature that the model does not hold (for example `NOT FOR REPLICATION`, `ROWGUIDCOL`, table compression, typed xml) | Keep the object unmanaged (`[unmanaged] objects`), and change it with a `raw` batch |
| `NF001` | `NULL` or `NOT NULL` is not written | Write it |
| `NF002` | A constraint or an index has no name | Name it |
| `NF003` | A data type has no explicit length, precision or scale | Write it |
| `NF004` | A type synonym (for example `integer`, `dec`, `rowversion`, `float(n)`) | Write the name that the message gives |
| `NF005` | `PRIMARY KEY`, `UNIQUE` or `CREATE INDEX` without `CLUSTERED` or `NONCLUSTERED` | Write it |
| `NF006` | `SYSTEM_VERSIONING = ON` without `HISTORY_TABLE`, or `HISTORY_TABLE` with a name of one part. The engine would make a history table with a generated name, and the name would differ between environments | Write `SYSTEM_VERSIONING = ON (HISTORY_TABLE = [schema].[name])` |

A finding that comes from a refusal of another part of the tool (for example `GIT_FAILED`) keeps
that reason code and is an error: see the row of the code under "Exit 22".

### Temporal tables

A system-versioned temporal table is one table file: the two period columns (`GENERATED ALWAYS AS
ROW START` / `ROW END`, with or without `HIDDEN`), `PERIOD FOR SYSTEM_TIME` and
`WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [schema].[name]))`, with
`HISTORY_RETENTION_PERIOD = <n> DAYS | WEEKS | MONTHS | YEARS` when the retention is finite. The
history table has no file and no state row: the engine owns it. `export` lists it as "owned by the
engine"; `plan` and `drift` do not list it as unmanaged.

`gen` writes: `CREATE TABLE` of a temporal table; column, constraint and index changes on one (the
engine changes the history table with it); the drop, as `SET (SYSTEM_VERSIONING = OFF)` under
`allow TEMPORAL_OFF` and then `DROP TABLE`. `gen` refuses every other change of the versioning with
`TEMPORAL_CHANGE` (in `GEN_REFUSED`). The refusal carries one hint. The hints are the keys of
`diff.TEMPORAL_HINTS`; the table has one row for each key:

| Hint (`diff.TEMPORAL_HINTS`) | Changes | What to do |
|---|---|---|
| `not supported` | The table becomes system-versioned, or stops being system-versioned, and stays. `PERIOD FOR SYSTEM_TIME` is added or dropped. A period column changes or is renamed (`--rename`) | Not supported in this version; change the table outside the tool and export again. No statement that the tool reads does this change, so there is no generated SQL and no proof |
| `off and on` | The `PRIMARY KEY` of a system-versioned table differs. The `HISTORY_RETENTION_PERIOD` differs | Hand-write the migration: `SET (SYSTEM_VERSIONING = OFF)` under `allow TEMPORAL_OFF`, then the change, then `SET (SYSTEM_VERSIONING = ON (...))`. A `PRIMARY KEY`: `DROP CONSTRAINT` and `ADD CONSTRAINT` between the two. A retention: write the `HISTORY_RETENTION_PERIOD` of the table file in the ON statement. `verify` proves it. Rows that change between OFF and ON get no history |
| `history table` | The table gets another history table | Hand-write OFF, then ON with the new history table. `verify` proves it. The old history table stays in the database as a plain table that the tool does not manage, with its rows: the engine does not move them |
| `add column` | A computed column or an `IDENTITY` column is added to a system-versioned table. `gen` refuses it (`TEMPORAL_CHANGE`) and `verify` refuses the plain `ADD` (`PRF001`) | Hand-write OFF, then `ALTER TABLE ... ADD` of the column, then a `raw` batch that adds a plain column of the same name, data type and nullability to the history table (the history table must be listed in `[unmanaged] objects`), then ON. `verify` proves the model batches. It does not read the `raw` batch: if the history column differs (seen live for the nullability), ON fails on the engine |
| `schema` | The history table stays in a schema that the change drops | `DROP TABLE` leaves the history table, and the engine refuses `DROP SCHEMA` while it is there. Hand-write OFF, `DROP TABLE`, a `raw` batch of its own that drops the history table (it must be listed in `[unmanaged] objects`), then `DROP SCHEMA` |

Each statement of OFF and ON is in a batch of its own. Any other column change that is written
by hand between OFF and ON needs the same change on the history table in a `raw` batch. The proof
does not check that batch (`docs/known-gaps.md`, section 11).

What an operator sees:

- After the drop of a temporal table the former history table stays in the database as a plain
  table with its rows. The plan of that release has a note that names it. Later plans and `drift`
  list it as unmanaged. Drop it by hand when the rows are no longer needed, or add it to
  `[unmanaged] objects`.
- Versioning switched off outside the tool: `drift` reports the table with the property
  `unsupported` (a period with no versioning is outside the model), and the former history table
  as unmanaged. Switch versioning on again with the history table of the file.
- A new temporal table whose `HISTORY_TABLE` name exists already (a leftover of an earlier drop):
  the engine takes the existing table when its columns fit and refuses the statement when they
  do not (exit 21, rolled back). The tool does not check this name before the deploy.

## Cases

### Job cancelled or runner lost

No exit code exists; the job shows cancelled or failed. A lost runner opens the incident issue
"no tool report". The session of the run ended with the runner, so the engine rolled back an open
transaction, unless the COMMIT reached the server first. The run row still says `running`.

1. Do not cancel a deploy job that is inside a non-transactional migration. The job timeout is
   sized for it.
2. Start again: `gh run rerun <run-id> --failed`. The next deploy takes the lock; a `running` row is
   then dead by construction and is reconciled.
3. The cancelled run was a transaction: the new run applies what is pending and continues.
4. The cancelled run was a non-transactional step: the new plan stops with exit 22
   `STEP_UNRESOLVED`. Go to "Unknown run", step 4.

### Lock timeout in prod at night

Exit 24 `LOCK_TIMEOUT`. The deploy waited `lock_timeout_ms` (10 s in prod) for a table that the
workload held, and stopped. Everything was rolled back. Nothing is broken; the workload was not
blocked for longer than the timeout.

1. Decide if it must be tonight. If not, stop here and do step 2 in a quiet hour.
2. `gh run rerun <run-id> --failed`. prod asks for a new approval, by a second DBA.
3. Exit 22 `STALE_PLAN` on the re-run: `gh workflow run db.yml --ref main -f release=r<n> -f from_stage=prod`.
4. Three timeouts in a row: stop. The change needs a lock that this table cannot give in 10 s.
   Do not raise the timeout at night; it is in `azsqlcd.toml` and a change of it passes all five
   stages. Options for the next day: move the statement to a non-transactional migration with
   `ONLINE = ON`; or agree a window with the application owner.

### Syntax check warnings of a deploy

`report.json` of a deploy (key `warnings`) and the job summary can hold one of three notes about
the syntax check (`SET PARSEONLY ON`). None of them stops the deploy. `plan.json` records the
check in the key `syntax_check` (`ran`, `skipped` or null).

| Note | When | What to do |
|---|---|---|
| `... was skipped: this run has no second session` | A plan job, or a deploy with `--inline-plan`, that could not open the second session | The engine did not parse the texts before the deploy. A syntax error then ends as 21 `BATCH_FAILED`, rolled back. |
| `... was skipped: the plan job had no second session, and the deploy of an approved plan does not run the check` | A deploy with `--expect-plan-file`, and `plan.json` says `"syntax_check": "skipped"` | The same |
| `... is not proven: the approved plan does not record that the plan job ran it, and the deploy of an approved plan does not run the check` | A deploy with `--expect-plan-file`, and `plan.json` has no key `syntax_check`: an older tool wrote the file | Plan and deploy with one tool version (PROMOTE) |

A deploy of an approved plan whose plan job ran the check has no such note. The key is not part of
`plan_sha256`: it is as good as the plan file. So `report.json` of a deploy says where the fact
comes from, in its key `syntax_check`:

| `syntax_check` of `report.json` | Meaning |
|---|---|
| `ran in this run` | `--inline-plan`: this run parsed the texts on its second session |
| `ran in the plan job (read from plan.json; not in plan_sha256)` | `--expect-plan-file`: the plan file says `ran`. This run did not parse the texts. A plan file whose key was changed by hand gives the same value |
| `skipped` | The texts were not parsed, or the plan file does not say. A note of `warnings` says which |
| `null` | The run had no unit of work, so no text to parse; or it stopped before the plan was computed |

### Stale plan

Exit 22 `STALE_PLAN`, in the deploy job, before any batch. Between the plan job and the deploy job
something changed: another release was deployed, an object drifted, a `resolve` ran, the release
assets changed, or the tool version changed. The approval was for another plan, so it is void.

1. `gh workflow run db.yml --ref main -f release=r<n> -f from_stage=<stage>`
2. Read the new plan summary: destructive list first, then the compare link.
3. Approve the new run. Reject the old pending approvals of this release.
4. If the new plan shows work that the old one did not: find out who changed the database (LOOK).

### Drift on a touched object

Exit 22 `DRIFT_TOUCHED` at plan time. The detail lines print object, property, the recorded hash
and the live hash. Someone changed, outside the pipeline, an object that this release changes too.

Module (view, procedure, function, trigger):
1. To drop the live change: nothing to revert by hand. Accept it, and the deploy overwrites it:
   `-f action=accept-drift -f subject="<object key>"`, then PROMOTE. The plan lists `OVERWRITE_MODULE`.
2. To keep the live change: put the live text into the module file by pull request first. A DBA
   takes the text from the database. The tool has the command `drift --export <object key>
   --out <directory>`, which writes the live text as an object file; no workflow runs it
   (`docs/known-gaps.md`). After the merge do step 1 with the new release.
3. The module is gone from the database: `accept-drift` does not apply
   (`RESOLVE_NOT_APPLICABLE`), and the tool does not create it again. A DBA creates the module from
   its file at the recorded commit, then PROMOTE. If the plan still reports drift, do step 1.

Table, type, sequence, synonym: see "Out-of-band change by a DBA".

### Out-of-band change by a DBA

A DBA changed prod by hand, for example added a column or widened a type to stop an incident.
An index or a statistic that the files do not hold is not drift: it is an unmanaged sub-object.
`plan` and `drift` do not list it (seen live: `drift` named the changed procedure and not the
index made by hand); only `baseline --report-only` lists it. The tool never drops it. A release
that creates the same name stops with `NAME_COLLISION`.

1. Open a pull request with the same change: the table file, and the migration that
   `azsqlcd gen --base origin/main --name <name>` writes (with `table_model = false`: a
   hand-written migration). Merge it. The release r<n> applies the migration in dev, sandbox, test
   and preprod.
2. In prod the plan stops (`DRIFT_TOUCHED`, or `NAME_COLLISION`): the change is there already.
   Record it:
   `gh workflow run resolve.yml --ref main -f environment=prod -f target=<target> -f release=r<n> -f action=mark-applied -f subject=<migration file> -f confirm_database=<database> -f reason="applied by hand in incident <id>"`
   With `table_model = true` the tool compares the live table with the files first and refuses if
   they differ (`READBACK_MISMATCH`): then the hand-made change is not the same as the migration;
   make them equal. With `table_model = false` no comparison is possible: add
   `-f force_no_readback=true`.
3. `gh workflow run db.yml --ref main -f release=r<n> -f from_stage=prod`
4. A module that was changed by hand: "Drift on a touched object", module.

### Refresh of an environment from prod

A DBA restored a copy of prod over, for example, sandbox. The copy holds the state rows of prod
(`meta.environment = 'prod'`, the release of prod) and the database users of prod. Every command
for sandbox stops with `FENCE_META_MISMATCH` until the copy is bound again.

1. An administrator runs the script of `azsqlcd setup-sql --env sandbox --target <target>` on the
   copy. It creates the sandbox users and their grants. The state tables exist already. The script
   ends with the error "this database is bound to another environment"; after a refresh this is
   expected, and the users and grants are made by then. If the database was not refreshed, the
   script ran in the wrong database: drop the two users that it made.
2. Bind the state to the environment. `<p>` is the release that prod held when the copy was taken:
   `gh workflow run resolve.yml --ref main -f environment=sandbox -f target=<target> -f release=r<p> -f action=rebind-environment -f subject=sandbox -f confirm_database=<database> -f reason="refresh from prod on <date>"`
   The tool refuses to bind a database to `prod` unless the server is the prod server of
   `azsqlcd.toml` (`REBIND_PROD_SERVER`), and it never binds the state of another project.
3. Catch up, release by release, from the first release after r<p> that holds a migration:
   `gh workflow run db.yml --ref main -f release=r<k> -f from_stage=sandbox`
   The plan says which release is next (`CATCHUP_REQUIRED`). Module-only releases may be skipped.

### Unknown run

Exit 23; or exit 22 `RUN_UNKNOWN` or `STEP_UNRESOLVED` on a later plan.

1. Stop. Do not start again. Tell the application owner if prod is the target.
2. LOOK. Note `run_id`, `status`, `release_seq`, `segments_committed`, `failed_step`, `note` and
   the status of each step.
3. Transactional run (no step of kind `nontx` with the status `started` or `unknown`).
   - The run row is `running` (`CONNECTION_LOST_TX`, or the runner was lost): the session is gone,
     so the engine ended the transaction. It rolled back; or, if COMMIT reached the server, it
     committed whole, with the step rows of its migrations. Nothing is left to resolve. Go to
     step 6. The next deploy takes the lock, closes the row as `failed` with the note `reconciled`,
     and applies what is still pending.
   - The run row is `unknown`: compare the objects that the migration touches with the migration
     file.
     - Nothing of it is there: the transaction rolled back. Go to step 5.
     - All of it is there and the migration has no step row:
       `-f action=mark-applied -f subject=<migration file>`. Then step 5.
     - All of it is there and the migration has a step row with the status `ok`: go to step 5.
     - A part is there: a DBA finishes it or reverts it by hand, statement by statement from the
       migration file. Then `mark-applied`, or nothing if it was reverted. Then step 5.
     A module that the unit deployed before it stopped shows as drift in the next plan: "Drift on
     a touched object", module, step 1.
4. Non-transactional step (status `started` or `unknown`). Look at the build:
   `SELECT name, state_desc, percent_complete, start_time, last_pause_time FROM sys.index_resumable_operations;`
   - A row exists: a DBA finishes it (`ALTER INDEX <index> ON <table> RESUME;`) or removes it
     (`ALTER INDEX <index> ON <table> ABORT;`).
   - The index or constraint exists and no row is left: `-f action=mark-applied -f subject=<migration file>`
   - The index or constraint is absent and no row is left: `-f action=mark-not-applied -f subject=<migration file>`
   - The batch is not an index build: the tool cannot prove that nothing is left
     (`NOT_PROVEN_ABSENT`). A DBA finishes the change by hand, then `mark-applied`.
   The tool does no catalog check for `mark-applied` of a non-transactional step.
5. Run LOOK again. If the run row has the status `unknown`, clear it:
   `-f action=clear-run -f subject=<run_id>` with a reason that says what was found. So a failed
   non-transactional step needs two actions: step 4, then this one. A run row that says `running`
   needs no `clear-run` (the tool answers `RESOLVE_NOT_APPLICABLE`): the next deploy closes it.
6. `gh workflow run db.yml --ref main -f release=r<n> -f from_stage=<stage>`

### The run row was not closed

Exit 24 `RUN_NOT_CLOSED`. The unit of work is committed: the migrations have their step rows and
the objects have their rows. Only the last write, which sets the run row to `ok`, failed. The
database is at the release, and the recorded release number is still the old one.

1. Promote the same release again, before any newer one:
   `gh workflow run db.yml --ref main -f release=r<n> -f from_stage=<stage>`
2. The plan shows no unit of work and `pending = true`. The deploy closes the old row as `failed`
   (note `reconciled`) and writes a run row that records the release.
3. `gh run rerun <run-id> --failed` works for dev and sandbox. In a gated environment it stops with
   `STALE_PLAN`, because the approved plan held the work that is now applied: use step 1.
4. After a `resolve` or a `baseline` with this code the action is recorded (LOOK shows its step).
   Do not run it again.

### Catch-up after a withdraw-and-replace

A migration M failed in an environment and was withdrawn. The release of the withdrawal holds
its replacement, or no replacement: then the corrected change comes later as a normal migration
(`docs/setup.md`, section 7, "Withdraw and replace a merged migration"). The releases from the
one that added M up to the one before the withdrawal still hold M as a normal migration. A
database that never applied M cannot take those releases: each would run M. This is also the way
out when a failed release blocks later releases that change other objects: those releases wait
with `CATCHUP_REQUIRED` until M is withdrawn.

1. `CATCHUP_REQUIRED` names r<k>. If r<k> is older than the release that added M, promote it:
   `gh workflow run db.yml --ref main -f release=r<k> -f from_stage=<stage>`
2. If r<k> is the release that added M, or a later one before the withdrawal, do not promote it.
   Promote the release that holds the withdrawal, or a newer one:
   `gh workflow run db.yml --ref main -f release=r<w> -f from_stage=<stage>`
   The plan then takes the pending migrations of the skipped releases in one transaction and
   prints a note that starts with "catch-up in one release". Read that list before you approve.
3. If that plan stops with `NONTX_NOT_ALONE`, one of the skipped releases holds a non-transactional
   migration. The tool has no path for this case: `docs/known-gaps.md`, section 4.

### First deploy to an empty database

A database that records no release, no migration, no baseline step and no deploy that committed
work (a new target, after the script of `setup-sql`) takes the whole chain with one release.
`CATCHUP_REQUIRED` is not raised for it.

1. PROMOTE the newest release: `gh workflow run db.yml --ref main -f release=r<n> -f from_stage=<stage>`.
   The plan has the note `first deploy: this database records no release and no migration, so the
   whole chain runs with r<n> in one unit of work`. Every pending migration and every module of
   the release is in that one transaction.
2. Exit 22 `NONTX_NOT_ALONE`: the chain holds a non-transactional migration, and a first deploy
   cannot run it together with other work. Make the new database a copy of a database of another
   environment that holds the chain, then follow "Refresh of an environment from prod"
   (`rebind-environment`).
3. Exit 22 `BASELINE_REQUIRED`: the chain starts with `baseline`. Such a chain has no first deploy
   to an empty database: the objects before the baseline are in no migration. Use a copy, as in
   step 2, or onboard the target (`docs/setup.md`).
4. After the first deploy the database is a normal target: each later release applies only what
   it added.

### Baseline acknowledgement file

`baseline` records an existing database. A module whose live text differs from its file is
recorded with no source, and the first deploy overwrites the live text with the file text
(`OVERWRITE_MODULE`). The tool wants a reviewed acknowledgement for each such module.

1. Run the report (read only):
   `gh workflow run onboard.yml --ref main -f environment=<env> -f target=<target> -f action=baseline-report -f release=r<n> -f confirm_database=<database>`
2. Download the artefact `onboard-baseline-report-<env>-<target>`. `baseline-diff.md` lists the
   modules that differ; `modules/` holds the live text of each one. Compare each file with the file
   under `schema/`. A live change that must stay goes into the module file first, by pull request.
3. By pull request, add `onboarding/<env>/overwrite-ack.toml` with exactly one key. The report
   prints the list to copy:

   ```toml
   modules = [
     "PROCEDURE:[sales].[usp_PlaceOrder]",
   ]
   ```

4. The merge creates a new release r<m>. Run the baseline with it:
   `gh workflow run onboard.yml --ref main -f environment=<env> -f target=<target> -f action=baseline -f release=r<m> -f confirm_database=<database>`

### A release is missing

`CATCHUP_REQUIRED` names r<k>, and `gh release view r<k>` finds nothing: the release job of that
push failed. A release is created only by a push to `main`; a dispatch never creates one.

Other cause: a pull request was merged with "Rebase and merge" and had two or more commits. One
push builds one release, for the head commit; the commit before it added a migration and has no
release and no run (step 1 finds nothing). `scripts/setup_repo.py` now allows squash merge only,
and `--check` fails for a repository that allows another method (`docs/setup.md`, section 4). For
a repository where it happened already there is no workflow that creates r<k>. The second review
names a way by hand that was not run: `azsqlcd build --commit <commit of r<k>> --out dist`, then
`gh release create r<k> dist/bundle.tar dist/manifest.json --target <commit>`, then promote r<k>
and after it the newer release.

1. Find the run of the push: `gh run list --workflow db.yml --event push --commit <sha>`
2. `gh run rerun <run-id>`. The release job creates r<k>; the stages run; a stage that holds a
   newer release answers `ALREADY_PAST`.
3. A tag `r<k>` that someone created by hand on another commit blocks the release job ("tag exists
   at another commit"). Tags `r*` cannot be moved or deleted by the ruleset: a repository
   administrator removes the tag (ruleset off, delete, ruleset on, `scripts/setup_repo.py --check`),
   then step 2.

### An older release waits for approval

A newer release was approved first. Reject the pending approval of the older one. If it is approved
by mistake, the deploy answers exit 0 `ALREADY_PAST`, or exit 22 `STALE_PLAN`, and sends no batch.

## State tables

The script of `azsqlcd setup-sql` creates schema `azsqlcd` (owner `dbo`) with four tables. Read
them with LOOK. Never change them by hand.

| Table | Key | One row for |
|---|---|---|
| `azsqlcd.meta` | `id` (always 1: the table has one row) | The binding of the database: `state_version`, `project`, `environment` |
| `azsqlcd.run` | `run_id` (identity) | Each deploy, baseline and resolve: status, `segments_committed`, `release_seq`, `git_sha`, `manifest_sha256`, `plan_sha256`, tool version and digest, times, `principal_name` |
| `azsqlcd.step` | `step_id` (identity); `run_id` names the run; `migration_id` is unique when it is not NULL | Each step of a run: kind `baseline`, `migration`, `nontx`, `modules` or `resolve` |
| `azsqlcd.object` | `object_key` | Each managed object: status, `source_sha256`, the catalog capture and its sha256, and the `run_id` that recorded it |

`azsqlcd.run.manifest_sha256` is the digest of the release. The digest is the sha256 of the file
`manifest.json`. The manifest holds four things and nothing else: the commit (`commit`), the
release number (`release_seq`), the path and the sha256 of each file of the release (`files`), and
for each line of `migrations/migrations.sum` the number of the release that added it (`chain_added_in`). It
holds no tool version, so two tool versions give one digest for one commit.

`principal_name` is the name that the engine gives for the session. With SQL authentication it is
the login name. The tool never reads the column back.

## Audit

- `azsqlcd.run`, one row per deploy, baseline and resolve: release, git commit, manifest digest,
  plan hash, tool digest, principal, `triggering_actor`, `ci_run_url`, `approved_by`,
  `approved_utc`, redacted error, resolve reason. The deploy identity can update these rows; they
  are a record, not a proof.
- The step row of a `resolve` action has the note `<action>: <reason>` (`clear-run <run id>` for
  `clear-run`); the run row has `<action> <subject>: <reason>`.
- A baseline row and a resolve row copy the release number and the commit of the last deploy that
  ended `ok`. Their `manifest_sha256` names the release that the command read.
- `approved_by` holds the logins that approved the run for the environment. `approved_utc` is the
  time at which the deploy job read the approval record: the GitHub approvals API holds no time of
  approval. The deploy job starts only after the approval, so the value is later than the approval
  by the start time of the runner.
- `plan-<env>-<target>-<run id>-<attempt>.json` and `report-...json` are assets of the GitHub
  release, so they outlive the artefact retention. The workflows keep no `report.json` of a
  baseline or of a `resolve`; the run row is their record.
- Azure SQL auditing (owned by the DBA team) is the record that the pipeline cannot change. Match
  a run by the principal name of the deploy identity and by `started_utc`. With the current driver
  the application name of the session is `MSSQL-Python`, not the run id (see known gaps).
