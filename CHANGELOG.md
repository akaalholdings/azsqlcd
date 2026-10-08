# Changelog

Changes to azsqlcd that a user of the tool can see. The newest version is first. Each change that
reaches `main` gets an entry under "Unreleased"; a release moves those entries under its version
number and date. Version numbers follow semantic versioning.

## Unreleased

### Fixed

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

### Changed

- `lint`, `NTX004` reads each statement of a batch alone. Before, one columnstore build, one
  `ALTER COLUMN` or one `WAIT_AT_LOW_PRIORITY` anywhere in a batch switched the rule off for the
  whole batch. An online rowstore index build with no low-priority wait is now reported also when
  such a statement is in the same batch.
- `lint`, `NTX004`: `WITH (ONLINE = ON, WAIT_AT_LOW_PRIORITY (...))`, with the wait outside the
  parentheses of `ONLINE = ON`, is now reported. The accepted form is
  `WITH (ONLINE = ON (WAIT_AT_LOW_PRIORITY (...)))`.
- `lint`: `DROP INDEX a.b.c (...)` and `DROP INDEX a.b.c ON schema.table` are now reported as
  `THREE_PART_NAME`. The engine refuses both statements.

## 0.1.0 - 2026-10-07

First public version.

- Command line: `lint`, `verify`, `gen`, `build`, `targets`, `setup-sql`, `export`, `baseline`,
  `plan`, `deploy`, `drift`, `resolve`, `show-log`, `support-bundle`.
- Reusable GitHub workflows and a composite action for the promotion of one release through five
  environments, and a template for a database repository.
- What ran and what did not run before this version: `README.md`, section "Status", and
  `docs/known-gaps.md`.
