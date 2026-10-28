# azsqlcd design

Status: design of record for the first build (2026-10-07). Part 2 is the blueprint: the plan as
it was written before the build. Part 1 records the decisions, the amendments and, in "As built",
what was built. This document does not hold the test status: `README.md`, section "Status", and
`docs/known-gaps.md` say what was proven on Azure SQL Database and on GitHub Actions, and what
was not. The tool is not production-ready. An item marked [spike] must be proven before
production use; `docs/known-gaps.md`, section 2, has the state of each one.

Reading order:
1. Part 1, "Owner decisions" and "Amendments". The amendments come from two adversarial reviews
   of the blueprint. **Where an amendment and Part 2 disagree, the amendment wins.**
2. Part 1, "Build contracts": exit codes, command line, module map.
3. Part 2: the blueprint. It is unchanged but for three lines: two of 2026-10-07 (the heading of
   the tool repository in section (a) no longer states a visibility, and the install line at the
   end of section (k) names the tag that exists) and one of 2026-10-28 (the onboarding line on a
   table-class object that differs names `azsqlcd align`, A38).

---

# Part 1

## Owner decisions (hard constraints)

- D1. Standalone tool, own repository, built from scratch. No dependency on other projects. Nothing
  to stay compatible with. "Flyway-like" means behaviour (ordered scripts, history, checksums,
  single-writer lock), not compatibility.
- D2. Azure SQL Database (PaaS) only. No SQL Server anywhere, including tests and containers.
- D3. No Microsoft dac stack: no DacFx, SqlPackage, ScriptDom or SMO. Parser, diff and runner are
  own code.
- D4. No throwaway, shadow or build database of any kind.
- D5. GitHub Actions first. GitHub Enterprise.
- D6. A change is submitted once (a pull request) and promoted dev > sandbox > test > preprod > prod
  with validation between stages and approval gates.
- D7. Full lifecycle (create, alter, drop) of every schema object type. DBA work is out of scope:
  index maintenance, tuning, backup, restore, scaling, server and database settings, firewall.
  Users, roles and permissions are assumed out of scope.
- D8. The engine is a command-line program on the pipeline runner.
- D9. Python.
- D10. Smallest maintainable production-ready design. No speculative features. Tests encode intent.
  No secrets in argv, logs or commits. Unit tests use fakes; live acceptance runs on a disposable
  Azure SQL Database that the owner provides.

## Amendments (these override Part 2)

### Exit codes and errors
- A1. Exit codes: 0 OK; 21 failed and rolled back; 22 refused, nothing executed; 23 outcome unknown,
  a human must run `resolve`; 24 clean stop, safe to start again; 25 another run holds the lock;
  30 drift found (`drift` only). Part 2 uses the old numbers 1, 2, 3, 4, 5, 10 for these, in that
  order. Every non-zero exit carries a stable `reason_code` (UPPER_SNAKE) in `report.json` and on
  stderr. An unclassified exception before the first dispatched batch is 22 `TOOL_DEFECT`; after
  the first dispatched batch it is 23 `TOOL_DEFECT_AFTER_DISPATCH`. Codes are in `errors.py`.
- A25. Redaction. Engine messages are stored and printed with every quoted or parenthesised value
  replaced by `<redacted>`. Full text only with `--show-error-text`, and never when
  `meta.environment = 'prod'`. Drift and read-back print object, property and two hashes; never
  definition or expression text. Export has the quarantine code `SECRET_LITERAL`.

### State in the database
- A2. Only the output of `azsqlcd setup-sql` (run once per target by an administrator) creates
  schema `azsqlcd`, its four tables, the grants and the `meta` row (project, environment). `deploy`
  and `baseline` never create them; if they are missing the tool exits 22 `STATE_MISSING`. The
  script also grants SELECT on `sys.sql_expression_dependencies` to the plan and deploy principals.
- A3. `azsqlcd.run` has these extra columns: `approved_by nvarchar(400) NULL`,
  `approved_utc datetime2(3) NULL`, `triggering_actor nvarchar(128) NULL`,
  `previous_git_sha char(40) NULL`, `tool_digest char(64) NOT NULL`. `error_text` holds redacted
  text. `session_id` is a note only and takes part in no decision. `run.command` is one of
  `deploy`, `baseline`, `resolve`. `step.kind` is one of `baseline`, `migration`, `nontx`,
  `modules`, `resolve`.
- A13. Capture format 1 also holds: for CHECK and FOREIGN KEY constraints `is_disabled`,
  `is_not_trusted`, `is_not_for_replication`; for triggers `is_disabled` and first/last order per
  event (`sys.trigger_events`); for indexes `is_disabled`. Export quarantines a trigger that has a
  first or last order (`TRIGGER_ORDER`).

### Lock, liveness, guards
- A4. The applock is the only liveness authority. Deploy: once the session applock is held, every
  run row with status `running` is dead and is reconciled. Plan (no lock):
  `SELECT APPLOCK_TEST(N'public', N'azsqlcd:deploy', N'Exclusive', N'Session')`; 0 means a run is
  live (exit 25 `RUN_LIVE`); 1 means a stale row, which is reported.
- A5. Guard after every batch: `(@@TRANCOUNT, XACT_STATE(), CURRENT_TRANSACTION_ID())` must equal
  `(1, 1, <id read directly after BEGIN TRANSACTION>)`. After any rollback path the tool reads
  `segments_committed`; if it is higher than the tool knows, the run becomes `unknown` and the exit
  is 23. `plan` refuses (22 `RUN_UNKNOWN`) while a run with status `unknown` has no later
  `resolve --clear-run` step.

### Unit of work, order, catch-up
- A8. A non-transactional migration is the only change of its release. `verify` error `NTX003`: a
  pull request adds a nontx migration together with any other migration, module change or
  tombstone. `plan` refuses (22 `NONTX_NOT_ALONE`) when a nontx migration is pending together with
  anything else. So a deploy run has exactly one unit of work: one transaction (migrations, then
  modules, then drops, then refresh, then read-back, then state rows) or one nontx step. Part 2
  text about several segments in one run applies only to A9.
- A9. Module chunking happens only on a first converge: no migration is pending and every module
  to deploy has `source_sha256` NULL. Then modules run in dependency-ordered chunks of
  `module_chunk`, each chunk its own transaction. Every other release is one transaction, whatever
  the module count.
- A7. Catch-up. Each chain entry in the manifest carries `added_in_release`: the release_seq of the
  first-parent commit on main that added the chain line. `plan` rule: every pending migration must
  have `added_in_release` equal to the artefact release_seq; otherwise 22 `CATCHUP_REQUIRED` with
  the message "promote r<k> first" (k = the smallest `added_in_release` among the pending
  migrations). A database that is behind is caught up release by release, each one atomic, as the
  lower environments ran them. Module-only releases may be skipped.
- A6. A no-op deploy still records the release: when nothing is pending and the artefact
  release_seq is higher than the recorded one, the tool writes a run row (status `ok`, no steps)
  under the lock. The recorded release_seq is part of `plan_sha256`. "Ahead and consistent" (the
  artefact chain is a prefix of the applied list and recorded release_seq >= artefact release_seq)
  is exit 0 with the note `ALREADY_PAST`; no batch is sent and older module text is never
  redeployed. A diverged chain is 22 `CHAIN_DIVERGED`.
- A17. Module order. A dependency cycle made only of procedures and triggers is broken in name
  order with a warning. A cycle that includes a view or a function is error `ORD004`.
  `-- azsqlcd:ignore-dep [s].[n]` in a module file removes one edge. A one-part name matches only
  an object in schema `dbo`.
- A18. A tombstoned module that is absent from the catalog inside the transaction (for example the
  trigger of a table that the same release dropped) is marked `dropped` and no statement is sent.
- A12. Dependants. `sp_refreshsqlmodule` is sent only for managed, unchanged, non-schema-bound
  views and table-valued functions that reference an altered table. Then, for every managed
  dependant of an altered table, inside the transaction, the tool reads
  `sys.dm_sql_referenced_entities` and fails the run (21 `DEPENDANT_BROKEN`) on error 2020, 207 or 208,
  `is_all_columns_found = 0`, `is_incomplete = 1`, or `referenced_id` NULL with
  `is_caller_dependent = 0`. Dependants that `plan` found broken before the change are excluded and
  listed as `PRE_BROKEN`. [spike]

### Lint and proof
- A10. `DAT001` (verify). Every `data` and `raw` batch is token-scanned for names of managed
  modules. If such a module is new or changed against the base revision in the same pull request,
  the batch needs `-- azsqlcd:deploy-module [s].[n]` above it or
  `-- azsqlcd:allow OLD_MODULE [s].[n] reason: ...`.
- A11. Batch classifier. In a `data` batch: `TRUNCATE` needs `allow TRUNCATE`; `EXEC` / `EXECUTE`
  of a procedure needs `allow EXEC_PROC <object>`; `ENABLE TRIGGER`, `DISABLE TRIGGER`,
  `SELECT ... INTO` a non-temporary table and `DBCC` are errors. In every batch that is not `raw`:
  `GRANT`, `DENY`, `REVOKE`, `EXECUTE AS`, `REVERT`, `ALTER ROLE`, `ALTER AUTHORIZATION` and
  `CREATE USER` are errors.
- A14. `NF000` (verify, table-class files). The significant tokens of `emit(parse(file))` must
  equal the significant tokens of the file, keywords case-folded. A token the parser dropped or
  defaulted then fails the pull request. Read-back compares the full model field set except
  expression text.
- A19. Residue lints `REN001`, `DRP002`, `DRP003` (old name still used) are warnings. A12 is the gate.
- A31. Withdraw-and-replace. `M_pre` comes from first-parent history of main
  (`git log --first-parent --diff-filter=A`), then replay of the other chain migrations that the
  same merge added before the withdrawn one.

### Resolve (human only; every action takes the lock and writes a run row and a `resolve` step)
- A16. `--mark-applied <migration>`: for a `started` or `unknown` nontx step, or for a pending
  transactional migration. With `table_model = true` the catalog read-back of the touched objects
  must equal the model after that migration; otherwise `--force-no-readback` and a reason are
  required. `--mark-not-applied <migration>`: nontx only; the target sub-object must be absent and
  `sys.index_resumable_operations` must hold no row for it. `--accept-drift <object key>`: modules
  (capture := live, `source_sha256` := NULL); table-class objects only with `table_model = true`
  and live equal to the head model. `--adopt-module <key>`. `--clear-run <run_id>`: an `unknown`
  run after inspection. `--rebind-environment <env>`: after a refresh from another environment;
  refuses to bind to `prod` unless the server is the configured prod server. All need
  `--confirm-database NAME` and `--reason TEXT`.
- A20. With `table_model = true`, `plan` refuses (22 `NAME_COLLISION`) a pending create whose index
  or constraint name exists live and is not recorded. The message names `resolve --mark-applied`.
- A15. Unbind replaces the name token in the header with the catalog name, refuses when the header
  cannot be located, and is idempotent inside a run. Baseline sets `source_sha256` to the file
  checksum only when the live definition, after the same header rewrite that export applies, has
  the same normal form as the file; otherwise NULL.

### Release artefact and tool identity
- A21. The bundle is never extracted to disk. `release.read_bundle()` reads members in memory,
  accepts only regular files that the manifest lists, checks each sha256, and rejects any extra,
  duplicate or path-traversing member.
- A22. Manifest digest = sha256 of the canonical JSON of the identity part only:
  `{commit, release_seq, files: sorted [path, sha256], chain_added_in: {migration_id: release_seq}}`.
  Derived data (modes, touched objects, destructive items, module kinds, edges) is recomputed by the
  running tool and lives in `plan.json`. Two tool versions give the same digest for one commit.
- A23. `build` refuses (22 `NOT_ON_MAIN`) unless the commit is on the first-parent chain of
  `origin/main`. On `workflow_dispatch`, `release.yml` requires the input to match `^r[0-9]+$`,
  checks out `refs/tags/<input>`, verifies, and never creates a release. Creation happens only on a
  push to main.
- A24. `tool_digest` = sha256 over the tool's own `.py` source files (sorted relative path, then
  bytes). It is part of `plan_sha256` and is stored in `azsqlcd.run`. The action runs
  `uv sync --frozen --no-dev --no-install-project [--extra db]`, then
  `uv run --no-sync python -m azsqlcd` with `PYTHONPATH=$GITHUB_ACTION_PATH/src`. No build backend
  runs on the deploy path.

### Pipeline
- A26. Environments without reviewers (dev, sandbox) use `deploy --inline-plan`: the plan is
  computed under the lock in the deploy job, with no separate plan job. Gated environments keep
  plan job, approval, then `deploy --expect-plan-file`.
- A27. `plan.json` and the job summary hold, per target: the recorded git sha, the artefact git
  sha, a compare URL, and for each step a permalink to the file at the artefact commit. No SQL text
  in logs.
- A28. Audit. `deploy` takes `--approved-by`, `--approved-utc`, `--triggering-actor`,
  `--ci-run-url`; the workflow reads them from the approvals API. Application name =
  `azsqlcd/<version> run=<run id>`. A last job attaches `plan.json` and `report.json` to the
  GitHub Release.
- A29. `stage.yml` inputs: `gated` (boolean), `runs-on` per stage (separate non-production and
  production runner groups; ephemeral runners are required). A gate job skips the deploy job when
  no target has pending work. The dispatch has a `from_stage` input. On a non-zero tool exit in a
  gated environment a last job opens or updates an issue labelled `azsqlcd-incident`. CODEOWNERS
  covers `schema/**`, `migrations/**`, `onboarding/**`, `azsqlcd.toml`, `.github/**`. Branch
  ruleset: at least one approval, stale approvals dismissed, approval of the most recent push
  required, no bypass actors. The production federated-credential subject must include
  `job_workflow_ref`; this is a blocking setup item for prod.

### Cut from this build (see docs/known-gaps.md)
- A30. `init`; `align` (the tool writes `rename-constraints.sql`; a DBA runs it); compensating
  migrations (`undoes=`); the `stub` directive for type changes; function kind change;
  `generic_plan_sha256` and the previous-stage compare; Actions concurrency as a safety control;
  Azure DevOps.

## Build contracts

### Command line

Offline commands (stdlib only, no network, no database):

| Command | Purpose | Exit |
|---|---|---|
| `azsqlcd lint [--root DIR]` | file rules, batch classifier, allow lines, tombstones | 0 / 22 |
| `azsqlcd verify --base SHA [--root DIR]` | lint, chain immutability, module rules, proof (table_model) | 0 / 22 |
| `azsqlcd gen --base REF --name NAME [--rename KIND:OLD=NEW]... [--resum] [--root DIR]` | write a migration from the model diff | 0 / 22 |
| `azsqlcd build --commit SHA --out DIR [--root DIR]` | `bundle.tar` + `manifest.json` from git blobs | 0 / 22 |
| `azsqlcd targets --bundle DIR --digest D --env E` | matrix of targets for the workflow | 0 / 22 |
| `azsqlcd setup-sql --env E --target T [--root DIR]` | print the administrator script | 0 / 22 |

Database commands (need the `db` extra). All take `--bundle DIR --digest D --env E --target T`,
except `export`, which takes `--root DIR --env E --target T --out DIR`.

| Command | Purpose | Exit |
|---|---|---|
| `azsqlcd plan --out DIR` | read-only plan, `plan.json` | 0 / 22 / 24 / 25 |
| `azsqlcd deploy (--expect-plan-file F \| --inline-plan) --out DIR [--approved-by S --approved-utc S --triggering-actor S --ci-run-url S]` | apply | 0 / 21 / 22 / 23 / 24 / 25 |
| `azsqlcd drift [--export KEY --out DIR]` | drift report | 0 / 30 / 22 |
| `azsqlcd export` | catalog to object files (onboarding) | 0 / 22 |
| `azsqlcd baseline [--report-only] --confirm-database NAME` | record an existing database | 0 / 22 |
| `azsqlcd resolve --confirm-database NAME --reason TEXT <one action>` | human recovery (A16) | 0 / 22 |

`--ci github` on any command writes outputs to `$GITHUB_OUTPUT` and a summary to
`$GITHUB_STEP_SUMMARY`. `--root` defaults to the current directory.

### Module map (src/azsqlcd)

| Module | Holds | Imports from |
|---|---|---|
| `errors.py` | `Exit`, `ToolError`, constructors | - |
| `lex.py` | tokenizer, GO splitter, module header, directives | - |
| `model.py` | typed model of table-class objects, operations, canonical JSON | lex |
| `parse.py` | DDL parser: CREATE forms and migration statements -> model / operations | lex, model |
| `emit.py` | model -> canonical CREATE text; operation -> SQL | model |
| `diff.py` | model x model -> ordered operations | model |
| `replay.py` | operations applied to a model, with precondition checks | model |
| `config.py` | `azsqlcd.toml` | errors |
| `chain.py` | `migrations.sum`, migration file headers and batches, tombstones | lex, errors |
| `modules.py` | module files: normal form, checksum, dependency order, unbind rewrite | lex, errors |
| `release.py` | build from git, manifest, digest, in-memory bundle read, tool digest | chain, modules, errors |
| `lint.py` | findings with codes; batch classifier; allow lines | lex, chain, modules, config |
| `sqlerrors.py` | engine error number from message text, classes, redaction | errors |
| `session.py` | the only importer of the driver; `Session`, `SqlError`, `connect` | sqlerrors |
| `state.py` | setup DDL; reads and writes of schema `azsqlcd` | session |
| `catalog.py` | read-only catalog queries -> captures (and model with table_model) | session, model |
| `plan.py` | bundle x recorded state x catalog -> plan, `plan_sha256` | release, chain, modules, state, catalog |
| `runner.py` | fence, lock, unit of work, guards, reconcile, resolve actions | plan, state, session |
| `onboard.py` | export, baseline, drift | catalog, state, modules, emit |
| `gen.py` | `gen` and the `verify` proof (git access, diff, replay) | parse, diff, emit, replay, chain |
| `cli.py` | argparse, exit handling, CI outputs | everything |

Rules for every module: type hints on public functions; no global state; no printing outside
`cli.py`; SQL text is built only in `state.py`, `catalog.py`, `runner.py` and `emit.py`; identifiers
are bracket-quoted with `]` doubled; values that come from files or the catalog reach the engine
only as bracket-quoted identifiers or `N'...'` literals with `'` doubled (a batch is sent with no
driver parameters, see Part 2 (k)).

## As built

Added after the first build (2026-10-07). The text above this section is the design of record and
is not changed. This section says what the code is. Where the two differ, the code is as this
section says, and `docs/known-gaps.md` section 3 holds the difference for the owner to confirm.
The first build ran offline: unit tests with a fake at the session boundary. The live runs on
Azure SQL Database and the runs on GitHub Actions came later that day: `README.md`, section
"Status".

### Module map as built (src/azsqlcd)

Imports are the modules of the package that each file imports. `names.py`, `catalog_tables.py`
and `tables.py` are not in the map of "Build contracts".

| Module | Holds | Imports from |
|---|---|---|
| `errors.py` | `Exit`, `ToolError`, the constructors `refused`, `failed`, `unknown`, `retry_safe`, `locked` | - |
| `lex.py` | tokenizer, GO splitter, module header, directives | - |
| `names.py` | identifier quoting, object keys, file layout of a database repository | - |
| `sqlerrors.py` | error number from message text, error classes, redaction, `SqlError`, `SignInFilter` (hides the login and the password of SQL authentication) | lex |
| `model.py` | typed model of table-class objects, operations, canonical JSON, `fold` | lex, names |
| `parse.py` | DDL parser: object files and migration statements; normal form NF001 to NF006 | lex, model, names |
| `emit.py` | model to canonical CREATE text, operation to SQL, the NF000 token check | lex, model, names, parse |
| `replay.py` | operations applied to a model, with precondition checks | model, names |
| `diff.py` | model x model to ordered operations; refusals; `classify` | lex, model, names, replay |
| `config.py` | `azsqlcd.toml`; `resolve_target`, `targets_matrix`; `Scope`, `in_scope`, `out_of_scope` | errors, model (`fold`), names |
| `chain.py` | `migrations.sum`, migration files and batches, tombstones | errors, lex, names |
| `modules.py` | module files: normal form, checksum, dependency order, header rewrites | errors, lex, names |
| `release.py` | build from git, manifest, digest, in-memory bundle read, tool digest, the one git entry point | chain, errors |
| `lint.py` | findings with codes, batch classifier, allow lines, secret rule | chain, config, errors, lex, modules, names |
| `session.py` | the only importer of the driver; `Session`, `connect`; the sign-in: `Credential` (a token provider or a `SqlLogin`), `AzureCliTokenProvider`, `ManagedIdentityTokenProvider`, `credential_from_environment`, `auth_kind` | errors, sqlerrors |
| `state.py` | setup script; reads and write statements of schema `azsqlcd` | config, errors, names, session, sqlerrors |
| `catalog.py` | read-only catalog queries for modules: fence facts, captures, dependants, approver facts; `schema_condition`, `list_schemas` | config, errors, names, session, sqlerrors, state |
| `catalog_tables.py` | read-only catalog queries for table-class objects: model, captures, engine-named constraints | catalog, config, errors, lex, model, names, session, state |
| `plan.py` | bundle x recorded state x catalog to plan and `plan_sha256`; fence; syntax check | catalog, catalog_tables (the history tables, and the renames of a name drift, A35), chain, config, errors, lex, model, modules, names, not_managed, products, release, session, set_aside, sqlerrors, state |
| `set_aside.py` | set-aside migrations: the facts of a park, the independence check (A40) | chain, config, diff, errors, model, modules, names, parse, session, state |
| `products.py` | products of `azsqlcd.toml`: membership, holds of an environment, plan notes, CODEOWNERS lines, the pull-request rules of holds (A42, A43) | chain, config, errors, modules, names, set_aside, state |
| `not_managed.py` | `[env.<name>] not_managed`: which pending migrations an environment skips, `MIXED_NOT_MANAGED` (A39, A40) | chain, config, errors, model, modules, names, set_aside |
| `runner.py` | lock, unit of work, guards, reconcile, `deploy`, the frame of a state-only run, the resolve actions | catalog, chain, config, errors, lex, model, modules, names, plan, release, session, set_aside, sqlerrors, state |
| `tables.py` | the table model against a database: hooks for plan and runner, table export, baseline compare | catalog, catalog_tables, chain, config, diff, emit, errors, lex, model, names, not_managed, parse, plan, release, session, state; runner for type checking only |
| `onboard.py` | export, baseline, drift, drift export | catalog, catalog_tables (the history tables, and the renames of a name drift, A35), config, errors, lint, model (`fold`), modules, names, plan, release, runner, session, set_aside, state, tables |
| `gen.py` | `gen`, `gen --resum`, model validation and the `verify` proof | chain, config, diff, emit, errors, lex, lint, model, modules, names, parse, release, replay |
| `align.py` | `align`: the alignment script and its report (A38); read-only | catalog, catalog_tables, config, diff, emit, gen, model, modules, names, onboard, plan, replay, session, tables |
| `cli.py` | argparse, the wiring of every command, exit handling, CI outputs. The only module that prints | align, catalog, chain, config, errors, gen, lint, onboard, plan, release, runner, session, sqlerrors, state, tables |

Rules as built. SQL text is built in `state.py`, `catalog.py`, `catalog_tables.py`, `runner.py` and
`emit.py`; `plan.py` holds three constant batches of the syntax check (`SET PARSEONLY ON;`,
`SELECT 1/0;`, `SELECT FROM;`) and formats no value into SQL. A test
(`tests/unit/test_module_boundaries.py`) refuses an import of a private name across modules.
Every session of a plan or a run gets `SET LANGUAGE us_english` with the other session options,
because the tool reads engine errors by their en-US text.

### Command line as built

`python -m azsqlcd`, or the console script `azsqlcd`. `azsqlcd --version` prints the version.
Every command takes `[--ci github]`. `--root` defaults to the current directory.

Offline commands (standard library only, no network, no database):

| Command | Writes and prints | Exit |
|---|---|---|
| `lint [--root DIR]` | Each finding as `path:line: severity CODE: message`. Table-file rules only when `azsqlcd.toml` has `table_model = true` | 0 / 22 `LINT_FAILED` |
| `verify --base SHA [--root DIR]` | Findings of lint, chain rules, module rules, model validation and proof. The summary states that lint is not a safety proof | 0 / 22 `VERIFY_FAILED` |
| `gen [--base REF] (--name NAME [--rename KIND:OLD=NEW]... \| --resum) [--root DIR]` | `--base` defaults to `origin/main`. `--name` writes `migrations/NNNN__NAME.sql` and the chain line, or prints `no migration: <reason>`; it needs `table_model = true`. `--resum` renumbers and re-hashes the new migrations | 0 / 22 |
| `build --commit SHA --out DIR [--root DIR]` | `DIR/bundle.tar`, `DIR/manifest.json`. Runs lint over the files of the commit first. Outputs: `release` (`r<seq>`), `digest` | 0 / 22 |
| `targets --bundle DIR --digest D --env E` | Outputs: `matrix` (JSON rows of the targets; each row has the key `auth`, `oidc` or `managed-identity`), `timeout` (the larger of `job_timeout_minutes` and 2 x the largest `expected-minutes` + 30) | 0 / 22 |
| `setup-sql --env E --target T [--root DIR]` | Prints the administrator script | 0 / 22 |
| `products [--env E] [--codeowners] [--root DIR]` | Each product: the count of its object files, its owners, its `promote_to`. `--env`: what the files hold back from that environment (no database is read). `--codeowners`: CODEOWNERS lines only | 0 / 22 |

Database commands (need the `db` extra). All take `--bundle DIR --digest D --env E --target T
[--show-error-text]`; `export` takes `--root DIR` in place of `--bundle` and `--digest`. Each one
signs in as the variable `AZSQLCD_AUTH` says (`entra`, `managed-identity` or `sql`; A32 below). A
sign-in that cannot be used is refused before the release is read (22 `AUTH_INVALID`,
22 `SQL_AUTH_MISSING`).

| Command | Writes and prints | Exit |
|---|---|---|
| `plan --out DIR` | `DIR/plan.json`. Outputs: `pending` (`true` or `false`), `plan_sha256` | 0 / 22 / 24 / 25 |
| `deploy (--expect-plan-file FILE \| --inline-plan) --out DIR [--approved-by S] [--approved-utc TIME] [--triggering-actor S] [--ci-run-url S]` | `DIR/report.json` for every end; `DIR/plan.json` with `--inline-plan`. `--approved-utc` is an ISO time with a time zone. Output: `plan_sha256` | 0 / 21 / 22 / 23 / 24 / 25 |
| `drift [--export KEY --out DIR]` | One line for each difference: `class key property stored_hash live_hash`. One more line, `rename <table key> <EXEC sys.sp_rename statement>`, for each recorded constraint that has a name of the engine (A35). A plan refusal for drift prints the same lines on stderr. With `park_failed = true` also one line `parked: <file> (run <id>, <reason code>)` for each park (A40), never drift. With `--export` and `--out` (given together): checks the fence, writes `DIR/schema/<kind dir>/<schema>.<name>.sql`, exit 0, no drift report | 0 / 30 `DRIFT_FOUND` / 22 / 24 |
| `export --root DIR --env E --target T --out DIR [--schema NAME]` | `DIR/schema/**` (modules and table-class files) and `DIR/onboarding/<env>/export.md`, `snapshot.json`, `rename-constraints.sql` (when not empty) | 0 / 22 / 24 |
| `baseline [--report-only] --confirm-database NAME [--out DIR] [--schema NAME]` | In the working directory: `onboarding/<env>/baseline-diff.md`, `onboarding/<env>/modules/*.sql` (live text of each module that differs), `onboarding/<env>/rename-constraints.sql`. Write mode with `--out`: `DIR/report.json` | 0 / 22; write mode also 21 / 23 / 24 / 25 |
| `align --root DIR [--schema NAME] [--out DIR]` | `onboarding/<env>/align.sql` and `align.md` under `--out` (default `--root`); read-only (A38). Takes `--root` in place of `--bundle` and `--digest` | 0 / 22 / 24 |
| `resolve --confirm-database NAME --reason TEXT (--mark-applied M \| --mark-not-applied M \| --accept-drift KEY \| --adopt-module KEY \| --clear-run RUN_ID \| --rebind-environment ENV \| --unpark M) [--force-no-readback] [--out DIR]` | With `--out`: `DIR/report.json`. `--force-no-readback` is for `--mark-applied` only. `--rebind-environment` must name the environment of `--env` | 0 / 21 / 22 / 23 / 24 / 25 |

Ends and outputs:

- A non-zero end prints on stderr `REASON_CODE: message`, then one line for each item of the
  detail lists `objects`, `blockers`, `schemes` and `failures` (names, properties, hashes,
  redacted text),
  then `azsqlcd <command>: exit N`.
- `--ci github` appends to the file of `GITHUB_OUTPUT` the keys of the command plus `exit_code`
  and `reason_code`, for every end and before the process ends, and to the file of
  `GITHUB_STEP_SUMMARY` the head line, the message and the summary of the command. Without both
  variables the command ends with exit 2.
- Exit 0 has the reason code `OK`; `deploy` can give `ALREADY_PAST`.
- A wrong argument is exit 2 (argparse): the tool did not start.
- `--show-error-text` prints the full engine message on stderr only. It is refused (22
  `SHOW_ERROR_TEXT_REFUSED`) for `--env prod` and with `--rebind-environment`, before any session.
- An engine error in a read-only command is 24 `CONNECTION_LOST` when the connection was lost or
  the database was not available, else 22 `READ_FAILED`. An exception that the tool does not know
  is 22 `TOOL_DEFECT` before the first batch of a unit of work and 23 `TOOL_DEFECT_AFTER_DISPATCH`
  after it.
- `docs/runbook.md` has one row for each reason code, under the exit code that the tool gives.

### Amendments that were built differently

Every amendment that is not in this table is built as written, as far as unit tests with a fake
session can show. The detail, the reason and the place in the source of each row are in
`docs/known-gaps.md`.

| Amendment | Difference | `docs/known-gaps.md` |
|---|---|---|
| A1 | A wrong argument is exit 2. A lock timeout in a read-only command is 22. `baseline` and `resolve` can end 21, 23, 24 and 25 | section 3 |
| A2 | The setup script makes the users of the environment before it raises on a database that is bound to another environment | section 3 |
| A3 | A `baseline` row and a `resolve` row copy the recorded release; only `deploy` and `baseline` rows count as the recorded release | section 3 |
| A6 | An equal recorded release with no extra migration is planned normally; it is not `ALREADY_PAST` | section 3 |
| A7 | Exemption for a catch-up over a withdrawn migration that the database never applied | sections 3 and 4 |
| A9 | First converge also needs: no drop pending, and a managed row with no source for every module to deploy | section 3 |
| A12 | The sweep and the refresh exist only with `table_model = true`; dependants at any depth since A36 | section 3 |
| A13 | Beyond the amendment: `IDENTITY NOT FOR REPLICATION` quarantines a table at export. A disabled trigger is exported as a normal file | section 3 |
| A14 | A fixed table of accepted differences; names are compared case-folded first, then by their exact letters | section 3 |
| A16 | `mark-applied` of a non-transactional step has no read-back. `mark-not-applied` proves an index only. `--rebind-environment` must equal `--env` | sections 3 and 4 |
| A20 | Wider: also a new table, sequence, synonym, type or schema name that exists and is not recorded | section 3 |
| A25 | Redaction is best effort. `--show-error-text` is refused by `--env prod`, not by `meta.environment`. A secret literal in a table-class file stops the export | section 3 |
| A28 | The application name is not sent: the driver reserves it. Not met | sections 3 and 6 |
| A30 | Cut: `init`, `align`, `undoes=`, `stub`, function kind change, `generic_plan_sha256`, concurrency as a control, Azure DevOps | section 1 |
| A31 | The history read has the flag `-m` | section 3 |

Part 2 text that the build did not follow, beyond the amendments: the normal-form rules also
apply to migration statements; `CREATE SEQUENCE` is fully explicit; every `ALTER COLUMN` needs
`allow ALTER_COLUMN_LOSSY` and a type-changing one gets no `LONG_LOCK`; forbidden tokens apply to
`raw` batches; `export` always exports tables, into a repository-shaped `--out`; exported module
files have LF line ends. All are rows of `docs/known-gaps.md` section 3.

### Constants that a live result flipped

Both were `False` in the first build and are `True` since the live runs of 2026-10-07.

| Constant | Value now | What proved it | Effect |
|---|---|---|---|
| `runner.MODULE_TEXT_READBACK` | `True` | Spike L7 and the acceptance run with the constant on. The condition of the first build ("the engine stores module text as it was sent") is false: the engine stores `CREATE` in place of `CREATE OR ALTER` and of `ALTER`, and every other byte as sent. So the compare is with `modules.stored_text` of the file | The read-back of a deploy also compares the stored definition (`READBACK_MISMATCH`, property `definition`) |
| `runner.RECONCILE_BY_LOCKING_READ` | `True` | Spike L6 (40 of 40 reads right, twice) and the acceptance check `CONNECTION_LOST_ROLLED_BACK` (4 of 4) | A lost connection in a transaction is decided by a locking read of the commit counter on a new session: exit 24 `CONNECTION_LOST_ROLLED_BACK` or `CONNECTION_LOST_COMMITTED`. Exit 23 `CONNECTION_LOST_TX` stays for the case where the read cannot decide |

A later live result that contradicts one of them sets it back to `False` in a pull request of its
own.

Values that a live result can change, and that are not switches: the message texts of
`sqlerrors._KNOWN_MESSAGES` (X1), `session.CONNECT_BUDGET_S` (L12), `min_token_minutes` in
`azsqlcd.toml` (L13), and the grants of `state.setup_sql` (L14). `docs/live-testing.md` section 6
lists the pull request for each result.

### Changes after the review and the first live run (2026-10-07)

Two fix waves followed the review of the build. `docs/known-gaps.md` section 7 has the finding
list and the limits; section 8 has the live results. What the code is now, where it differs from
the text above:

- Scope (owner decision): structural changes only. `[project] data_batches` (optional, default
  `false`) switches data batches on; without it each `-- azsqlcd:data` batch is the finding
  `DATA000`. `[project] server_suffixes` (optional) is the list of DNS suffixes that a target
  server may end with; the default is the four Azure SQL Database suffixes.
- A6: a release counts as "ahead" also when a deploy of it committed work and did not end `ok`;
  an older release then answers `ALREADY_PAST`.
- A7: a database with no recorded release, no migration, no baseline step and no committed deploy
  takes the whole chain with one release (plan note `first deploy: ...`). A replacement that is
  merged after later migrations is planned with the note `late replacement: ...`.
- A12: the dependants of a changed table are read at plan time (`Plan.dependants`) with the
  findings that the engine gives for each before the change (`Plan.dependant_findings`); both are
  in `plan_sha256`. The live run showed that the engine reports findings for sound modules
  (`is_all_columns_found = 0` for references to objects with no columns and for statements with a
  `#temp` table; `referenced_id` NULL for an alias of a `#temp` table), so those two clauses of
  A12 are not signs of a broken module on their own: the check after the change is differential.
  A managed view with an index refuses the release (`INDEXED_VIEW`).
- A16: `mark-not-applied` proves absence only for a model batch of one statement. The note of a
  resolve step is `<action>: <reason>`.
- A1 in the command line: an error that the runner does not report, in `deploy`, `baseline` or
  `resolve` after a session was asked for, is exit 23 `TOOL_DEFECT_AFTER_DISPATCH`. Printing never
  raises, and no printed line has the form of a workflow command (`::` at the line start, `##[`).
- A27 and plan step 12, job summary: the destructive list is first with a banner, or one line
  that says there is none; then the plan with the compare link, the dependants with their findings
  before the change, the steps with their links, table facts, drift and notes. Every value is
  escaped for Markdown. A summary above 900 000 bytes is cut and says so.
- `baseline`: on the refusal `CONSTRAINT_NAMES` the command line writes
  `onboarding/<env>/rename-constraints.sql`.
- `action.yml` runs `python -P -m azsqlcd`: the working directory, which is the checkout of the
  database repository, is not on the module path.
- New reason codes: `MAIN_REF_INVALID` (22), `INDEXED_VIEW` (22, 21), `CONSTRAINT_NAMES` (22). New
  finding codes: `NTX006`, `PRF005`, `WDR004`, `WDR005`, `DATA000`, `CHN006`, `RAW002`. Each has
  its row in `docs/runbook.md`.

### Temporal tables (2026-10-07)

System-versioned temporal tables are in the model. Part 2 lists "temporal" under `UNSUPPORTED`
((c) 3, and `refused_in_mvp`); that is no longer so. What the code is:

- Model: `Column.generated` (`ROW_START`, `ROW_END`) and `Column.hidden`; `Temporal` (period start
  and end column, history schema and table, retention) as `Table.temporal`; the operation
  `SetSystemVersioning`. A table file holds the two period columns, `PERIOD FOR SYSTEM_TIME` and
  `WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [schema].[name]))`, with
  `HISTORY_RETENTION_PERIOD = <n> DAYS | WEEKS | MONTHS | YEARS` for a finite retention. `NF006`:
  `SYSTEM_VERSIONING = ON` needs `HISTORY_TABLE` with a two-part name. The canonical JSON writes
  the new fields only when they are not the default, so the JSON of a plain table did not change.
- The history table is not an object. It has no file, no capture, no state row; it is not
  unmanaged and not `only here`. The engine keeps its structure. `catalog_tables.read_model` gives
  the list as `CatalogRead.history_tables`, `tables.export_tables` as
  `TableExport.history_tables`, and `catalog_tables.history_tables(session)` is the read for the
  callers that list live objects: `plan` (the unmanaged list) and `drift` (the `unmanaged` items)
  leave these keys out. `export` prints their number in its count line and one line for each in
  `export.md`. The schema of the history table is a schema of the model and a need of the table
  file.
- Catalog: `read_tabulars` has 19 columns (was 11). `temporal_type = 2` is a `Table` with
  `Temporal`; `temporal_type = 1` is a history table. The capture of a temporal table has the
  property `temporal`, and its period columns have `generated_always` and, when hidden,
  `is_hidden`. They are written only for a temporal table, so the capture and the hash of every
  other table are as before.
- `gen` writes: `CREATE TABLE` of a temporal table; column, constraint and index changes on one;
  the drop as `ALTER TABLE ... SET (SYSTEM_VERSIONING = OFF)` and then `DROP TABLE`. The allow code
  `TEMPORAL_OFF` (object `[schema].[table]`) is in the closed list. `lint.py` reads the two `SET`
  forms whole, because its closed rule does not look inside parentheses:
  `SET (SYSTEM_VERSIONING = OFF)` and `SET (SYSTEM_VERSIONING = ON [(HISTORY_TABLE = ...,
  DATA_CONSISTENCY_CHECK = ON | OFF, HISTORY_RETENTION_PERIOD = ...)])`; any other `SET` is
  `MODEL_STATEMENT`. `gen` stops with `GEN_INVALID` when `diff.classify` gives a code for a
  statement that `lint.py` does not read from its text.
- `gen` refuses with `TEMPORAL_CHANGE`: versioning on or off for a table that stays; a change of
  the period columns, of the history table, of the retention or of the `PRIMARY KEY` of a
  temporal table; the rename of a period column; the drop of a schema in which a history table
  would stay. `docs/runbook.md`, "Temporal tables", has the row for each.
- A plan whose pending migrations hold `allow TEMPORAL_OFF` has a note: the history table stays in
  the database as a plain table that the tool does not manage. The note names the history table
  when the catalog still has it.

Limits:

- After the drop of a temporal table the history table stays, as a plain unmanaged table. Nothing
  in the tool drops it. A later `CREATE TABLE` with the same `HISTORY_TABLE` name meets it: the
  engine takes it or refuses it; the model and `sub_object_collisions` do not see it.
- A table cannot become temporal, or stop being temporal, through generated SQL, and a
  hand-written conversion has no proof: `ADD PERIOD`, `DROP PERIOD`, `ADD` of a `GENERATED ALWAYS`
  column and `ALTER COLUMN ... ADD | DROP HIDDEN` are `UNSUPPORTED` in a model batch. A history
  that ends with versioning off is the replay state `UnversionedTable`, which equals no table of a
  file (`PRF001`).
- The rename of a period column is refused by `gen` and by the replay. The engine allows it.
- The replay does not see a leftover history table: a hand-written OFF, `DROP TABLE`,
  `DROP SCHEMA` passes the proof and fails on the engine.
- `DATA_CONSISTENCY_CHECK` in a hand-written `SET` is read and not kept. `SET (... = ON ...)`
  gets no `LONG_LOCK`, although the engine reads the rows for the consistency check.
- Still `UNSUPPORTED`: a period on a table that is not system-versioned (so a table whose
  versioning was switched off outside the tool shows in `drift` as the property `unsupported`);
  `GENERATED ALWAYS` of any other kind (ledger); `HIDDEN` on a column that is not a period column;
  a temporal memory-optimized table; `DATA_CONSISTENCY_CHECK` in `CREATE TABLE`.
- A retention with a singular unit (`1 YEAR`) or `HISTORY_RETENTION_PERIOD = INFINITE` in a file
  parses and fails `NF000`: the canonical text is the plural unit, and no clause for no limit.
- Proven on a live database: the emitted DDL of six temporal tables, and the read of 17 temporal
  tables of the sample database (`docs/known-gaps.md`, section 8). Hidden period columns and a
  finite retention are read from the catalog in unit tests only.

Dynamic data masking: `lint.py` reads `ALTER COLUMN ... ADD MASKED WITH (FUNCTION = '...')` and
`ALTER COLUMN ... DROP MASKED` as model statements, each as the whole statement, with no
`ALTER_COLUMN_LOSSY`. `DROP MASKED` needs the allow code `UNMASK` (object
`[schema].[table].[column]`).

### Changes after the live write-path runs and the audits (2026-10-07)

`docs/known-gaps.md` section 9 has the live results and section 10 the open findings. What the
code is now, where it differs from the text above:

- Plan order. `catalog.applock_test` is the first statement of `plan.compute_plan` when the
  caller does not hold the lock; 25 `RUN_LIVE` is raised there, before the fence and before any
  catalog read. Reason (measured live): a catalog read waits for the schema locks of an open DDL
  transaction, also with `READ_COMMITTED_SNAPSHOT` on, so with the old order a plan during a live
  deploy ended as a lock timeout and never as `RUN_LIVE`. A deploy that starts after the lock
  test can still make a read of the plan wait: 22 `READ_FAILED`.
- A12, differential dependant check. The findings of a dependant are `ERROR_207`, `ERROR_208`,
  `ERROR_2020` (`catalog.NOT_BOUND_FINDINGS`: the read of `sys.dm_sql_referenced_entities` raised
  that error), `COLUMNS_NOT_FOUND` and `UNRESOLVED`. `is_all_columns_found = 0` counts only for a
  referenced table, view or table-valued function. The run fails (21 `DEPENDANT_BROKEN`) only for
  a finding that `Plan.dependant_findings` does not hold for that dependant; the detail holds the
  new findings. A dependant that had a not-bound error before the change is not read again. The
  failed read does not end the transaction (measured live), so the runner does not treat it as a
  lost transaction. A view or a table-valued function that the release does not change is
  refreshed first; a refresh that the engine refuses is 21 `DEPENDANT_BROKEN` too, with the failed
  step `refresh:<key>` (the live run saw 21 `BATCH_FAILED` there, before this change). The message
  tells the way out: withdraw and replace the migration with the module change in one pull
  request, because a release that only changes the module is refused with `CATCHUP_REQUIRED`.
- Plan and resolve. A tombstoned module that was dropped behind the tool is not drift. The
  `DRIFT_TOUCHED` message names `resolve --mark-applied <migration>` when the catalog equals the
  model after the next pending migration. `mark-applied` of an index or key build is refused
  unless the catalog shows the index and no open resumable build. `NONTX_NOT_ALONE` names the
  release to start from. A module file renamed by letter case only needs no tombstone.
- Module text. `modules.stored_text(sent)` is what the engine keeps of a module batch;
  `modules.stored_checksum` is its checksum. The read-back of the runner compares with it.
  `rewrite_for_export` writes `CREATE OR ALTER` and one space in place of the stored verb and the
  white space after it, so the export of a deployed module is its file when the file is written
  that way.
- Lost session. `session.py` classes a driver error with the text "Connection may have been
  terminated by the server" outside connect as `SESSION_LOST`. The driver does not send the
  application name; `APP=azsqlcd/<version>` of Part 2 does not hold with `mssql-python` 1.15.0.
- Error texts. `sqlerrors._KNOWN_MESSAGES` holds the texts of 207, 245, 2601, 2627, 3729, 3998
  and 18456 as the driver returned them. A token connect to a database that does not exist is
  18456, in one attempt; the text of 4060 was not seen.
- Session options. The option row has twelve values; `IMPLICIT_TRANSACTIONS` is one of them.
- `verify --base` and the proof of a withdrawal take a full commit id or a full ref name that is
  not a tag (`MAIN_REF_INVALID` as a finding). `gen --base` still takes any revision; its output
  is proven by `verify`.
- Proof. A raw batch may not name a managed table-class object or module (`RAW002`). The drop of
  a key on the columns of a foreign key is refused while the foreign key stays. A rename that a
  later `DROP TABLE` removes and that `plan` could not follow is `PRF006`.
- Onboarding. `export.md` has one `[unmanaged]` block, at the end; a listed module is left out of
  the export as a listed table is. `drift` leaves `start_value` of a sequence out
  (`catalog_tables.NOT_IN_DRIFT`) until spike X6 decides. `export` refuses with
  `EXPORT_INCOMPLETE` (22) when a file under `--out` cannot be written.
- Names and Windows. `names.py` refuses a file name that Windows cannot check out (device names,
  `: ? * " < > |`, a dot or a space at the end, more than 255 bytes); `release.py` refuses a root
  folder in another letter case and such paths (`TREE_INVALID`, `BUNDLE_INVALID`), starts git by
  its full path, and counts CRLF as LF in `tool_digest`. `.gitattributes` pins LF.
- Command line. `cli.main` sets both streams to escape what their encoding cannot write and keeps
  the encoding; each printed line is flushed; when a print fails the stream is pointed at the
  null device, so that the exit code of the command is the exit code of the process (before: 120
  for a closed pipe). `drift` prints `-` for an absent hash. A command that finds no
  `azsqlcd.toml` under `--root` names the directory and the flag.
- `lex.tokenize` keeps the tokens of the last 16 texts.
- New reason code: `EXPORT_INCOMPLETE` (22). New finding code: `PRF006`. New allow and finding
  codes of the temporal work: `TEMPORAL_OFF`, `UNMASK`. Each has its row in `docs/runbook.md`.

### Sign-in modes and the fixes of the first pilot

- A32. Three sign-in modes. This overrides Part 2 (k), "`AzureCliCredential` only ... no SQL
  password". A database command signs in as the variable `AZSQLCD_AUTH` says, exact and in lower
  case: `entra` (also when the variable is not set or empty; the Azure CLI session, as before),
  `managed-identity` (`azure.identity.ManagedIdentityCredential`, the same token scope;
  `AZSQLCD_MANAGED_IDENTITY_CLIENT_ID` names a user-assigned identity and must be a GUID), or
  `sql` (a `SqlLogin` from `AZSQLCD_SQL_USER` and `AZSQLCD_SQL_PASSWORD`; the same connection
  keywords plus `UID` and `PWD`, every value in braces). Rules of `sql`: the password comes from
  the environment only; it is refused in GitHub Actions (`session.in_github_actions`:
  `GITHUB_ACTIONS` is `true` or `1` in any letter case, `GITHUB_RUN_ID` is set, or the command
  has `--ci github`; 22 `AUTH_INVALID`, no override), in `credential_from_environment`, in
  `cli` for a `SqlLogin` that a caller gave, and in `session.connect`. This guards against a
  mistake; the author of a workflow can remove the variables. A control character in either
  value and a password of fewer than 8 characters are refused
  before any connect; no token is asked for and no token life is checked (`token_minutes_left`
  is null); the login and the password are taken out of every driver text in `session.py` before
  redaction, and out of every string of the triage log when the sign-in is `sql`
  (`sqlerrors.SignInFilter`: the password wherever it stands, the login as a whole word, both
  also in braces with `}` doubled; a driver text that names `UID=` or `PWD=` is cut there); the
  driver exception is not chained; a failed login is 24 `CONNECT_FAILED` with the number and the class
  and without the driver message. The kind of sign-in, never a login or a client id, is in the
  plan output (`sign-in: <kind>`), in the summary (row `Sign-in`), in `plan.json` (a note, so
  outside `plan_sha256`), in `report.json` (key `auth`) and in the `config` event of the triage
  log. The state tables are unchanged. No code compares the identity of the session with an
  identity of `azsqlcd.toml`, in any mode. `setup-sql` makes users for the client ids of
  `[identities]` only: none for a SQL login. `cli.main` reads the environment only when the
  caller gave no credential.
- A33. The sign-in of an environment in the workflows. `[env.<name>] auth` of `azsqlcd.toml` is
  `"oidc"` (the default) or `"managed-identity"`; another value is `CONFIG_INVALID`. `targets`
  puts it into every matrix row. `stage.yml`, `drift.yml`, `onboard.yml` and `resolve.yml` set,
  at job level, `AZSQLCD_AUTH` (`managed-identity`, else `entra`) and
  `AZSQLCD_MANAGED_IDENTITY_CLIENT_ID` (the plan or the deploy client id of the row, else
  empty), and run `azure/login` only when the row is not `managed-identity`. A row with no
  `auth` key behaves as `oidc`. `id-token: write` stays. The setting is for the workflows only:
  a command on a workstation follows `AZSQLCD_AUTH`. With a managed identity the identity is
  bound to the runner machine, not to the workflow run (`docs/setup.md`, section 2).
- `plan.json` has the key `syntax_check`: `ran`, `skipped` or null (the plan has no unit; in a
  file also: a tool that did not write the key). It is not in `plan_sha256`: the plan job
  parses the texts, the deploy job computes the plan again under the lock with no second
  session, and both must give one hash. `Plan.from_json` reads a file without the key; another
  value is 22 `PLAN_INVALID`. `PLAN_FORMAT` stays 1. `compute_plan` has the argument
  `approved_plan`; `runner.deploy` passes the expected plan. The key decides a note of the
  report only: no note when the approved plan says `ran`; "was skipped: the plan job had no
  second session, and the deploy of an approved plan does not run the check" for `skipped`; "is
  not proven: the approved plan does not record that the plan job ran it, and the deploy of an
  approved plan does not run the check" for a file without the key. A plan job or an inline
  plan with no second session keeps "was skipped: this run has no second session".
- `azsqlcd.toml`: `[project] module_chunk` is optional (default 100). The message for a missing
  required key names the key, its table and one example line.
- New reason codes: `AUTH_INVALID` (22), `SQL_AUTH_MISSING` (22). New finding code: `PAR001`
  (error; unbalanced parentheses of a batch or a module; no allow line). Each has its row in
  `docs/runbook.md`.
- `sqlerrors.redact` keeps the names of nine messages of names only, when the whole text is that
  one message. Names of the catalog (1913, 2714, 3726, 5074) always stay. A name as the statement
  wrote it (207, 208, 2705, 3701, 4902) stays only when the batch that raised the error holds
  each part of it as an identifier (`sqlerrors.names_written`, `SqlError.for_batch`, called by
  `session.DriverSession.execute`): dynamic SQL that builds a statement from data puts a row
  value there. `state.set_run_status` redacts with no batch, so `azsqlcd.run.error_text` never
  keeps such a name.
- `report.json` of a deploy has the key `syntax_check` (`runner.SYNTAX_IN_THIS_RUN`,
  `runner.SYNTAX_OF_PLAN_FILE`, `skipped` or null): where the fact of the syntax check comes
  from. `plan.json` states it in a key that `plan_sha256` does not cover.
- `lint` does not report `PAR001` for a migration whose chain line says `withdrawn`: the file is
  merged and can never change, and the pull request of its withdrawal must pass.
- `release.git` adds `--git-dir=<root>` when the root itself is a bare repository, `GIT_DIR` is
  not set and no folder at or above the root holds `.git`.
- The message of 22 `CATCHUP_REQUIRED` gives two cases (promote the earlier release, or withdraw
  its migration). It names the replacement in the pull request of the withdrawal as the way, and
  the condition of the other one (no database applied the migration, and `table_model = true`).
  The detail keys are unchanged.

### Partitioned tables and indexes (2026-10-13)

- A34. Partitioned tables and indexes are in the table model. Partition functions and partition
  schemes are prerequisites that a DBA owns, not objects of the model: the tool creates none,
  changes none and reads no boundary. This overrides Part 2 ((c) 3), which lists "partitioned"
  under `UNSUPPORTED`. A table, a `PRIMARY KEY` or `UNIQUE` constraint and an index (rowstore or
  columnstore) can state `ON [scheme]([column])` with one partitioning column; `ON [PRIMARY]`
  stays accepted. `SPLIT`, `MERGE`, `SWITCH` and a `TRUNCATE` of partitions are operations, not
  design, and stay outside the tool.
- Model (`model.py`): `Partition` (scheme name, column), `ON_PRIMARY`, `Place`, `place_on`;
  the field `on` of `Table`, `PrimaryKey`, `Unique` and `Index`. `Table.on` is the place of the
  heap or of the clustered key or index, and `None` when the table is not partitioned. A key or
  an index of a partitioned table holds a `Partition` or `ON_PRIMARY`, never `None`: the engine
  puts an index with no `ON` clause on the scheme of the table, so the file always writes the
  place. A key or an index of any other table holds `None` or a `Partition`. The clustered key
  or index is the table and has the place of the table. The canonical JSON writes `on` only when
  it is not `None`, so the JSON and the file of a table that is not partitioned did not change.
- Rules of the model, so that the parser and the replay refuse what the engine refuses: the
  partitioning column is a column of the table; it is a key column of a `PRIMARY KEY`, a
  `UNIQUE` constraint or a unique index on a scheme; a computed partitioning column is
  `PERSISTED`. The option `STATISTICS_INCREMENTAL` (`ON`, `OFF`) goes only with a key or an
  index on a scheme. A table type has no place.
- Operations: a statement of a migration keeps the clause that it states (`None` is "no `ON`
  clause"), and `replay.py` gives the key or index the place that the engine gives it
  (`place_on`). One statement moves a table: a clustered key or index that is created on a heap
  and states its place. The other keys and indexes stay where they are. `ALTER COLUMN` and
  `DROP COLUMN` of a partitioning column are blocked; `sp_rename` of it is followed.
  `replay.partitioned_on` is public. `RebuildTable.all_partitions` is
  `ALTER TABLE ... REBUILD PARTITION = ALL WITH (DATA_COMPRESSION = ...)`.
- `gen` (`diff.py`) writes each key and index with the `ON` clause of the head revision, and
  `REBUILD PARTITION = ALL` for a compression change of a partitioned heap. It refuses with
  `PARTITION_CHANGE` (in `diff.REFUSALS`, so in `GEN_REFUSED`): a table that goes on a scheme or
  off one, another scheme, another partitioning column, and `ALTER COLUMN` of a partitioning
  column. `docs/runbook.md`, "Partitioned tables", has the hand route.
- Lint: the closed list of a model batch holds
  `REBUILD [PARTITION = ALL] WITH (DATA_COMPRESSION = ...)`, read whole. The `ON` clause is a
  part of its statement; a second statement behind it is `MODEL_STATEMENT`. A partition function
  or scheme statement in a model batch is `MODEL_STATEMENT`. The rules of a `raw` batch did not
  change. No new allow code: `LONG_LOCK` covers the builds and the rebuild, and the drop of a
  clustered index or key of a table that exists (the table becomes a heap). The lexer cannot
  see that an index is clustered: `lint.BatchFacts.may_lock` lists the table of a `DROP INDEX`
  or `DROP CONSTRAINT`, so its `LONG_LOCK` line is not `ALLOW_UNUSED`; `diff.classify` gives
  the code from the model, the proof (`gen._clustered_drop_findings`) asks for the line with
  `PRF003` in a tx migration, and `gen` writes it.
- Catalog (`catalog_tables.py`): `read_indexes` has 29 columns (was 26: `partition_scheme`,
  `is_incremental`, `compression_kinds`), `read_index_columns` has 9 (was 8:
  `partition_ordinal`). The reader no longer refuses a partitioned table. It refuses, as
  `UNSUPPORTED` with a reason: a heap or an index whose partitions do not all have the same
  compression; a partitioned object whose scheme or column the catalog does not give; a table
  type on a scheme. The partitioning column that the engine adds to an index is no column of
  the index in the model. The capture of a table has the property `partition`, always (null, or
  scheme and column), as `heap_compression`; the capture of a key or an index has it only on a
  scheme. No capture holds a boundary or a number of partitions. No older version could record
  a partitioned table, so drift and the baseline compare read a stored table capture without
  `partition` as null (`catalog_tables.with_stored_defaults`).
  `catalog_tables.partition_schemes(session)` reads the names of the schemes (tag
  `azsqlcd:partition_schemes`).
- Plan: `TableHooks.missing_partition_schemes(session, work)` is asked first in step 8, before
  the dependants and the blockers. `tables.Hooks` reads the scheme names from the pending model
  statements (`CREATE TABLE`, `CREATE INDEX`, `ADD CONSTRAINT` of a key) and makes one read of
  `sys.partition_schemes`; no read when no statement names a scheme. A scheme that a pending
  `raw` or `data` batch creates before its use counts as present. New reason code:
  `PARTITION_SCHEME_MISSING` (22), detail `schemes`. The deploy computes the plan again under the
  lock, so it refuses the same way. With `table_model = false` there is no check.
- Read-back, drift, export and the baseline compare needed no code of their own: the place is a
  field of the model and a part of the capture.
- Not proven: all of it ran against fakes only. `docs/known-gaps.md`, section 14, has the scope
  cut, the assumptions about the engine and the limits.

### Schema scope (2026-10-26)

Owner decision of 2026-10-26: the whole database comes under the tool, onboarded one schema at a
time. `docs/setup.md`, section 7, "One schema at a time", is the procedure; `docs/runbook.md` has
the rows of `OUT_OF_SCOPE` and `SCHEMA_UNSCOPED`.

- `azsqlcd.toml` takes the optional table `[scope]`: `schemas` (required in the table; `["*"]` is
  every schema that is not excluded), `exclude`, `exclude_objects` (patterns `schema.name`, `*` and
  `?`; split at the first dot, each part matches its own name). `config.Scope`,
  `config.out_of_scope(scope, key)`. Names are compared with `model.fold`.
  Without the table, `Config.scope` is None and every function behaves as before: no batch
  changes, no output changes.
- Reads. `catalog.schema_condition` limits a read of every object to the schemas of the scope in
  the engine (`IN` or `NOT IN` a subquery on `sys.schemas`): `list_user_objects`,
  `capture_modules` with no keys, and the eleven queries of `catalog_tables.read_model` with no
  keys (argument `scope`). A pattern of `exclude_objects` is decided in Python on the rows: such an
  object is not read into the model, is in no list, and still holds its name in its schema. An
  external table of the scope is skipped by its type (`CatalogRead.external_tables`, no column
  read); without a scope it stays `UNSUPPORTED`. `catalog.list_schemas` reads the schema names
  for `SCHEMA_UNSCOPED`; it is sent only with a scope whose `schemas` is not `["*"]`.
- Plan, drift, runner: `plan.scoped_state` leaves out the rows of objects outside the scope, so a
  schema that leaves the scope is not planned, compared, orphaned or dropped. The unmanaged list
  holds objects of the scope only; a DML trigger follows its table (`catalog.capture_in_scope`). A
  plan note for each `SCHEMA_UNSCOPED` schema and one for the number of external tables; notes are
  outside `plan_sha256`. `drift` gives the class `unscoped`, which is not drift (exit 0). The table
  hooks take a module outside the scope as unmanaged (blockers, `[ack]`, no refresh, no unbind).
- Rows of objects that leave the scope (review M2): `plan.left_scope` lists the managed rows that
  the scope of the release does not manage. A managed DML trigger leaves with its table: the
  recorded capture names the table (`parent`), and `verify` asks for no tombstone for its removed
  file (review V1). The plan adds a tx unit of `unscope:<key>` steps (in
  `plan_sha256`, so the approved plan holds it); the runner sends no statement for such a step and
  writes `state.mark_dropped` for the key in the transaction of the unit. No new column and no new
  status: `dropped` already means "no longer managed". An object that comes back has no managed
  row: `BASELINE_REQUIRED` names `baseline --schema`, which reads it back and records it again.
- A target that skipped the release in which an object left keeps its managed row. Under
  `[scope]` the plan reads back, against the model of the release, each managed table-class object
  that no pending migration changes, that has no drift and that no withdrawn migration that ran
  here changed; one that differs refuses the plan before any statement (`STALE_MANAGED_ROW`,
  review V2).
- The proof of a replacement (`replaces=`) compares the two models for the objects of the head
  scope only, and refuses a statement of the replacement on an object outside it (`WDR002`, review
  V3).
- `lint` refuses a `[[drop]]` for an object outside the scope (`OUT_OF_SCOPE`). The plan notes a
  tombstone for an object of the scope that the database has with no managed row
  (`TOMBSTONE_UNMANAGED`, review V4): the tool does not drop it.
- Under a scope, `NAME_COLLISION` of modules that the database has with the kind of their file,
  and the table `BASELINE_REQUIRED`, are `BASELINE_REQUIRED` that names `azsqlcd baseline --schema`.
- Export and baseline of one schema: `onboard.export(..., schema)` and `onboard.baseline(...,
  schema)`, `--schema` on the command line. The export reads that schema with the patterns of the
  config; a need of a file in another schema that the scope manages counts as managed. The command
  line merges the captures into `onboarding/<env>/snapshot.json` of `--root`
  (`tables.merge_snapshot`) and prints what to add to `[scope] schemas`; it does not edit
  `azsqlcd.toml`. `export --schema` of a schema that the database does not have is
  `SCHEMA_NOT_FOUND`. The baseline of a schema compares and records the objects of that schema that
  have no managed row (review M1: also an object that enters the scope in a recorded schema), with
  a baseline step of its own, and is refused (`ALREADY_BASELINED`) when nothing is left to record.
  `resolve --adopt-module` of a module outside the scope is `OUT_OF_SCOPE`.
- Lint and proof: `OUT_OF_SCOPE` (error) for an object file outside the scope; `TMB001` is not
  given for a module that leaves the scope; `RAW001` is not given for a raw batch for an object
  outside it; a trigger file on a table outside the scope is `OUT_OF_SCOPE`. `gen` and the proof
  start from a model that the head scope filters, the base model and the model from the history
  of a withdrawal alike (review C1): objects outside it are left out, objects that enter it are
  taken as their files say (`PRF000` names `baseline --schema`). `gen` refuses (`GEN_INVALID`) a
  statement on an object outside the head scope, and unbinds no module outside it.
- `onboard.yml` (reusable, template, example) takes the input `schema` and passes `--schema` to
  `export` and to both baselines; an empty input passes nothing.
- New reason codes `OUT_OF_SCOPE` and `SCHEMA_NOT_FOUND` (22). New finding codes `OUT_OF_SCOPE`
  (lint) and `SCHEMA_UNSCOPED` (plan note, drift class `unscoped`). New plan step kind `unscope`.
- Proven against fakes only. `docs/known-gaps.md`, section 15, has the limits.

### Drift of a constraint name

- A35 (owner decision, 2026-10-07; ported 2026-10-28). A constraint of a managed table that the
  database holds under a name that the engine made is drift. `tables.Hooks.table_drift` gives the
  difference `constraint_name [<recorded name>] (<kind>, live name [<name>])`, so `drift`, the
  plan and the runner have the rule from one function. An engine-named constraint is the recorded
  constraint with the same capture value (for a key on a scheme, the place is a part of it); the
  fixed name of the reader does not decide it, so two CHECK constraints do not swap. The
  read-back records such a constraint under the name of its file. The rename gives the recorded
  name. Only for a record of v0.1.0, which holds a fixed name, it gives the name of the file of
  the release. It does not do that when a migration that the database did not apply drops or
  renames the recorded name, or when the database applied a migration that the release does not
  hold. The way out is the `sp_rename` statement of `catalog_tables.rename_constraints_sql`:
  `drift` prints it as a `rename` line, and a plan refusal has the same lines in its detail
  `renames`, which the command line prints. No message holds the statement: a message goes into
  the triage log. A table outside `[scope]` is not read, so it gives no such difference. The state
  tables are unchanged: the difference is a property and two hashes, as every drift item. No new
  reason code; `docs/runbook.md`, "A constraint with a name that the engine made", has the way
  out. Proven against fakes only.

### Transitive dependants (2026-10-28)

- A36. The dependants of A12 are read at any depth. `catalog.dependant_closure(session, tables,
  modules)` sends the query of `dependants_of` for the tables (unchanged), then the query
  `azsqlcd:dependant_chain` once for each level (one batch for each 500 keys) on the modules that
  the level before found, and on the modules that the release changes. A module is read once, so a
  cycle ends; the search stops after 32 levels (`catalog.DEPENDANT_DEPTH`, plan note). The order
  is the refresh order: a module after each found module that it references, else by key. The
  refresh set, the dependants to check and their findings, and the rule of `[ack]
  unmanaged_dependants` and `TABLE_BLOCKER` use that set. A release with no migration and a
  changed module also refreshes and checks the dependants of that module. A plan note gives the
  path of each dependant found through another module; a schema-bound blocker names the
  schema-bound modules on it. A release whose dependants are all direct gives the plan output and
  the statements of before; the plan sends more catalog reads. No reason code and no state column
  was added. Proven against fakes only.
- A37. Close of the review of A36. A module that the release drops is a root of the search too, as
  a changed module is: the plan reads before the drop, so its name still resolves. A release that
  only drops modules also refreshes and checks their dependants; a view on a dropped view fails
  with 21 `DEPENDANT_BROKEN` at `refresh:<key>` (`ERROR_208`). In a cycle, the first member of the
  cycle by key goes first; a module that only uses the cycle comes after it. When the release has
  no migration, the `DEPENDANT_BROKEN` text says "the change of a module it uses" and tells the
  operator to change the dependant in the same pull request; the text with a migration is
  unchanged. `tables.Hooks` keeps the closure and the `broken_references` answer of each module
  for one plan (the same session and the same `Work`), so a plan reads each dependant once. No
  reason code, no state value and no state column was added.

### Align (2026-10-28)

- A38. `azsqlcd align --root DIR --env E --target T [--schema S] [--out DIR]` writes
  `onboarding/<E>/align.sql` and `align.md` under `--out` (default `--root`). This overrides A30
  for `align` in part: the tool writes the alignment script, and it never runs it. The command is
  read-only: fence facts and catalog reads only, no lock, no state row. The database model is
  `catalog_tables.read_model` under `[scope]` (or the one schema of `--schema`, as
  `export --schema`); the file model is `gen.load_model` of the working tree, cut to the same
  scope. Sections: 1, `sp_rename` of each engine-named constraint that a file holds in the same
  shape (`tables.declared_names`); 2, the operations of `diff.diff` (the order of `gen`); 3, the
  file text of each module that differs or is missing (`onboard.compare_modules`), in deploy
  order; 4, `WITH CHECK CHECK CONSTRAINT` and `ALTER INDEX ... REBUILD` for constraints that are
  disabled or not trusted and indexes or keys that are disabled, with the row count of the table
  (`catalog.table_facts`), smallest table first. A change that `diff` refuses gets no statement:
  `align.md` lists it under "Hand-write" with the refusal code, and the object stays as the
  database has it. An object that only the database has is never dropped ("Only in the
  database"). A statement whose `diff.classify` code can lose data (`DROP_COLUMN`,
  `ALTER_COLUMN_LOSSY`, and the drops) is written commented out after `-- REVIEW: loses data:`.
  No new reason code; a file that cannot be written is `EXPORT_INCOMPLETE`. The state tables are
  not read or written. Proven against fakes only (`tests/unit/test_align.py`).
- A39. Review of align (2026-10-28). A table with no file keeps the engine names of its
  constraints (no `sp_rename`). Section 4 has every `ALTER INDEX ... REBUILD` first (smallest
  table first, the clustered index first in its table), then every `WITH CHECK CHECK CONSTRAINT`.
  `ALTER COLUMN ... DROP MASKED` (`UNMASK`) is commented out after `-- REVIEW: unmasks column`.
  A file for the name of an external table or a history table goes to "Hand-write". When only
  "Hand-write" items differ, `align.sql` is `-- no statement: see Hand-write in align.md`, not
  `-- nothing to align`. No new reason code.

### Set-aside migrations and parking (2026-10-28)

Owner problem: one migration that fails in an environment blocks every later release there
(`CATCHUP_REQUIRED`) until it is withdrawn. `docs/setup.md`, section 7, "Park a migration that
fails", is the procedure.

- A40. Set aside. In an environment, a pending migration can be set aside for a reason. It is not
  applied there, is never marked applied, and is no pending migration for A7. Where migrations are
  set aside (or were, and are still pending), a release deploys in chain order its own migrations
  and the pending migrations of earlier releases that are not set aside (note `catch-up past
  set-aside: ...`), in place of `CATCHUP_REQUIRED`; a pending set-aside migration before an
  applied one is no hole (`CHAIN_DIVERGED`). What runs must be independent of each set-aside
  migration (22 `SET_ASIDE_DEPENDENCY`). One reason is built: `parked`, opt-in with
  `[project] park_failed = true`. Without it the planner sends no extra batch and plans as before.
- Module `set_aside.py`. Later reasons plug in at `set_aside.REASONS`, `set_aside.WAY_OUT` and
  `set_aside.read` (it returns an `Aside`: `active` migration id to `SetAside`, and `released`).
  `plan.pending_work(..., aside)` and `set_aside.check` are the same for every reason.
- Facts of a park, from rows that exist: `state.read_failed_runs` (one SELECT of the deploy runs
  with the status `failed` or `unknown` and a `failed_step`; sent only when `park_failed` is
  true). A run parks the migration of its failed step `<file>#<n>` when its note (the reason code)
  is `BATCH_FAILED`, `GOVERNANCE_LIMIT` or `NONTX_FAILED`. `resolve --unpark <file>` writes a
  resolve step with the note `unpark <file>` (`set_aside.unpark_note`), as `clear-run` does; a
  later failure parks again.
- Independence (`set_aside.check`): a model batch touches what `diff.touched_objects` gives for
  its operation; a data or raw batch touches every name of their text. Names come from the token
  scan of modules (`modules.named_objects`), names written with their schema only. A module is
  compared by the names that the dependency scan of modules reads (`modules.referenced_objects`):
  a one-part name is an object of dbo (A17).
- New reason code `SET_ASIDE_DEPENDENCY` (22). New plan finding `parked`. `resolve --unpark`
  answers `RESOLVE_NOT_APPLICABLE` for a migration that is not parked.
- Where a set-aside migration is shown: the plan output (`note: parked: <file> (run <id>, <reason
  code>)`), `plan.json` (`notes`), `report.json` (`warnings`), the job summary of `plan` and
  `deploy`, and `drift`. `drift` prints one line `parked: <file> (run <id>, <reason code>)` for
  each park that still holds (`set_aside.in_force`: not applied here, not withdrawn in the chain of
  the release) and one line of its job summary. A park is reported, never drift: the exit code
  does not change. LOOK shows the run row of the park (`failed_step`, `note`).
- Plug-in points for a later reason, with the signatures of this build: `REASONS`, `WAY_OUT`,
  `read(session, config, recorded) -> Aside`, `SetAside(file, reason, why)`,
  `Aside(active, released)`, `check(pending, aside, sent_modules, model=None) -> notes`,
  `in_force(aside, steps, files) -> tuple[SetAside, ...]`, `plan.pending_work(bundle, recorded,
  module_chunk, aside, model=None)`. `read` is the one place that switches a reason on; `check`, `in_force`
  and the planner take every reason as it is.
- Strict default: with no reason switched on, `read` sends no batch and returns `NOTHING`. The
  test `test_with_park_failed_off_the_plan_and_the_state_writes_are_those_of_the_code_without_parking`
  runs one story (a failure, a failed rerun, a later release refused with `CATCHUP_REQUIRED`,
  `drift`, a refused `--unpark`) with and without the set-aside code paths, for a project without
  the key and with `park_failed = false`: the batches, outputs, `plan.json` files, job summaries,
  reports and state rows are equal.
- Not done: a name that a raw or data batch writes without its schema is not compared, and a
  three-part name (`[db].[s].[n]`) is read as `[db].[s]`. Proven against fakes only.
- A41. Review of A40. (1) A set-aside migration that creates a table, a schema, a type, a sequence
  or a synonym, or renames a table, does not give `BASELINE_REQUIRED`: `Work.set_aside` holds the
  parsed set-aside migrations, and `tables.Hooks.unrecorded_objects` takes what they create as
  created. (2) The independence check follows a rename with the head model
  (`plan.TableHooks.model`, passed to `pending_work` only when a migration is set aside): it
  touches the renamed table and the tables with a key to it. Without the model, a rename touches
  the names of its statement (in string literals, which the token scan does not read) and gets the
  note `independence not proven for a model batch: <file>`. (3) A module that names a set-aside
  table without its schema is refused (A17). (4) `resolve --mark-applied` reads the parks: a
  migration that later migrations ran past while it was parked is no hole. A migration that is
  still parked is refused with `RESOLVE_NOT_APPLICABLE` and the advice to unpark it first. (5) A
  parked replacement gets no note `late replacement: ...`. No state-table change.

### Products and holds (2026-10-28)

Owner problem: the objects of one product (for example `Balance`) need a tag, and a product can
stay in dev, never promoted, without a pull request that stays open. `docs/setup.md`, section 7,
"Products and holds", is the procedure.

- A42. Products. `azsqlcd.toml` takes the optional tables `[products.<name>]`: `objects`
  (required; patterns `schema.name` with the syntax, the match and the checks of `[scope]
  exclude_objects`, so `config._check_pattern` serves both), `owners` (`@user` or `@org/team`),
  `promote_to` (a name of `ENVIRONMENTS`). `config.Product`, `Config.products` (empty without the
  tables). An object belongs to at most one product: lint error `PRODUCT_OVERLAP`.
- Holds. In an environment after `promote_to` the product is held (`products.is_held`). A held
  migration is a set-aside migration with the reason `held` (`set_aside.HELD`, `set_aside.WAY_OUT`)
  and uses A40 as built: no run, catch-up past it, the independence check. `set_aside.read` is not
  changed: the hold needs the environment and the files of the release, so `products.hold(config,
  env, files)` returns a `Hold`, and `plan.pending_work(..., hold)` merges it into the `Aside`
  (`Hold.merge`). A migration of a product that is not held in the environment (any more) counts
  as set aside before (`Aside.released`), so it runs at its place in the chain after `promote_to`
  is raised; in dev, where nothing is held, not so. A module change or a drop of a module of a held
  product is not sent. A pending migration or a module to send that names a held module that the
  database lacks is 22 `SET_ASIDE_DEPENDENCY` (`products.check_held_modules`).
- What a migration touches is `set_aside.footprint`, the rule of the independence check.
- Not expected: in `compute_plan` an object of a held product, or one that a held migration
  creates, is not `BASELINE_REQUIRED`. `onboard.baseline` leaves out the objects of a held
  product that the database lacks (`onboard.absent_held`: one read of `catalog.list_user_objects`,
  sent only with `[products]`). `align` writes no statement for them, for the same reason: a
  DBA who creates such an object makes the held migration fail when it runs. `drift` and the read-back compare managed rows only, so a held object that was
  never recorded is not in them.
- Pull requests: `verify` and `gen` give `MIXED_HOLD` for a new migration that touches objects of
  a product with `promote_to` and other objects.
- Reports: plan notes `held: <file> (...)`, `products: <name> (<n> migrations, <m> modules)` and
  `held in <env>: ...`; notes are in the plan text, `plan.json`, `report.json` and the job summary,
  and outside `plan_sha256`. Command `azsqlcd products [--env E] [--codeowners]`, offline.
- State: nothing is written for a hold. No column, status or constraint was added.
- Imports: `plan.py`, `gen.py`, `onboard.py` and `cli.py` import `products`.
- New reason code `MIXED_HOLD` (22, gen). New finding codes `PRODUCT_OVERLAP` (lint), `MIXED_HOLD`
  (verify), `held` and `products` (plan notes).
- Not done: `drift` lists no pending or held migrations. `azsqlcd products --env` reads no
  database. Proven against fakes only.
- A43. Review of A42 (2026-10-28). (a) `Hold.merge` releases (`Aside.released`) only a migration
  of a product that the database could have held: not applied, and added by a release that the
  database holds (`chain_added_in <= at_seq`). Any other pending migration keeps A6 and A7, so a
  product with no `promote_to` changes nothing. (b) An unbind of a held module whose file text is
  not the recorded text is 22 `SET_ASIDE_DEPENDENCY` (`products.check_held_rebinds`): the rebind
  would send the held text. (c) `verify`: `HOLD_DEPENDENCY` (warning), a new migration that is not
  independent of a chain migration of a product with `promote_to`; `HOLD_DROPPED` (error), a pull
  request that changes `[products]` so that a merged migration of a product with `promote_to` at
  the base is no migration of any product (it would strand a database with `CHAIN_DIVERGED`);
  `MIXED_HOLD_MERGED` (warning), a merged migration that the changed `[products]` would hold as a
  whole. (d) `config.PROMOTION_ORDER` (dev to prod): `promote_to` is one of these, and nothing is
  held in `disposable`. (e) `azsqlcd products --codeowners` writes a space of a path as `\ `.

### Objects that one environment does not manage (2026-10-28)

Owner problem: the databases of some environments are not at the level of the files for some
objects, so those objects cannot be managed there yet. `docs/setup.md`, section 7, "Objects that
one environment does not manage", is the procedure.

- A39. `[env.<name>] not_managed`: patterns `schema.name` with the syntax and checks of `[scope]
  exclude_objects` (`config._check_patterns`). `config.for_env(config, env)` (applied by
  `cli._release` for plan, deploy, drift, baseline and resolve) joins them to the scope as
  `Scope.not_managed`; without [scope] the scope is every schema. In that environment the objects
  are outside the scope: not read, compared, baselined, listed or sent (`plan._managed_files`
  takes their module files out of the release for `pending_work`; `tables.Hooks.unrecorded_objects`
  skips their model keys). A managed row of such an object is marked `dropped` (the `unscope` step).
- Migrations (module `not_managed.py`): a pending migration that touches only such objects
  (`set_aside.footprint`) is set aside with the reason `not_managed` for good. The plan gets a step
  of the kind `not_managed` in the unit of state writes; the runner records a migration step with
  the status `not_applied` and the note `not_managed in <env>` (`set_aside.skip_note`), and
  `set_aside.skipped` reads it back from the steps, also after the pattern went. A migration that
  touches managed objects too: 22 `MIXED_NOT_MANAGED`. `set_aside.check` does not hold other
  migrations or modules to a `not_managed` one: its objects are outside the scope.
- A pattern that went: the objects come back as in [scope]; the table `BASELINE_REQUIRED` names
  `azsqlcd baseline --schema` also without [scope] when each unrecorded object has a `dropped`
  row. The skipped migration never runs.
- Plan note `not managed in <env>: <count> object(s); skipped migrations: ...` (outside
  `plan_sha256`). Without the key in the environment, `for_env` returns the config itself and the
  plan, the batches and the state writes are those of before.
- New reason code `MIXED_NOT_MANAGED` (22). New plan finding `not_managed`. Proven against fakes
  only.
- A40. Review of A39. (a) A DML trigger follows its table: `plan._managed_files` takes out a
  trigger file whose table (the name after `ON`, `modules.trigger_table`) a pattern matches, and
  `plan.scoped_state` and `plan.left_scope` take a trigger row out by its recorded `parent`
  (`catalog.capture_in_scope`), only when the environment has `not_managed`. (b) A skip does not
  replace A7 by the catch-up past set-aside migrations: only a park does. A release that adds a
  skipped migration can always be deployed, because the migration is skipped there. (c) A schema
  whose every object of the release a pattern matches is not managed either
  (`not_managed.hidden_schemas`): `tables.Hooks.manages` leaves it out of the unrecorded objects
  and of the baseline compare, so a migration that creates the schema and its objects is skipped
  and the schema is not asked for. (d) `BASELINE_REQUIRED` names `azsqlcd baseline --schema` also
  when an unrecorded object is of a schema that a skipped migration touches, and when the database
  has a baseline step and a table row (a whole baseline is refused there, `ALREADY_BASELINED`). (e)
  Plan notes, with `not_managed` only: a data or raw batch that runs (`not_managed not proven for a
  <kind> batch: ...`), and a migration or a module to send that names a matched object
  (`not_managed: <what> names ...`). The object of a raw batch directive counts as touched.

### Parking, holds and not_managed together (2026-10-28)

- A41. The three reasons of A35, A37 and A39 in one environment. Each set-aside migration keeps
  one reason: `compute_plan` adds the skips of `not_managed` to the `Aside` first, and
  `Hold.merge` keeps an entry that is there (`setdefault`), so a park or a skip wins over a hold.
  A product migration that an environment does not manage is skipped there for good, also after
  `promote_to` is raised. (a) `Work.set_aside` lists only the set-aside migrations that can run
  later (parked, held): a skip never creates its objects, so they stay `BASELINE_REQUIRED` when
  the pattern goes. (b) `drift` lists the parks only (`parked: ...`); a skip is shown by `plan` and
  `deploy`. (c) The notes `held in <env>: ...` and `products: ...` count the held migrations only,
  not a parked or skipped migration of a product. No state-table change. Scenario tests:
  `test_a_held_a_skipped_and_a_parked_migration_in_one_environment`,
  `test_a_product_migration_that_test_does_not_manage_is_skipped_there_not_held`.

---

# Part 2: blueprint (unchanged but for three lines; see "Reading order")

# azsqlcd: final blueprint

Terms. *Lineage*: one logical database that exists in dev, sandbox, test, preprod, prod. *Target*: one physical Azure SQL database (server + database). *Table-class object*: schema, table (columns, constraints, indexes), alias type, table type, sequence, synonym. *Module*: view, procedure, scalar or table-valued function, DML trigger. *Model*: parsed table-class objects of one git revision. *Release*: one bundle built from one commit on main. *Segment*: one transaction, or one non-transactional step. Markers: [assumption] = not in the evidence. [spike Ln / Gn] = must be proven live first (list at the end).

Version scope. v0.1 = runner, state, modules by checksum, hand-written migrations, pipeline. v0.2 = parser, model, proof, generator, table onboarding, table drift. Both are the MVP.

---

## (a) Repositories and layout

### Tool repository `akaalholdings/azsqlcd` (new; creation needs the owner's yes)

```
action.yml                       composite action: runs the CLI from this checkout
pyproject.toml  uv.lock  README.md
src/azsqlcd/
  cli.py        argparse commands, exit codes
  config.py     azsqlcd.toml (tomllib)
  lex.py        tokenizer, GO splitter, module header (from prototype tsql_lex.py)
  parse.py      DDL parser: CREATE forms, ALTER TABLE, DROP, sp_rename (from tsql_ddl.py)   v0.2
  model.py      typed model, canonical JSON, equality, defaults table                       v0.2
  emit.py       model -> canonical DDL (export, gen, init)                                  v0.2
  diff.py       model x model -> ordered statements (narrow rule set)                       v0.2
  replay.py     parsed migration statements -> model, with precondition checks              v0.2
  lint.py       rules with codes; batch classifier
  chain.py      migrations.sum, tombstones, effective order
  modules.py    module normal form, checksum, token-scan order, unbind rewrite
  release.py    build from git blobs, manifest, digest, verify
  plan.py       bundle x recorded state -> steps, plan hash, generic plan hash
  catalog.py    read-only catalog queries -> captures and model
  session.py    the ONLY module that imports the driver (lazy import)
  runner.py     fence, lock, segments, guards, reconcile
  state.py      DDL and reads/writes of schema azsqlcd
  errors.py     error-number parse from text, classes, exit codes
tests/unit  tests/fixtures  tests/live (owner-run only)
scripts/live_spike.py  scripts/live_acceptance.py  scripts/setup_repo.py
.github/workflows/verify.yml release.yml stage.yml drift.yml resolve.yml   (reusable)
.github/workflows/ci.yml                                                   (tool CI, no database job)
docs/runbook.md (one section per exit code)  docs/setup.md
```

### Database repository: one per lineage, for example `akaalholdings/db-sales`

Reason: GitHub environments, reviewers and the OIDC subject are per repository. A production deploy identity then exists in one lineage only.

```
azsqlcd.toml
schema/
  _tombstones.toml
  schemas/sales.sql
  types/sales.OrderLine_tt.sql
  sequences/sales.OrderNo.sql
  tables/sales.Order.sql          CREATE TABLE, GO, then its CREATE INDEX statements
  synonyms/dbo.LegacyOrder.sql
  views/sales.vw_OpenOrders.sql   one module per file, one batch, starts with CREATE OR ALTER
  functions/sales.fn_Tax.sql
  procedures/sales.usp_PlaceOrder.sql
  triggers/sales.tr_Order_Audit.sql
migrations/
  migrations.sum
  0001__add_order_status.sql
onboarding/<env>/                 reports, rename-constraints.sql, align.sql (one time)
.github/workflows/db.yml
.github/CODEOWNERS
```
CODEOWNERS (DBA team): `azsqlcd.toml`, `.github/**`, `.github/CODEOWNERS`, `migrations/**`, `schema/_tombstones.toml`.

`azsqlcd.toml` (no secrets; client ids are not secrets):
```toml
[project]
name = "sales"
tenant_id = "<guid>"
table_model = false          # true after table files are exported (v0.2)
module_chunk = 100
min_token_minutes = 20       # [assumption] tune after spike L13

[identities]
nonprod_plan = "<client id>"
prod_plan = "<client id>"
nonprod_deploy = "<client id>"
test_deploy = "<client id>"
preprod_deploy = "<client id>"
prod_deploy = "<client id>"

[env.dev]
plan_identity = "nonprod_plan"
deploy_identity = "nonprod_deploy"
drift = "report"             # drift on objects the plan touches always blocks
lock_timeout_ms = 30000
applock_wait_s = 600
job_timeout_minutes = 120
targets = [{ id = "sales-dev", server = "sql-sales-dev.database.windows.net", database = "sales" }]
# [env.sandbox] [env.test] [env.preprod]: same shape
[env.prod]
plan_identity = "prod_plan"
deploy_identity = "prod_deploy"
drift = "block"
lock_timeout_ms = 10000
applock_wait_s = 600
job_timeout_minutes = 120
targets = [{ id = "sales-prod", server = "sql-sales-prod.database.windows.net", database = "sales" }]

[unmanaged]
objects = ["TABLE:[audit].[Log]"]            # written by export, with reason codes in the report
[ack]
unmanaged_dependants = []                    # "[rpt].[vMargin] -> [sales].[Order].[LegacyCode]"
```
Estate registration: each repository registers its own targets. The estate is the set of repositories with topic `azsqlcd`. Several physical databases with one schema are several `targets` of one environment (matrix legs). No central registry, no cross-repository status command (no identity could read other lineages; the later front end reads the toml files).

File rules (lint errors): path equals `<kind dir>/<schema>.<name>.sql`; one object per file; two-part names; no USE, no three-part names, no `PASSWORD =` / `SECRET =` literal; module files are exactly one batch and start with `CREATE OR ALTER`. Table files, normal form (v0.2): NF001 explicit NULL / NOT NULL; NF002 every constraint and index named; NF003 explicit length, precision, scale; NF004 no type synonyms (integer, dec, numeric alias forms, rowversion, national forms, float(n)); NF005 explicit CLUSTERED / NONCLUSTERED on PRIMARY KEY, UNIQUE and CREATE INDEX; ORD001 a new column is the last column.

---

## (b) The change model

### What a developer edits
Object files under `schema/`, a tombstone entry for a deleted module, and (optionally) the SQL of a migration before merge.

### Migrations
`migrations/migrations.sum`, one line per migration, apply order:
```
azsqlcd-sum 1
baseline
0001__add_order_status.sql sha256:<hex> tx
0002__ix_order_status.sql  sha256:<hex> nontx
0003__status_not_null.sql  sha256:<hex> tx withdrawn
0004__status_not_null_v2.sql sha256:<hex> tx replaces=0003__status_not_null.sql
```
New lines append at the end, so two branches conflict in git. The loser merges main and runs `azsqlcd gen --resum` (renumber, re-hash; v0.2 also re-proves). A line present at the base revision is byte-identical at head except for the added word `withdrawn`. Migration files are immutable after merge. Effective order: a replacement runs at the chain position of the migration it replaces.

Migration file:
```sql
-- azsqlcd:migration 0001__add_order_status
-- azsqlcd:mode tx
ALTER TABLE [sales].[Order] ADD [Status] tinyint NOT NULL CONSTRAINT [DF_Order_Status] DEFAULT (0);
GO
-- azsqlcd:data
UPDATE [sales].[Order] SET [Status] = 1 WHERE [ShippedUtc] IS NOT NULL;
GO
```
Batch kinds (closed list):
- model batch (default): one statement. v0.2 with `table_model = true`: must parse with the closed grammar (CREATE/DROP TABLE; ALTER TABLE ADD / ALTER COLUMN / DROP COLUMN / ADD or DROP CONSTRAINT; CREATE/DROP INDEX; CREATE/ALTER/DROP SEQUENCE; CREATE/DROP SCHEMA, TYPE, SYNONYM; `EXEC sys.sp_rename` with three literal arguments). v0.1: not parsed, classified by the lexer only.
- `-- azsqlcd:data` first line: DML only. The lexer rejects CREATE, ALTER, DROP, sp_rename tokens.
- `-- azsqlcd:raw <object key> reason: <text>` first line: hand-written DDL for an unmanaged object. Not modelled. Always needs `allow RAW`.
- `-- azsqlcd:mode nontx expected-minutes: N` in the header: the file is exactly one batch and runs outside a transaction.
Directives (own line, column 0): `-- azsqlcd:deploy-module [s].[n]`, `-- azsqlcd:unbind [s].[n]`, `-- azsqlcd:allow <CODE> <object> reason: <text>`.
Forbidden tokens in any batch: BEGIN TRANSACTION, COMMIT, ROLLBACK, SAVE TRANSACTION, RAISERROR (use THROW), RETURN, GOTO, SET NOEXEC/PARSEONLY/XACT_ABORT, USE, `GO n`, sqlcmd directives.

### Generation (v0.2): `azsqlcd gen --base origin/main --name add_order_status [--rename column:[sales].[Order].[Stat]=[Status]]`
Runs on the developer machine or in the coding agent. Stdlib only, no database, no network. Not in the pull-request job (that job has `contents: read`, and the reviewer must see the SQL that will run).
1. M_base = parse(`schema/` at merge base, bytes from `git show`). M_head = parse(working tree). Same lexer, parser and defaults table on both sides (E2a).
2. Statements = diff(M_base, M_head) in fixed order: renames; drop FKs; drop CHECK/DEFAULT, indexes, PK/UNIQUE; drop columns; drop tables; create schemas, types, sequences; create tables without FKs; add columns; alter columns; add PK/UNIQUE; create indexes; add CHECK/DEFAULT; add every FK as a separate ALTER TABLE; synonyms; drop sequences, types.
3. For each DEFAULT, CHECK or computed expression that is added: token-scan for names of function files; write `-- azsqlcd:deploy-module` before the statement. If that function text names a table-class object created later in the same migration: stop with ORD002 (split the migration).
4. For each ALTER COLUMN, DROP COLUMN, DROP TABLE or rename: scan module files at the base revision whose header has SCHEMABINDING for the table name; write `-- azsqlcd:unbind` lines at the top, dependants first.
5. Destructive statements are written with a `-- azsqlcd:allow <CODE> <object> reason: TODO` line. Lint fails until a reason replaces TODO.
6. Write the file and append the chain line. If only modules changed: write nothing.
Generator refuses, with a code and a hand-write hint (the author writes SQL; replay proves it): column reorder, add or remove IDENTITY, computed-column definition change, collation change, ALTER COLUMN with modelled dependants (index, constraint, computed column), alias or table type change. Index change = DROP + CREATE (two statements).

### Human edit
Free edit inside the closed list before merge: split ADD NOT NULL into add-null / data backfill / alter; replace DROP + ADD with sp_rename; move an index build to its own nontx migration. Then `azsqlcd gen --resum`.

### Proof with no build database: `azsqlcd verify --base <base sha>` (pull-request job)
1. Lint every file at head. Unknown syntax = error with file, line, expected tokens.
2. Chain: base lines unchanged (except `withdrawn`); every new file sha256 matches; numbers rise; `replaces` names a withdrawn line.
3. Modules: header kind and name equal the path; kind unchanged against base (KND001); every module key in base and not in head has a tombstone; a tombstone for a key that still has a file is an error; the name of a tombstoned or renamed object does not appear as an identifier token in any module file (DRP002 / REN001); order is computable (ORD004 = cycle).
4. Batch classification and allow lines (section h); LCK001; nontx rules.
5. v0.2, `table_model = true`: M_base, M_head as in gen. Replay the new migrations in effective order on M_base. Each statement has precondition checks on the in-memory model: object exists / does not exist; ALTER COLUMN and DROP COLUMN blocked by a modelled index, PK/UNIQUE/FK, CHECK, DEFAULT (drop column) or computed column; FK target has a matching key and equal types. Require result == M_head (canonical JSON; expressions compared as token sequences, trivia removed, keywords case-folded). A difference prints object and property. A pull request that changes a table-class file with no migration fails and prints what `gen` would write.
6. Withdraw-and-replace: the pull request changes no table-class file (WDR001). M_pre = parse(`schema/` at the parent of the commit that added the withdrawn file, found with `git log --diff-filter=A`). Require replay(withdrawn, M_pre) == replay(replacement, M_pre). The replacement needs `-- azsqlcd:allow REPLACEMENT_EDGE <withdrawn id> reason: ...`.
Limits, printed in the job summary: data and raw batches are not modelled; engine acceptance is proven by the first real deploy (dev) and by the in-transaction read-back. In v0.1 there is no model proof at all.

### Modules
Edit the file. Nothing is generated. Checksum = sha256 of the frozen normal form: UTF-8, BOM removed, CRLF to LF, trailing whitespace removed per line, trailing blank lines removed. The bytes sent to the engine are the file bytes. Which modules a target needs is decided at plan time: file checksum against `azsqlcd.object.source_sha256` (NULL = redeploy).

---

## (c) Onboarding

Reference environment: prod (read-only, plan identity) [owner question].

1. `azsqlcd export --env prod --target sales-prod --out schema/`.
   - v0.1: modules only. Text from `sys.sql_modules.definition`, bytes kept. Two token-level edits in the header span: verb becomes `CREATE OR ALTER`; the name becomes the catalog name when it differs (after sp_rename). Each edit is listed in `onboarding/prod/export.md`.
   - v0.2: table-class objects too. catalog.py reads sys.schemas, tables, columns, types, default_constraints, check_constraints, computed_columns, identity_columns, key_constraints, indexes, index_columns, foreign_keys, foreign_key_columns, sequences, synonyms, table_types. emit.py writes canonical CREATE files. Expression text is written exactly as the catalog returns it. System-named constraints (`is_system_named = 1`) are written with a fixed name: `DF_<table>_<column>`, `CK_<table>_<n>`, `PK_<table>`, `UQ_<table>_<cols>`, `FK_<table>_<reftable>_<n>`.
2. Round-trip gate: parse(exported file) must equal the catalog model, object by object (names of system-named constraints excluded).
3. Quarantine. An object that fails goes to `[unmanaged]` in azsqlcd.toml and to `onboarding/prod/unmanaged.md` with a reason code: UNSUPPORTED (property outside the model: temporal, ledger, memory-optimized, graph, external, partitioned, columnstore, indexed view, full-text/XML/spatial/JSON/vector index, Always Encrypted, masking, sparse), ROUNDTRIP, ENCRYPTED (definition NULL with VIEW DEFINITION granted; NULL without the grant is a hard error), SET_OPTIONS (uses_ansi_nulls = 0 or uses_quoted_identifier = 0), SIGNED (row in sys.crypt_properties), PARSEONLY (file fails `SET PARSEONLY ON` on the source). The tool never creates, alters, drops or refreshes an unmanaged object. Change path: a `raw` batch. Managed modules may reference unmanaged objects.
4. Sub-objects on managed tables. Managed = present in the file. At export every index is written to the file except `auto_created = 1` and `is_hypothetical = 1`. Later, a live index, user statistic or constraint that is not recorded is an unmanaged sub-object: reported, never dropped, not in read-back or drift.
5. Commit the export by pull request. `migrations.sum` gets the line `baseline`.
6. Other environments. `azsqlcd baseline --env <E> --target <T> --report-only` compares the catalog of T with the catalog capture of prod stored in `onboarding/prod/snapshot.json` (catalog against catalog: both engine-normalised, sound by E2). Output `onboarding/<E>/baseline-diff.md`, per object: equal / differs (property) / only here / missing here. Column ordinal differences are a warning, not a difference.
   - Module differs: no action. Its row is written with `source_sha256 = NULL`; the first deploy overwrites it with the file text; the plan lists it as OVERWRITE_MODULE.
   - Only here: unmanaged in that target. Reported, never dropped.
   - System-named constraint with equal shape (table, kind, columns, expression): the tool writes `onboarding/<E>/rename-constraints.sql` (only `EXEC sys.sp_rename`, metadata only), for prod too. A human reviews it.
   - Table-class object differs or is missing: baseline refuses. Fix: refresh the environment from prod (DBA work), or `azsqlcd align` writes `onboarding/<E>/align.sql` (read-only; a DBA reviews and runs it, A38).
   - `azsqlcd align --env <E> --target <T> --script onboarding/<E>/rename-constraints.sql --confirm-database <db>` runs a script through the runner (lock, run row, one transaction, guard). Used for rename and align scripts only; available from `resolve.yml`.
7. `azsqlcd baseline --env <E> --target <T> --confirm-database <db>`: takes the lock; creates schema `azsqlcd` and its tables; writes meta (project, environment); requires structural equality of the catalog and the release model for managed table-class objects (v0.2); writes a `baseline` step and one object row per managed object with its capture. No object DDL. Order: dev, sandbox, test, preprod, prod.
8. v0.1 to v0.2 switch: one pull request adds the table files and sets `table_model = true`, with no migration. A plan that finds a table-class object in the model with no object row and no pending migration that creates it refuses: "run baseline".
9. Empty database (v0.2): `azsqlcd init --env <E> --target <T> --confirm-database <db>`. Allowed only when the database has zero user objects. Emits schemas, types, sequences, tables without FKs, indexes, FKs, synonyms, modules in order through the runner with read-back; records every chain migration as applied. A greenfield chain without a `baseline` line needs no init: `deploy` creates the state tables on an empty database and applies the chain.
`deploy` refuses (exit 2, "baseline required") a database that has user objects and no `azsqlcd` schema.

---

## (d) Ordering inside one release

Content of a transactional segment, fixed order:
1. Pending transactional migrations in effective chain order; batches in file order; `unbind` and `deploy-module` directives at their written position.
2. Modules: every remaining module whose checksum differs or is NULL. Order = topological order of a token-scan graph (lex each file; strings and comments excluded; two-part and bare names matched against managed object names), plus `-- azsqlcd:after [s].[n]` lines in a module file. Ties: functions, views, procedures, triggers, then name. No retry.
3. Drops: tombstoned modules that have a managed row, reverse dependency order.
4. Refresh: `EXEC sys.sp_refreshsqlmodule` for every managed, unchanged, non-schema-bound view, function, procedure and trigger that references a table this segment altered (set from `sys.sql_expression_dependencies` at plan time). A failure rolls the segment back.
5. Read-back, object rows, step rows, COMMIT (section e).
A nontx migration ends the current segment. Modules, drops and refresh ride with the last transactional segment. Lint warning NTX003: a nontx migration in a release that has other pending changes.

The five cases:
- New column used by a procedure: migration (1) before modules (2), same transaction.
- Function used by a computed column, CHECK or DEFAULT: `-- azsqlcd:deploy-module [dbo].[fn_Tax]` in the migration, written by `gen`, visible in review. The function is created inside the migration's transaction. ORD002: the function needs an object created later in the same migration = split. ORD003: a pull request that changes a function named by an existing CHECK or computed column must hold a migration that drops the constraint, has `deploy-module`, and adds the constraint again [spike L11b].
- Schema-bound view or function that blocks ALTER TABLE: `-- azsqlcd:unbind [s].[v]` at the top of the migration. Runner: read `sys.sql_modules.definition`; its hash must equal the recorded capture; token-level rewrite: verb to ALTER, remove SCHEMABINDING from the header WITH list (tokens before the first depth-0 AS); send it. Permissions and object_id stay. Set the row `source_sha256 = NULL`, so step 2 deploys the file text and binds again. Chained schema-bound modules: unbind dependants first [spike L11c]. An unmanaged schema-bound dependant is found by the plan-time blocker scan and blocks. Indexed views are refused. Before the unbind code ships: lint SB001 refuses the migration.
- Circular foreign keys: no FK inside CREATE TABLE in generated SQL; all FKs are separate ALTER TABLE statements at the end. `init` uses the same rule.
- Dropped objects: FKs first, then tables, inside the migration (allow line). Modules last (step 3). A schema-bound module that must go: unbind, then tombstone.
Catch-up limit (runbook): `deploy-module` and step 2 always deploy head text. If head text needs a later release, the segment rolls back cleanly; promote the intermediate release first: `gh workflow run db.yml -f release=r<seq>`.

---

## (e) State in the database, lock, failure matrix

### DDL (created by `baseline`, `init`, or the first `deploy` to an empty database)
```sql
CREATE SCHEMA [azsqlcd] AUTHORIZATION [dbo];
GO
CREATE TABLE [azsqlcd].[meta] (
  [id]            tinyint       NOT NULL CONSTRAINT [PK_meta] PRIMARY KEY CLUSTERED
                                CONSTRAINT [CK_meta_one] CHECK ([id] = 1),
  [state_version] int           NOT NULL,      -- 1
  [project]       nvarchar(128) NOT NULL,      -- azsqlcd.toml [project].name
  [environment]   varchar(20)   NOT NULL,      -- dev sandbox test preprod prod disposable
  [created_utc]   datetime2(3)  NOT NULL
);
CREATE TABLE [azsqlcd].[run] (
  [run_id]             bigint IDENTITY(1,1) NOT NULL CONSTRAINT [PK_run] PRIMARY KEY CLUSTERED,
  [command]            varchar(10)    NOT NULL CONSTRAINT [CK_run_command] CHECK ([command] IN ('deploy','init','baseline','align','resolve')),
  [status]             varchar(10)    NOT NULL CONSTRAINT [CK_run_status] CHECK ([status] IN ('running','ok','failed','unknown')),
  [segments_committed] int            NOT NULL,   -- fence: incremented inside each segment transaction
  [release_seq]        int            NOT NULL,
  [git_sha]            char(40)       NOT NULL,
  [manifest_sha256]    char(64)       NOT NULL,
  [plan_sha256]        char(64)       NOT NULL,
  [tool_version]       varchar(32)    NOT NULL,
  [started_utc]        datetime2(3)   NOT NULL,   -- server SYSUTCDATETIME() before the first batch = restore reference
  [finished_utc]       datetime2(3)   NULL,
  [session_id]         int            NOT NULL,
  [principal_name]     sysname        NOT NULL,
  [ci_actor]           nvarchar(128)  NULL,
  [ci_run_url]         nvarchar(400)  NULL,
  [failed_step]        nvarchar(200)  NULL,
  [error_number]       int            NULL,
  [error_text]         nvarchar(300)  NULL,       -- truncated server message; never batch text
  [note]               nvarchar(1000) NULL        -- resolve reason, accepted drift, adopted object
);
CREATE TABLE [azsqlcd].[step] (
  [step_id]      bigint IDENTITY(1,1) NOT NULL CONSTRAINT [PK_step] PRIMARY KEY CLUSTERED,
  [run_id]       bigint        NOT NULL CONSTRAINT [FK_step_run] REFERENCES [azsqlcd].[run] ([run_id]),
  [kind]         varchar(10)   NOT NULL CONSTRAINT [CK_step_kind] CHECK ([kind] IN ('baseline','init','migration','nontx','modules','align','resolve')),
  [migration_id] nvarchar(200) NULL,
  [file_sha256]  char(64)      NULL,
  [status]       varchar(12)   NOT NULL CONSTRAINT [CK_step_status] CHECK ([status] IN ('ok','started','unknown','not_applied')),
  [applied_utc]  datetime2(3)  NOT NULL,
  [note]         nvarchar(400) NULL
);
CREATE UNIQUE NONCLUSTERED INDEX [UX_step_migration] ON [azsqlcd].[step] ([migration_id]) WHERE [migration_id] IS NOT NULL;
CREATE TABLE [azsqlcd].[object] (
  [object_key]      nvarchar(300) NOT NULL CONSTRAINT [PK_object] PRIMARY KEY CLUSTERED,  -- 'PROCEDURE:[sales].[usp_x]'
  [status]          varchar(8)    NOT NULL CONSTRAINT [CK_object_status] CHECK ([status] IN ('managed','dropped')),
  [source_sha256]   char(64)      NULL,       -- module: file normal form; NULL = redeploy. Table-class: always NULL
  [capture_format]  int           NOT NULL,   -- version of the capture field set
  [catalog_capture] nvarchar(max) NOT NULL,   -- canonical JSON of catalog rows, engine text and flags
  [catalog_sha256]  char(64)      NOT NULL,   -- sha256 of catalog_capture; fast path only when formats are equal
  [run_id]          bigint        NOT NULL CONSTRAINT [FK_object_run] REFERENCES [azsqlcd].[run] ([run_id]),
  [recorded_utc]    datetime2(3)  NOT NULL
);
```
An object is managed if and only if it has a row with status `managed` (E5). Rows are never deleted (the deploy identity has no DELETE).

Capture content. Module: definition (line ends normalised), uses_ansi_nulls, uses_quoted_identifier, is_schema_bound, execute_as_principal_id, object type. Table: column rows (name, base type, max_length, precision, scale, is_nullable, identity seed/increment, is_computed, is_persisted, collation when declared); constraint rows (name, kind, columns, FK target and actions, engine expression text); recorded index rows (name, type, unique, key columns with direction, includes, filter text, declared options). Never in a capture: object_id, dates, ordinal, identity last value, sequence current value, statistics, unrecorded sub-objects, auto_created and hypothetical indexes.
Comparison rule (drift, precondition, read-back): project the live catalog onto the field set of the stored capture, compare field by field. Expression text is compared capture against capture only. Read-back against the model compares structure only (names, types, nullability, identity, keys, columns of constraints and indexes); expression text is captured, not compared.

### Connection, fence, lock
- One dedicated connection, not pooled, autocommit. `Encrypt=yes;TrustServerCertificate=no;ConnectRetryCount=0;APP=azsqlcd/<version>`. No client statement timeout.
- Session: `SET XACT_ABORT ON; SET LOCK_TIMEOUT <ms>; SET NOCOUNT ON; SET ANSI_NULLS, QUOTED_IDENTIFIER, ANSI_PADDING, ANSI_WARNINGS, ARITHABORT, CONCAT_NULL_YIELDS_NULL ON; SET NUMERIC_ROUNDABORT OFF;` then asserted.
- Fence, no override, exit 2: `SERVERPROPERTY('EngineEdition') = 5`; `DATABASEPROPERTYEX(DB_NAME(),'Updateability') = 'READ_WRITE'`; `DB_NAME()` = configured database; meta.project and meta.environment = config (when meta exists); catalog collation is case-insensitive [spike L16].
- Lock: `EXEC @r = sys.sp_getapplock @Resource = N'azsqlcd:deploy', @LockMode = N'Exclusive', @LockOwner = N'Session', @LockTimeout = <applock_wait_s * 1000>;` Accept 0 or 1; else exit 5. Store `@@SPID`.

### Plan algorithm (`azsqlcd plan`, read-only identity; the same function runs inside deploy)
1. Verify the bundle: every file sha256 against the manifest; manifest digest = `--digest`.
2. Token, connect (connect retry only), session options, fence.
3. Read meta, run, step, object. Refuse (exit 2) when: a step is `started` or `unknown`; a run is `running` and its session still exists; an applied migration is not in the chain or its sha256 differs; recorded release_seq > artefact release_seq (no downgrade).
4. Pending migrations = chain entries, effective order, that are not withdrawn-and-unapplied-replaced, have no `ok` step, and do not replace an applied migration. A withdrawn migration with no `ok` step is skipped. Module changes = files whose checksum differs from `source_sha256` or whose row has NULL. Drops = tombstones with a managed row. A managed module row with neither file nor tombstone: exit 2.
5. Name collision: a module or table-class create for a key with no managed row whose name exists in the live catalog: exit 2 ("unmanaged object with this name; adopt it with resolve or rename").
6. Drift: touched objects must equal their capture (exit 2). Other managed objects: listed; exit 2 when `drift = "block"`. Unmanaged objects: listed.
7. Legacy flags: a module to deploy whose live flags are not ANSI_NULLS ON and QUOTED_IDENTIFIER ON: exit 2.
8. v0.2 scans on the target for every table or column that pending statements drop, rename or retype (list from the manifest): (a) blockers: unrecorded indexes, user statistics, FKs, schema-bound modules on the column: exit 2 with names; (b) dependants: all referencing modules from `sys.sql_expression_dependencies` and `sys.dm_sql_referencing_entities`; an unmanaged one blocks unless listed in `[ack] unmanaged_dependants`; managed unchanged ones form the refresh set; managed dependants that are already broken (`sys.dm_sql_referenced_entities` error) are a warning.
9. PARSEONLY rung on a second connection: canary first (a syntax-error batch must raise; `SELECT 1/0` must not raise). Canary fails: stop, exit 2. Then every pending batch and module under `SET PARSEONLY ON`. Any error: exit 2.
10. Approver facts: per touched table row count and reserved pages (`sys.dm_db_partition_stats`), service objective, token minutes left.
11. Segments. Destructive list (section h). `plan_sha256` = sha256 of canonical JSON {tool version, server, database, manifest digest, applied migration list (id, sha256, status), capture hashes of touched objects, ordered segments with step ids, file hashes, modes, module keys and checksums, destructive list}. `generic_plan_sha256` = sha256 of {ordered step ids, file hashes, modes, module keys and checksums, drops, destructive list}. Printed next to the value in the previous stage's report: EQUAL or DIFFERENT (report only).
12. Write `plan.json`, the job summary (destructive list first, with a banner) and outputs.

### Deploy algorithm (`azsqlcd deploy --expect-plan-file plan/plan.json`)
1. Verify the bundle and the digest. Running tool version = plan tool version. Else exit 2.
2. Token with at least `min_token_minutes` left, else exit 4. Connect (retry connect only: 5, 10, 20, 40, 60 s; budget 180 s; covers serverless resume), else exit 4.
3. Session options, fence, lock (exit 5).
4. Reconcile under the lock: a `running` run whose session is gone becomes `failed` (note "reconciled"); if one of its nontx steps is `started`, that step becomes `unknown` and the run `unknown`: exit 2 ("run resolve").
5. Recompute the plan (steps 3-8 and 11 of the plan algorithm). `plan_sha256` must equal the expected value, else exit 2 ("stale plan; start again from the plan job"). Nothing has been executed.
6. Nothing pending: release the lock, exit 0.
7. INSERT the run row (`running`, `segments_committed = 0`, `started_utc = SYSUTCDATETIME()`), autocommit.
8. Transactional segment n:
   a. One batch: `IF @@SPID <> <spid> OR APPLOCK_MODE(N'public', N'azsqlcd:deploy', N'Session') <> N'Exclusive' THROW 51000, N'azsqlcd: session or lock lost', 1; BEGIN TRANSACTION; UPDATE [azsqlcd].[run] SET [segments_committed] = [segments_committed] + 1 WHERE [run_id] = <id>;`
   b. For each batch: send the exact file bytes of the batch; drain every result set with nextset(); then `SELECT @@TRANCOUNT, XACT_STATE()` must return (1, 1).
   c. Module batches: before CREATE OR ALTER the recorded flags must equal the session flags. Unbind and deploy-module at their position.
   d. Drops, refresh.
   e. Read-back in the same transaction: touched table-class objects against the model after this segment (v0.2); modules: object exists, kind matches, flags ON, is_schema_bound equals the header, and (if spike L7 confirms the engine stores the submitted text) normalised definition = normalised file text. If the segment holds a `data` or `raw` batch: capture ALL managed objects; any untouched object that changed = failure.
   f. Upsert object rows (new capture, checksum); mark dropped rows; INSERT step rows (`ok`); guard; `COMMIT TRANSACTION`; `SELECT @@TRANCOUNT` must be 0.
9. Non-transactional step: assert spid and lock; INSERT (or UPDATE from `not_applied`) the step row to `started`, committed; send the single batch with no client timeout; then one small transaction: step `ok`, read-back, object rows.
10. UPDATE run `ok`, `finished_utc`. Release the lock. Close. Write `report.json` and the job summary (includes `started_utc`).
On an error with the connection alive: `IF @@TRANCOUNT > 0 ROLLBACK`; assert `@@TRANCOUNT = 0`; UPDATE run `failed` (failed_step, error number and text). Never logged: batch text, tokens. Retried: connect and lock wait only. No batch is sent twice.

### Exit codes
0 done or nothing to do. 1 failed; the failing segment was rolled back; state is at a segment boundary. 2 refused; nothing executed. 3 outcome unknown; a human must run resolve. 4 clean stop; safe to start again. 5 another run holds the lock. 10 drift found (`azsqlcd drift` only).

### Failure matrix
"Rolled back" means schema and data of the segment are undone. Consumed identity and sequence values, UPDATE STATISTICS and audit records remain (evidence).

| Failure | Database state | History state | Exit | Recovery |
|---|---|---|---|---|
| Compile error in a batch, tx segment (syntax is normally caught by PARSEONLY, exit 2) | Segment rolled back; earlier segments stay | No step rows for the segment; run `failed` | 1 | Not applied anywhere: fix the file before merge. Applied elsewhere: withdraw-and-replace by pull request. Then the new release runs from dev |
| Run-time error in batch N, tx segment | Rolled back by XACT_ABORT; `@@TRANCOUNT = 0` asserted | Same | 1 | Same |
| Guard not (1,1) and no error raised (script ended the transaction) | Unknown part applied | run `unknown` | 3 | Inspect; `resolve` |
| Read-back differs, or an untouched object changed | Rolled back | run `failed`, failed_step = object | 1 | Tool or migration defect; fix parser, generator or migration |
| Refresh of a managed dependant fails | Rolled back | run `failed` names the module | 1 | Fix the module in the same pull request, or tombstone it |
| Lock timeout 1222 or deadlock 1205 on a user object | Rolled back | run `failed` | 4 (1 if the number cannot be parsed) | `gh workflow run db.yml -f release=r<seq>` |
| Applock not granted | Untouched | None | 5 | Wait for the other run; start again |
| Connection lost, tx segment | Rolled back by the engine, or committed if COMMIT reached the server | After spike L6: new connection, applock, `SELECT segments_committed FROM azsqlcd.run WITH (READCOMMITTEDLOCK, ROWLOCK) WHERE run_id = <id>` under LOCK_TIMEOUT; value >= n = committed, else rolled back; run `failed` with note | 4 when determined; 3 on timeout; 3 always until L6 passes | Exit 4: start again (continues at the next pending step). Exit 3: wait, `azsqlcd plan`, then start again or `resolve` |
| Connection lost, nontx step | Unknown (build may run, be paused, or be done) | step `started`; next run sets `unknown` | 3 | Inspect `sys.index_resumable_operations`; finish or abort by hand; `gh workflow run resolve.yml -f environment=prod -f target=sales-prod -f action=mark-applied -f subject=0002__ix_order_status.sql -f confirm_database=sales -f reason="..."` (or `mark-not-applied`) |
| Error in a nontx step, connection alive | Partial effect possible | step `unknown`, run `unknown` | 3 | Same resolve |
| Runner killed or job cancelled | As connection lost | run stays `running` until the next run reconciles under the lock | none (job shows cancelled) | Start again. tx: continues. nontx: exit 2 until resolve |
| Token cannot be minted, or has too little life | Untouched | None | 4 | Start again (new OIDC login) |
| Token expires on an open session [spike L13] | Assumed: session ends = connection lost | As connection lost | 4 or 3 | As connection lost. Long work belongs in RESUMABLE nontx steps |
| Governance error 40552, 9002, 40549, 40550, 40551, 40544 | tx: rolled back. nontx: unknown | tx: run `failed`. nontx: step `unknown` | 1 (tx), 3 (nontx) | Never retried. Redesign: smaller data batches, ONLINE/RESUMABLE nontx step |
| Transient 40613, 40197, 40501, 49918-49920, 10928, 10929, 4060 at connect | Untouched | None | Retried inside the connect budget, then 4 | Start again |
| Stale plan, drift, collision, blocker, started/unknown step | Untouched | None | 2 | Fix the cause; start again from the plan job |

`azsqlcd resolve --env E --target T --confirm-database NAME --reason TEXT` with one of: `--mark-applied <migration>` / `--mark-not-applied <migration>` (only for a `started` or `unknown` nontx step); `--accept-drift <module key>` (capture := live, `source_sha256 := NULL`); `--adopt-module <key>` (unmanaged module becomes managed with `source_sha256 = NULL`; the next plan lists OVERWRITE_MODULE). It takes the lock, writes a run row (`command = 'resolve'`, note) and a `resolve` step. It runs from `resolve.yml` behind the environment approval, or from a machine with a network path.

---

## (f) Promotion

Build once. `azsqlcd build --commit <sha> --out dist/` reads file bytes from git objects of that commit (not the working tree), runs lint, writes `bundle.tar` (schema/, migrations/, azsqlcd.toml) and `manifest.json`: commit sha, release_seq = `git rev-list --count --first-parent <sha>`, sorted list of (path, sha256), chain entries with mode, touched objects and destructive items, module list (key, kind, checksum, schema-bound flag, scan edges), largest `expected-minutes`. Digest = sha256 of the canonical manifest JSON. No tool version in the manifest.
The release job creates tag `r<release_seq>` and a GitHub Release with both files. Tags `r*` are protected by a ruleset. Every later job downloads the asset, verifies each file against the manifest and the manifest against the `digest` input. A dispatched promotion (`-f release=r57`) checks out the tag, rebuilds, and requires the same digest as the stored manifest.

Order: dev > sandbox > test > preprod > prod, linked by `needs`. Each stage: plan job, environment approval, deploy job.

Environment N fails: later stages do not run. Exit 1: fix forward by pull request; the new release starts at dev; each target applies only what it lacks. Exit 4 or 5: `gh workflow run db.yml -f release=r<seq>` (lower stages are no-ops). Exit 3: resolve, then the same command. Exit 2: fix the cause, same command.
Behind: all pending migrations in order plus the current module set, one plan. The plan prints the database release, the artefact release and the generic plan compare line. Ahead or diverged: exit 2; the tool never moves a database backward.
Hotfix: trunk only. A hotfix is a pull request to main that passes five stages; speed comes from fast approvals. An unreleasable change on main is reverted by pull request first (the revert of a table change is a new forward migration).
Rollback: no in-place restore on Azure SQL Database. Forward fix and expand/contract. `azsqlcd.run.started_utc` is the point-in-time-restore reference; the summary prints it.
Drift before a deploy: touched object = exit 2 with object, property, captured value, live value. Module paths: revert by hand; or `azsqlcd drift --env E --target T --export <key> --out schema/` writes the live text for a pull request, then `resolve --accept-drift`. Table paths: revert by hand, or revert and ship a migration. There is no accept for table drift in the MVP. `drift.yml` runs `azsqlcd drift` on a schedule with the plan identity (exit 10 fails the job).

---

## (g) Validation ladder and gates

| Rung | Where | Database access | Blocks |
|---|---|---|---|
| 1 Lex every file; file rules; batch classification; forbidden tokens; nontx rules; LCK001 | pull request | none | yes |
| 2 Chain immutability; tombstones; module header, kind, order; rename and drop residue | pull request | none | yes |
| 3 v0.2: parse + model validation (duplicate names, FK target and types, columns of indexes and constraints exist); replay proof; withdraw-and-replace proof; normal-form lint | pull request | none | yes |
| 4 Fence, state, chain position, no downgrade, name collision, drift, legacy flags | plan job | read-only | yes |
| 5 v0.2: blocker scan and dependant scan on the target | plan job | read-only | yes (ack list for unmanaged dependants) |
| 6 PARSEONLY with canary for every pending batch (syntax only) | plan job | public | yes |
| 7 Approver facts; generic plan compare with the previous stage | plan job | read-only | no |
| 8 Environment approval | GitHub | none | yes (test optional, preprod, prod) |
| 9 Plan hash recomputed under the lock | deploy job | deploy | yes |
| 10 Guard after every batch; read-back and refresh before COMMIT | deploy job | deploy | yes, rollback |
The pull-request job has `contents: read`, no id-token, no environment, event `pull_request` (never `pull_request_target`), and installs no driver. Its summary states that lint is not a safety proof. Not rungs: SET NOEXEC, rolled-back rehearsal. dev and sandbox are the rehearsal.

---

## (h) Destructive changes and renames

Annotation, on the line directly above the statement or batch:
```
-- azsqlcd:allow <CODE> <object, bracketed> reason: <text, not empty>
```
Codes (closed list). Model batches (v0.2 from the parser; v0.1 from the lexer): DROP_TABLE, DROP_COLUMN, ALTER_COLUMN_LOSSY (narrowing, type change, Unicode to non-Unicode), SET_NOT_NULL, DROP_SEQUENCE, DROP_TYPE, DROP_SCHEMA, RENAME. Data batches: DATA_NO_WHERE (DELETE or UPDATE with no WHERE), TRUNCATE, DYNAMIC_SQL (`EXEC(`, `EXECUTE(`, sp_executesql). Raw batches: RAW (always). Locking: LONG_LOCK for CREATE INDEX, ADD PRIMARY KEY/UNIQUE, ADD CHECK/FOREIGN KEY WITH CHECK and type-changing ALTER COLUMN on a table that exists at base, inside a transactional migration (lint LCK001 is an error without it; the hint says "use a nontx migration with ONLINE = ON"). Chain: REPLACEMENT_EDGE. Code and object must match the statement; an allow line with no matching statement is an error. DROP INDEX and DROP CONSTRAINT are warnings.
Plan-time items with no annotation, shown in the destructive list: DROP_MODULE (tombstone), OVERWRITE_MODULE (adopted or drift-accepted module, or module that differs at first deploy).

Module drop, `schema/_tombstones.toml`, same pull request that deletes the file:
```toml
[[drop]]
object = "PROCEDURE:[sales].[usp_LegacyExport]"
reason = "replaced by usp_Export in r40"
```
Rename, end to end:
1. Developer renames in the table file and in every module file that uses the name.
2. `azsqlcd gen --base origin/main --name rename_stat --rename "column:[sales].[Order].[Stat]=[Status]"` (kinds: table, column, index, constraint). Output:
```sql
-- azsqlcd:allow RENAME [sales].[Order].[Stat] reason: aligned with the API name
EXEC sys.sp_rename N'[sales].[Order].[Stat]', N'Status', N'COLUMN';
```
   Without `--rename` the diff is DROP + ADD (DROP_COLUMN, blocked). The tool never infers a rename.
3. verify: replay applies the rename; REN001 fails if a module file still holds the old name as an identifier token.
4. Deploy: rename in the migration, changed modules in the same transaction, refresh of unchanged dependants; a broken dependant rolls back. The step row makes it run once.
Modules are never renamed with sp_rename: new file + tombstone (permissions on the old object are lost; the plan lists DROP_MODULE).

Approval, three layers:
1. Pull request: allow lines and tombstones are reviewed; CODEOWNERS makes the DBA team a required reviewer.
2. Plan: destructive list first, with row counts; `plan_sha256` output.
3. Environment reviewers approve the deploy job of that run. Deploy recomputes the plan under the lock; any change of state, artefact, target or tool gives exit 2. `azsqlcd.run` stores plan hash, actor and run URL. The approver identity is read from GitHub deployment history.
Drop rule (E5): only an object with a managed row that is absent from the files (and tombstoned, for modules) is dropped.

---

## (i) GitHub Enterprise wiring (assumes Enterprise Cloud)

Environments per database repository: `dev`, `sandbox`, `test`, `preprod`, `prod` and `dev-plan` ... `prod-plan`. All ten: deployment branch = main only. Reviewers: dev, sandbox none; test optional; preprod DBA team; prod DBA team with prevent self-review. Plan environments: no reviewers. Branch ruleset on main: pull request required, CODEOWNERS review, required check `db / verify`. `scripts/setup_repo.py --repo akaalholdings/db-sales` (owner-run, gh CLI, idempotent) creates the environments, branch policies, reviewers and the tag ruleset, then prints the `az` commands for the federated credentials; `--check` compares a repository with the policy.

Identities (user-assigned managed identities or app registrations; no Azure role). Issuer `https://token.actions.githubusercontent.com`, audience `api://AzureADTokenExchange`.
| Identity | Federated subjects |
|---|---|
| sales-nonprod-plan | `repo:akaalholdings/db-sales:environment:dev-plan`, `...:sandbox-plan`, `...:test-plan` |
| sales-prod-plan | `...:environment:preprod-plan`, `...:prod-plan` |
| sales-nonprod-deploy | `...:environment:dev`, `...:sandbox` |
| sales-test-deploy | `...:environment:test` |
| sales-preprod-deploy | `...:environment:preprod` |
| sales-prod-deploy | `...:environment:prod` |
No credential for pull_request or branch subjects. Later hardening [spike G6]: subject with job_workflow_ref.

Database principals (`azsqlcd setup-sql --env prod` prints this; an administrator runs it once per target):
```sql
CREATE USER [sales-prod-plan] FROM EXTERNAL PROVIDER;          -- contained user of its own, not group-only
GRANT VIEW DEFINITION TO [sales-prod-plan];
GRANT VIEW DATABASE STATE TO [sales-prod-plan];
GRANT SELECT ON SCHEMA::[azsqlcd] TO [sales-prod-plan];        -- after baseline

CREATE USER [sales-prod-deploy] FROM EXTERNAL PROVIDER;
ALTER ROLE [db_ddladmin] ADD MEMBER [sales-prod-deploy];
GRANT VIEW DEFINITION TO [sales-prod-deploy];
GRANT VIEW DATABASE STATE TO [sales-prod-deploy];
GRANT SELECT, INSERT, UPDATE ON SCHEMA::[azsqlcd] TO [sales-prod-deploy];   -- no DELETE
-- per schema, only where data batches or row-rewriting ALTER exist:
GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::[sales] TO [sales-prod-deploy];
```
No db_securityadmin on any pipeline identity. The minimum under db_ddladmin and the grant needed to read dependency views are spike L14. sp_getapplock and PARSEONLY need public.

Caller, `db-sales/.github/workflows/db.yml`:
```yaml
name: db
on:
  pull_request:                       # no paths filter: the required check always reports
  push:
    branches: [main]
    paths: ['schema/**', 'migrations/**', 'azsqlcd.toml']
  workflow_dispatch:
    inputs:
      release: { description: 'existing release tag, for example r57', required: true }
permissions: {}
jobs:
  verify:
    if: github.event_name == 'pull_request'
    uses: akaalholdings/azsqlcd/.github/workflows/verify.yml@v0.1.0
    permissions: { contents: read }
  release:
    if: github.event_name != 'pull_request' && github.ref == 'refs/heads/main'
    uses: akaalholdings/azsqlcd/.github/workflows/release.yml@v0.1.0
    permissions: { contents: write }
    with: { release: '${{ inputs.release }}' }
  dev:
    needs: release
    uses: akaalholdings/azsqlcd/.github/workflows/stage.yml@v0.1.0
    permissions: { contents: read, id-token: write }
    with: { environment: dev, release: '${{ needs.release.outputs.release }}', digest: '${{ needs.release.outputs.digest }}' }
  sandbox:
    needs: [release, dev]
    uses: akaalholdings/azsqlcd/.github/workflows/stage.yml@v0.1.0
    permissions: { contents: read, id-token: write }
    with: { environment: sandbox, previous: dev, release: '${{ needs.release.outputs.release }}', digest: '${{ needs.release.outputs.digest }}' }
  # test (needs sandbox), preprod (needs test), prod (needs preprod): same shape
```
Reusable `release.yml` (tool repository):
```yaml
on:
  workflow_call:
    inputs: { release: { type: string, default: '' }, runs-on: { type: string, default: '"ubuntu-latest"' } }
    outputs:
      release: { value: '${{ jobs.build.outputs.release }}' }
      digest:  { value: '${{ jobs.build.outputs.digest }}' }
jobs:
  build:
    runs-on: ${{ fromJSON(inputs.runs-on) }}
    concurrency: { group: 'azsqlcd-release-${{ github.repository }}', cancel-in-progress: false }
    outputs: { release: '${{ steps.b.outputs.release }}', digest: '${{ steps.b.outputs.digest }}' }
    steps:
      - uses: actions/checkout@<sha>
        with: { fetch-depth: 0, ref: '${{ inputs.release || github.sha }}', persist-credentials: false }
      - id: b
        uses: akaalholdings/azsqlcd@v0.1.0
        with:
          args: |
            build
            --commit
            HEAD
            --out
            dist
            --ci
            github
      - env: { GH_TOKEN: '${{ github.token }}', RELEASE: '${{ steps.b.outputs.release }}', DIGEST: '${{ steps.b.outputs.digest }}' }
        shell: bash
        run: |
          if gh release view "$RELEASE" >/dev/null 2>&1; then
            gh release download "$RELEASE" -p manifest.json -D stored
            test "$(sha256sum stored/manifest.json | cut -d' ' -f1)" = "$DIGEST"   # rebuild must match the stored release
          else
            gh release create "$RELEASE" dist/bundle.tar dist/manifest.json --target "$(git rev-parse HEAD)" --title "$RELEASE" --notes "digest $DIGEST"
          fi
```
Reusable `stage.yml`:
```yaml
on:
  workflow_call:
    inputs:
      environment: { type: string, required: true }
      previous:    { type: string, default: '' }
      release:     { type: string, required: true }
      digest:      { type: string, required: true }
      runs-on:     { type: string, default: '["self-hosted","azsql-vnet"]' }
      runs-on-light: { type: string, default: '"ubuntu-latest"' }
jobs:
  targets:                                  # no database access, no id-token
    runs-on: ${{ fromJSON(inputs.runs-on-light) }}
    permissions: { contents: read }
    outputs: { matrix: '${{ steps.t.outputs.matrix }}', timeout: '${{ steps.t.outputs.timeout }}' }
    steps:
      - &fetch
        env: { GH_TOKEN: '${{ github.token }}', RELEASE: '${{ inputs.release }}' }
        shell: bash
        run: gh release download "$RELEASE" -R "$GITHUB_REPOSITORY" -p bundle.tar -p manifest.json -D release
      - id: t
        uses: akaalholdings/azsqlcd@v0.1.0
        with:
          args: |
            targets
            --bundle
            release
            --digest
            ${{ inputs.digest }}
            --env
            ${{ inputs.environment }}
            --ci
            github
  plan:
    needs: targets
    runs-on: ${{ fromJSON(inputs.runs-on) }}
    environment: ${{ inputs.environment }}-plan
    permissions: { contents: read, id-token: write }
    strategy: { fail-fast: false, matrix: { target: '${{ fromJSON(needs.targets.outputs.matrix) }}' } }
    steps:
      - *fetch                                # written out in full in the real file
      - uses: actions/download-artifact@<sha>
        if: inputs.previous != ''
        continue-on-error: true
        with: { pattern: 'report-${{ inputs.previous }}-*', path: previous }
      - uses: azure/login@<sha>
        with: { client-id: '${{ matrix.target.plan_client_id }}', tenant-id: '${{ matrix.target.tenant_id }}', allow-no-subscriptions: true }
      - uses: akaalholdings/azsqlcd@v0.1.0
        with:
          db: true
          args: |
            plan
            --bundle
            release
            --digest
            ${{ inputs.digest }}
            --env
            ${{ inputs.environment }}
            --target
            ${{ matrix.target.id }}
            --previous-reports
            previous
            --out
            plan
            --ci
            github
      - uses: actions/upload-artifact@<sha>
        if: always()
        with: { name: 'plan-${{ inputs.environment }}-${{ matrix.target.id }}', path: plan }
  deploy:
    needs: [targets, plan]
    runs-on: ${{ fromJSON(inputs.runs-on) }}
    environment: ${{ inputs.environment }}          # approval gate; deploy identity
    permissions: { contents: read, id-token: write }
    timeout-minutes: ${{ fromJSON(needs.targets.outputs.timeout) }}
    concurrency: { group: 'azsqlcd-${{ github.repository }}-${{ inputs.environment }}-${{ matrix.target.id }}', cancel-in-progress: false }
    strategy: { fail-fast: false, matrix: { target: '${{ fromJSON(needs.targets.outputs.matrix) }}' } }
    steps:
      - *fetch
      - uses: actions/download-artifact@<sha>
        with: { name: 'plan-${{ inputs.environment }}-${{ matrix.target.id }}', path: plan }
      - uses: azure/login@<sha>
        with: { client-id: '${{ matrix.target.deploy_client_id }}', tenant-id: '${{ matrix.target.tenant_id }}', allow-no-subscriptions: true }
      - uses: akaalholdings/azsqlcd@v0.1.0
        with:
          db: true
          args: |
            deploy
            --bundle
            release
            --digest
            ${{ inputs.digest }}
            --env
            ${{ inputs.environment }}
            --target
            ${{ matrix.target.id }}
            --expect-plan-file
            plan/plan.json
            --out
            report
            --ci
            github
      - uses: actions/upload-artifact@<sha>
        if: always()
        with: { name: 'report-${{ inputs.environment }}-${{ matrix.target.id }}', path: report }
```
(YAML anchors are shorthand in this skeleton only.) `verify.yml`: one job, `contents: read`, checkout with `fetch-depth: 0` and `persist-credentials: false`, then the action with `verify --base ${{ github.event.pull_request.base.sha }}` (no `db` extra). `drift.yml`: schedule, matrix over targets, `<env>-plan` environment, `azsqlcd drift`. `resolve.yml`: workflow_dispatch with typed inputs (environment, target, action, subject, confirm_database, reason), deploy environment (approval), `azsqlcd resolve`; also runs `align`.

`timeout` from the targets job = max(env `job_timeout_minutes`, 2 x the largest nontx `expected-minutes` in the chain + 30). A job timeout must not act as a client timeout on a RESUMABLE build.
Concurrency: one group per repository, environment and target, `cancel-in-progress: false`. It saves runner time only; order is not guaranteed and an older pending run can be dropped (E10). Correctness = the applock, the chain-prefix check, the release_seq check and the plan hash.
Fan-out: one matrix leg per target, `fail-fast: false`; one failed leg fails the stage, so the next stage does not start. Each leg that names a protected environment is its own approval until spike G4 decides a pattern. No canary, no hold flag. Tested shape in the MVP: one target per environment.
Network: (1) preferred: self-hosted runner in the virtual network (label `azsql-vnet`), private endpoint, public access denied; the image holds uv, gh, az, libltdl7, libkrb5-3, libgssapi-krb5-2 and the pre-synced tool environment. (2) GitHub-hosted larger runner with Azure private networking. (3) Temporary firewall rule: not in the MVP unless the owner has no private runner; it needs an Azure role, opens every database on the server to a shared address, and can lag 5 minutes.
Enterprise Server [assumption, no evidence file]: different OIDC issuer and subject host; hosted runners may not exist (every `runs-on` is an input); `azure/login` and `astral-sh/setup-uv` must be mirrored; release assets and `gh` work against the server host.

---

## (j) Scope cut

MVP object types and operations:
- v0.1: views, procedures, scalar / inline / multi-statement functions, DML triggers: create, alter (CREATE OR ALTER), drop (tombstone), export, baseline, drift. Hand-written ordered migrations for any table-class DDL the engine accepts: lexer classification, allow lines, guards, tx and nontx modes. No table model, no table read-back, no table drift in v0.1; the plan summary says so.
- v0.2: schemas; tables (columns with type, NULL/NOT NULL, IDENTITY, named DEFAULT, computed with PERSISTED, COLLATE; named PK, UNIQUE, FK, CHECK; rowstore indexes: clustered, nonclustered, unique, INCLUDE, filter, DESC, closed option list); alias types, table types (create, drop); sequences; synonyms: create, alter, drop through proven migrations; renames (table, column, index, constraint); export, baseline, init, drift, read-back, blocker and dependant scans, unbind.
Refused loudly (lint error for files, `[unmanaged]` entry at export, exit 2 at plan): see `refused_in_mvp`.

Phases: see `mvp_steps` (P0 to P7). Tags: v0.1.0 after P3, v0.2.0 after P6.

Test strategy (D2, D10):
- No SQL Server anywhere: no container, no local engine. The fence (`EngineEdition = 5`) is in code and has a unit test.
- Unit tests use `FakeSession` (a fake, not a mock): records every batch, returns scripted rows, fails at a chosen call. Tests are named after the invariant: "no batch is sent twice"; "step rows are inserted after the last batch and before COMMIT"; "a batch error leaves no step row"; "a guard that is not (1,1) stops dispatch"; "a plan hash mismatch sends zero DDL"; "applock result -1 sends zero DDL"; "the nontx marker is committed before dispatch"; "a drop is planned only for a recorded, tombstoned object"; "a create over an unmanaged name is refused".
- Pure functions (lexer, parser, emitter, diff, replay, chain, planner, classifier) are table-driven from fixtures: base files, head files, expected statements. Property test over all fixture pairs: replay(gen(a, b), a) == b. Each destructive code has a positive and a near-miss test.
- catalog.py and errors.py are tested from catalog rows and driver error texts captured on Azure SQL in P0 and in live acceptance, committed as fixtures.
- sqlfluff is a dev-only differential oracle, only where it parses.
- Live acceptance: `uv run python scripts/live_acceptance.py --server S --database D --confirm-disposable-database D`. It refuses a database whose `azsqlcd.meta.environment` exists and is not `disposable`. Owner-run; never in CI. If tests cannot run, the report says why and gives the command `uv run pytest -q`.

Effort: see `effort`.

---

## (k) Python stack

Driver: `mssql-python==1.15.0`. `src/azsqlcd/session.py` is the only importer (lazy, so offline commands need no driver):
```python
class SqlError(Exception):
    number: int | None
    sqlstate: str | None
    message: str


class Session(Protocol):
    def execute(
        self, batch: str
    ) -> list[list[tuple]]: ...  # one batch, no parameters, every result set drained
    def close(self) -> None: ...


def connect(server: str, database: str, token: str, app_name: str) -> Session: ...
```
Why not pyodbc as default: it needs the system ODBC Driver 18 on every runner image and an MSI on the Windows workstation; its claimed advantage (native error number) is listed as not verified in the evidence. How it stays swappable: a pyodbc adapter implements the same three functions; it is written only if a P0 gate fails, and `tests/live/test_session_contract.py` is the contract any adapter must pass. Switch gates (any fail = pyodbc before runner work): G-D1 an error in statement 2..n of a batch is raised by execute() or by the nextset() drain, or is caught by the guard, in every probe case; G-D2 with ConnectRetryCount=0 a killed idle session is never silently replaced; G-D3 batch text with `?`, `{d ...}`, `{call ...}`, `{fn ...}` in literals, comments and bracket names arrives byte-identical; G-D4 connection loss during COMMIT raises; G-D5 token connect works on Windows + Python 3.14 and on the runner image. Re-run the contract test on every driver bump.

Containment of each E11 gap:
| Gap | Containment | Test |
|---|---|---|
| No engine error number | `errors.py` parses the number from message text. It selects only connect retry, exit 4 for 1222/1205, and the governance hint. Unknown text = not transient, exit 1. Nothing is replayed in any case | table tests on texts captured in P0 (208, 2714, 1222, 1205, 40613) |
| Later-statement error: execute() or nextset() | drain to the end; XACT_ABORT ON; (@@TRANCOUNT, XACT_STATE()) = (1,1) after every batch; generator emits one statement per batch; read-back before COMMIT | spike L2; FakeSession tests |
| cursor.messages absent | not used | none needed |
| No cancel | no client timeout; stop = close the connection; bounds = LOCK_TIMEOUT and the job timeout | spike L5b: close mid-transaction leaves no row |
| ConnectRetryCount=0 unverified | keyword set; `@@SPID` + `APPLOCK_MODE` asserted in the BEGIN TRANSACTION batch and before every nontx dispatch | spike L5 |
| `?` / `{...}` scanning | execute passes no parameters | spike L4 |
| Token on a long session | fresh token before the single connect; minimum life check; no reconnect-and-continue | spike L13 soak |

Auth: `azure-identity`, `AzureCliCredential` only (after `azure/login` with OIDC in CI; after `az login` on the workstation). Scope `https://database.windows.net/.default`. Token packed as `attrs_before={1256: <4-byte length + UTF-16-LE bytes>}`. No DefaultAzureCredential, no client secret, no SQL password, nothing on argv.
Python: >= 3.12. CI: 3.12 and 3.14 on ubuntu and windows. Runtime dependencies: core none (argparse, tomllib, json, hashlib, tarfile, subprocess for git); extra `db`: `mssql-python==1.15.0`, `azure-identity`. Dev: pytest, ruff, pyright, sqlfluff.
Packaging: hatchling, console script `azsqlcd`, `uv.lock` committed, `uv build` gives a wheel. No PyPI, no private index.
GitHub install: `action.yml` at the tool repository root, referenced by protected tag (`akaalholdings/azsqlcd@v0.1.0`; ruleset: tags cannot move or be deleted; repository setting Actions > Access = organisation) [spike G1]. The runner downloads the repository at the tag, so code and `uv.lock` come from the same commit.
```yaml
inputs:
  args: { required: true, description: 'one argument per line' }
  db:   { default: 'false' }
outputs:
  matrix:  { value: '${{ steps.run.outputs.matrix }}' }
  timeout: { value: '${{ steps.run.outputs.timeout }}' }
  release: { value: '${{ steps.run.outputs.release }}' }
  digest:  { value: '${{ steps.run.outputs.digest }}' }
runs:
  using: composite
  steps:
    - if: env.AZSQLCD_OFFLINE != '1'
      uses: astral-sh/setup-uv@<sha>
      with: { version: '<pinned uv version>' }
    - id: run
      shell: bash
      env:
        UV_PYTHON_DOWNLOADS: never
        AZSQLCD_ARGS: ${{ inputs.args }}
        AZSQLCD_EXTRA: ${{ inputs.db == 'true' && '--extra db' || '' }}
        AZSQLCD_NET: ${{ env.AZSQLCD_OFFLINE == '1' && '--offline' || '' }}
      run: |
        mapfile -t ARGS <<< "$AZSQLCD_ARGS"
        uv sync --project "$GITHUB_ACTION_PATH" --frozen --no-dev $AZSQLCD_EXTRA $AZSQLCD_NET
        uv run --project "$GITHUB_ACTION_PATH" --frozen --no-dev --no-sync azsqlcd "${ARGS[@]}"
```
Arguments are an array, so brackets and spaces are safe. The tool writes its outputs to `$GITHUB_OUTPUT` when `--ci github` is given. Private runner: the image holds uv and a uv cache pre-filled for the tool version, and sets `AZSQLCD_OFFLINE=1`, so a production deploy needs neither PyPI nor the setup-uv download. Hosted runners install from PyPI with the locked versions.
Workstation: `uv tool install "git+https://github.com/akaalholdings/azsqlcd@v0.1.0"` for `gen`, `verify`, `lint` (no third-party dependency is installed). For database commands: clone at the tag, `uv sync --frozen --extra db`, `uv run azsqlcd ...` (assumption: `uv tool install` does not read `uv.lock`).
Rejected: PyPI or a private index (a registry and a token for no gain); pipx (uv is the house tool); zipapp (binary wheels); PyInstaller (no cross-compile); container action (Linux only).

Commands: `lint`, `verify --base SHA`, `gen --base REF --name N [--rename K:OLD=NEW] [--resum]`, `build --commit SHA --out DIR`, `targets --bundle DIR --digest D --env E`, `plan`, `deploy --expect-plan-file F`, `drift [--export KEY --out DIR]`, `export`, `baseline [--report-only]`, `align --script F`, `init`, `resolve`, `setup-sql --env E`. Database commands take `--bundle DIR --digest D --env E --target T`; write commands outside deploy take `--confirm-database NAME`.