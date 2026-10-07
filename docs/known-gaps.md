# azsqlcd known gaps

Status (2026-10-07). Two kinds of live run were made on Azure SQL Database that day. The read
side (catalog queries, `export`, `lint`, `verify`, `build`, `drift --export`) ran against a copy
of WideWorldImporters: section 8. The write path ran on one disposable database through the live
scripts: the spike (19 pass, 1 inconclusive by design, 3 manual), the acceptance suite (50 of 50
on the final commit; 52 of 52 on an earlier commit, where one scenario had 2 more checks)
and the table-model script (18 of 18): section 9. Later that day `baseline`, `deploy` and
`resolve` ran through the real command line on dummy databases (section 11), and the workflows
ran on GitHub Actions up to the Azure login step (section 12). Still true: no workflow job
passed the Azure login, so no workflow reached a database; nothing ran on Windows against a
database (the unit tests pass on the Windows runners of `ci.yml`), or on Python 3.14 against a
database; the command-line runs of section 11 were made before the last fix wave and were not
repeated on the final commit, so the tree as it stands now did not run against a database as a
whole. The unit tests use fakes at the session boundary. Do not use the tool on a
production database before the open tests of section 2 pass.

Scope decision of the owner (2026-10-07): structural changes only. Data batches are switched off
by default: `[project] data_batches` of `azsqlcd.toml` is `false` unless the file says `true`, and
`lint`, `verify` and `build` then refuse every `-- azsqlcd:data` batch with the finding `DATA000`.
The rules, the tests and the review of data batches have a low priority. Every line of this file
about data batches applies only to a repository that sets `data_batches = true`.

This file is the one list of what the build does not do, did not prove, or does in another way
than the design says. Sources: the amendments of `docs/design.md`, the source, and the report of
each build task.

1. Not built.
2. Not proven on a live database, and the test that proves each item.
3. Decisions made during the build that need the owner's confirmation.
4. Known dead ends.
5. Findings of the two blueprint reviews that are still open.
6. Known limits of the GitHub wiring.
7. The review of the build (2026-10-07): what is fixed, and the limits that the fixes left.
8. First live results (2026-10-07): the read side.
9. Write-path live results (2026-10-07): spike, acceptance, table model.
10. Hardening audits and the later fix waves (2026-10-07): open findings and limits.
11. Live command-line runs and the second review (2026-10-07, afternoon).
12. GitHub Actions proof (2026-10-07).

Ids: L1 to L17, L5b and X1 to X10 are spike items of `scripts/live_spike.py`. "Acc" names a
scenario of `scripts/live_acceptance.py`. G1 to G7 are the GitHub checklist. All are in
`docs/live-testing.md`. W1 to W15 and B1 to B6 are in section 2 of this file. "No test yet" means
that no script and no checklist item covers the item.

## 1. Not built

### Cut by amendment A30

| Cut | What to do instead |
|---|---|
| `init` (create a full schema in an empty database) | New database: run `setup-sql`, then the first deploy applies the chain. An existing schema cannot be copied into an empty database by the tool |
| `align` (run an alignment script through the runner) | The tool writes `rename-constraints.sql`. A DBA reads it and runs it by hand. Other differences: refresh the environment from prod, or a DBA aligns by hand |
| Compensating migrations (`undoes=`) | Fix forward with a new migration, or withdraw and replace |
| The `stub` directive for type changes | Hand-written migration with a new type name; `gen` refuses the change (`USER_TYPE_CHANGE`) |
| Function kind change (scalar, inline, multi-statement) | New function name and a tombstone for the old one (`KND001` refuses the change of kind) |
| `generic_plan_sha256` and the compare with the previous stage | None. The plan prints the recorded commit, the release commit and a compare link (A27) |
| Actions concurrency groups as a safety control | None is used. The applock, the release order and the plan hash decide |
| Azure DevOps pipelines | Not built |

### Left out during the build

| Not built | Effect | What to do instead |
|---|---|---|
| A workflow for `drift --export` | The command exists; no workflow runs it | A DBA takes the live text of a drifted module from the database (runbook, "Drift on a touched object") |
| `report.json` of a baseline and of a `resolve` in the workflows | `onboard.yml` and `resolve.yml` pass no `--out`; only the run row and the job summary record the action | LOOK in the runbook. To keep the file: add `--out` and an upload step to the two workflows |
| Live checks for the command line, `export`, `baseline`, `drift` and the table model | `live_acceptance.py` calls the library with `table_hooks=None`; no script runs `cli.py`, an onboarding command or a query of `catalog_tables.py` on a database | None. Section 2 marks each item "no test yet" |
| Lint `SB001` (refuse `unbind`) and `ORD003` (a changed function that a CHECK or a computed column uses) | `unbind` ships without a live proof (L11). A pull request that changes such a function passes `verify`; the engine is expected to refuse the deploy in dev (L11 (b)) | The author writes the migration that drops the constraint, has `deploy-module`, and adds the constraint again |
| A state upgrade command | A state version that the tool does not read stops every command (`STATE_VERSION_UNSUPPORTED`) | Only state version 1 exists |
| `LOCK_TIMEOUT -1` for a non-transactional batch | The batch runs with `lock_timeout_ms` of the environment | Write `WAIT_AT_LOW_PRIORITY` in the statement (`NTX004` warns when it is missing) |
| A rule for a large data backfill in a non-transactional migration | A non-transactional batch that is not an index build cannot be marked not applied (section 4) | Keep data batches in transactional migrations, in sizes that fit the service limits |
| The pyodbc adapter | Written only if a driver gate fails (G-D1 to G-D5) | `docs/live-testing.md`, section 6 |
| The parser fixtures under `SET PARSEONLY ON` (blueprint step P4) | No engine check of the grammar that the parser accepts | The first deploy to dev |
| The fixtures under `tests/fixtures/live/` | `tests/unit/test_sqlerrors.py` uses message texts from the documentation | The owner's spike run writes them (X1, L7, L8) |
| `plan --connect-only` | No preflight for the network path and the database user | The first plan job |
| The list of grants that a `DROP_MODULE` removes | The plan names the module only | A DBA reads `sys.database_permissions` before the approval |
| A canary for the dependency views | An empty read of `sys.sql_expression_dependencies` is read as "no dependant" | `setup-sql` grants SELECT on the view (L14 proves the grant) |
| Lint for `INSERT` without a column list on a table whose column order differs between environments | The baseline report gives the order difference as a warning only | Review the modules that the report names |
| The merge method in `setup_repo.py` | The withdraw-and-replace proof takes the files at the parent of the first-parent commit of `main` that added the withdrawn migration (A31). The supported merge methods are not defined and not set. With a squash merge or a merge commit that parent is the state before the pull request; a rebase merge was not analysed | Use squash merges or merge commits |
| sqlfluff as a differential oracle (Part 2 (j)) | Not a dev dependency; no test uses it | None |
| A scheduled `setup_repo.py --check`, a temporary firewall rule for hosted runners, `setup_repo.py --bump` | Section 6 | Section 6 |

## 2. Not proven on a live database

How to prove: `docs/live-testing.md`. A row leaves this section when its test passes; the result
goes into the pull request that removes the row.

### Engine and driver: spike items

Proven by `scripts/live_spike.py` on a disposable Azure SQL Database that the owner provides.
L1 to L17 are the spikes of the blueprint; L5b and X1 to X10 were added by the build.

Passed on 2026-10-07 on one database and one machine (macOS arm64, Python 3.12, mssql-python
1.15.0): the items of the second table below. Section 9 has the results and the engine facts.
Three assumptions of the build were wrong and were corrected: the engine does not store module
text as it was sent (L7); the first error of a broken dependant is 207 or 208, not 2020, and it
does not end the transaction (X3); the guard outside a transaction is (0, 1, new id), not (0, 0)
(X2). The rows of the first table are still open.

| Id | Behaviour that the build assumes | Test that proves it |
|---|---|---|
| L1 | The driver connects with an access token of `AzureCliCredential` on Windows with Python 3.14 and on the runner image (passed on macOS with Python 3.12: gate G-D5 on that machine only) | `SELECT @@SPID` returns on both machines; a closed session is gone from the server; `runner.set_session_options` passes |
| L8 | The engine writes DEFAULT, CHECK, computed and filter expressions in one normal form on every database | A fixed input set on two databases gives equal text |
| L12 | The first connect to a paused serverless database succeeds inside 180 seconds | Connect to a paused database; record the error texts |
| L13 | A session outlives the expiry of its token, or ends cleanly | Hold a session 90 minutes past expiry with one statement every 5 minutes and one long statement over the expiry; record the token life right after `azure/login` |
| L14 | `db_ddladmin`, VIEW DEFINITION, VIEW DATABASE STATE and the grants of `setup-sql` are enough for deploy and plan, also for CREATE SCHEMA | Run every statement kind and every catalog read with both principals; record each missing right |
| X5 (rest) | A loss of the network after COMMIT reached the server ends as exit 23, or as 24 `CONNECTION_LOST_COMMITTED` when the locking read decides. The kill case passed; `CONNECTION_LOST_COMMITTED` was never seen live | No controlled test (`docs/live-testing.md`, section 7) |
| X3 (rest) | The three `#temp` cases: a sound procedure with a `#temp` table gets no new finding from a change that does not touch what it uses | `--items X3`; written after the live run, proven on scripted fakes only |
| X6 | `ALTER SEQUENCE ... RESTART WITH n` moves, or does not move, `sys.sequences.start_value` | `--items X6`. Decides `catalog_tables.NOT_IN_DRIFT`; until then `drift` does not report a changed `start_value` |
| X7 | `sys.numbered_procedures` and `sys.indexes` of a view give the export facts | `--items X7` |
| X8 | The session-option row of twelve values with `IMPLICIT_TRANSACTIONS` on and off | `--items X8` |
| X9 | `sys.tables.temporal_type`, `sys.periods` and the drop with versioning off, through the script. It assumes that temporal DDL is allowed inside a user transaction | `--items X9` |
| X10 | The form in which the engine stores each masking function in `sys.masked_columns` | `--items X10` |

Passed on 2026-10-07 (one database, one machine):

| Id | What the live spike showed |
|---|---|
| L2 | 9 of 9 batch shapes with a late error raise (gate G-D1) |
| L3 | 5 of 5 compile and run-time shapes inside a transaction raise |
| L4 | Batch text with `?` and `{...}` arrives byte-identical; the stored definition equals `modules.stored_text` of the batch (gate G-D3) |
| L5 | A killed idle session raises 08S01 at the next batch and is never replaced (gate G-D2) |
| L5b | A connection that is closed inside a transaction leaves no row |
| L6 | The locking read after a kill: 40 of 40 right in each of two runs; while the writer lives the read waits and ends as 1222 |
| L7 | The engine does not store the text as sent: `CREATE OR ALTER` and `ALTER` are stored as `CREATE`, every other byte as sent. The read-back with `modules.stored_text` accepts every deployed file |
| L9 | Catalog reads inside the open transaction see 500 new procedures, a table, an index and a trigger; capture of all in 0.86 s |
| L10 | `SET PARSEONLY ON` works through the driver; `SET PARSEONLY OFF; SELECT 1` in one batch runs its SELECT, so `FORBIDDEN_TOKEN` stays |
| L11 | Deferred names: procedure, trigger, scalar and multi-statement function can be created before the object they use; view and inline function cannot (208). A function that a CHECK uses cannot be changed (3729). Unbind of chained schema-bound views works dependant first |
| L15 | The session applock: `Exclusive` on the owner only, free after KILL and after close; the filtered unique index is accepted |
| L16 | The collation is readable and the case probe agrees; a failed `sp_refreshsqlmodule` in a transaction raises and rolls back |
| L17 | A killed `ONLINE = ON, RESUMABLE = ON` build leaves a row in `sys.index_resumable_operations`; the index of a PRIMARY KEY has the constraint name; ADD NOT NULL with a constant default on 300,000 rows is metadata only |
| X1 | The driver texts of 208, 2714, 2627, 245, 1222, 1205 are read; a wrong database name gives the text of 18456, not 4060 |
| X2 | Guard: (1, 1, id) in the transaction; (0, 1, new id) after a run-time error; a swallowed error raises 3998 at the end of the batch |
| X3 | `catalog.broken_references` reports a module on a dropped column (`ERROR_207`) or a dropped table (`ERROR_208`) and not a sound view; the failed read does not end the transaction |
| X4 | `CREATE USER ... WITH SID = 0x..., TYPE = E` and the four grants of `setup-sql` are accepted |
| X5 | A session that is killed while the client waits for the batch with COMMIT raises (gate G-D4) |

The message texts in `sqlerrors.py` for 207, 208, 245, 1205, 1222, 2020, 2601, 2627, 2714, 3729,
3998 and 18456 are the texts that the driver returned in the live spike
(`tests/fixtures/live/error_texts.json`). The others come from the Microsoft documentation. The
text of 4060 was not seen: a token connect to a database that does not exist gives the text of
18456 and ends at once as `CONNECT_FAILED`. The texts of 156, 8134 and 5074 are in the fixture
with no number confirmed. The governance texts (40552, 9002, 40549, 40550, 40551, 40544) have no
test: the errors cannot be made safely.

### Assumptions of the modules

A row below that names only items of the passed list above (for example "L7", "X2", "L15") was
exercised by the live spike of 2026-10-07 on one database. The rows stay until each is checked
against the output of its item and the fixtures are committed.

Every entry of the "not verified live" list of each build report, merged. "Where" names the file.

Driver and session (`session.py`, `sqlerrors.py`).

| Assumption | Proven by |
|---|---|
| Token connect through `attrs_before`, autocommit, pooling off, the drain of every result set | L1, L2 |
| The ODBC layer does not translate `{...}` escape clauses in batch text and in the capture JSON inside an `N'...'` literal (the driver does not set SQL_ATTR_NOSCAN) | L4 |
| A lost connection is recognised by its SQLSTATE label (08xxx, 40003), also during COMMIT; a killed session can first show as another class | L5, X5; Acc: session killed in the transaction |
| The first diagnostic record of a failed batch is the engine error; the driver keeps one record and cuts it at about 512 characters | X1 for single errors; no test yet for a batch with several records |
| `close()` on a dead connection raises an error that the tool can suppress | L5 |
| `AzureCliTokenProvider` with the real credential; the `az` call has the library timeout of 10 seconds | L1 |
| No login timeout is set, so a connect attempt that starts inside the 180-second budget can end after it | L12 |
| A token that expires on an open session | L13 |
| Connection values with hostile characters are safe in the braced form that `session.open_session` writes (checked against the Python parser of the driver only) | No test yet |
| A message in another language, or with a changed wording, gives no error number: class OTHER, no retry, exit 21 | X1 for the session language `us_english`; no test for a server that ignores `SET LANGUAGE` |

State, setup script and catalog of modules (`state.py`, `catalog.py`).

| Assumption | Proven by |
|---|---|
| Every statement of the two modules runs on Azure SQL Database | Acc: setup-sql, first release |
| The setup script in the client tool of the administrator (sqlcmd, SSMS, Azure Data Studio, the portal query editor): the guard `RAISERROR` + `SET NOEXEC ON` ... `SET NOEXEC OFF` | No test yet: the acceptance sends the script through the driver |
| `CREATE USER ... WITH SID, TYPE = E`; `sys.database_principals.sid` equals the bytes; the grant on `sys.sql_expression_dependencies` | X4, L14 |
| A second run of the setup script changes nothing and raises nothing | Acc: setup-sql |
| `COLLATE Latin1_General_100_BIN2` on the compare of the meta row | Acc: setup-sql |
| `ORIGINAL_LOGIN()` for a contained Entra user or a service principal (stored in `run.principal_name`) | Acc: first release, for the owner's login; a service principal: the first pipeline deploy (G2) |
| The case-sensitivity probe of the fence | L16. Measured on 2026-10-07 for a case-insensitive catalog only (section 8) |
| `sys.dm_sql_referenced_entities`: the meaning of `is_all_columns_found`, `is_incomplete` and `referenced_id` NULL | X3. Measured on a real catalog on 2026-10-07 (read only): the meaning differs from the assumption, see section 8 (RO-1, RO-2). The write cases of X3 are still open |
| Driver value types: bit, `char(2)` padding of `sys.objects.type`, CAST of `sql_variant`, `CONVERT(char(23), datetime2, 126)`, `STRING_AGG` over `sys.trigger_events`, `SCOPE_IDENTITY()` | L1, L7; Acc: first release. Read side measured on 2026-10-07: bit is `bool`, `sys.objects.type` is padded, `sql_variant` is readable without CAST (section 8). `STRING_AGG` over `sys.trigger_events` and `SCOPE_IDENTITY()` are still open |
| `HAS_PERMS_BY_NAME` for VIEW DEFINITION on a module and SELECT on a state table | L14. Measured on 2026-10-07 for `dbo` only: int 1, and 0 for a name that does not exist (section 8). The SELECT on a state table is still open |
| T-SQL line continuation (a backslash and a line break inside a literal), which `state.literal` refuses | No test yet |
| The size of one batch that holds a capture with a large definition as an `N'...'` literal | No test yet |
| Python `casefold` agrees with the catalog collation for every name (the fence refuses only a case-sensitive catalog; accent and width rules are not covered). Used for name collisions, directive targets, the unmanaged list and the keys of state rows | No test yet |

Modules (`modules.py`).

| Assumption | Proven by |
|---|---|
| The verb that the engine stores in `sys.sql_modules.definition` after CREATE OR ALTER and after ALTER | L7 |
| ALTER without SCHEMABINDING on chained schema-bound modules, dependants first; an ALTER of a schema-bound module is blocked by a schema-bound module on top of it | L11 (c) |
| `@param AS type` in a procedure header without parentheses is valid | L7 |
| A module batch with the BOM removed and CRLF kept is accepted | L7 |

Plan (`plan.py`).

| Assumption | Proven by |
|---|---|
| Under `SET PARSEONLY ON`, `SELECT 1/0` returns no result set and raises nothing; `SELECT FROM;` raises an error of the class OTHER | L10 |
| A batch that holds `SET PARSEONLY OFF` runs when it is sent under PARSEONLY ON (the reason for `FORBIDDEN_TOKEN`) | L10 |
| `SET PARSEONLY OFF`, sent under PARSEONLY ON, gives the setting back, so the session of `export` is usable after the check | No test yet |
| `APPLOCK_TEST` is 0 while another session holds the lock, for the plan identity | L15; Acc: failing batch |
| `DB_NAME()` equals the configured database name without case on every target | No test yet |
| Error 3729 on CREATE OR ALTER of an unchanged function that a CHECK or a computed column uses (the reason a `deploy-module` skips text that the database holds) | L11 (b) |
| The link shapes `<repo>/blob/<commit>/<path>#L<line>` and `<repo>/compare/<a>...<b>` on GitHub Enterprise | No test yet: read the first plan summary |
| A catch-up in one release (A7 exemption): the migrations of several releases in one transaction | No test yet |

Runner and resolve (`runner.py`).

| Assumption | Proven by |
|---|---|
| `sp_getapplock` through `DECLARE @r int; EXEC @r = ...; SELECT @r;` returns one row; `APPLOCK_MODE` is `Exclusive` on the owner | L15 |
| `IF <condition> THROW ...; BEGIN TRANSACTION; UPDATE ...` as one batch; a stable `CURRENT_TRANSACTION_ID()` for the whole transaction | X2, L2, L3 |
| `IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION` followed by state writes in one batch. Live fact (X2): a doomed transaction never reaches this batch; the engine raises 3998 and rolls back at the end of the batch that swallowed the error | Acc: batch that ends the transaction (passed) |
| Catalog reads inside the open transaction see the uncommitted DDL: `capture_modules`, `object_exists`, `dependants_of` | L9, X3 |
| The unbind rewrite of the live definition is accepted; permissions and object id stay | L7, L11 (c) |
| `EXEC sys.sp_refreshsqlmodule @name = N'[s].[n]'` and its rollback on failure | L16 |
| The DROP of a module whose table was dropped in the same transaction is skipped (A18) | L9 |
| `reconcile_by_locking_read`: a new session gets the applock only after the lost session is gone, and the locking read gives the committed counter | L6; Acc: session killed in the transaction |
| `mark-not-applied`: `sys.index_resumable_operations` has the columns `object_id` and `name` and the deploy identity can read it | L17; Acc: nontx, session lost |
| `rebind-environment` to prod: `SERVERPROPERTY('ServerName')` is the first label of the configured server name | No test yet: the acceptance binds to sandbox |
| A session survives the governance errors 9002 and 40544, so that the rollback can be proven (else the end is exit 23) | No test yet |
| State writes under a key of another letter case: `[object_key]` compares without case in the state table | No test yet |

Onboarding and drift (`onboard.py`).

| Assumption | Proven by |
|---|---|
| The export facts query: `sys.crypt_properties` for signed modules, `sys.numbered_procedures`, `sys.triggers` with `parent_class = 0`, the type codes of CLR modules, and that the plan identity sees the rows | No test yet |
| `SET PARSEONLY ON`, the two canaries, module text and `SET PARSEONLY OFF` on the one session of `export` | L10 for the setting; no test yet for the export path |
| A signed module loses its signature when it is deployed (the reason for the quarantine code `SIGNED`) | No test yet |
| The baseline write path: the frame of the runner on a real database | L5, L6, L15 for the frame; no test yet for `baseline` |
| `drift`, `drift --export`, `export` and both modes of `baseline` on a real catalog | No test yet |

Table model: parser, emitter, generator, proof (`parse.py`, `emit.py`, `diff.py`, `replay.py`, `gen.py`).

| Assumption | Proven by |
|---|---|
| The engine accepts every statement form that `emit.py` writes: `ONLINE = ON (WAIT_AT_LOW_PRIORITY (...))` on ADD CONSTRAINT and ALTER COLUMN, `MAX_DURATION`, an inline INDEX in CREATE TABLE and in a table type, `CACHE` without a size, `vector(n)` and `json` columns, bare collation names, re-spaced expression text | No test yet: the first deploy to dev |
| The engine accepts the statements of a generated migration in the fixed order, for example ADD PRIMARY KEY NONCLUSTERED before CREATE CLUSTERED INDEX on the same table | No test yet: the first deploy to dev |
| COLLATE is valid only directly after the data type; ALTER COLUMN without COLLATE resets a column collation to the database default | No test yet |
| `sp_rename ... N'OBJECT'` renames a table or a constraint and takes the new name literally; `N'INDEX'` on the index of a key renames the constraint too; the first argument is accepted as `[s].[t].[c]` and as `s.t.c` | L17 for the index name of a PRIMARY KEY; else no test yet |
| `sp_rename` of a column that a CHECK, a computed column or an index filter names is refused by the engine (15336). Replay blocks it | No test yet |
| `sp_rename` of an index or a constraint is not blocked by a schema-bound module (`gen` writes no unbind line for it) | No test yet |
| `ALTER SEQUENCE ... RESTART WITH n` changes `sys.sequences.start_value` (not known: X6 decides; `catalog_tables.NOT_IN_DRIFT` leaves `start_value` out of drift until then, the capture still records it); `CACHE` with no size leaves `cache_size` NULL | X6; no test yet for `cache_size` |
| A foreign key may reference a unique index without a filter, list the key columns in another order, and join an alias-type column to a column of its base type | No test yet |
| A one-part sequence name in a DEFAULT resolves to schema `dbo` | No test yet |
| The engine refuses a data type change of a column that has a DEFAULT. Replay does not: such a migration passes the proof and fails at the first deploy | No test yet |
| The unparenthesised DEFAULT forms that the parser accepts are valid; `ON "default"` and `ON PRIMARY` name the PRIMARY filegroup; the order of INCLUDE columns has no meaning | No test yet |
| The names in `sys.types` of the newer types (`json`, `vector`) and `timestamp` for rowversion | No test yet |
| `gen` writes `deploy-module` only for a function that the new expression names directly. A function that calls another new function needs a second line by hand; without it the deploy fails clean (exit 21) | No test yet |
| The export round-trip gate against a real catalog: only parser-made and hand-built models were used | No test yet |
| The script of `emit.emit_create_script` on an empty database (order of schemas, types, sequences, tables, indexes, foreign keys, synonyms). No command sends it, because `init` is cut; the model validation replays it in memory | No test yet |

Table catalog and hooks (`catalog_tables.py`, `tables.py`). No spike item and no acceptance
scenario reads a table through these modules. A column that does not exist fails the whole read.

| Assumption | Proven by |
|---|---|
| Every SELECT of `catalog_tables.py`, and the existence of these columns on Azure SQL Database: `sys.tables.ledger_type`, `is_node`, `is_edge`, `is_external`; `sys.table_types.is_memory_optimized`; `sys.indexes.auto_created`, `optimize_for_sequential_key`; `sys.partitions.xml_compression`; `sys.columns.is_masked`, `encryption_type`, `generated_always_type`, `is_hidden`, `is_column_set`, `is_filestream`, `xml_collection_id`; `sys.stats.user_created`, `no_recompute`; `sys.fulltext_indexes` | No test yet |
| `TYPE_ID(N'[schema].[name]')`, `SCHEMA_ID`, `COLUMNPROPERTY`, `INDEXPROPERTY` inside a VALUES row constructor; the UNION ALL of name columns with `COLLATE DATABASE_DEFAULT` in the blocker query | No test yet |
| Driver value types of the table queries: bit, tinyint, `decimal(38,0)`, `char(2)` padding, the `sql_variant` seed and increment of `sys.identity_columns` | No test yet |
| Catalog values: vector length `(max_length - 8) / 4`; `fill_factor` 0 or 100 for FILLFACTOR = 100; `data_compression` per partition; `sys.sequences.cache_size` NULL for NO CACHE and for the default size; the spelling of `sys.synonyms.base_object_name`; the rows of the backing object of a table type | No test yet |
| A persisted computed column that is declared NOT NULL when the engine already derives it gives the same catalog row | No test yet |
| `sys.sql_expression_dependencies`: `is_schema_bound_reference` and `referenced_minor_id` per column; `sys.database_principals` shows schema owners to the plan identity (a hidden owner quarantines the schema) | L14 for the grants; else no test yet |
| Which sub-objects the engine refuses on DROP COLUMN, ALTER COLUMN and `sp_rename` (the blocker scan counts hypothetical indexes; a foreign key blocks a table rename by the design rule) | No test yet |
| How the engine writes DEFAULT, CHECK, computed and filter text, which the table captures hold | L8 |

Lint (`lint.py`).

| Assumption | Proven by |
|---|---|
| `WITH NOCHECK` on ADD CHECK or FOREIGN KEY skips the row scan (the reason no LONG_LOCK is asked) | No test yet |
| ADD of a NULL column, or of a NOT NULL column with a constant default, takes no long lock | L17 records the time and the pages |
| Every keyword that the classifier matches unquoted is a reserved word, so it cannot be an unbracketed column name | No test yet |
| `CREATE INDEX ... WITH (ONLINE = ON (WAIT_AT_LOW_PRIORITY (...)))` is accepted (`NTX004` recommends it) | No test yet |

git, platforms and the command line (`release.py`, `gen.py`, `cli.py`).

| Assumption | Proven by |
|---|---|
| git behaviour was checked with git 2.50.1 on macOS and Python 3.12 only. `--end-of-options` needs git 2.24; the `-m` flag of the A31 history read is for git older than 2.31 and was not exercised | The first run of `ci.yml` (ubuntu, windows; Python 3.12, 3.14); G5. The run of `ci.yml` on `main` of 2026-10-07 passed on all four; G5 is open |
| Windows: path handling, the symbolic-link test, two tree tests that are skipped there, multiprocessing under `uv run` in the live scripts | The first run of `ci.yml`; L1 on Windows. The run of `ci.yml` on `main` of 2026-10-07 passed on `windows-latest`; L1 on Windows is open |
| The command line with a real token and a real driver: `AzureCliTokenProvider` and `session.connect` as `cli.py` calls them | The first plan job (G2); no script runs `cli.py` on a database. By hand the command line ran on 2026-10-07 with the token of the Azure CLI login (section 11); G2 is open |
| The `GITHUB_OUTPUT` delimiter form for a value with a line break, and what the runner does with a lone carriage return | W3; no value of the tool holds a line break today |
| `--approved-utc` as the workflow sends it (`...Z`); the time shape of the approvals API | W2 |
| The live scripts themselves: every T-SQL statement in them is written from the documentation. A probe that meets an error it did not plan for reports `inconclusive`: a defect of the script, not a result. They need the right to KILL, `sys.dm_exec_connections`, `EXECUTE AS USER` for a `TYPE = E` user, and READ_COMMITTED_SNAPSHOT ON | The owner's first run (`docs/live-testing.md`, section 9) |

### GitHub and Azure

The tests of this table need a scratch database repository with ten environments, two runner
groups and six identities (`docs/live-testing.md`, section 8). That setup was not made: the
scratch demo repository of section 12 has no tenant and no runner group. The workflows ran there
on 2026-10-07. They called the tool at a branch, and every job that needs Azure stopped at the
login. Section 12 says what those runs proved; the tests of this table are open.

| Id | Behaviour that the build assumes | Test that proves it |
|---|---|---|
| G1 | A private tool repository shares its action and its reusable workflows by tag, with no token | The scratch `db.yml` runs `verify` |
| G2 | A job in a called workflow that names an environment gets the subject `repo:<caller>:environment:<name>`; `azure/login` works with `allow-no-subscriptions` for an identity with no Azure role | The dev plan job logs in; the log shows the subject |
| G3 | A run from another branch that names an environment is refused by the deployment-branch rule before a token exists; a missing environment name does not create an open environment that has a credential | Dispatch `db.yml` from a branch: no job of an environment starts |
| G4 | Two matrix legs that name one protected environment: how many approvals, and in which order | Two targets in preprod. Until then one target for each environment is the tested shape |
| G5 | A dispatch builds the tag again and gets the stored digest, on Linux and on Windows; two merges one minute apart give two releases | `gh workflow run db.yml -f release=r<n>` on both runner kinds; two quick merges |
| G6 | Entra accepts a subject with `job_workflow_ref`; the claim of a reusable workflow that is called by tag is `<tool repository>/.github/workflows/<file>@refs/tags/<tag>`; the subject limits the identity to that workflow | Follow section 3 of `setup_repo.py --print-azure`; the prod deploy logs in; a job of another workflow file in environment prod does not. Blocking for prod (A29) |
| G7 | The runner image holds the libraries of the driver, gh, az and Python 3.12; `uv sync --frozen` works offline from a filled cache | A deploy on the private runner with `AZSQLCD_OFFLINE=1` and no route to PyPI |

### Wiring of this build (W1 to W15)

Assumptions of `action.yml`, the workflows and `scripts/setup_repo.py` that the unit tests cannot
prove. All on the scratch repository. The workflows ran on GitHub Actions up to the Azure login
step (section 12), so every item that needs a step after the login is open; section 12 names the
parts that ran. No actionlint or shellcheck was run: the YAML is parsed by the unit tests, and the
shell steps run there with a fake `uv`, a fake `gh` and the real `jq`.

| Id | Behaviour that the build assumes | Test that proves it |
|---|---|---|
| W1 | The pinned releases accept the inputs that the workflows pass: checkout v7 (`ref`, `fetch-depth`, `persist-credentials`), upload-artifact v7 (`name`, `path`, `if-no-files-found`, `overwrite`), download-artifact v8 (`name`, `pattern`, `path`), azure/login v3 (`client-id`, `tenant-id`, `allow-no-subscriptions`), setup-uv v10 (`version`, `python-version`, `enable-cache`). The input names were not read from the pinned commits | First run of `ci.yml` and of the scratch `db.yml`: no "Unexpected input" warning |
| W2 | `GET /repos/{repo}/actions/runs/{run_id}/approvals` with `actions: read` returns the approval of the environment of the running job, with `state`, `user.login` and `environments[].name`, when the deploy job starts. It holds no time of approval | One approved preprod deploy: `azsqlcd.run.approved_by` holds the login |
| W3 | The outputs of the action (`exit_code`, `reason_code`, `pending`) stay readable after the action step failed | Force exit 22 in a gated stage: the incident issue shows 22 and the reason code |
| W4 | The check of the verify job is named `verify / verify` | First pull request of the scratch repository (setup section 4, step 4) |
| W5 | `runs-on: ${{ fromJSON(inputs.runs-on) }}` accepts `{"group":"..."}` | The dev plan job starts on the runner group |
| W6 | A caller job grants the upper limit of the token; each called job lowers it; GitHub checks the limits before the run starts, also for a job that an `if` skips | The scratch `db.yml`, `resolve.yml` and `onboard.yml` start with no validation error |
| W7 | `gh run rerun <run-id> --failed` after exit 24: the plan artefact of the earlier attempt is downloaded, a new approval is asked, a new token is issued, the report artefact is replaced | Force a lock timeout in preprod, then re-run |
| W8 | download-artifact with a `pattern` that matches nothing succeeds; it works in a job with `permissions: {}` (the gate) | A stage with `gated: false` (no plan artefact): the record job is green |
| W9 | The job conditions work in a called workflow: `needs.gate.result == 'skipped'` with `!cancelled()`, and `contains(needs.*.result, 'failure')` | dev (not gated) deploys; a failed preprod plan opens an issue |
| W10 | The workflow token with `contents: write` creates tag `r<n>` under the tag ruleset and adds assets to a release later | First push to the scratch repository; record job of dev |
| W11 | The REST calls of `setup_repo.py`: environment PUT with `reviewers: null`, branch policies, ruleset bodies (rule `update` on tags), `bypass_actors` and `can_admins_bypass` in the answers, the OIDC subject endpoint | Apply on the scratch repository, then `--check`; compare with the settings pages |
| W12 | On a push the `inputs` context is empty: `inputs.release` is `''` and every `check-only` expression is false | First push: the release is created and dev deploys |
| W13 | A job that calls a workflow can use `strategy.matrix` and pass `matrix` values in `with` (drift in `db.yml`) | First scheduled run |
| W14 | uv: `uv sync --frozen --no-dev --no-install-project [--extra db]` runs no build backend; `UV_OFFLINE`, `UV_PYTHON=3.12` and `uv run --no-sync python -m azsqlcd` with `PYTHONPATH` work on the runner | The plan job on a hosted runner and on the private runner |
| W15 | The script tools on the runners: `gh api --jq` with `@tsv`, `awk`, `sort`, `paste`, `cmp`; `jq` on the light runner | Covered by the runs of W2, W3 and G5 |

Bypass tests of the review. None was run.

| Id | Attack | Expected result |
|---|---|---|
| B1 | Dispatch with `release=` a branch name, a commit SHA, or `r<n>` while a branch of that name exists | The release job fails before any environment job |
| B2 | A pull request adds a job with `runs-on: {group: azsql-prod}` | The job stays queued or is refused (needs the runner group workflow limit, setup section 5) |
| B3 | A pull request adds a job that names environment `prod` | Refused by the deployment-branch rule; no token |
| B4 | `gh release upload r<n> bundle.tar --clobber` between plan and deploy | The deploy stops with exit 22 `BUNDLE_INVALID` before it connects to the database |
| B5 | Move or delete the tool tag, or a tag `r<n>` | Refused by the tag ruleset |
| B6 | Delete environment `prod` and create it again with no protection | With the default subject the credential still matches: only `setup_repo.py --check` sees it. With `job_workflow_ref` the tool workflows are still the only ones that get the token |

### Assumptions of the blueprint that still stand

- GitHub Enterprise Cloud. Other products: `docs/setup.md`, section 10; not verified.
- Users, roles and permissions are out of scope. The tool never grants. A module that is replaced
  by a new name loses its grants; a DBA grants again.
- prod is the reference environment for onboarding (`onboard.REFERENCE_ENV`).
- One target for each environment is the tested shape.
- A self-hosted runner in the virtual network exists. A temporary firewall rule is not built.
- `min_token_minutes = 20` and the 180-second connect budget have no source; tune after L12, L13.
- QUOTED_IDENTIFIER is ON for all managed files; double-quoted text is an identifier.
- Effort and setup times in the blueprint are estimates. The setup time for one database
  repository was not measured.

## 3. Decisions made during the build that need the owner's confirmation

Each row is behaviour that differs from the design text, or that the design left open, and that
an operator or a developer sees. Until the owner says otherwise, the row is how the tool works.

### Amendments that are not met, or met in another way

| Amendment | As built | Where |
|---|---|---|
| A28, application name | The driver (`mssql-python` 1.15.0) reserves the keyword and always sends `MSSQL-Python`. `cli.py` builds `azsqlcd/<version> run=<run id>` and `session.open_session` leaves it out. No decision of the tool uses the name; only the audit match by application name is lost. Options: accept; switch to pyodbc; record the run id in the session context | `session.py` `open_session`; a unit test fails when the driver starts to accept the keyword |
| A16, `mark-applied` | For a non-transactional step with the status `started` or `unknown` the step becomes `ok` with no catalog read-back and no flag, also with `table_model = true`. The read-back of A16 runs only for a pending transactional migration. No capture is written for the step, so with a table model the stored capture of the table does not hold the new index until a later release reads the table back | `runner.py` `mark_applied` |
| A16, `mark-not-applied` | The tool proves absence only for a batch that is `CREATE INDEX` or `ADD CONSTRAINT ... PRIMARY KEY` or `UNIQUE` (read with the lexer). Any other batch is refused (`NOT_PROVEN_ABSENT`); there is no force flag | `runner.py` `mark_not_applied`, `_built_sub_object` |
| A16, `rebind-environment` | The value must equal `--env` (`REBIND_ENV_MISMATCH`); the command binds the database to the environment of the target | `cli.py` `_cmd_resolve`; `runner.py` `rebind_environment` |
| A7, catch-up | Exemption: a withdrawn migration W that has no step here, on a database whose recorded release is before the release that added W, switches the rule off for the pending migrations that W's release and later releases added. They run with the artefact release, in one transaction; the plan prints a note. Earlier releases are still caught up first. The task text was wider (any withdrawn W with no step); that reading would switch the rule off for good | `plan.py` `_refuse_catch_up` |
| A6, "ahead and consistent" | `ALREADY_PAST` when the recorded release is newer, or when it is equal and the database holds migrations that the release does not. An equal release with nothing extra is planned normally (no-op, or module work after a baseline, an adoption or an accepted drift) | `plan.py` `_pending_entries` |
| A9, first converge | Chunks only when no migration and no drop is pending and every module to deploy has a managed row with no source. One module with no row makes the release one transaction | `plan.py` `pending_work` |
| A12, dependants | The sweep, the refresh, the blocker scan and table drift exist only with `table_model = true`; with `false` the plan prints a note and makes none of these checks. The dependants come from `sys.sql_expression_dependencies` only. The refresh set holds direct dependants only: a view on a view is not refreshed | `plan.py` `compute_plan`; `runner.py` `_sweep_dependants`; `tables.py` `refresh_set` |
| A14, NF000 | The table of accepted differences is: spelling, terminator, option order, element order, batch order. Tokens are compared case-folded, then the exact letters of each bracketed name are checked; a bare word inside an expression is not checked for case. See "Canonical form" below | `emit.py` `token_roundtrip_differences`, `NORMAL_FORM_EQUIVALENTS` |
| A25, redaction | Best effort: a value that holds a quote followed by a space can leak a fragment, and THROW text without quotes passes unchanged. `--show-error-text` is refused by `--env prod` and with `--rebind-environment`, before a session opens; `meta.environment` is not read for it. `PARSEONLY_FAILED` stores only the redacted text, so the flag cannot show the full syntax error. `PRF002` prints generated SQL, with DEFAULT expression text from the repository files, in the verify summary | `sqlerrors.py` `redact`; `cli.py` `main`; `plan.py` `_parse_only`; `gen.py` `_prove` |
| A25, `SECRET_LITERAL` in export | A module with such a literal is quarantined (also when the literal is inside a string or a comment, which lint accepts in a file). A table-class file with such a literal stops the whole export, exit 22: a table cannot be left out without the objects that need it | `onboard.py` `_secret_literal`, `export` |
| A1, exit codes | A wrong argument, a missing plan flag of `deploy`, a `--clear-run` value that is not a number, and `--ci github` without its two variables are exit 2 (the tool did not start), not 22. A lock timeout or a deadlock in a read-only command is 22 `READ_FAILED`, although it is safe to start again. `baseline` and `resolve` can also end 21, 23, 24 and 25; the command table of Part 1 lists 0 / 22 | `cli.py` `parse`, `main`, `_read_failure`; `runner.py` `state_run` |
| A2, setup script | On a database that is bound to another environment the script creates the two users and their grants first and raises at its end; the meta row stays. The refresh flow of A16 needs this. Risk: the script, run in the wrong database of the same project and the same database name, creates deploy users there before it raises. When plan and deploy have one client id the script makes one user | `state.py` `setup_sql` |
| A3, run rows | A `resolve` row and a `baseline` row copy the release number and the commit of the last `ok` deploy (0 and forty zeros on a fresh target); they never move the recorded release. The recorded release is the highest release of an `ok` run of `deploy` or `baseline`. Reason: a baseline that recorded the release of its bundle on a target that is behind would make the catch-up `CHAIN_DIVERGED` | `runner.py` `state_run`; `state.py` `read_state` |
| A31, history read | The command is `git log -m --first-parent --diff-filter=A` | `gen.py` |

### Command line

| As built | Effect | Where |
|---|---|---|
| `export` always exports the table-class objects too, also with `table_model = false` | One failing table read fails the module export as well. Every table query is unproven live | `cli.py` `_cmd_export`; `onboard.py` `export` |
| `export --out DIR` is a repository-shaped root: `DIR/schema/**` and `DIR/onboarding/<env>/` | The example of Part 2, `--out schema/`, would give `schema/schema/` | `cli.py` `_cmd_export` |
| `drift --export KEY --out DIR` writes the one file, checks the fence, and ends with exit 0. It makes no drift report | An export of a drifted module is not exit 30 | `cli.py` `_cmd_drift` |
| `gen --base` has the default `origin/main` | The contract table shows it as required | `cli.py` `_parser` |
| `gen --name` needs `table_model = true` (`GEN_NEEDS_TABLE_MODEL`); `gen --resum` works with `false` | Hand-written chains use `--resum` after a merge collision | `cli.py` `_cmd_gen` |
| Flags beyond the contract table: `--show-error-text` on database commands, `--out` on `baseline` and `resolve`, `--force-no-readback` on `resolve`. `plan --out` and `deploy --out` are required | See `docs/design.md`, "As built" | `cli.py` `_parser` |
| `targets`: the job timeout counts every non-transactional chain entry, withdrawn ones included | A withdrawn long build still raises the timeout | `cli.py` `_largest_nontx_minutes` |
| A report file that cannot be written after a run is a warning on stderr | The exit code stays that of the run | `cli.py` `_write_after_run` |
| A second baseline is allowed only with a table model, on a state that holds no table-class row of any status. It records table rows and a second baseline step; module rows and the acknowledgement check are skipped | The switch to `table_model = true` (Part 2 (c) 8) | `onboard.py` `baseline` |
| A file and a tombstone, or two state keys, that differ only by letter case are one object | `TOMBSTONE_CONFLICT`; state writes go to the row under the key that the row has | `plan.py` `pending_work`; `runner.py` `_stored_keys` |

### Migration files and lint

| As built | Effect | Where |
|---|---|---|
| The forbidden tokens apply to `raw` batches too | A `raw` batch cannot create an unmanaged module whose body holds `RETURN`, `COMMIT`, `ROLLBACK` or `RAISERROR` | `lint.py` `classify_batch` |
| Every `ALTER COLUMN` needs `allow ALTER_COLUMN_LOSSY`, also a widening. A type-changing `ALTER COLUMN` gets no `LONG_LOCK` finding, although Part 2 (h) lists it; an `allow LONG_LOCK` above it is `ALLOW_UNUSED` | Section 4, "LONG_LOCK gap" | `lint.py` `alter_table`; `gen.py` (allow lines come from lint) |
| `LONG_LOCK` is asked for a table that the same migration did not create (Part 2 says: a table that exists at base), and for ADD CHECK or FOREIGN KEY unless `WITH NOCHECK` is written | A second migration of one pull request on a new table needs the allow line | `lint.py` `long_lock` |
| An allow line matches a statement of its batch, by code and object; it need not be the line directly above | Part 2 (h) says directly above | `lint.py` `_allow_findings` |
| `-- azsqlcd:data` and `-- azsqlcd:raw` must be the first line of their batch. The allow line goes below it, above the statement; a plain comment above it is refused. Also refused: an unknown directive, a directive that is not in column 0, a batch with no statement, a header directive given twice | `MIGRATION_INVALID` | `chain.py` `parse_migration` |
| A model batch is one statement of a closed list (`MODEL_STATEMENT`). A data batch refuses every CREATE, ALTER and DROP, temporary tables included; `SELECT ... INTO #t` is allowed | Use `SELECT ... INTO #t` for a work table | `lint.py` |
| `DATA_NO_WHERE` covers UPDATE and DELETE only. `MERGE ... WHEN NOT MATCHED BY SOURCE THEN DELETE` needs no allow line. A table variable and a temporary table are not exempt | | `lint.py` `data` |
| `THREE_PART_NAME` is a heuristic: `db.schema.fn(...)` is missed; `schema.table.column` in a data batch is flagged | Use a table alias | `lint.py` `forbidden` |
| Lint reads a secret as tokens: `PASSWORD = '<literal>'` inside a dynamic-SQL string is not found in a file | The export rule is stricter (section 3, A25) | `lint.py` `secret_findings` |
| `DAT001` checks only migrations that the pull request adds. A `deploy-module` line counts in the same batch or an earlier batch of the same file, not in another new migration | Write the line in each migration that needs it | `lint.py` `lint_change` |
| `NTX003` is an error (A8); `NTX005` (no `expected-minutes`) is an error; `TMB002` is reported by `lint` and `build` too | | `lint.py` |
| `MODULE_DUPLICATE` covers module files only | A table file and a view file with one name are not found by lint; the model or the engine refuses later | `lint.py` `_module_findings` |
| `lint` checks every migration, merged ones included | Section 4, "A new lint rule" | `lint.py` `lint_repo` |
| The chain reader is stricter than the listed grammar: a number that does not rise, `replaces=` that names no earlier withdrawn line, a second replacement of one line. A `baseline` line may appear when the base chain has no migration | `CHAIN_INVALID`, `CHN001` | `chain.py` `parse_sum`, `check_immutable` |
| `azsqlcd.toml`: every key is required except `gated`, `[unmanaged]` and `[ack]`. `gated` defaults to true for test, preprod and prod. A key name that holds `key`, `secret` or `password` (any case) is refused, also an identity name such as `monkey_deploy` | `CONFIG_INVALID` | `config.py` `load_config`, `_reject_secret_keys` |

### Modules

| As built | Effect | Where |
|---|---|---|
| The checksum normal form removes trailing white space and makes line ends LF inside multi-line string literals too | A change that is only there does not change the checksum, so the module is not deployed again | `modules.py` `checksum` |
| The text that is sent is the file text with one leading BOM removed; CRLF is kept | Part 2 says the bytes sent are the file bytes | `modules.py` `read_module` |
| Exported module files have LF line ends | A CRLF inside a string literal of a live module becomes LF when the file is deployed | `catalog.py` `capture_modules`; `onboard.py` |
| A one-part name in a module matches an object of schema `dbo` (A17). A `dbo` object named like a column, an alias or a keyword gives a false dependency edge, and in `gen` a false `deploy-module` line | Add `-- azsqlcd:ignore-dep [dbo].[name]`; remove the false line from the migration | `modules.py` `scan_references`; `gen.py` |
| A cycle of procedures and triggers is broken in name order: with a warning at deploy order, with none at drop order | | `modules.py` `deploy_order`, `drop_order` |
| A disabled trigger is exported like any other | A deploy of the file elsewhere creates it enabled | `onboard.py` `_refuse_properties` |
| A function file may leave out `AS` (`RETURNS ... BEGIN`, `RETURNS TABLE RETURN`); other kinds may not | | `modules.py` |
| A `deploy-module` directive sends its module only when the database does not hold that text | Avoids error 3729 for an unchanged function that a CHECK uses (L11) | `plan.py` `_migration_steps` |
| An `unbind` target must be a managed module of the database; a non-transactional migration with an `unbind` is `NONTX_NOT_ALONE` | `DIRECTIVE_UNRESOLVED` | `plan.py` `_migration_steps` |
| Drift is strict for a tombstoned module that is gone from the catalog when the plan runs: `DRIFT_TOUCHED`. A18 covers only absence inside the transaction | Section 4, "A managed module that was dropped by hand" | `plan.py` `compute_plan` |

### Deploy and resolve

| As built | Effect | Where |
|---|---|---|
| The session language is `us_english` for every plan and deploy session: the tool reads engine errors by their en-US text. This also fixes DATEFORMAT mdy and DATEFIRST 7 for every migration batch | A batch that changes a session option fails with 21 `SESSION_OPTIONS` before the next module step | `runner.py` `set_session_options` |
| `deploy --inline-plan` runs the syntax check too (it opens a second session); it is refused for a gated environment (`INLINE_PLAN_GATED`) | dev and sandbox get the PARSEONLY check | `runner.py` `deploy` |
| `UNTOUCHED_CHANGED` compares every managed object before and after the batches of the unit, not live against the recorded capture | A drift that existed before the run does not fail a release with a data batch | `runner.py` `_refuse_untouched_changes` |
| A governance error that ends the session is exit 23 `CONNECTION_LOST_TX`, not 21: the rollback cannot be proven on a dead session | Runbook, "Unknown run" | `runner.py` `_tx_failure` |
| Reason codes that the failure matrix does not name: `SQL_ERROR`, `CONNECTION_LOST`, `RUN_NOT_CLOSED`, `FENCE_MISMATCH`, `ROLLBACK_UNVERIFIED`. An error of a guard query on a live session is 23 `GUARD_FAILED` | Runbook rows | `runner.py` |
| A `resolve` action does not reconcile dead runs; `deploy` and `baseline` do | `clear-run` applies only to a run that a deploy already set to `unknown` | `runner.py` `state_run` |
| `clear-run` leaves the run row `unknown`; a `resolve` step with a fixed note makes the plan pass | LOOK shows the old status | `runner.py` `clear_run`; `plan.py` `clear_run_note` |
| After `RUN_NOT_CLOSED` the release is applied and not recorded until the next deploy. If the operator then jumps two releases, the A7 exemption can apply once | Runbook: promote the same release first | `runner.py` `_outside_unit` |
| `BASELINE_REQUIRED` has no "the database has user objects" condition: a chain that starts with `baseline` always needs the step | An empty database with such a chain must be baselined too | `plan.py` `pending_work` |
| A connect error that is not in the transient list is `CONNECT_FAILED` at once; a network blip at connect (08001, a login timeout) is not retried | RERUN | `session.py` `connect` |

### Table model (`table_model = true`)

| As built | Effect | Where |
|---|---|---|
| The normal-form rules NF001 to NF006 apply to the statements of a migration too, not only to object files | `ALTER COLUMN` must state `NULL` or `NOT NULL`; a constraint in ADD must have a name; `CREATE INDEX` must state `CLUSTERED` or `NONCLUSTERED` | `parse.py` `parse_statement` |
| `CREATE SEQUENCE` must state AS, START WITH, INCREMENT BY, MINVALUE, MAXVALUE, CYCLE or NO CYCLE, CACHE or NO CACHE. `NO MINVALUE` and `NO MAXVALUE` are refused. The code is `SYNTAX` | Write every clause | `parse.py` |
| File-rule errors of table-class files have the code `SYNTAX`: the path does not match the name, a second object in a file, `GO` with a count | | `parse.py` `parse_object_file` |
| `UNSUPPORTED` beyond the design list: NOT FOR REPLICATION, IF EXISTS, table DATA_COMPRESSION and XML_COMPRESSION, typed xml, HASH indexes, CLR types, CHECK or NOCHECK CONSTRAINT, ROWGUIDCOL, `sp_rename` of a type or a statistic | Such an object stays unmanaged | `parse.py` |
| An unparenthesised DEFAULT or computed expression has a closed shape (literal, name, call, NEXT VALUE FOR, a parenthesised run, joined by arithmetic operators). CASE, IS NULL and money literals need parentheses | `SYNTAX` | `parse.py` |
| One known hole in "unknown syntax is an error": an unparenthesised index filter runs to the next known clause word, so an unknown word after it stays inside the filter expression | The PARSEONLY check on the target is the backstop | `parse.py` |
| Not in the grammar: `WITH (ONLINE ...)` on DROP INDEX and DROP CONSTRAINT; REFERENCES with no column list; COLLATE anywhere but after the data type; a one-part type name | Hand-write in a `raw` batch is not possible for a managed table (`RAW001`, `RAW002`); change the statement | `parse.py` |
| `gen` refusal codes beyond `ORD001`: `IDENTITY_CHANGE` (also seed or increment), `COMPUTED_CHANGE`, `COLLATION_CHANGE`, `ALTER_COLUMN_DEPENDANTS`, `USER_TYPE_CHANGE`, `SEQUENCE_CHANGE`, `SCHEMA_OWNER_CHANGE`, `ORDER_BLOCKED`, `TEMPORAL_CHANGE`, and `ORD002` | Hand-write; the proof checks it | `diff.py` `REFUSALS`; `gen.py` |
| A changed START WITH of a sequence is refused, not written as `RESTART WITH`: that statement also sets the next value of the live sequence | Hand-write `RESTART WITH n` | `diff.py` |
| `ALTER COLUMN` with an index, constraint or computed-column dependant is refused by `gen` and blocked by the replay. One exception: a `varchar`, `nvarchar` or `varbinary` column whose type, nullability and collation stay and whose length stays or grows (not `max`) is altered in place under `UNIQUE`, `CHECK` and a rowstore index (key or `INCLUDE`). `PRIMARY KEY`, `FOREIGN KEY`, a computed column, an index filter and a columnstore index still block. The tool is stricter than the engine here: the engine accepts a longer column under a `PRIMARY KEY` that no foreign key references, a nullability change together with a longer length under `UNIQUE`, and a nonclustered columnstore index (measured live; clustered columnstore not tested) | Hand-write drop, alter, add | `diff.py`; `replay.py` |
| An unchanged foreign key is dropped and added again when the migration drops the key that it references | More statements than expected in a generated migration | `diff.py` |
| ADD of a NOT NULL column with no DEFAULT is generated as written | It fails on a table with rows at the first deploy (exit 21) | `diff.py` |
| The replay does not check: one clustered index per table, a PRIMARY KEY on a nullable column, one IDENTITY per table, sequence bounds. A column reference in an expression is matched by token, so a type or a function with the name of the column also blocks | The engine refuses at the first deploy; or the replay blocks too much | `replay.py` |
| A change of letter case only in a name is no change for the model | No statement is written | `model.py` |
| `gen` starts from the model at the merge base plus the migrations that the branch already added | A second `gen` on one branch writes only the rest | `gen.py` `generate` |
| `gen --resum` compares with the base ref itself, not the merge base, and takes up migration files that have no chain line | It works while a merge is open; it refuses when the working tree lacks a migration of the base | `gen.py` `resum` |
| The pull request that sets `table_model = true` gets no proof (`PRF000`) and may add no migration (`PRF004`) | `docs/setup.md`, section 7 | `gen.py` `_prove` |
| `RAW001` and `RAW002` run only with `table_model = true` (or in a pull request that switches it off) and only for new migrations | With `false` a `raw` batch can name a managed object | `gen.py` `_prove` |
| The verify proof stops at the first input that cannot be read | Later findings appear after the fix | `gen.py` `verify` |
| A read-back compares the columns and every sub-object of the model. It ignores live indexes, constraints and DEFAULTs that the model does not hold; an extra live column fails. Expression text is not compared | An index that a DBA added is not drift and not a read-back failure | `tables.py` `read_back` |
| A computed column: `export` writes `PERSISTED NOT NULL` for a persisted column that the engine derived as not nullable. A schema owner is compared only when the file states one | | `catalog_tables.py`; `tables.py` |
| A schema that holds only modules gets a schema file | Else a later table in it would need a CREATE SCHEMA that fails | `catalog_tables.py` `read_model` |
| Export quarantine of tables: `IDENTITY NOT FOR REPLICATION` quarantines the table (the parser refuses it; A13 does not name it). This can quarantine many tables of a migrated legacy database. Also: an object in a built-in schema other than `dbo`; an object whose schema, alias type or foreign-key target is not exported (`DEPENDS_ON_UNMANAGED`); a table whose fixed constraint name is longer than 128 characters or taken | The export report lists each with its code | `catalog_tables.py`; `tables.py` `export_tables` |
| Captured and not quarantined (A13): a disabled or not trusted CHECK or foreign key, NOT FOR REPLICATION on them, a disabled index | The export report lists them | `catalog_tables.py` |
| Fixed names for engine-named constraints: `CK_<table>_<n>` numbered by definition text, `FK_<table>_<reftable>_<n>` by columns | A later rename to another name shows as drift until `resolve --accept-drift` | `catalog_tables.py` |
| A definition that the principal cannot read stops the table read (`NO_VIEW_DEFINITION`); the table is not quarantined | | `catalog_tables.py` `_expression` |
| `NAME_COLLISION` also covers a new table, sequence, synonym, type or schema name that exists and is not recorded | | `tables.py` `sub_object_collisions` |
| The blocker scan covers unrecorded indexes, user statistics, foreign keys and schema-bound modules. An unrecorded CHECK or DEFAULT on a dropped column is not scanned | The engine refuses at deploy (exit 21, rolled back) | `tables.py` `blockers_and_dependants` |
| Unmanaged dependants are found per table, not per column. An unmanaged view on the table blocks a column change that it does not use, until an `[ack]` line names it. Accepted lines: `[s].[module] -> [s].[table]` and `[s].[module] -> [s].[table].[column]` | `TABLE_BLOCKER` | `tables.py` |
| No report of unmanaged sub-objects outside the baseline compare | A live index that the files do not hold is silent in `plan` and `drift` | `tables.py` |
| Temporal tables are in the model (design, "As built", "Temporal tables"). The history table is not an object: no file, no state row, not unmanaged. After the drop of a temporal table it stays in the database as a plain table | Later plans and `drift` list the former history table as unmanaged; a later `CREATE TABLE` with the same `HISTORY_TABLE` name meets it on the engine, and nothing checks the name before the deploy | `diff.py`; `plan.py`; `tables.py` `sub_object_collisions` |
| A table cannot be changed to temporal or back through generated SQL (`TEMPORAL_CHANGE`), and a hand-written conversion has no proof: `ADD PERIOD`, `DROP PERIOD`, `ADD` of a `GENERATED ALWAYS` column and `ALTER COLUMN ... ADD / DROP HIDDEN` are `UNSUPPORTED` | New table under a new name and a copy of the rows; or keep the table `[unmanaged]` | `parse.py`; `diff.py` |
| The rename of a period column is refused by `gen` and by the replay; the engine allows `sp_rename` there (measured) | Keep the name | `diff.py`; `replay.py` |
| The replay does not see a leftover history table. Only `gen` refuses `DROP SCHEMA` for a schema in which one stays | A hand-written OFF, `DROP TABLE`, `DROP SCHEMA` passes `verify` and fails on the engine (exit 21, rolled back) | `replay.py` |
| `SET (SYSTEM_VERSIONING = ON ...)` needs no `LONG_LOCK`, and `DATA_CONSISTENCY_CHECK` of a hand-written statement is read and not kept | With the check on, the engine reads the rows under its lock | `diff.py` `classify`; `parse.py` |
| A table whose versioning was switched off outside the tool, with the period still in place, is outside the model | `drift` and the read-back report the one property `unsupported`, not `temporal`. A table recorded as plain that was made temporal outside the tool shows drift on its two period columns only | `catalog_tables.py` |
| A retention with a singular unit (`1 YEAR`) or `HISTORY_RETENTION_PERIOD = INFINITE` in a table file fails `NF000` | Write the plural unit; write no clause for no limit | `emit.py` |

### Canonical form of a table-class file (NF000)

The form that `emit.py` writes, and that a file must have:

- Four-space indent, one table element per line, LF line ends, one line break at the end, `;` after
  each statement, `GO` alone on a line between batches, no `GO` at the end.
- Column: `[name] type [COLLATE x] [IDENTITY(seed, increment)] NULL|NOT NULL [CONSTRAINT [n] DEFAULT expr]`.
  Computed: `[name] AS expr [PERSISTED [NOT NULL]]`.
- Built-in types in lower case with `, ` between arguments: `decimal(19, 4)`, `nvarchar(max)`.
  Alias types as `[schema].[name]`. Collation names bare.
- Constraints and indexes as table elements or statements of their own, each with a name. Options
  sorted by name.

Refused as NF000: `ASC`; `ON [PRIMARY]`, `ON "default"`, `TEXTIMAGE_ON`; `ON DELETE NO ACTION` and
`ON UPDATE NO ACTION`; IDENTITY without `(seed, increment)`; PRIMARY KEY, UNIQUE, FOREIGN KEY or
CHECK written on a column; INDEX inside CREATE TABLE; another clause order inside a column or a
sequence. Eleven hand-written object-file fixtures fail NF000 by this rule (`NOT_CANONICAL` in
`tests/unit/test_roundtrip.py`). The owner decides if any of these forms should pass. No command
prints the canonical text of a file.

## 4. Known dead ends

A dead end is a state from which no command of the tool leads out. The last column is what works
today.

| Situation | Why | Way out today |
|---|---|---|
| Catch-up over a withdrawn migration, and one of the skipped releases holds a non-transactional migration | The exemption of A7 puts the skipped migrations into one release; A8 then refuses (`NONTX_NOT_ALONE`). `resolve --mark-applied` computes the same pending work and refuses the same way | None in the tool. Not tested: a pull request that withdraws the non-transactional migration and replaces it with a transactional one. The owner decides a rule |
| The first deploy to an empty database, with a chain that holds a non-transactional migration and anything else | The whole chain is one release for that database, and A8 refuses (`NONTX_NOT_ALONE`) | Make the database a copy of a database of another environment, then `resolve --rebind-environment` (runbook, "First deploy to an empty database") |
| The first deploy to an empty database, with a chain that starts with `baseline` | `BASELINE_REQUIRED`: the objects before the baseline are in no migration | A copy of another environment and `rebind-environment`, or onboard the target |
| A managed view with an index, when a release changes or unbinds the view | `ALTER VIEW` drops every index of a view, so the plan refuses (`INDEXED_VIEW`) | A DBA drops the indexes by hand, the release is deployed, the DBA creates them again; or a tombstone |
| A replacement that is merged after later migrations, on a database that never ran the withdrawn migration | It runs after the migrations that the database applied already (plan note `late replacement: ...`), so the order of added columns differs from a database that ran the chain in order | None. Add the replacement in the pull request that withdraws (`WDR005`, `CHN006` ask for it) |
| A foreign key from a managed table to an unmanaged table, with `table_model = true` | The replay needs the target in the model, and a `raw` batch is not modelled. `export` leaves such a table unmanaged (`DEPENDS_ON_UNMANAGED`) | Manage the target table too, or keep both unmanaged |
| LONG_LOCK gap: a type-changing `ALTER COLUMN` in a transactional migration | Lint asks only for `allow ALTER_COLUMN_LOSSY`. `allow LONG_LOCK` above the statement is `ALLOW_UNUSED`, so the author cannot state the lock risk | The reviewer reads every `ALTER COLUMN` as a long lock. `lint.py` owner: raise LONG_LOCK for it, or defer the code to the proof |
| A new lint rule of a later tool version fails a merged migration | `build` runs lint over every migration and refuses the release (`LINT_FAILED`). A merged migration is immutable, and a withdrawn file is still read | None in the repository. The tool must relax the rule. Read the tool release notes before an upgrade |
| A withdraw-and-replace of a migration that was merged before `table_model = true` | The model before the withdrawn migration is empty, so the proof fails closed (`WDR002`) | Fix forward with a new migration |
| A failed non-transactional migration that is not an index build | `mark-not-applied` proves absence only for an index (`NOT_PROVEN_ABSENT`) | A DBA finishes the change by hand, then `mark-applied` |
| A managed module that was dropped by hand | The tool never creates it again: its checksum is unchanged, and `accept-drift` refuses an object that is gone. A release that touches or tombstones it stops with `DRIFT_TOUCHED` | A DBA creates the module from its file at the recorded commit |
| `adopt-module` for a module that has no file in the release | The next plan stops with `MODULE_ORPHAN` | A pull request adds the file or a tombstone |
| A row of `azsqlcd.object` that was changed by hand to invalid JSON (`STATE_INVALID`) | Every command, `resolve` included, reads the state first | A DBA restores the value from a point-in-time copy |
| A database with a case-sensitive catalog collation (`FENCE_CASE_SENSITIVE`) | The tool compares names without case; there is no override | The tool cannot manage the database |
| A state version that the tool does not read (`STATE_VERSION_UNSUPPORTED`) | No upgrade command | Use the tool version that wrote the state |
| A table that differs between environments at onboarding | `baseline` refuses (`READBACK_MISMATCH`); `align` is cut | Refresh the environment from prod, or a DBA aligns it by hand |
| A change of the owner of a schema, or of an alias type or a table type that a module uses | No migration statement exists (`SCHEMA_OWNER_CHANGE`, `USER_TYPE_CHANGE`); `stub` is cut | A DBA changes the owner. A type: a new type name |
| A data type change of a column that has a DEFAULT | The proof passes; the engine refuses at the first deploy (exit 21) | Withdraw and replace: drop the DEFAULT, alter, add the DEFAULT |
| A broken lower environment while prod needs a fix | Each stage before `from_stage` must prove that it holds the release | Repair the lower environment first. There is no emergency path |
| `mark-applied` of a migration when a later migration of the release changes the same object (`READBACK_NOT_COMPUTABLE`) | The model after the migration is the model of the release only when nothing changes the object later | RESOLVE with the release that added the migration, or `--force-no-readback` after a check by hand |

## 5. Findings of the two blueprint reviews that are still open

The blueprint had two adversarial reviews. Their fixes are the amendments of `docs/design.md`.
"Tool" means the fix is an amendment that the tool modules implement, not the wiring.

### Review of pipeline and security (24 flaws, 20 missing items)

| # | Finding | State | What remains |
|---|---|---|---|
| 1 | Dispatch input not bound to `main` | Fixed, one part open | `release.yml` accepts only `^r[0-9]+$`, checks out `refs/tags/`, never creates on a dispatch; `build` refuses a commit off `main` (A23). Open: tag creation is not restricted; immutable releases are not used; `plan` does not repeat the first-parent check; test B1 |
| 2 | Self-hosted runner: persistence and shared use | Partly | `runs-on` is a required input of each stage and the template uses two runner groups. Ephemeral runners, group access and the workflow limit are requirements in `docs/setup.md`; no script checks them. A read-only tool environment and azure/login pre-cleanup are not set; ephemeral runners make both unnecessary. Test B2 |
| 3 | Ruleset and CODEOWNERS coverage | Fixed, two parts open | `setup_repo.py` sets and checks the ruleset; CODEOWNERS covers the five paths. `EXECUTE AS` in a module header is the lint warning `EXA001`, not a reviewed list. Open: environment admin bypass is a manual item; no organisation-level required workflow for `verify` |
| 4 | `align` and onboarding scripts bypass promotion | Fixed by the cut | `align` is cut; no workflow runs a hand-written script. Open: a DBA runs `rename-constraints.sql` by hand, outside the pipeline and with no run row |
| 5 | Bundle extraction and mutable release assets | Tool (A21), built | Assets stay mutable (section 6). Test B4 |
| 6 | Approver cannot see the SQL | Tool (A27), built | The approval dialog of GitHub does not show the job summary; the approver must open the run. The link shapes are not proven on GitHub Enterprise |
| 7 | OIDC subject binds names only | Partly | `--print-azure` prints the `job_workflow_ref` subjects and no default subject for prod; `--check` reports the subject form. Open: the switch is manual and not verified (G6); the subject holds names, not `repository_id`; no scheduled check; prod-plan has no reviewers |
| 8 | Tool version pinning and tool repository control | Partly | One version string, checked by a test; `tool_digest` in the plan hash (A24); no build backend on the deploy path. Open: tool repository rules are manual; references are tags, not commits; the digest depends on the line ends of the checkout |
| 9 | Manifest digest depends on the tool version | Tool (A22), built | None |
| 10 | Exit codes collide with Python, argparse and uv | Fixed | Codes 21 to 25 and 30 (A1); the action prints "tool did not start" for any other code; the runbook is keyed by reason code and a test fails when a code that the source writes as a literal has no row. A wrong argument stays exit 2 |
| 11 | Recovery cost and the two-person rule | Partly | Re-run of failed jobs is the first recovery (W7, not verified); `from_stage`; the gate skips the deploy and the approval of a no-op. Open: the team-size question for the owner; a broken lower environment blocks |
| 12 | `resolve` has no path for transactional migrations, table changes or an unknown run | Tool (A5, A16), built with differences | `resolve.yml` offers the six actions and `force_no_readback`. Section 3: `mark-applied` of a non-transactional step has no read-back; `mark-not-applied` proves an index only |
| 13 | A refresh from prod cannot be bound again | Tool (A16), built | Runbook section. The setup script on a copy that is bound to another environment makes the users and raises at its end; a unit test reads the script text, nothing ran |
| 14 | Liveness by session id | Tool (A4), built | L15 |
| 15 | Audit record is not durable | Partly | Approvers, triggering actor and run URL go to `azsqlcd.run`; `plan.json` and `report.json` of a deploy become release assets. Open: application name (A28 not met) and `approved_utc` (section 6); the deploy identity can update its run rows; no `report.json` of a baseline or a `resolve` is kept |
| 16 | Data and source text in errors, summaries and artefacts | Tool (A25), built with limits | The incident issue holds codes and links only. Section 3: redaction is best effort; `PRF002` prints generated SQL |
| 17 | Onboarding is not self-service | Partly | `setup-sql` creates the state (A2); `onboard.yml` runs export, baseline report and baseline. Open: no lint for CREATE SCHEMA in a migration, and the right of the deploy identity to create a schema is not proven (L14); `init` is cut; `setup_repo.py` does not run the Azure commands and prints no single checklist |
| 18 | Estate scale and day-two operation | Partly | Incident issues for gated stages; one version string. Open: no issue for drift; no `--bump` command; no cross-repository view of releases; setup time not measured |
| 19 | Catch-up with a non-transactional migration | Tool (A7, A8), built | Open: the dead end of section 4 (catch-up over a withdrawn migration with a non-transactional migration) |
| 20 | Data batches: procedure calls and security statements | Tool (A11), built | None |
| 21 | Concurrency group on a job that waits for approval | Fixed | No workflow has a concurrency group, also not the release job: a queued release run must never be dropped, because catch-up needs every release that adds a migration |
| 22 | Network path and connect-failure classes | Partly | `docs/setup.md` lists egress, DNS and ports (from the review, not verified). Open: `CONNECT_FAILED` covers permanent failures; no `plan --connect-only` |
| 23 | Skeleton defects in `action.yml` and `db.yml` | Fixed | Offline flag read in bash; empty lines dropped; ids checked in `config.py` and again in the workflows; line breaks refused in `resolve` and `onboard` inputs. The check name is W4 |
| 24 | Enterprise Server and data residency | Not fixed | `docs/setup.md`, section 10. The issuer is a constant in `setup_repo.py`; the workflows do not set `GH_HOST` |

Missing items of the review.

| # | Item | State |
|---|---|---|
| 1 | Runner specification | Written (`docs/setup.md`, section 5). Not enforced |
| 2 | Tool repository governance | Written (section 1). Not automated |
| 3 | Exact ruleset settings | Fixed in `setup_repo.py`. A scheduled `--check` is not built |
| 4 | First-parent check, input validation, tag creation limit | Fixed, except the tag creation limit |
| 5 | Alerting and estate view | Partly: incident issues. No paging, no list of targets that are behind |
| 6 | Durable audit | Partly: see finding 15 |
| 7 | Team-size question | Open; for the owner |
| 8 | Emergency path when a lower environment is broken | Not built |
| 9 | Break-glass rule for a run from a workstation with a human identity | Not defined. Nothing refuses it; `azsqlcd.run.principal_name` records who ran |
| 10 | Workflows for onboarding | Fixed (`onboard.yml`), without `init` |
| 11 | Bind again after a refresh | Fixed (A16, runbook) |
| 12 | Decommission of a lineage or a target | Not written |
| 13 | Tool upgrade across repositories; mixed versions | Written (`docs/setup.md`, section 9). Mixed versions inside one repository are stopped only by review |
| 14 | Stable reason codes and a runbook keyed by them; the cancelled-job case | Fixed |
| 15 | Redaction; secret scanning | Tool (A25); secret scanning is a manual step (setup section 4) |
| 16 | Organisation Actions policy; visibility of the tool repository | Written (section 1) |
| 17 | Failover-group guidance | One rule: use the listener name. Behaviour after a geo-failover is not tested |
| 18 | Test list for the bypass cases | Written (B1 to B6). Not run |
| 19 | Stage duration | Not measured |
| 20 | Approval requests that become stale | Runbook rule. Not automated |

Contradictions and unsupported claims of the review: D10 (secrets in logs) is A25; D10 (features
with no decision behind them) is the cut in section 1; D6 (paths that skip promotion or review)
is findings 1, 3 and 4; D5 is finding 24; D7 is section 1. Each unsupported claim is a row of
section 2 or 6: tag rulesets (W10, W11), the dispatch checkout (B1), the approver record (W2), the
offline flag (G7), the build backend (W14), the check name (W4), approval for each matrix leg
(G4), the grants of `setup-sql` (L14), session liveness (A4), error 4060 (`CONNECT_FAILED`; live: a wrong database name gives 18456 in one attempt, the text of 4060 was not seen), the
setup time (not measured), the token life (L13), the driver choice (L1 to L5), Enterprise Server
(finding 24), the subject of a called workflow and `allow-no-subscriptions` (G2).

### Review of data and correctness (34 flaws, 15 missing items)

Built as the amendment says, with nothing open beyond the live proof of section 2: findings 1
(A6), 3 (A5), 4 (A8), 6 (A6), 13 (A14), 15 (A15), 16 (A4), 18 (A9), 19 (A17), 21 (A18), 24 (A16),
26 (A26) and 30 (the chain checksum is taken over LF-normalised bytes; the template has
`.gitattributes`). The findings below have a part that is open.

| # | Finding | State | What remains |
|---|---|---|---|
| 2 | A data batch runs before the modules of its release | Tool (A10), built | `DAT001` checks new migrations only. The module names are not stored for a later catch-up; A7 makes a catch-up run release by release, so each release deploys its own module text |
| 5 | A catch-up plan is a combination that no stage rehearsed | Tool (A7), built with an exemption | Section 3 (A7) and the first dead end of section 4. The catch-up in one release was never run |
| 7 | No back-out path for a merged migration | Cut (A30) | `undoes=` is not built. Fix forward, or withdraw and replace |
| 8 | The model before a withdrawn migration | Tool (A31), built | The supported merge methods are not set by `setup_repo.py`. A migration merged before `table_model = true` cannot be replaced (section 4) |
| 9 | Residue lint | Tool (A19), built as warnings | `REN001`, `DRP002`, `DRP003` scan module files only; a synonym or a CHECK expression that names the old object is not scanned. The gate is A12, which exists only with `table_model = true` |
| 10 | Refresh used as the dependant check | Tool (A12), built | Only with `table_model = true`; direct dependants only; X3 not run |
| 11 | No path for an out-of-band change | Tool (A16, A20), built | `READBACK_NOT_COMPUTABLE` when a later migration changes the object again (section 4) |
| 12 | The capture misses state | Tool (A13), built | A disabled trigger is exported as a normal file; the first and last order of a trigger is captured and the trigger is quarantined at export, but `ALTER TRIGGER` effects are not proven |
| 14 | The read-back target of a segment | Tool (A8), built | One unit of work per release removes the case. It remains for `mark-applied` (finding 11) |
| 17 | `resolve` of a non-transactional step is not verified | Tool (A16), built with differences | `mark-applied` of such a step has no read-back (section 3). `WAIT_AT_LOW_PRIORITY` is a warning (`NTX004`), not an error. No `LOCK_TIMEOUT -1` for the batch |
| 20 | A type that a module uses cannot change | Cut (A30) | `stub` is not built (section 4) |
| 22 | First deploy and baseline compare at onboarding | Partly | The live text of each module that differs and the acknowledgement file are built. A write baseline reads each table back against the model, without expression text; it does not repeat the catalog-to-catalog compare. No rule that the baseline of prod must find zero differences against a fresh export |
| 23 | `init` records migrations without their data batches | Cut (A30) | `init` is not built |
| 25 | `align` runs unreviewed SQL | Cut (A30) | A DBA runs `rename-constraints.sql` by hand, with no run row |
| 27 | Driver gate G-D1 | Open | L2 has nine batch shapes, also RAISERROR after a result set. Not run |
| 28 | Function kind change | Cut (A30) | `KND001` refuses. The plan does not list the grants that a `DROP_MODULE` removes |
| 29 | Token expiry during a deploy | Partly | A lost session in a transaction is exit 23 with the run row `running`. The plan prints the token minutes left; it does not warn when the expected minutes exceed them. L13 not run |
| 31 | Lock and data-size classification | Partly | Every `ALTER COLUMN` needs an allow line. Not built: LCK001 for a data batch on a table that the same migration altered, and for ADD NOT NULL with a default that is not a literal; the LONG_LOCK gap of section 4 |
| 32 | Column order is in no compare | Partly | The baseline report warns about another column order. No lint for INSERT without a column list |
| 33 | Dependency views: permission and an empty result | Partly | `setup-sql` grants SELECT on `sys.sql_expression_dependencies` (L14). No canary view |
| 34 | The switch from v0.1 to v0.2 | Partly | A second baseline records the tables and refuses a table that differs. No check that the applied list of a target equals the chain when the table files were exported: do the switch when no migration is pending anywhere (`docs/setup.md`, section 7) |

Missing items of the review that are still open: 2 (`undoes=`, cut); 9 (a supported form for a
large data backfill, section 1); 10 (`init`, cut); 11 (a state upgrade command, section 1); 12
(the grants that a `DROP_MODULE` removes, section 1); 13 (spike items: `ALTER TRIGGER` and the
disabled flag, `sp_rename` of a column that an expression names, the lock timeout in the last
phase of an online build have no test yet; the guard id is X2); 15 (the merge method, section 1).
The other missing items are built with the amendment that the flaw of the same subject names.

## 6. Known limits of the GitHub wiring

| Limit | Effect | Way out |
|---|---|---|
| A tag `r<n>` can be created by anyone with write access; only update and deletion are restricted | A hand-made tag on another commit blocks the release job of that number and the promotion of that release by dispatch. No unreviewed SQL is released: `build` refuses a commit that is not on `main`, and the name must equal the release number of the commit | Runbook, "A release is missing". A creation limit needs a ruleset bypass for the Actions app (not verified) |
| Release assets can be replaced by anyone with write access. Immutable releases are not used, because the record job adds `plan.json` and `report.json` later | A replaced asset stops every job with exit 22; it cannot run. Promotion of that release is blocked until the asset is restored | Runbook, `DIGEST_MISMATCH` |
| A deploy in a gated environment that holds no approval record is not refused | If the reviewers of preprod or prod are removed, a deploy runs with `approved_by` NULL | `setup_repo.py --check` sees the missing reviewers. A scheduled check is not built |
| `approved_utc` is the time at which the deploy job read the approval, not the time of the approval | The value is late by the start time of the runner | Azure SQL auditing and the GitHub audit log hold exact times |
| The application name of the session is `MSSQL-Python`: the driver does not let the tool set `azsqlcd/<version> run=<run id>` (A28) | Azure SQL auditing cannot be matched to a run by application name | Match by principal name and `azsqlcd.run.started_utc` |
| test is gated (plan job, then the deploy of that plan) but the policy gives it no reviewers | No human approves a test deploy. Incident issues are opened for test | Add reviewers to test in the repository settings; `setup_repo.py` then reports a difference, so change `REVIEWED` in the script too |
| A stage before `from_stage` must pass its check | A broken lower environment blocks a prod fix. There is no emergency path that skips a stage | Repair the lower environment first (runbook) |
| The gate reads `pending` from the plan. A deploy that only records a newer release number (A6) counts as pending | A gated environment asks for an approval of a deploy that sends no batch | Approve it: the run row is the record of the release |
| A rejected approval fails the deploy job, so the incident issue "no tool report" is opened for it | One issue that reports no fault | Close the issue |
| No incident issue for drift, for a cancelled job, or for dev and sandbox | A failed scheduled drift run notifies only the person who last changed the workflow | Watch the workflow; route the notification |
| The drift report builds its bundle from the head of `main`, not from a release | A target that is in `azsqlcd.toml` on `main` and not yet set up fails the drift job with `STATE_MISSING` | Expected until the target is onboarded |
| `drift --export` has no workflow; `baseline` and `resolve` keep no `report.json` | Section 1 | Section 1 |
| During onboarding the stages of each release fail until the target is set up and baselined (`STATE_MISSING`, `BASELINE_REQUIRED`) | Incident issues are opened for test, preprod and prod | Close them after the baseline |
| `resolve.yml` and `onboard.yml` of a database repository grant `contents: write` to their release job | The grant is the upper limit that `release.yml` declares for a push. On a dispatch only the job with `contents: read` runs | None needed; a separate verify-only workflow file would remove the grant |
| The stage workflows validate `gated` against `azsqlcd.toml` with `jq` | The light runner needs `jq` | Hosted runners have it |
| The action builds the argument array with a `read` loop, not with `mapfile` | Same result. It also runs on bash 3.2, so the unit tests run the real script | None needed |
| Every connect failure is exit 24 `CONNECT_FAILED`, also a permanent one (missing database user, no network path) | "Start again" does not help for those | Runbook row `CONNECT_FAILED` tells how to read the text |
| `setup_repo.py` prints the Azure commands; it does not run them. It does not set the subject form and the admin bypass | Three manual steps for each database repository | `docs/setup.md`, sections 2 and 4 |
| `setup_repo.py` holds the policy of a database repository only | The rules of the tool repository are set by hand | `docs/setup.md`, section 1 |
| An enterprise policy "actions must be pinned by commit" refuses the tool references, which are tags | The workflows do not start | Not supported in this build |

## 7. The review of the build (2026-10-07)

Six reviewers attacked the build by execution: lint bypass (LB), runner safety (RS), onboarding
and modules (OM), SQL text (SQL), parser and proof (PP), test quality (TQ). The reviewer of the
command line and the workflows was lost; that review was done in fix wave 2 (the rows "CLI" and
"WF" below). Two fix waves followed. This section was written during wave 2: for a finding that
another fixer of wave 2 holds, the status says so and is not a claim that it is fixed.

### Findings and their status

| Id | Severity | Finding | Status |
|---|---|---|---|
| LB-01 | critical | `build` accepts a commit that is not on main when a tag named `origin/main` exists | Fixed, wave 1 (`MAIN_REF_INVALID`; the ref is read with `git show-ref --verify`) |
| LB-02 | critical | `DELETE` or `UPDATE` with no `WHERE` passes with no allow line when the token before it is `ON` | Fixed, wave 1 |
| LB-03 | critical | A procedure call without `EXEC` in a data batch needs no `EXEC_PROC` allow line | Fixed, wave 1 |
| LB-04 | major | The lexer splits `1eKEYWORD` as number + identifier | Fixed, wave 1 (a number glued to a word is a lexer error) |
| LB-05 | major | `SET` with a comma list hides `XACT_ABORT` and `NOEXEC` from the forbidden-token rule | Fixed, wave 1 |
| LB-06, PP-3 | major | `verify` passes a pull request that sets `table_model = false` | Fixed, wave 1 (`PRF005`) |
| LB-07, PP-1 | major, critical | The proof passes when a merged migration is withdrawn with no replacement | Fixed, wave 1 (`WDR004`, `WDR005`); wave 2 adds the chain rule `CHN006` for `table_model = false` |
| LB-08 | major | NUL and other control characters inside comments pass lint | Fixed, wave 1 (lexer and lint) |
| LB-09 | minor | A symbolic link at `schema/` is followed by `verify` and dropped by `build` | Fixed, wave 1 (`TREE_INVALID` in `build` and in `verify`) |
| LB-10 | minor | `THREE_PART_NAME` misses `INSERT INTO db.schema.table (columns)` | Fixed, wave 1 |
| LB-11 | minor | `SELECT ... INTO` a permanent table passes when a column alias is `OUTPUT` | Fixed, wave 1 |
| LB-12 | minor | `read_bundle` accepts a manifest with duplicate paths, a commit that is not 40 hex, a negative `release_seq` | Fixed, wave 1 |
| LB-13 | minor | `TMB001` is matched by file path | Fixed, wave 1. `TMB002` still matches by path (limit below) |
| OM-01 | critical | Indexed views are exported and deployed as plain modules; `ALTER VIEW` drops their indexes | Fixed, wave 1 (`INDEXED_VIEW`, exit 22 at plan and exit 21 in the unit; export quarantines the view) |
| OM-02, SQL-1 | major | The A12 sweep never checks managed dependants of a dropped or renamed table | Fixed, wave 1 (dependants are read at plan time and sealed in the plan hash). Changed again in wave 2 after the live result RO-2: section 8 |
| OM-03 | major | Read-back ignores live index and key options that the file does not state | Fixed, wave 1 |
| OM-04 | major | A table file with `COLLATE` equal to the database collation always fails read-back | Fixed, wave 1 |
| OM-05 | major | Baseline records `equal` for a module stored with `ANSI_NULLS` or `QUOTED_IDENTIFIER` off, and for a disabled trigger | Fixed, wave 1 |
| OM-06 | major | Engine-named constraints pass a write baseline | Fixed, wave 1 (`CONSTRAINT_NAMES`); wave 2: the command line writes `onboarding/<env>/rename-constraints.sql` on that refusal |
| OM-07 | minor | The export secret rule lets hard-coded credentials through | Fixed, wave 1 (one wide rule, `lint.secret_in_text`) |
| OM-08 | minor | Export writes module files that lint and build reject | Fixed, wave 1 (export quarantine code `LINT`) |
| OM-09 | minor | `export_tables` drops a table when two objects have one file path | Fixed, wave 1 |
| OM-10 | minor | The fixed-name clash check sees only objects of the model | Fixed, wave 1, for `export` (limit below) |
| OM-11 | minor | Export reads the tables after the PARSEONLY check with no proof that the setting is off | Fixed, wave 1 (the tables are read before) |
| RS-1 | major | An older release redeploys older module text over a newer release that committed and did not end ok | Fixed, wave 1 (such a release counts as "ahead": `ALREADY_PAST`) |
| RS-2 | major | A replacement merged after a later migration makes databases that skipped the withdrawn migration `CHAIN_DIVERGED` | Fixed, wave 1 (plan note `late replacement: ...`; limit below) |
| RS-3 | major | A non-SqlError in the last state write leaves `deploy()`; the command line reports exit 22 "Nothing was executed" | Fixed, wave 1 (runner) and wave 2 (command line: exit 23 `TOOL_DEFECT_AFTER_DISPATCH` for any error that the runner does not report after a session was asked for) |
| RS-4 | major | `mark-not-applied` proves absence for a raw nontx batch that only starts with `CREATE INDEX` | Fixed, wave 1 |
| RS-5 | major | A7 refuses the first deploy to an empty database | Fixed, wave 1 (plan note `first deploy: ...`; limits below; runbook "First deploy to an empty database") |
| RS-6 | minor | The exit-23 message for a lost nontx session names `--clear-run`, which is then refused | Fixed, wave 1 |
| RS-7 | minor | Any resolve action whose reason text is `clear-run N` clears unknown run N | Fixed, wave 1 (step note `<action>: <reason>`) |
| RS-8 | minor | `pre_broken` is not in `plan_sha256`; the job summary does not list it | Fixed, wave 1 (hash) and wave 2 (the summary lists each dependant with its findings before the change) |
| RS-9, SQL-5 | minor, major | `SET IMPLICIT_TRANSACTIONS ON` leaves the last run-status write uncommitted; exit 0 | Fixed, wave 1 (lint refuses the `SET`; the runner checks `@@OPTIONS & 2` and `@@TRANCOUNT`; limits below) |
| SQL-2 | minor | `NTX004` asks for `WAIT_AT_LOW_PRIORITY` on an online `ALTER COLUMN` | Fixed, wave 1 |
| SQL-3 | minor | `export_facts` reads `sys.numbered_procedures` in the main batch | Fixed, wave 1. Live: the view exists on Azure SQL Database (section 8) |
| SQL-4 | minor | `RESUMABLE = ON` passes lint in a transactional migration | Fixed, wave 1 (`NTX006`; the parser refuses the invalid option combinations; limit below for `emit.py`) |
| PP-2 | major | `RAW001` reads only the object of the `raw` directive | Fixed, wave 2 (`RAW002`: the text of a raw batch may not name a managed table-class object or module). Limits: section 10 |
| PP-4 | major | Replay accepts `CREATE SCHEMA` for built-in schemas | Wave 1 report: `MDL001` / `PRF001` for `dbo`, `sys`, `guest`, `INFORMATION_SCHEMA`, `db_*` and `azsqlcd` |
| PP-5 to PP-8 | minor | Lexer `AssertionError` on digit-like characters; a droppable key behind a foreign key; `gen --resum` and a BOM; a rename that a later statement removes | Fixed, wave 2. The droppable key: the proof refuses the drop of a key on the columns of a foreign key (stricter than the engine: section 10). The rename: `PRF006`. `gen --resum` and a BOM: see N4-07 in section 10 |
| TQ-01, TQ-02, TQ-04 to TQ-07, TQ-10, TQ-11 | major, minor | Missing tests in lint, runner, plan, catalog and state queries, release, parser | Wave 2, other fixers; not known to this document |
| TQ-03 | major | No test checks what the approver sees | Fixed, wave 2: tests pin the banner, the order, each destructive row, drift, table facts, notes, dependants, the compare link and the step links |
| TQ-08 | minor | Workflow tests: plan job identity, `--digest`, release name check, `fetch-depth` | Fixed, wave 2 (tests; the four mutants of the review are killed) |
| TQ-09 | minor | `--report-only` confirm, fence of drift, `min_token_minutes` for read-only commands, `GITHUB_TRIGGERING_ACTOR` | Fixed, wave 2 (tests, also the two of `onboard.py`) |
| CLI-1 | major | A printed line could be a workflow command: the runner reads a stdout or stderr line that starts with `::`, or holds `##[`, as a command, and a path or an object name can hold a line break | Fixed, wave 2: no printed line has that form |
| CLI-2 | major | The reason of an allow line, an object name or an engine message went into the job summary as Markdown: `<!--` hid the rows after it, `<redacted>` was not shown at all | Fixed, wave 2: every value of the summary is escaped. Limit: `report_md` of a baseline is written by `onboard.py` and is not escaped by the command line |
| CLI-3 | minor | A closed stdout, a character that the stream cannot encode, or a detail that JSON cannot write turned the end of a run into exit 22 `TOOL_DEFECT` or a traceback, also after a committed deploy; an error in the report of a recorded baseline did the same | Fixed, wave 2: printing never raises; the exit code and the CI outputs are always written. Wave 3 (N4-02): both streams escape what their encoding cannot write, each line is flushed, and a closed pipe no longer makes the exit code 120 when the interpreter ends |
| CLI-4 | minor | The text of an unknown error was printed when the token provider raised it, before any session | Fixed, wave 2: only the type is told once a token was asked for |
| CLI-5 | minor | A job summary above 1 MiB is dropped by GitHub without a failed step | Fixed, wave 2: the summary is cut at 900 000 bytes with a line that says so; the destructive list is first, so it stays |
| WF-1 | critical | The action ran `python -m azsqlcd` in the checkout: a directory `azsqlcd/` or a file `argparse.py` of the database repository (for `verify`: of the pull request) ran in place of the tool and could answer exit 0 | Fixed, wave 2: `python -P`. Reproduced before the fix by execution |
| WF-2 | minor | The incident job wrote the unchecked `release` input into an issue | Fixed, wave 2 |

Checked in wave 2 and found clean. By execution: `uv run --project` reads no `uv.toml`,
`pyproject.toml` or `.python-version` of the working directory (uv 0.10.4). By reading the files,
and by the unit tests that run the workflow scripts: every job with `id-token: write`
names an environment; no pull-request event reaches a job with an identity (the stage jobs need
the release job, which runs only for a push or a dispatch on `refs/heads/main`); a failed plan
job skips the gate, and the deploy job needs a gate that succeeded; `pending` is `true` for a
deploy that only records a release (A6); the dispatch path of `release.yml` checks the name
before the checkout, builds the tag again and compares the stored manifest; no script
interpolates an expression; an environment name is one of five fixed words and a target id has
no path character, so no report path leaves `--out` or `onboarding/<env>/`.

### Limits that the fixes left

From the "deferred" lists of the four wave 1 reports, unchanged in meaning.

Proof and generation:

- Expressions are opaque to the model. A `CHECK`, a computed column, an index filter or a
  `DEFAULT` that names a column or a sequence that does not exist passes `validate_model` and the
  proof; the engine refuses the `CREATE` at the first deploy.
- After a withdrawal without a replacement (the way out of `WDR004`: the table files are
  restored), a database that did run the withdrawn migration differs from the model. `verify`
  cannot see databases, and `drift` does not report it either (N2-F5, section 10): a DBA compares
  that database with the table files and repairs it by hand.
- With `table_model = false` a withdrawal has no model to prove; the chain rule `CHN006` (wave 2)
  asks for the replacement.
- `emit.py` does not refuse the `RESUMABLE` / `SORT_IN_TEMPDB` / `MAX_DURATION` combinations that
  `parse.py` refuses. `diff` never produces execution options, so no generated text holds them.
- `gen.verify` takes `main_ref = 'origin/main'` as a short name for the withdrawn / replacement
  proof. `build` reads the full ref name.

Lint and lexer:

- `TMB002` matches tombstone and file by path: a tombstone for `[a].[b.c]` next to the file of
  `[a.b].[c]` gives a false `TMB002`. It fails closed.
- An unquoted identifier with a character that a case or width mapping turns into ASCII (Turkish
  dotless i and dotted I, long s, full-width letters, ligatures) is a lexer error; the name must
  be in brackets. A live module with such a name unquoted is quarantined at export
  (`MODULE_INVALID`). The sharp s is exempt.
- The mapping that the engine uses to recognise keywords in non-ASCII text is not proven; the
  lexer refuses the doubtful characters.
- A backslash before a line break is refused only outside strings and comments. Inside a string
  literal the lexer keeps both characters and the engine removes them; token boundaries agree.
- With `table_model = false` nothing before `SET PARSEONLY ON` at plan refuses
  `WAIT_AT_LOW_PRIORITY` on `ALTER COLUMN`, or `RESUMABLE` without `ONLINE`.
- `THREE_PART_NAME`: `db.schema.fn(...)` outside `FROM`, `JOIN` or `INTO` position is still read
  as two parts.
- `secret_in_text` is a text rule: it can quarantine a module that compares a password variable
  with a literal, and it finds no credential in a name without one of its five words.
- New refusals of text that passed before (each fails closed): a number glued to a word; control
  characters in repository SQL; form feed or vertical tab in a comment; look-alike characters in
  unquoted words; a data batch that starts with a bare procedure name; `SET ROWCOUNT`, `FMTONLY`,
  `IMPLICIT_TRANSACTIONS`, `ANSI_DEFAULTS` in migrations; a target server outside
  `[project] server_suffixes` (optional key; the default is the four Azure SQL Database suffixes).

Runner, plan and state:

- A baseline run copies the recorded release, and does not record the release of the bundle that
  it compared with. Deploy the same or a newer release after a baseline.
- First deploy: a chain that holds a non-transactional migration together with anything else is
  `NONTX_NOT_ALONE`; a chain with a `baseline` line has no first deploy to an empty database
  (`BASELINE_REQUIRED`). Way out: a copy of another environment and `rebind-environment`.
- A late replacement runs after the later migrations that the database applied already, so the
  column order can differ between environments. The plan note says so.
- After exit 21 `SESSION_OPTIONS` caused by `IMPLICIT_TRANSACTIONS ON` the write of the status
  `failed` is itself in an implicit transaction and is lost: the run row stays `running` until the
  next deploy closes it as `failed`. The exit code and the rollback are right.
- A managed view with an index blocks every release that changes or unbinds it
  (`INDEXED_VIEW`). Way out: by hand (drop the indexes, deploy, create them again), or a tombstone.
- A module that reaches a dropped or renamed table only through a synonym, or by a one-part name
  outside `dbo`, is not found as a dependant. Unmanaged dependants are not swept (`TABLE_BLOCKER`
  and `[ack] unmanaged_dependants` cover them).
- The session-option check now runs after the last batch of every unit: a batch that leaves
  `NOCOUNT`, `LOCK_TIMEOUT`, the language or an ANSI option changed fails with exit 21 also when no
  module step follows.

Tables, catalog and onboarding:

- The fixed-name clash check sees every object of a schema only in `export`. A read by key
  (read-back, drift) sees only the objects that it asks for.
- `baseline --report-only` gives the model-based rename script only when the release holds
  `onboarding/prod/snapshot.json`. Without it no table is read; the write baseline refuses with
  `CONSTRAINT_NAMES` and the command line writes the script then.
- `export` lints the module files as a tree without `azsqlcd.toml` and without table files; a
  rule that needs those is not applied at export.
- An index on a managed view is reported by the `drift` command only (property `has_index`). The
  module capture does not hold it, so the drift check of the plan does not see it; the gate is
  `catalog.has_index` in the runner.
- The read-back of a deploy still records an engine-named constraint under its fixed name; only
  `baseline` refuses.
- Read-back, baseline, `mark-applied` and `accept-drift` now fail for a live key or index with a
  non-default option that the file does not state. `export` writes the option.
- Reasoned and not run on a database: `@@OPTIONS & 2` and implicit transactions (RS-9), that
  `ALTER VIEW` drops the indexes of a view (OM-01), `sp_rename` to the identical name (error
  15335), the SQL text of the batches `has_index`, `indexed_views`, the `INDEXED_VIEW` branch and
  `database_collation`.

Command line and workflows (wave 2):

- The job summary escapes Markdown and HTML. A bare address such as `https://example.org` in a
  reason is still made a link by GitHub; the address is visible.
- `report_md` of `baseline` goes into the summary as `onboard.py` wrote it.
- `TOKEN_UNAVAILABLE` prints the first 300 characters of the error of the Azure credential
  (`session.py`).
- `KeyboardInterrupt` and `SystemExit` are not caught by `cli.main`: a cancelled job writes no
  `exit_code` and no `reason_code`.
- The record job uploads with names that hold the run id and the attempt; a second run of the
  record job in one attempt fails on the duplicate name and replaces nothing.

## 8. First live results (2026-10-07)

Source: the report of the read-only run on a copy of WideWorldImporters on Azure SQL Database
(engine 12.0.2000.8, service objective GP_S_Gen5_2, collation SQL_Latin1_General_CP1_CI_AS,
driver mssql-python 1.15.0). The run used the commit before fix wave 1 with two patches. Every
batch went through a read-only guard (SELECT and SET only; module text only inside the
`SET PARSEONLY ON` window). `docs/live-testing.md`, section 10, has the same facts for the tester.

### What ran

- 26 query shapes of the tool ran on the engine: 24 tagged `azsqlcd:*`, the SET-options batch and
  the state-table probe, plus the PARSEONLY canary sequence. 26 ran clean on the first try; 0
  needed a change of SQL text. 1 (`broken_references`) needed a change of its rule.
- 583 batches were sent. The guard refused none. No write was sent.
- `export` ended with exit 0 on the first run and wrote 106 files: 42 procedures, 4 views, 1
  function, 9 schemas, 26 sequences, 20 tables, 4 table types. A second export was byte-identical.
  0 modules were quarantined.
- `lint` exit 0 (0 errors, 33 `EXA001` warnings); `verify` exit 0 (0 errors, and the `PRF000`
  warning). The table-file check (parse and `NF000`) on 159 table-class files: 0 findings.
- Fidelity: each emitted file was parsed back and compared with independent catalog queries: 33
  of 33 tables of the model, 26 of 26 sequences, 4 of 4 table types, 0 differences.
- A diagnostic run that read the 34 temporal tables as plain tables: 67 of 67 tables, 0
  differences. It was a check of reader and emitter, not support for temporal tables; the support
  came after this run ("Temporal tables: live proof after the first run").
- `build` exit 0. `plan`, `drift` and `baseline --report-only` each refused with exit 22
  `STATE_MISSING` after 4 batches: the database has no state tables. `drift --export` wrote a file
  equal to the export file.

### What it found

| Id | Class | Finding | State |
|---|---|---|---|
| RO-1 | tool, major | `catalog.broken_references` reported `COLUMNS_NOT_FOUND` for references to objects that have no columns: 18 of 47 modules of a healthy database were called broken. The engine gives `is_all_columns_found = 0` on every entity row for a procedure, a scalar function, a sequence and a table type | Patched and proven live: 18 became 10. The patch is applied in wave 2 by the owner of `catalog.py` |
| RO-2 | engine fact, critical | Of those 10, 7 are sound procedures with `#temp` tables: a real table used in a statement that also uses a `#temp` table gets `is_all_columns_found = 0`, and `UPDATE alias ... FROM #t AS alias` gives an entity row with `referenced_id` NULL. 2 procedures give error 2020 in a healthy database. 1 two-part name is truly missing (deferred name resolution). So the A12 rule of the design called sound modules broken | Not proven fixed live. Wave 2 makes the check differential (`Plan.dependant_findings`: a dependant fails the run only for a finding that it did not have before the change). The consequence in the runner (exit 21 `DEPENDANT_BROKEN` for a redeployed procedure with a `#temp` table) was read from code, not executed |
| RO-3 | engine fact, major | `sys.sequences.start_value` equals `current_value` for 26 of 26 sequences, and the database holds a procedure that issues `ALTER SEQUENCE ... RESTART WITH`. If `RESTART WITH` moves `start_value`, the capture of a sequence reports drift after every reseed | An inference from these rows; not proven by a write. One check is needed on a disposable database |
| RO-4 | tool, minor | `GIT_FAILED` did not name the revision that git could not resolve | Patched (`release.py`) and proven in the run |
| RO-5 | tool, minor | `export.md` holds two `[unmanaged]` blocks for `azsqlcd.toml` (table part, and the whole list at the end) | Fixed, wave 2: `export.md` has one `[unmanaged]` block, at the end; a listed module is left out of the export as a listed table is |
| RO-6 | environment, major | This copy has no columnstore index, partitioning, full-text index, memory-optimized table, filtered index, synonym, alias type, trigger, non-default index option, compression, foreign key action, disabled or untrusted constraint, system-named table constraint, numbered procedure, CLR, signed or encrypted module, DDL trigger | Those reader and reject branches stay unproven |

### Temporal tables: live proof after the first run (2026-10-07)

At the first run temporal tables were outside the model, and that decided the result on this
database: 47 of its 48 tables were quarantined (34 `UNSUPPORTED`, a temporal table or its history
table; 13 `DEPENDS_ON_UNMANAGED`, a foreign key to a temporal table), and 1 table of the sample
schema was written. Temporal tables are now in the model. Two live runs followed.

Write side, a disposable database, one schema that the run made and dropped:

- The emitted DDL of six temporal tables (plain, hidden period columns, finite retention, a
  foreign key to another temporal table, the shape of the sample database with a sequence default
  and computed columns, period columns with defaults) ran with their indexes and foreign keys.
  The engine refused 0 statements; no change of the emitter was needed.
- 37 of 37 catalog checks agreed with the model: `temporal_type = 2` and the named history table;
  the `sys.periods` row; `generated_always_type` and `is_hidden` of every column; period columns
  `datetime2 NOT NULL` with the scale of the model; retention -1 for no limit and 6 MONTH for
  `6 MONTHS`.
- The generated statements of two fixture pairs ran: column, CHECK and index added to a temporal
  table (the history table got the columns, the table stayed temporal); the drop
  (`SET (SYSTEM_VERSIONING = OFF)`, then `DROP TABLE`: the table is gone, the history table is left
  as a plain table).
- Engine refusals that the replay also blocks: `ALTER COLUMN` and `DROP COLUMN` of a period
  column; `DROP CONSTRAINT` of the `PRIMARY KEY` of a temporal table; `DROP TABLE` while
  versioning is on; `ADD` of a `GENERATED ALWAYS` column; `CREATE TABLE` under the name of a
  history table; `DROP SCHEMA` while a history table is left in it.
- One difference: the engine accepts `sp_rename` of a period column; the tool refuses it.
- `HISTORY_RETENTION_PERIOD = 1 WEEKS`, the form that the emitter writes, is accepted.

Read side, the same copy of WideWorldImporters, read-only through the guard (0 refused batches):

- `export` exit 0: 125 object files; 11 objects stay unmanaged. Table files went from 20 to 39
  (20 of the sample, 15 of them temporal, and 19 tables that this copy holds and the sample does
  not); quarantined tables went from 47 to 11; 17 history tables are listed as owned by the
  engine.
- The remaining 11 are one table with a masked column (`[Purchasing].[Suppliers]`, `UNSUPPORTED`:
  dynamic data masking) and the 10 tables that reference it, directly or through
  `[Warehouse].[StockItems]` (`DEPENDS_ON_UNMANAGED`). No reason is temporal.
- `lint` with `table_model = true` on the exported files: 0 errors, 33 `EXA001` warnings. All 39
  table files parse and have 0 token differences (`NF000`).
- Five temporal tables were compared with catalog queries that the reader does not use: equal on
  versioning, history table, retention, period columns, column order, generated kind and hidden
  flag of each column, primary key, unique count, foreign keys.

Not proven live: hidden period columns and a finite retention on the read side (the sample has
neither; the unit codes 3 to 6 of `history_retention_period_unit` come from the documentation);
the sub-options of `SYSTEM_VERSIONING = ON (...)` in another order than the canonical one; `plan`
and `drift` with a history table in the catalog (unit tests only); a deploy through the runner.

### What is still not proven

The list of this read-only run. Section 9 has the state after the write-path runs of the same
day: the spike, the acceptance run, `setup-sql`, `deploy` and the resolve actions ran there
through the library.

- `deploy`, `baseline` that writes, `resolve`, `setup-sql`, `live_spike.py` and
  `live_acceptance.py`: not run (forbidden on this database).
- Any write experiment: sequence `RESTART WITH`, a dropped column against
  `sys.dm_sql_referenced_entities`.
- The consequence of RO-2 in the runner.
- `plan`, `drift` and `baseline` beyond `STATE_MISSING`: drift comparison, table drift, the table
  hooks and the PARSEONLY step of the plan did not run.
- Reader branches with no object in this database: columnstore, partitioning, full-text,
  memory-optimized, XML compression, filtered index, index options, data compression, foreign key
  actions, disabled or untrusted constraints, system-named table constraints and
  `rename-constraints.sql`, alias types, synonyms, triggers and `STRING_AGG` over
  `sys.trigger_events`, numbered procedures, CLR, signed or encrypted modules, database DDL
  triggers, a non-default column collation, `NO_VIEW_DEFINITION` (the login was `dbo`).
- A module definition with a leading comment before `CREATE`: none exists in this database.
- The key-chunk paths above 500 keys: the largest call had 67 keys.
- The unit tests of this repository after the two patches ran only in the working copy of the
  live run (4260 passed), not in this worktree.

### Engine and driver facts

- Driver value types: bit as `bool`, NULL as `None`, the integer types as `int`, decimal and money
  as `Decimal`, datetime as a naive `datetime`, datetime2(7) as a naive `datetime` cut to
  microseconds (the 7th digit is lost), date, time, datetimeoffset as an aware `datetime`,
  uniqueidentifier as `uuid.UUID`, varbinary as `bytes`, nchar and char padded.
- `sql_variant` is read natively: `sys.sequences` values come as `int`, `SERVERPROPERTY('EngineEdition')`
  as `int`, the collation as `str`. The `CAST AS decimal(38, 0)` of the tool gives `Decimal` and works.
- `sys.objects.type` is char(2) and comes padded (`'U '`): `RTRIM` is needed.
- Engine errors carry no number through this driver: the text is `[Microsoft][SQL Server]<message>`.
  Error 2020 was recognised by its en-US text; the PARSEONLY canary error has no number.
- `sys.sql_modules.definition`: the text starts with white space before `CREATE` in 45 of 47
  modules; line ends are CRLF with some bare LF; a module made with `CREATE OR ALTER` is stored as
  `CREATE   VIEW` (three spaces). The export header edit on all 47 modules was only `CREATE` to
  `CREATE OR ALTER`. Since the live spike, `export` also makes the white space after the verb one
  space: `CREATE   VIEW` is exported as `CREATE OR ALTER VIEW` (section 9).
- Stored forms: a DEFAULT always has outer parentheses, a numeric literal is doubled (`((0))`),
  functions are lower case, `NEXT VALUE FOR` has brackets and upper-case keywords. A CHECK has one
  outer pair of parentheses, bracketed columns, no spaces around operators. In a computed column
  `CONVERT` and `TRY_CONVERT` are upper case with bracketed type names; other functions are lower
  case. All these forms lex, emit and parse back with 0 token differences.
- `sys.sql_expression_dependencies` is readable (166 rows; 13 with `referenced_id` NULL).
- `sys.numbered_procedures` exists on Azure SQL Database (0 rows here); `sys.crypt_properties` is
  readable.
- `HAS_PERMS_BY_NAME('[s].[n]', 'OBJECT', 'VIEW DEFINITION')` gives int 1 for `dbo` on every
  module, and 0 (not NULL) for a name that does not exist.
- `sys.dm_sql_referenced_entities`: `is_all_columns_found = 0` on every entity row for a
  procedure, a scalar function, a sequence, a table type and an unresolved name, and for a real
  table used together with a `#temp` table; an `UPDATE` alias of a `#temp` table is an unresolved
  one-part entity; `is_incomplete` was 0 on all rows; error 2020 is raised for 2 procedures of a
  healthy database.
- These catalog columns exist on Azure SQL Database (the queries ran): `sys.tables.ledger_type`,
  `is_node`, `is_edge`, `is_external`, `temporal_type`; `sys.columns.generated_always_type`,
  `is_masked`, `encryption_type`, `xml_collection_id`, `is_hidden`;
  `sys.indexes.optimize_for_sequential_key`; `sys.partitions.xml_compression`; `is_system_named`
  on default, check, key and foreign key constraints.
- A key constraint of a table type has `is_system_named = 1`; the tool emits it unnamed inside
  `CREATE TYPE`.
- `sys.sequences`: `cache_size` NULL with `is_cached = 1` on all 26; `start_value` equals
  `current_value` on all 26.
- `geography` and `sysname` columns read and emit under those names (seen in the diagnostic run).
- Fence probe: `'SYS'` matches `sys.schemas` on this collation, so `case_sensitive` is false.
  `APPLOCK_TEST('public', 'azsqlcd:deploy', 'Exclusive', 'Session')` gives 1.
- `SET PARSEONLY ON` holds across batches on one session: `SELECT 1/0;` returns no result set and
  no error, `SELECT FROM;` raises, and 47 `CREATE OR ALTER` texts parse and create nothing.

Rows of section 2 that this run touches, without closing them: X3 (the meaning of
`is_all_columns_found`, `is_incomplete` and `referenced_id` NULL is measured, and it differs from
the assumption of A12); L1 and L7 (bit as `bool`, the padding, `sql_variant` without CAST;
`STRING_AGG` over `sys.trigger_events` and `SCOPE_IDENTITY()` are still open); L14
(`HAS_PERMS_BY_NAME` for `dbo` only; the SELECT on a state table is open); L16 (the case probe,
for a case-insensitive catalog only).

## 9. Write-path live results (2026-10-07)

Source: the reports of the live spike and of the live acceptance run on a disposable database
(Azure SQL Database, GP_S_Gen5_1, macOS arm64, Python 3.12.12, mssql-python 1.15.0).
`docs/live-testing.md`, section 11, has the same facts in full for the tester. The runs used work
copies of the commit of that hour plus the patches that the runs wrote; the tree holds those
changes now, together with later ones, and has not run against a database as a whole.

### What ran

- Spike: 19 pass, 1 inconclusive by design (L8 needs a second database), 3 manual (L12, L13,
  L14). The first run had 4 fails (L4, L7, X2, X3), none of the driver: 5 defects of the tool and
  5 of the script, fixed and proven in the later runs. The database was left clean.
- Driver gates G-D1 to G-D5: all pass on this machine. `mssql-python` 1.15.0 stays; no pyodbc
  adapter is written. G-D5 is not proven on Windows with Python 3.14 or on the runner image.
- Acceptance: 52 of 52 checks on the real engine, about 75 s. On the commit before the patches:
  14 pass, 1 fail, 8 not run. Covered: `setup-sql`, plan, deploy in one transaction, read-back,
  state rows, a deploy with nothing to do, `STALE_PLAN`, `ALREADY_PAST`, a failing batch, a lock
  timeout, a second runner (25), a non-transactional step lost and failed, a killed session, the
  guard, `UNTOUCHED_CHANGED`, drift, a name collision, a tombstone, `adopt-module`,
  `accept-drift`, PARSEONLY, `rebind-environment`. One more run with both constants True, as the
  tree has them now: 50 of 50.
- Table model (`live_tables.py`): 18 of 18 checks, 37 s: `gen`, `verify`, plan, deploy with the
  table read-back, a refresh step, table drift, `mark-applied` with the table hooks, a column
  drop with `DROP_COLUMN`, and 21 `DEPENDANT_BROKEN` (`ERROR_207`) for a procedure on a dropped
  column.
- By hand through the command line: `build`, `plan`, `deploy --inline-plan` (`ALREADY_PAST`),
  `drift`.

### Defects of the tool that the runs found, and their state

| Id | Finding | State |
|---|---|---|
| Spike T1 | The module read-back compared with the file text; the engine stores another verb, so the read-back would have failed every deploy | Fixed: `modules.stored_text`, `modules.stored_checksum`; proven live |
| Spike T2 | `export` kept the three spaces that `CREATE OR ALTER` leaves, so the export of a deployed module was not its file | Fixed: the verb and the white space after it become `CREATE OR ALTER` and one space; proven live for files written that way. Limit below |
| Spike T3 | `catalog.broken_references` re-raised 207 and 208 and so did not see a dependant that a column drop breaks | Fixed: findings `ERROR_207`, `ERROR_208`, `ERROR_2020`; proven live |
| Spike T4 | `sqlerrors.known_number` did not read 2627, 245 and five more texts | Fixed from the captured texts; 25 texts, 0 misses |
| Spike T5 | A session killed while the client waits for a batch was class `OTHER`, not `SESSION_LOST` | Fixed in `session.py`; proven live |
| Acceptance A1 | A plan during a live deploy ended as lock timeout 1222, never as 25 `RUN_LIVE`: the lock test came after the catalog reads | Fixed: the lock test is the first statement of a plan; proven live. A run that starts after the test can still make a read wait: 22 `READ_FAILED` |

### Engine facts that changed the build

- Module text: after `CREATE OR ALTER` the engine stores the batch with the words `OR` and
  `ALTER` deleted and everything else byte for byte (`CREATE   VIEW`, three spaces); after
  `ALTER` it stores `CREATE`.
- A broken dependant: the first error of `sys.dm_sql_referenced_entities` is 207 (dropped column)
  or 208 (dropped table); 2020 alone only for a procedure that names a missing table. The driver
  returns the first record only. These errors do not end the transaction (guard stays (1, 1)); a
  failed `sp_refreshsqlmodule` does.
- Catalog reads wait for the schema locks of an open DDL transaction, also with
  `READ_COMMITTED_SNAPSHOT` on. `APPLOCK_TEST`, the fence query and the reads of the state rows
  do not wait. `LOCK_TIMEOUT` ends the wait on time (1222 after 1.53 to 1.60 s at 1500 ms, 8 of 8).
- Guard `(@@TRANCOUNT, XACT_STATE(), CURRENT_TRANSACTION_ID())`: (1, 1, id) in the transaction;
  (0, 1, a new id) after a run-time error and outside any transaction; an error that a TRY block
  swallowed raises 3998 at the end of the batch, so (1, -1) never reaches the guard.
- A token connect to a database that does not exist gives the text of 18456 in one attempt.
- The driver does not send the application name (`MSSQL-Python` in `sys.dm_exec_sessions`).

### The two constants

`runner.MODULE_TEXT_READBACK` and `runner.RECONCILE_BY_LOCKING_READ` are True since these runs.
The first: L7 passed with the compare against `modules.stored_text`, and every module deploy of
the acceptance run with the constant True passed the read-back. The second: L6 passed twice (40
of 40 reads right each time), and the acceptance check gave 24 `CONNECTION_LOST_ROLLED_BACK` on
the real run row 4 of 4 times. So `CONNECTION_LOST_ROLLED_BACK` and `CONNECTION_LOST_COMMITTED`
can be reached now; the second was not seen live.

### Limits and open points of these runs

- A file whose verb is not written `CREATE OR ALTER <kind>` with one space (lower case, a comment
  or a line break inside the verb) still exports with another verb text than the file: 8 of 18
  spike files. The engine keeps nothing of `OR ALTER`. A comment between verb and kind word is
  kept, so the header is then `CREATE OR ALTER /* c */ <KIND>`.
- A view or a table-valued function that uses a dropped column and is not changed in the release
  fails at its refresh step. The live run saw 21 `BATCH_FAILED`, step `refresh:<key>`; since
  then the runner reports it as 21 `DEPENDANT_BROKEN` with the module named (N1-F3; unit tests
  only). `verify` does not see it before the merge. The next release cannot fix it (22
  `CATCHUP_REQUIRED`): withdraw and replace (N2-F1, section 10).
- Twice a session was lost with 08S01 "TCP Provider: Error code 0x274C" after a long silent wait.
  Not reproduced. The design has no client statement timeout, so a connection that the network
  drops in silence is seen only when TCP gives up.
- Another process used the disposable database during the acceptance work and waited behind a
  probe. One writer at a time on that database.
- `scripts/live_tables.py`, and `scripts/live_acceptance.py` with the killed-session scenario
  that follows `runner.RECONCILE_BY_LOCKING_READ`, are in the tree as the run wrote them. The
  acceptance script gave 50 of 50 on the final commit. This document records no run of
  `scripts/live_tables.py` on the final commit.

### Still not proven

This list is the state after the runs of this section. Later that day the command line ran on
other disposable databases (section 11): `setup-sql`, `export`, `baseline`, `plan`, `deploy`,
`drift` and `resolve` ran there through `cli.py`. Where section 11 names an item of this list as
run, section 11 wins. `--ci github` did not run there.

- `tests/live`; L8 on a second database; L12; L13; L14; G-D5 on Windows with Python 3.14 and on
  the runner image.
- X6 to X10, the L10 addition and the `#temp` cases of X3 (written after the runs).
- Loss of the network during COMMIT.
- Through `cli.py` on a database: `setup-sql`, `resolve`, `export`, `baseline`,
  `deploy --expect-plan-file` with real work, `--ci github`.
- `drift`, `export` and `baseline` while a deploy is live.
- Table model outside `live_tables.py`: user-defined types, sequences, synonyms, renames,
  `ALTER COLUMN`, IDENTITY, unbind of schema-bound modules, `TABLE_BLOCKER`, unmanaged dependants
  and `[ack]`, a non-transactional migration with table hooks, `accept-drift` of a table, `export`
  and `baseline`; a temporal change through `deploy`; masked columns.
- The unit tests that the flip of the two constants changes: see the report of the wave.

## 10. Hardening audits and the later fix waves (2026-10-07)

Six audits (N1 to N6) ran on fakes, with no database: closed-loop and scenario tests through
`cli.main` (N1, N2), the demo repository and the quickstart (N3), Windows and Python 3.14 (N4),
performance on a 2,000-table estate (N5), wider table coverage (N6). Fix waves 2 and 3 ran the
same day. This section lists what is open in the tree at the end of wave 3. A finding of N1 or N2
that is open has a test with `xfail(strict=True)` in `tests/unit/test_scenarios.py`; when the
finding is fixed the test fails until the mark is removed. The work on N6 (table compression,
column properties) was in progress when this section was written: check `diff.REFUSALS` and the
fixture pairs for its state.

### Open findings

| Id | Severity | Finding | Way out today |
|---|---|---|---|
| N1-F4, N5-06 | minor | One plan reads the whole recorded state four to five times; a first converge asks `has_index` once for each view | None needed; cost only |
| N2-F1 | major | A release that failed on a module cannot be fixed by a later pull request that only changes the module: `CATCHUP_REQUIRED` sends the operator back to the release that fails. Owner decision on A7 needed. Since wave 3 the `DEPENDANT_BROKEN` message says what to do | Withdraw the migration and add its statements again as a replacement (`allow REPLACEMENT_EDGE`), with the module change in the same pull request |
| N2-F5 | minor | A migration withdrawn with no replacement: `drift` does not report the database that ran it | Compare that database with the table files by hand |
| N3-F2 | major | No rule and no guidance for the owner of a schema that a migration creates: `CREATE SCHEMA` by the deploy principal makes that principal the owner. Owner decision needed (ownership chains, a later change of identity); spike L14 does not cover both `CREATE SCHEMA` forms | Write `CREATE SCHEMA [x] AUTHORIZATION [dbo]` by hand if the deploy principal may do that, or let an administrator create the schema |
| N3-F3 | minor | The first `gen` in a repository with no `origin/main` fails with a raw git message | `git fetch origin main`, or give `--base` |
| N3-F5 | minor | `PRF002` tells the author to hand-write a migration for a column that is not last; no migration can pass (`ORD001`) | Put the new column last |
| N3-F6 | minor | No generated path for an online index build | Hand-write the non-transactional migration |
| N3-F7 | minor | A refusal of `gen` is printed twice, and message and hint run together | Cosmetic |
| N3-F8 (rest) | minor | `lint` and `verify` run below the repository root say only that `azsqlcd.toml` does not exist (the finding `CONFIG_INVALID` of `lint.py`). `gen`, `setup-sql` and `export` name the directory and `--root` since wave 3 | Run in the root, or give `--root` |
| N5-01 | major | `verify` after a one-table change takes about 100 s on 2,000 tables (limit about 60 s): `validate_model` builds the whole model again for each replayed statement | None. Numbers below |
| N5-05, N5-08, N5-09 | minor | A plan reads all module files in full to compare checksums; `lex.split_batches` is quadratic in the batches of one file; `release.build` reads every revision of `migrations.sum` | Cost only |
| N6-F1 | major | A change of `DATA_COMPRESSION` of a heap or of a clustered index passes `gen` and `verify` and cannot deploy. In progress at the time of writing (a `rebuild table` statement and the refusal `COMPRESSION_INHERITED` are in `diff.py`; their tests did not pass yet) | Do not change the compression of a heap or of a clustered index through the table model until the fixture pair `heap_compression_change` passes |
| N6-F2 | minor | A table file with two clustered indexes passes the parser, the model, `NF000`, diff and replay | The engine refuses it at the first deploy (rollback) |
| N6-F3 | minor | Lint reads `ALTER COLUMN ... ADD` or `DROP <property>` as a lossy type change. Masking (`ADD MASKED`, `DROP MASKED`) has its own rule since then; `ROWGUIDCOL`, `SPARSE`, `NOT FOR REPLICATION`, `PERSISTED` do not | Allow line `ALTER_COLUMN_LOSSY` |

Fixed in these waves, and so not in the table: N1-F1 (a tombstoned module that was dropped
behind the tool is no drift: the plan notes it and the deploy marks it dropped), N1-F2 (the
`DRIFT_TOUCHED` message names `resolve --mark-applied <migration>` when the catalog equals the
model after the next pending migration), N1-F3 (a refresh that the engine refuses is 21
`DEPENDANT_BROKEN` with the module named), N2-F2 (`mark-applied` of an index or key build is
refused unless the catalog shows the index and no open build), N2-F3 (a module file renamed by
letter case only needs no tombstone), N2-F4 (`NONTX_NOT_ALONE` names the release to start from),
N5-03, N5-04 and N5-07 (`verify` reads the modules for the raw-batch check only when a migration
has a raw batch and parses the head once; `gen` reads only the base modules that hold the word
SCHEMABINDING), N3-F1 (the demo repository and the two documents
are committed), N3-F4 (each printed line is flushed, so a failure line no longer comes before the
findings on a pipe), N4-01 (`.gitattributes` pins LF), N4-02 (streams), N4-04
(Windows device names and characters are refused by `names.py`, `release.py`), N4-05 (a file name
above 255 bytes is refused; `export` names a file that cannot be written: `EXPORT_INCOMPLETE`),
N4-08 (`tool_digest` counts CRLF as LF), N4-09 for git (the program is started by its full path),
N4-16 (the wait for `az`: 60 s), N5-02 (`lex.tokenize` keeps the tokens of the last 16 texts).
`drift` prints `-` in place of an absent hash of an unmanaged object (before: `None None`).

### Performance on a large estate (N5)

Estate: 2,000 tables (34,898 columns, 7,154 indexes, 2,893 foreign keys), 3,000 procedures, 1,000
views, 500 functions, 209 other table-class objects, 301 migrations; 7,014 files, 15.5 MB. Fake
session, zero database latency, macOS. Measured before N5-02 was applied; not measured again.

| Command | CPU seconds | Peak memory |
|---|---|---|
| `lint` | 33.8 | 74 MB |
| `verify --base` after a change of one table (one column, one index) | 100.3 | 147 MB |
| `gen` for the same change; with a dropped and a widened column | 6.3; 15.3 | 215 MB; 230 MB |
| `gen --resum` | 0.5 | 88 MB |
| `build` | 33.3 | 117 MB |
| `plan`, tool time: steady; first converge | 11.0 (77 batches); 27.0 (5,560 batches) | not recorded |

`verify` is over its limit of about 60 s; the plan is inside its limit of about 2 minutes. Where
`verify` spends the time: `lint_repo` 32.8 s, `validate_model` 39.6 s, `lint_change` 8.6 s, the
managed-key read 9.6 s. `validate_model` is quadratic: 250, 500, 1,000 and 2,000 tables take 1.1,
3.2, 10.7 and 41.2 s. With N5-01 and N5-02 patched in a scratch copy: `verify` 36.1 s, `lint` 19.1
s, `build` 19.0 s, steady plan 6.2 s.

### Windows (N4): not fixed

Every finding below is a simulation on macOS or comes from reading the code. The unit tests
passed on the `windows-latest` runners of GitHub (`ci.yml`, Python 3.12 and 3.14, 2026-10-07).
Apart from the runs of `ci.yml`, nothing ran on Windows: no command against a database, no
workflow of a database repository, and no line of `docs/porting.md`, section C.

- N4-03, working-tree side: a folder `Schema/` is read as `schema/` by `lint` and `verify` on a
  file system without letter case. `build` refuses it (`TREE_INVALID`).
- N4-04, rest: `lint` gives `UNKNOWN_PATH` with the general text for a file name that Windows
  cannot check out, and no finding for such a name under `onboarding/`. The release refuses them.
- N4-05, rest: a full path above 259 characters on Windows without long paths is not checked
  before the write. `git config --global core.longpaths true` and `LongPathsEnabled` are needed.
- N4-06: in a CRLF working tree `verify` gives a false `WDR001` and `gen` says "only modules
  changed" (byte compare of blob and working file). Keep the `.gitattributes` of the template.
- N4-07: `onboarding/<env>/overwrite-ack.toml` with a UTF-8 BOM is refused as
  `BASELINE_ACK_INVALID`.
- N4-10: `action.yml` under Git Bash rewrites an argument that starts with `/` to a Windows path.
- N4-11: the `jq` calls of `stage.yml`, `onboard.yml` and `resolve.yml` write CRLF on Windows.
- N4-12: a self-hosted Windows runner needs Git Bash on PATH; the requirements are not written
  in `docs/setup.md`. Treat the workflows as Linux only.
- N4-13: `gen --resum` deletes the old files before it writes the new ones; a file lock can lose
  a migration from disk.
- N4-14: `lint` and `verify` read files that git ignores (`Thumbs.db`, `desktop.ini`,
  `.DS_Store`): local exit 22, CI passes.
- N4-15, N4-09 for `gh`: `scripts/setup_repo.py` decodes `gh` output with the code page and
  starts `gh` by bare name.
- N4-17: 27 test call sites use the default encoding; five symbolic-link tests need a privilege.
- N4-18: `cryptography` has no win_arm64 wheel in `uv.lock`.
- N4-20: the commands of `README.md` and `docs/quickstart.md` are POSIX shell. On Windows use Git
  Bash. A case-only rename of a file needs `git mv`.

### Limits that waves 2 and 3 left

Raw batches and the proof:

- `RAW002` is a token scan. A column or an alias in a raw batch that has the one-part name of a
  managed `dbo` object gives `RAW002`, and no allow line passes it: write the name in another
  form. A three-part name is read by its first two parts and is not matched.
- The proof is stricter than the engine for a key behind a foreign key: a hand-written `DROP` of
  a redundant key on the columns of a foreign key is refused also when the foreign key is bound
  to the other key. Write: drop the foreign key, drop the key, add the foreign key (it needs
  `allow LONG_LOCK`).
- A constraint that is renamed and then goes with `DROP TABLE` of its table: in one pull request
  `PRF006`; in two pull requests that are pending together on one target the plan refuses with
  `RENAME_NOT_RESOLVED`. Deploy the first release before the second.
- `touched_objects` does not list the tables with a foreign key to a table that is renamed and
  then dropped in the same list, when the model holds neither name.
- `gen` (also `--resum`) takes `--base` as any git revision, default `origin/main`; a tag of that
  name can stand in for it. The output of `gen` is not trusted: `verify` proves it against an
  exact base. `verify --base` and `build` are strict (`MAIN_REF_INVALID`).

Lint and configuration:

- `DATA000` is a lint rule only. `plan` and `deploy` do not read `[project] data_batches`: a
  bundle that was built by another path with a data batch would still run it.
- The one-statement rule of a model batch reads tokens, not a grammar. It does not check the
  order of the words of the model list. A token outside the closed word list outside parentheses
  is `MODEL_STATEMENT` (for example `CREATE TYPE ... EXTERNAL NAME`, `LIKE ... ESCAPE` in a
  filtered index, a money literal, `::`): use brackets for a name, or a raw batch. With
  `table_model = false` it is the only check before PARSEONLY at plan.
- `CHN006`: with `table_model = false` a merged migration cannot be withdrawn without a
  replacement line in the same pull request.
- `SET (SYSTEM_VERSIONING = ON)` with no option list passes lint with `table_model = false`, and
  asks no `LONG_LOCK` in any mode (with `DATA_CONSISTENCY_CHECK = ON` the engine reads the rows).

Dependant check (A12, differential):

- A dependant whose findings before the change hold a not-bound error (`ERROR_2020`, `ERROR_207`,
  `ERROR_208`) is not read again after the change. A table change that breaks it more is not
  found.
- A module that the unit of work sent and that is not in the plan list is failed only for a
  not-bound error and for a touched table that does not resolve. A column that a later batch of
  the same unit drops under it is not found.
- A new module on which the engine raises error 2020 (sound procedures can: measured on the
  sample database) fails 21 `DEPENDANT_BROKEN` when its file names a touched table. A changed
  dependant whose new text adds a `#temp` statement can show a new finding and fail the same
  way. Workaround for both: deploy the table change and the module in two releases.
- `tables.Hooks` still treats any finding before the change as "does not bind" for the refresh
  set: a view or a table-valued function with an old finding is not refreshed.
- After 21 `SESSION_OPTIONS` caused by `SET IMPLICIT_TRANSACTIONS ON`, the write of the status
  `failed` can be lost at close; the next deploy reconciles the run row.

Onboarding:

- `drift` does not report a changed `start_value` of a sequence (`catalog_tables.NOT_IN_DRIFT`,
  until X6 decides). If `RESTART WITH` moves the value, a write baseline of an environment whose
  sequences were reseeded ends `READBACK_MISMATCH`; that needs a design decision.
- `export` never removes an entry of `[unmanaged] objects`: an entry whose object is gone stays
  in the list of `export.md` until a human removes it.
- A module that `[unmanaged] objects` lists and whose definition cannot be read still stops the
  export with `NO_VIEW_DEFINITION`.
- `plan` does not check the `HISTORY_TABLE` name of a pending `CREATE TABLE` against a history
  table that an earlier drop left behind.
- The count line of `export` gives the number of history tables; the names are in `export.md`.

Command line:

- `KeyboardInterrupt` and `SystemExit` are not caught by `cli.main` (section 7).
- When a print fails, the tool points the stream at the null device so that the process can end
  with its own exit code. Lines after that point are lost.
- `EXPORT_INCOMPLETE` leaves the files that were written before the failure under `--out`.

## 11. Live command-line runs and the second review (2026-10-07, afternoon)

Two runs through the real command line on a test server of Azure SQL Database, and a second
review of the tree. Sources: the reports of the runs and of the review. Nothing here ran in
production or on GitHub Actions.

What ran:

- Everyday work on one database: 8 releases built, 7 deployed, 1 failed on purpose (exit 21) and
  rolled back with nothing committed; 11 run rows, 16 step rows, 43 object rows at the end.
  `gen` (also `--rename`), a non-transactional index build, a temporal table, gated deploy with a
  plan file and the audit flags, `STALE_PLAN`, `DRIFT_TOUCHED`, `NAME_COLLISION`,
  `INLINE_PLAN_GATED`, `FENCE_META_MISMATCH`, `resolve` (rebind, accept-drift, mark-applied),
  withdraw-and-replace.
- Onboarding of an existing database: 162 of 162 emitted create statements accepted by the
  engine; export of 133 object files with 2 unmanaged tables (clustered columnstore, `SPARSE`, as
  the tool was at that time); `CONSTRAINT_NAMES` and the rename script; baseline of 84 table-class
  objects and 49 modules; two changes deployed; drift clean.
- Read-only export of Microsoft's sample database: all catalog queries clean, 48 of 48 tables (31
  table files, 17 history tables that the engine owns), 0 unmanaged, 0 fidelity differences.

Not run in these two runs: sandbox, preprod and prod; `--ci github` outputs and every workflow;
`resolve --mark-not-applied`, `--adopt-module`, `--clear-run`; `drift --export`; a failed or
interrupted non-transactional step; a sign-in as the plan or the deploy identity (every command
ran as the Entra administrator, so the grants of `setup-sql` were not exercised); the overwrite
acknowledgement file (no module differed); the table comparison of `baseline --report-only`
against a reference snapshot; a drop of a temporal table; token expiry during a run; a long index
build; Windows.

Fixed after the runs and the review (unit tests; not run again on a database):

| Id | What | Where |
|---|---|---|
| Owner request | A triage log for each database command, `log: <path>` on a non-zero exit, `show-log`, `support-bundle`, an upload step in each database job | `src/azsqlcd/trace.py`, `cli.py`, the workflows, `docs/triage.md`. No log of a run on a real database has been read |
| WP2-1 | "Rebase and merge" of two commits gave a release that no database can take | `scripts/setup_repo.py`: squash merge only, checked by `--check`. A repository where it already happened: runbook, "A release is missing" (way by hand, not run) |
| CU-05 | `verify --base` refused `origin/main`, `HEAD` and a short commit id | `cli.py` resolves the revision to a full commit id. A short name that a tag also has stays refused |
| CU-07 | `build` with a wrong `--out` was `TOOL_DEFECT`; from a sub-directory it was `GIT_FAILED` | `OUT_NOT_WRITABLE`; `CONFIG_INVALID` that names the sub-directory |
| CU-11 | Flags with no help text; sub-command errors with the top-level usage | Every flag has a description; the usage is that of the sub-command |
| D1, E2E-7 | `TOKEN_TOO_SHORT` for a read on a workstation | `cli.py` asks the session module for the short token life of a read-only command (`plan`, `drift`, `export`, `baseline --report-only`) when `session.connect` has the keyword `read_only`. Not proven live: the life of a cached token cannot be forced |
| T5 | `None None` in `drift` for an object with no hash | `drift` prints `-` |
| T3 | `verify` passed a migration that needs an unbind line | `PRF007` (error) |
| E2E-9 | No word about `ADD ... NOT NULL` with no `DEFAULT` | `NNL001` (warning) |
| D2, D3, E2E-5, E2E-10 | No local onboarding path; plan after baseline; export round trip; withdraw-and-replace how-to | `docs/setup.md`, section 7 |

Open:

- E2E-11. The message of a deploy says `<n> step(s) applied` and counts plan steps (66 in the live
  run, beside 2 rows of `azsqlcd.step`). The summary table now says "Plan steps applied"; the
  message of `runner.py` is unchanged. The report of every gated deploy warns that the syntax
  check was skipped, although the plan job ran it.
- E2E-1, H1. A temporal table cannot be dropped through `gen`: lint refuses the statement that
  `gen` writes (`MODEL_STATEMENT`). Open in the reports of the runs; check
  `uv run pytest tests/unit/test_verify_proof.py -k temporal_table_drop` on the tree.
- E2E-3, T2. The history table of a managed temporal table was listed as an unmanaged object. In
  the tree now: with `table_model = true`, `plan.py` and `onboard.py` read the history tables
  (`catalog_tables.history_tables`) and do not list them as unmanaged. This code did not run on a
  database.
- E2E-2. `setup-sql` for a second environment on a database that holds the users of another
  environment, when the two share an identity. In the tree now (`state.py`): the script goes on.
  The user of the other environment is kept, a role with the user name of this environment is
  made with that user as member, and the grants go to the role. The `PRINT` line names the kept
  user. The stop stays for a name that is taken under another SID. The role path has unit tests
  only: it did not run on a database. Run `setup-sql` on a database that holds the users of
  another environment before you rely on it.
- E2E-4 is closed with limits: a longer `varchar`, `nvarchar` or `varbinary` under `UNIQUE`,
  `CHECK` or a rowstore index is allowed (section 3, "Table model"). `PRIMARY KEY`, `FOREIGN KEY`,
  a computed column, an index filter and a columnstore index still block. The engine accepts a
  longer column under a primary key with no foreign key; the tool refuses it.
- E2E-6, T4. `drift` does not name an index made by hand. A managed key constraint that was made
  again with an engine name is not drift: an onboarded database has engine-named constraints
  until the rename script runs, so the tool cannot tell the two cases apart. Owner decision.
- T1. `baseline --report-only` compares tables only against `onboarding/prod/snapshot.json`.
  In the tree now (`onboard.py`): without that file the report still names each constraint of
  the model that the engine named, with the rename script. Not run again on a database. Open:
  the reference environment is the constant `prod`; a repository with `[env.dev]` only still
  looks for `onboarding/prod/snapshot.json`.
- E1. An export can hold a view whose table is unmanaged; that file set cannot be created on an
  empty database.
- T6. A column change that is written by hand between `SET (SYSTEM_VERSIONING = OFF)` and `ON`
  needs the same change on the history table in a `raw` batch. The proof does not read that batch
  and passes without it; the engine then refuses `ON` (seen live: the nullability of the history
  column differed).
- `COLUMN_GONE` (in `DEPENDANT_BROKEN`) is a scan of the module text. A column of the same name in
  another table gives a false finding. No check for an encrypted module, and none in a
  non-transactional unit. The finding text of `ERROR_207` and `ERROR_208` holds no object or
  column name. The answer of the engine for a procedure with a `#temp` table after a real
  `DROP COLUMN` was not measured.
- `TOKEN_TOO_SHORT`: the 2 minutes of a read-only command hold for the commands that open their
  session through the command line. A read-only command that gets its token inside the runner
  still needs `min_token_minutes`; this was not checked.
- `PRF007` is a token scan and does not know which table a column name belongs to. A schema-bound
  module that names the table, and a column of the same name from another table, gets a false
  `PRF007`. The correction is the unbind line; it does no harm unless the view has an index.
  `gen` writes unbind lines for the whole table, so it can write a line that the engine does not
  need; for a view with an index the plan then refuses with `INDEXED_VIEW`.
- `TMB003` runs only with `table_model = true`. With `false` there is no finding.
- `SECRET_LITERAL` does not flag an empty literal, or a literal in a comparison in `WHERE`, `ON`,
  `IF`, `HAVING` or `WHEN`. A secret that is written as a comparison value is not found.
- `ALLOW_FORMAT` is for migration files only. In a module file a line such as
  `--azsqlcd:after ...` is a plain comment with no finding.
- A column or table name `Throw` or `Kill` without brackets is refused by lint (the message says
  to add the brackets). `'$ 5'` with a space passes lint and the engine refuses it. The parser
  refuses `sp_rename` without `sys.`, and an `ALTER TABLE` with more than one action.
- The `--help` text of `--rename` and the general hint of `ORD001` do not name the form of the
  argument. `gen` prints the exact argument only when one column is gone and one is new with the
  same data type.
- The triage log: no log of a run on a real database has been read. The upload step ran on GitHub
  only in jobs that stopped at the login.
- WP2-2. After `gh run rerun --failed`, the jobs `record` and `incident` can use the report
  artefact of the earlier attempt and label it with the new attempt.
- WP2-3. `setup_repo.py` in apply mode lowers a repository that is stricter than the policy
  (reviewers, self-review, approvals, extra checks and rules), then prints "meets the policy".
- WP2-4. `action.yml` says "tool did not start" for a tool process that was killed during a run
  (exit 130, 137, 143) or that ended with a traceback (exit 1).
- The triage log: `KeyboardInterrupt` and a killed process leave a log with no `end` event. Two
  calls of one command in the same second share one file. The directory `.azsqlcd/` is not in the
  `.gitignore` of the template (`tests/unit/test_example_repo.py` fixes the file set of the
  template): add the line by hand.
- Column and index properties (`SPARSE`, `ROWGUIDCOL`, `NOT FOR REPLICATION`, columnstore, data
  compression) were being changed in the tree while this section was written; the documents do not
  describe them.
- Merge commits are off with squash merge only. The proof of a withdrawal for a rebase merge was
  not analysed (section 1) and is no longer reachable under the policy.

## 12. GitHub Actions proof (2026-10-07)

The workflows of the tool ran on real GitHub Actions with a scratch demo repository. The demo has
no real tenant, so every job that needs Azure stops at the `azure/login` step. All runs used the
tool at a branch reference, not at a tag. Source: the report of the proof.

What ran green on GitHub:

- Release create: checkout, download of the action of the tool, `uv sync`, `build`, and
  `gh release create` with `bundle.tar` and `manifest.json`. Two releases, `r1` and `r2`. The
  digest in the log equals the sha256 of the downloaded `manifest.json` for both.
- `verify`, pass: a pull request with a table change and its generated migration.
- `verify`, fail as required: a pull request with a table change and no migration. Exit 22,
  `VERIFY_FAILED`, finding `PRF002`, readable in the step log.
- Dispatch of an existing release: the job checked out the tag of the release, built the release
  again, and the manifest compare passed. No release was created. A release name that does not
  exist stops at the checkout; no stage starts.
- `targets`: for the stage, drift, onboard and resolve workflows. A `gated` input that differs
  from `azsqlcd.toml` stops the job with a readable error.
- `record`: the job ran. It attached nothing, because no plan or report file was made.
- `incident`: the job made the label and one issue, and added a comment on the second failure
  (no second issue).

The proof found defects in the workflows. Three are fixed and were seen fixed on GitHub: the
layout of a downloaded artefact with one target (critical), the order of the lines in the step
log, a second error of the onboard upload step after a failed login. One was reported and not
fixed in the proof: the subject of the federated credentials (`docs/setup.md`, section 2).

Not proven on GitHub:

- Everything after `azure/login`: the tool calls of plan, deploy, drift, export, baseline-report,
  baseline and resolve; the install of the `db` extra (`uv sync --extra db`); the token life
  check; every database connection.
- The gate job. It needs a plan job that passes the login. Its script has unit tests only.
- The gated deploy of an approved plan: the download of the plan artefact and
  `--expect-plan-file`. Unit tests only.
- `record` with a `plan.json` or a `report.json` to attach.
- Approvals: environments with required reviewers, the approvals API with a list that is not
  empty, `--approved-by` and `--approved-utc`. It is not known if required reviewers are
  available for the demo repository on the plan of its account.
- The OIDC subject against Entra. The default subject that GitHub sent is the immutable form with
  the owner id and the repository id. The customised subject with `job_workflow_ref` and the
  limit of 20 credentials are not tested.
- Self-hosted runners and runner groups, the `AZSQLCD_OFFLINE=1` path, workflow access of a
  runner group.
- The rendered job summary (verify, build, targets). The content was made locally only.
- The stages sandbox, test, preprod and prod inside `db.yml`, and the `from_stage` and check-only
  chain: dev fails at the login first, so they are always skipped.
- The scheduled drift trigger (cron).
- Tool references by tag. The tag `v0.1.0` exists now, on `main`. The runs of the proof called
  the tool at a branch, and this document records no run that called it at the tag. Tag and
  branch rulesets, CODEOWNERS review, the Actions policy of an organisation.
- Windows runners, an environment with more than one target, a second run for the same commit.
- The onboard actions baseline-report and baseline, and the resolve actions other than
  clear-run.

The owner account is a GitHub user account, not an organisation. Runner groups do not exist
there, and a team for CODEOWNERS does not exist there. The Actions access level of the tool
repository is `user` (`docs/setup.md`, "User account instead of an organisation").
