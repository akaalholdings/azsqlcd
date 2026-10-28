# Changelog

Changes to azsqlcd that a user of the tool can see. The newest version is first. Each change that
reaches `main` gets an entry under "Unreleased"; a release moves those entries under its version
number and date. Version numbers follow semantic versioning.

## Unreleased

### Added

- Optional table `[scope]` in `azsqlcd.toml` (`schemas`, `exclude`, `exclude_objects`): the tool
  manages only the schemas that it lists, so a database is onboarded one schema at a time. Without
  the table nothing changes. How to use it: `docs/setup.md`, section 7, "One schema at a time".
- `export --schema NAME` exports one schema, adds its captures to `onboarding/<env>/snapshot.json`
  of the repository, and prints what to add to `[scope] schemas`. `baseline --schema NAME` compares
  and records the objects of one schema that have no managed row; the other rows stay as they are.
  The workflow `onboard.yml` takes the input `schema` for both.
- Reason codes 22 `OUT_OF_SCOPE` (`export --schema`, `baseline --schema` and
  `resolve --adopt-module` outside the scope) and 22 `SCHEMA_NOT_FOUND` (`export --schema` of a
  schema that the database does not have). Lint and verify finding `OUT_OF_SCOPE`: an object file,
  or a trigger file on a table, outside `[scope]`.
- Finding `SCHEMA_UNSCOPED`: a plan note and a drift line (class `unscoped`) for a schema in
  neither list of `[scope]`. It never changes an exit code.

### Changed

- With `[scope]`, `export`, `plan` and `drift` do not read the schemas outside it and list nothing
  of them as unmanaged; a trigger follows its table. External tables are skipped by their type and
  counted. A module outside the scope is a dependant as an unmanaged module is (`[ack]`, no
  refresh, no unbind). `verify` and `gen` take the files of objects that enter the scope as they are
  (`PRF000`), and start from a model without the objects that left it, also for a withdrawal;
  `gen` writes no statement on an object outside the scope. A schema that leaves needs no
  migration and no tombstone: the deploy marks the rows of its objects `dropped` (plan steps
  `unscope:<key>`, no statement on the objects), and when the schema comes back,
  `baseline --schema` compares the objects with their files again.

## 0.2.0 - 2026-10-26

### Added

- Partitioned tables and indexes in the table model (`table_model = true`). A table, a
  `PRIMARY KEY` or `UNIQUE` constraint and an index (rowstore or columnstore) can state
  `ON [scheme]([column])`, with one partitioning column. `export` no longer leaves a partitioned
  table unmanaged with the reason "it is partitioned": it writes the table file with its `ON`
  clauses. `gen`, `verify`, `plan`, `deploy`, the read-back, `drift` and `baseline` see the place
  of the table and of each key and index. Partition functions and partition schemes are not
  managed: a DBA creates them in every environment before the release. This did not run on a
  database yet (`docs/known-gaps.md`, section 14). How to use it: `docs/setup.md`, section 7,
  "Partitioned tables".
- The file of a partitioned table writes the place of every key and index:
  `ON [scheme]([column])`, or `ON [PRIMARY]` for one that is not on a scheme (`NF000` when a
  clause is missing). The file of a table that is not partitioned did not change.
- One compression for a table or an index. A table whose partitions do not all have the same
  compression stays unmanaged at `export`, with a reason that says so.
- Reason code 22 `PARTITION_SCHEME_MISSING`: `plan` and `deploy` refuse a release that creates a
  table, a key or an index on a partition scheme that the database does not have, before any
  statement runs. Each scheme is a detail line.
- `gen` refusal `PARTITION_CHANGE` (in `GEN_REFUSED`): `gen` writes no statement that moves a
  table on to a partition scheme, off one, to another scheme or to another partitioning column,
  and no `ALTER COLUMN` of a partitioning column. A person writes that migration, and `verify`
  proves it (`docs/runbook.md`, "Partitioned tables").
- The index option `STATISTICS_INCREMENTAL = ON | OFF` for an index or a key on a partition
  scheme. Before, the parser refused it.
- `ALTER TABLE ... REBUILD PARTITION = ALL WITH (DATA_COMPRESSION = ...)` is a model statement.
  `gen` writes it for a compression change of a partitioned heap. It needs `allow LONG_LOCK` in
  a transactional migration, as `REBUILD WITH (...)` does.
- `scripts/live_tables.py` has a first pass for partitioned tables (17 checks). It makes and
  drops its own partition function and scheme. It passed on Azure SQL Database on 2026-10-26
  (`docs/live-testing.md`, section 12).

- Sign-in with a managed identity: with `AZSQLCD_AUTH=managed-identity` a database command asks
  the managed identity of the machine for the token and needs no Azure CLI.
  `AZSQLCD_MANAGED_IDENTITY_CLIENT_ID` names a user-assigned identity.
- Sign-in with SQL authentication on a workstation: `AZSQLCD_AUTH=sql`, with the login in
  `AZSQLCD_SQL_USER` and the password in `AZSQLCD_SQL_PASSWORD`. It is refused in GitHub Actions:
  when `GITHUB_ACTIONS` is `true` or `1` (any letter case), when `GITHUB_RUN_ID` is set, or when
  the command has `--ci github`. A password of fewer than 8 characters is refused. The login and
  the password are kept out of every message, log and report, also in the form that a
  connection string holds them (in braces, `}` doubled); a driver text that names `UID=` or
  `PWD=` is cut there.
- `azsqlcd.toml`: `auth = "managed-identity"` for an environment. The reusable workflows then
  skip `azure/login`, and the tool asks the managed identity of the self-hosted runner for the
  token of the plan or the deploy identity. The default `"oidc"` is the behaviour of before.
  Read the security note in `docs/setup.md`, section 2, before you set it.
- Reason codes, both exit 22: `AUTH_INVALID` (a sign-in that cannot be used) and
  `SQL_AUTH_MISSING` (the login or the password variable is not set).
- The plan output, `plan.json` (note `sign-in: <kind>`), `report.json` (key `auth`), the step
  summary and the triage log say which sign-in was used: `entra`, `managed-identity` or `sql`.
- `azsqlcd targets` writes the key `auth` into every matrix row.
- `lint`, `PAR001` (error): the parentheses of a migration batch or of a module file do not
  balance. Before, only the engine refused such text. No allow line exists for it. A migration
  whose line of `migrations/migrations.sum` says `withdrawn` is not reported. A repository that
  already holds a merged migration with this finding gets `PAR001` from `lint`, `verify` and
  `build` until that migration is withdrawn and replaced by pull request (`docs/setup.md`,
  "Withdraw and replace a merged migration"): the merged file cannot be corrected.
- `plan.json` has the key `syntax_check` (`ran`, `skipped` or `null`). It is not part of
  `plan_sha256`.
- `report.json` of a deploy has the key `syntax_check`: `ran in this run`,
  `ran in the plan job (read from plan.json; not in plan_sha256)`, `skipped` or `null`. It says
  where the fact comes from, so a plan file whose key was changed does not give a silent report.

### Fixed

- A connect that ends with a TCP timeout (`TCP Provider: Timeout error`) is retried inside the
  180-second connect budget. The first connect to a paused serverless database ends that way
  while the database resumes (live spike L12). Before, it was 24 `CONNECT_FAILED` at once. A login
  timeout and the other connect errors are not retried, as before.
- `scripts/live_spike.py`: the cleanup stopped on a statistics tree (object type `ST`) that the
  engine makes for incremental statistics. The tree goes with its table; the cleanup leaves it.
- `scripts/live_tables.py`: the `SPLIT` check split a partition that held rows. With a columnstore
  index on the scheme the engine splits an empty partition only. The function has one boundary
  more, and the check splits the empty last partition.
- `lint`, `THREE_PART_NAME`: the old form `DROP INDEX schema.table.index` was reported as a
  cross-database name when the statement was not the first statement of its batch: behind `IF`,
  in `BEGIN ... END`, in `ELSE`, in `BEGIN TRY`, or after another statement. It is now accepted
  wherever the statement stands. A name with four parts and the form
  `DROP INDEX index ON other.schema.table` are still reported.
- `lint`, `THREE_PART_NAME`: `DROP STATISTICS schema.table.statistics` is no longer reported. A
  name with four parts is still reported.
- `lint`, `THREE_PART_NAME`: two cross-database names that were not reported are now reported.
  The first is a name in a comma list of a later statement, in a batch that starts with
  `DROP INDEX`. The second is the three-part object of `DROP INDEX index ON other.schema.table`
  when a parenthesis follows it.
- `lint`, `NTX004`: an online columnstore index build behind a condition or in a block was asked
  for a low-priority wait that the engine does not have for it. It gets no finding now.
- `report.json` of `deploy --expect-plan-file` no longer warns "the syntax check (SET PARSEONLY
  ON) was skipped: this run has no second session" when the plan job ran the check. A deploy of
  a plan whose plan job did not run the check, or of a plan file without the key `syntax_check`,
  still warns, with a text that says which.
- `build` works in a bare repository when git has the setting `safe.bareRepository=explicit`.
- The onboard workflow uploads the result of `export` and `baseline-report` also when the login
  step is skipped for a managed identity.

### Changed

- The capture of every table has the new property `partition`. A table capture that an older
  version stored does not hold it. No older version could record a partitioned table, so drift,
  the plan and `baseline` read the missing property as "not partitioned": a table that was not
  partitioned gives no drift, and a heap that was moved on to a scheme behind the tool is drift
  of `partition`. The object gets the property the next time it is recorded.
- The drop of a clustered index, a clustered columnstore index or a clustered `PRIMARY KEY` or
  `UNIQUE` constraint of a table that exists needs `allow LONG_LOCK` in a transactional
  migration: the table becomes a heap, and the engine writes every row again and rebuilds every
  nonclustered index. This is also true for a table that is not partitioned. `lint` cannot see
  that an index is clustered, so `verify` asks for the line (`PRF003`) and `gen` writes it.
  `lint` accepts the line on a `DROP INDEX` or `DROP CONSTRAINT`. Before, `gen` wrote such a drop
  with no allow line, also as the first statement of the drop and create of a clustered item.
- The note of a plan with `table_model = false` says that the partition scheme check does not
  run.
- A model statement that states `ON [PRIMARY]` behind a key or an index keeps the clause. On a
  table that is not partitioned the model is the same as before.
- `lint`, `NTX004` reads each statement of a batch alone. Before, one columnstore build, one
  `ALTER COLUMN` or one `WAIT_AT_LOW_PRIORITY` anywhere in a batch switched the rule off for the
  whole batch. An online rowstore index build with no low-priority wait is now reported also when
  such a statement is in the same batch.
- `lint`, `NTX004`: `WITH (ONLINE = ON, WAIT_AT_LOW_PRIORITY (...))`, with the wait outside the
  parentheses of `ONLINE = ON`, is now reported. The accepted form is
  `WITH (ONLINE = ON (WAIT_AT_LOW_PRIORITY (...)))`.
- `lint`: `DROP INDEX a.b.c (...)` and `DROP INDEX a.b.c ON schema.table` are now reported as
  `THREE_PART_NAME`. The engine refuses both statements.
- The message of 22 `CATCHUP_REQUIRED` gives both cases: promote the earlier release when it
  can still be deployed; when it cannot, withdraw its migration by pull request, and the release
  with the withdrawal carries the later migrations. The corrected change is a replacement in the
  pull request of the withdrawal. The message says when the other way (a withdrawal with no
  replacement, then a new migration) works: no database applied the migration, and
  `table_model = true`. Detail keys are unchanged.
- Engine messages that hold only names of objects and columns keep the names. Four messages
  name objects that exist in the database and always keep them (1913, 2714, 3726, 5074), for
  example `The index 'IX_Order_Cust' is dependent on column 'CustId'.`. Five messages print a
  name as the statement wrote it (207, 208, 2705, 3701, 4902), for example
  `Invalid column name 'Status'.`. They keep the name only when the batch that was sent holds it
  as an identifier: a statement that dynamic SQL builds from data puts a row value there. The
  row of `azsqlcd.run` never keeps a name of these five. Every other quoted or parenthesised
  part is still `<redacted>`.
- `TOKEN_UNAVAILABLE`: a token and the client id of a managed identity (with or without its
  dashes) in the error text of the identity library are written as `<token>` and `<client id>`.
- The triage log hides the login and the password of SQL authentication only when the sign-in
  is `sql`. The login is hidden where it stands as a whole word.
- `[project] module_chunk` is optional in `azsqlcd.toml`. The default is 100.
- The `CONFIG_INVALID` message for a missing required key names the key, its table and one
  example line.
- With SQL authentication the token life is not applicable: it is not checked, and the plan
  shows "Token minutes left" as not applicable.
- A failed SQL login ends as 24 `CONNECT_FAILED` with the error number and the class. The driver
  message is not shown for SQL authentication.
- `GIT_FAILED`: a long error text of git is shown as its first line, ` [...] `, and its last 300
  characters.
- The live scripts (`live_spike.py`, `live_acceptance.py`, `live_tables.py`) sign in as
  `AZSQLCD_AUTH` says. With SQL authentication the token-life items are reported as
  `not applicable`.
- Live spike item L9 reports the reads of the deploy path and the read of every module (export,
  baseline) apart. Only a slow read of the deploy path makes the item `inconclusive`. The limit
  stays 10 s.

## 0.1.0 - 2026-10-07

First public version.

- Command line: `lint`, `verify`, `gen`, `build`, `targets`, `setup-sql`, `export`, `baseline`,
  `plan`, `deploy`, `drift`, `resolve`, `show-log`, `support-bundle`.
- Reusable GitHub workflows and a composite action for the promotion of one release through five
  environments, and a template for a database repository.
- What ran and what did not run before this version: `README.md`, section "Status", and
  `docs/known-gaps.md`.
