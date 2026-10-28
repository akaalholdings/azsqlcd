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
  The workflow `onboard.yml` takes the input `schema` for both. A database repository gets it when
  its callers pin this tag: the callers of the template and the example keep `schema:` as a comment
  line while they pin v0.2.0, which refuses an unknown input.
- Reason codes 22 `OUT_OF_SCOPE` (`export --schema`, `baseline --schema`, `align --schema` and
  `resolve --adopt-module` outside the scope) and 22 `SCHEMA_NOT_FOUND` (`export --schema` of a
  schema that the database does not have). Lint and verify finding `OUT_OF_SCOPE`: an object file,
  a trigger file on a table, or a `[[drop]]` of `schema/_tombstones.toml`, outside `[scope]`.
- Reason code 22 `STALE_MANAGED_ROW`: `plan` and `deploy` refuse, before any statement, a target
  that did not deploy the release in which a schema left `[scope]`, when a later release brings an
  object of that schema back with a changed file.
- Plan note `TOMBSTONE_UNMANAGED`: the release has a tombstone for an object of `[scope]` that the
  database has and that has no managed row. The deploy does not drop it.
- Finding `SCHEMA_UNSCOPED`: a plan note and a drift line (class `unscoped`) for a schema in
  neither list of `[scope]`. It never changes an exit code.
- `azsqlcd align --root DIR --env E --target T [--schema S] [--out DIR]` (A38). It is read-only
  and writes `onboarding/<env>/align.sql` and `align.md`. `align.sql` holds:
  1. `sp_rename` of each constraint with an engine-made name that a file holds in the same shape.
     A table that only the database has keeps the engine names of its constraints.
  2. The table-class statements of the gen engine (`diff.diff`, in its order).
  3. The file text of each module that differs or is missing, in deploy order.
  4. `ALTER INDEX ... REBUILD` for disabled indexes and keys (smallest table first, the clustered
     index first in its table), then `WITH CHECK CHECK CONSTRAINT` for CHECK and FOREIGN KEY
     constraints that are disabled or not trusted. Each has the row count of its table in a
     comment.

  A change that gen refuses gets no statement and is listed under "Hand-write" in `align.md` with
  its code, sorted. A file for the name of an external table or a history table goes to
  Hand-write. When only Hand-write items differ, `align.sql` is
  `-- no statement: see Hand-write in align.md`. An object that only the database has is never
  dropped; `align.md` counts these objects by kind and schema. A statement that can lose data (a
  dropped column, a narrowing type change) is written commented out after
  `-- REVIEW: loses data: <why>`, and `ALTER COLUMN ... DROP MASKED` after
  `-- REVIEW: unmasks column ...`. The tool never runs the script: a DBA reviews it and runs it,
  then `azsqlcd baseline` must pass. No new reason code; a file that cannot be written is
  `EXPORT_INCOMPLETE`. The state tables are not read or changed. Proven against the fake engine
  only.

- Opt-in parking of a migration that fails: `[project] park_failed = true` (default `false`). A
  deploy fails on a batch of migration M in an environment (21 `BATCH_FAILED`, 21
  `GOVERNANCE_LIMIT`, or 23 `NONTX_FAILED`). The deploy ends as before, with the same exit code and
  reason code. From then on M is parked in that environment. The next deploy, of the same release or
  of a later one, runs everything except M. The pending migrations of earlier releases run with the
  release in chain order (plan note `catch-up past set-aside: ...`), in place of `CATCHUP_REQUIRED`.
  A lost session, a lock timeout or a deadlock parks nothing. No state table changed: a park is the
  run row of the failed deploy. How to use it: `docs/setup.md`, section 7, "Park a migration that
  fails".
- The plan output, `plan.json`, `report.json` (`warnings`), the job summary of `plan` and `deploy`,
  and `drift` show each parked migration: `parked: <file> (run <id>, <reason code>)`. In `drift`, a
  park is reported and is never drift: the exit code does not change.
- Reason code 22 `SET_ASIDE_DEPENDENCY`. A pending migration or a module of the release is not
  independent of a parked migration, so the plan stops. The message names the migration or the
  module, the parked migration, its reason, the run, and the shared object. A raw or data batch is
  compared by the names in its text (note `independence not proven for a raw batch: <file>`).
- `resolve --unpark <file>` (RESOLVE with `-f action=unpark -f subject=<file>`) ends a park. The
  next deploy runs the migration at its place among the pending migrations. A withdrawn parked
  migration never runs, and its replacement (`replaces=`) runs as before. `resolve.yml` of this
  tag accepts `action=unpark`; `resolve.yml@v0.2.0` refuses it, so a caller needs this tag.
- Without the key, or with `park_failed = false`, every batch, output, plan, report and state write
  is the same as in 0.2.0.
- Set-aside migrations (review fixes, A41): a parked migration that creates a table, schema, type,
  sequence or synonym, or renames a table, no longer makes later deploys stop with
  `BASELINE_REQUIRED`. The independence check follows a rename: with `table_model = true` it uses
  the head model (also the tables with a key to the renamed table); without it, it uses the names of
  the rename statement. A module that names a set-aside table without its schema is refused (dbo,
  A17). `resolve --mark-applied` works for a migration that was unparked after later migrations ran
  past it. A parked migration is refused with the advice to unpark it first. A parked replacement no
  longer gets the note `late replacement: ...`.
- Products and holds (opt-in). `azsqlcd.toml` takes `[products.<name>]` with `objects` (patterns
  `schema.name`, as `[scope] exclude_objects`), `owners` (`@user` or `@org/team`) and `promote_to`
  (the last environment that the changes of the product reach). An object belongs to one product at
  most (lint error `PRODUCT_OVERLAP`).
- In an environment after `promote_to`, a pending migration that touches an object of the product is
  held: it does not run there, and later releases deploy past it, as past a parked migration
  (`SET_ASIDE_DEPENDENCY` when they are not independent of it). A module change of the product is
  not sent there. When a later release raises or removes `promote_to`, the held migrations run
  first, in chain order, and the held modules are sent.
- `plan` and `deploy` notes: `held: ...`, `products: <name> (<n> migrations, <m> modules)`, `held in
  <env>: ...`, in the plan text, `plan.json`, `report.json` and the job summary.
- `verify` and `gen` give `MIXED_HOLD` for a new migration that touches objects of a product with
  `promote_to` and other objects.
- `plan` does not ask for a baseline of the objects of a held product, and `baseline` does not
  compare or record the held objects that a database lacks.
- New offline command `azsqlcd products [--env E] [--codeowners]`: each product with its owners,
  `promote_to` and object file count; with `--env`, what the files hold back from that environment;
  with `--codeowners`, CODEOWNERS lines.
- Without `[products]` the tool behaves as before: the same batches, plan text, plan.json, plan
  hash, reports and state writes.
- Products and holds, review fixes: a product migration runs past the catch-up rule (A7) and the
  hole check only when the database could have held it (not applied, and added by a release that the
  database holds). A product with no `promote_to` changes nothing.
- A shared migration that unbinds a held module whose file text changed is refused with 22
  `SET_ASIDE_DEPENDENCY`. The held text is never sent.
- `verify`: new warning `HOLD_DEPENDENCY` (a new migration that will stop where a product is held),
  new error `HOLD_DROPPED` (a change of `[products]` that would strand a held migration with
  `CHAIN_DIVERGED`), new warning `MIXED_HOLD_MERGED` (a changed `[products]` holds a merged mixed
  migration as a whole).
- `promote_to` must name an environment of the promotion order (dev, sandbox, test, preprod, prod).
  Nothing is held in disposable.
- `azsqlcd products --codeowners` writes a space in a path as `\ `.
- `[env.<name>] not_managed` in `azsqlcd.toml`: a list of patterns `schema.name` (`*` and `?`), with
  the syntax and the checks of `[scope] exclude_objects`. In that environment the matching objects
  are outside the scope: they are not read, compared, baselined or listed as unmanaged, `drift`
  reports nothing for them, and their module files are not sent. A managed row of such an object is
  marked `dropped`.
- A pending migration that touches only objects that the environment does not manage is skipped
  there for good. The deploy records it as a migration step with the status `not_applied` and the
  note `not_managed in <env>`. It never runs there, also after the pattern is taken away. Other
  environments run it as usual.
- New reason code `MIXED_NOT_MANAGED` (22): a pending migration touches objects that the environment
  manages and objects that it does not.
- Plan note `not managed in <env>: <count> object(s); skipped migrations: ...` in the output,
  `plan.json`, `report.json` and the job summary.
- When a pattern is taken away, the table `BASELINE_REQUIRED` names `azsqlcd baseline --schema
  <name>`, also without `[scope]`.
- No change to the state tables. Without the key in an environment, the plan, the batches and the
  state writes are the same as before.
- Parking, holds and `not_managed` in one environment: each set-aside migration keeps one reason. A
  park or a skip as not managed wins over a hold, so a product migration that an environment does
  not manage never runs there, also after `promote_to` is raised. `drift` lists the parks only. The
  notes `held in <env>: ...` and `products: ...` count the held migrations only.
- New verify warning `HOLD_OUT_OF_SCOPE`: a pull request changes `[scope]`, and a migration of a
  product with `promote_to` touches an object outside it.
- `azsqlcd products --env <env>` lists apart the held items that `not_managed` takes out of that
  environment (`not managed in <env>: ...`).
- The optional input `runs-on-deploy` in `stage.yml`, `onboard.yml` and `resolve.yml`. When it
  is set, the jobs of the deploy identity (`stage.yml` deploy, `onboard.yml` baseline,
  `resolve.yml` resolve) run on it. The jobs of the plan identity stay on `runs-on`. With a managed
  identity, a runner of a plan job then does not hold the deploy identity. When it is empty, the
  deploy jobs run on `runs-on`, as before. `drift.yml` does not have this input: drift uses the plan identity
  only. A caller can give the input only after its `uses:` lines name this release or a later
  one, because 0.2.0 does not have the input. The caller workflows of the template and of the
  example have the input as a comment line. How to split the runners and the order of the steps:
  `docs/setup.md`, section 2.
- `ALTER TABLE ... REBUILD [PARTITION = ALL] WITH (DATA_COMPRESSION = ...)` accepts
  `COLUMNSTORE` and `COLUMNSTORE_ARCHIVE`, in `lint` and in the parser. The replay sets the value
  on the clustered columnstore index of the table. It blocks these values on a table that has no
  clustered columnstore index, and `NONE`, `ROW` and `PAGE` on a table that has one.

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
- `drift` reports a managed constraint that the database holds under a name that the engine made,
  for example a primary key that someone dropped and added again with no name (A35). The property
  is `constraint_name [<name of the file>] (<kind>, live name [<name in the database>])`. `drift`
  also prints one line, `rename <table key> EXEC sys.sp_rename ...;`, and the step summary has the
  same statement in a table. A plan that touches such a table stops with `DRIFT_TOUCHED` (or
  `DRIFT_BLOCK`) and prints the same lines below its message. The message and the triage log hold
  no statement. The tool finds the recorded constraint by what it is (kind, columns, target,
  expression, state and, for a key on a scheme, its place), not by a name, so two CHECK
  constraints do not swap. The rename gives the recorded name, also when the next release renames
  the constraint in its file. A database with no such constraint gives the same output as before.
  Not run on a database yet. What to do: `docs/runbook.md`, "A constraint with a name that the
  engine made".
- The read-back of a deploy (and of `mark-applied` and `accept-drift`) records an engine-named
  constraint under the name of its file, not under the fixed name of the reader (A35). The state
  tables did not change. Upgrade note: a database recorded by 0.1 or 0.2 can show new
  `constraint_name` drift, and new `DRIFT_TOUCHED` or `DRIFT_BLOCK` refusals, for constraints that
  were not visible before. There is no state-table change and no upgrade step: a DBA runs the
  rename lines.
- Transitive dependants (A36). With `table_model = true`, the plan reads the dependants of a
  release at any depth. These are the modules that use a changed table, the modules that use a
  module that the release changes or drops, and the modules that use those modules (a view on a
  view on a table). The deploy refreshes the managed, unchanged views and table-valued functions of
  that set in dependency order: a view before the views that use it. Then it checks every managed
  module of the set (21 `DEPENDANT_BROKEN`, differential as before). A release with no migration
  that changes a module does the same for the modules that use it.
- An unmanaged module or a module outside `[scope]` at any depth is a `TABLE_BLOCKER` unless
  `[ack] unmanaged_dependants` lists it. The detail line names the path. A schema-bound blocker
  names the schema-bound modules on it (`schema-bound chain: ...`).
- The plan gives a note with the path of each dependant that it found through other modules. It
  also gives a note when the search stops at 32 levels.
- `DEPENDANT_BROKEN` for a release with no migration now says "the change of a module it uses" and
  tells the operator to change the dependant in the same pull request. It no longer says to
  withdraw a migration (A37).
- A plan reads each dependant once (A37).
- A release whose dependants are all direct gives the same plan and the same SQL as before. No new
  reason code. No change to the state tables.

- `BASELINE_REQUIRED` names `azsqlcd baseline --schema <name>` also without `[scope]` in three
  cases: every object it lists has a dropped row (an object that left `[scope]` or `[env.<name>]
  not_managed` and came back); an object is in a schema that a migration skipped as not managed
  touches; the database has a baseline step and a table row, so a whole baseline is refused there
  (`ALREADY_BASELINED`).
- `gen` writes one `ALTER TABLE ... REBUILD WITH (DATA_COMPRESSION = ...)` for a change of only the
  compression of the clustered key or index, with `PARTITION = ALL` on a partitioned table and
  `allow LONG_LOCK`. Before, it wrote a drop and a create of the key or index: two writes of every
  row, and a heap between them. A foreign key on such a key is no longer dropped and added again.
  Any other change of the options of the clustered item stays a drop and a create. The output of
  two older fixture pairs changed: `index_filter_and_option_change` and
  `partitioned_index_compression_and_statistics_change`.
- No `DATA_COMPRESSION` and the value that no option gives (`NONE` on a rowstore key or index,
  `COLUMNSTORE` on a columnstore index) are one state of the model. A hand-written rebuild proves
  both. The read-back report names an option that the file states and the database does not hold
  as `options[DATA_COMPRESSION] (absent in other)`.
- A nonclustered columnstore index that is not aligned with its table is refused: `SYNTAX` in a
  table file, `PRF001` in `verify` for a migration. Aligned means the scheme and the partitioning
  column of the table, or no `ON` clause; on a table that is not partitioned, no `ON` clause. The
  engine refuses such an index, except on another scheme of the same partition function. The
  tool does not hold partition functions, so it refuses that case too.
- `export`: a table with a nonclustered columnstore index on another scheme of the same partition
  function now stays `[unmanaged]`, with the message of the model. Before, it was exported. To
  manage it, move the index to the scheme of the table.

`docs/runbook.md`, section "Partitioned tables", and `docs/known-gaps.md`, section 14, have the
details.

### Fixed

- A release that only drops a view now fails with 21 `DEPENDANT_BROKEN` when a managed view still
  uses that view (A37). Before, the deploy ended ok and left that view broken.
- `[env.<name>] not_managed`: a DML trigger follows its table. A trigger on a table that the
  environment does not manage is not sent there, and its row is marked dropped.
- `[env.<name>] not_managed`: a migration skipped as not managed no longer turns off the catch-up
  rule (`CATCHUP_REQUIRED`) in that environment.
- `[env.<name>] not_managed`: a schema whose every object of the release the patterns match is not
  asked for by plan or baseline. A migration that creates the schema and its objects is skipped
  there.
- `[env.<name>] not_managed`: the object of a raw batch directive counts as touched. The plan has a
  note for each data or raw batch that runs, and for each migration or module to send that names a
  matched object.
- `[env.<name>] not_managed` with parking: a migration skipped as not managed creates nothing. When
  the pattern is taken away, its objects are asked for with `BASELINE_REQUIRED` as before.
- A held product migration that touches objects that `[env.<name>] not_managed` matches and managed
  objects is held, not refused with `MIXED_NOT_MANAGED`. Later releases deploy past it. The refusal
  comes when `promote_to` is raised.
- A migration that runs late past set-aside migrations (a held one after `promote_to` is raised, a
  parked one after `resolve --unpark`) and touches an object outside `[scope]` is refused by the
  plan with 22 `OUT_OF_SCOPE`. Before, the deploy failed its read-back with 21
  `READBACK_MISMATCH`.
- The advice of `SET_ASIDE_DEPENDENCY` says that the set-aside migration runs at its place in the
  chain after the unpark or the raise of `promote_to`, not first.

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
