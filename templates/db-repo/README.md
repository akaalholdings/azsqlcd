# Schema repository of one database

The files in this repository are the source of truth for one database in five environments.
The pipeline is `azsqlcd` (tool repository `akaalholdings/azsqlcd`). Owners: `.github/CODEOWNERS`.

## Change the schema

1. Branch from `main`. Edit the object files under `schema/` (one object per file).
2. Table, type, sequence or synonym changed, with `table_model = true` in `azsqlcd.toml`: run
   `azsqlcd gen --name <short_name>` (the base is `origin/main`). It writes
   `migrations/NNNN__<short_name>.sql` and one line in `migrations/migrations.sum`. Read the SQL.
   With `table_model = false` the command refuses: write the migration file by hand, with the
   header lines `-- azsqlcd:migration NNNN__<short_name>` and `-- azsqlcd:mode tx`.
3. Replace `TODO` in each `-- azsqlcd:allow ... reason: TODO` line with a reason. After every edit
   of a new migration run `azsqlcd gen --resum`: it numbers the file and writes its checksum line. To write a new migration again: delete its file under `migrations/`, run `azsqlcd gen --resum` (it drops the chain line of a new migration whose file is gone; a merged migration is never dropped), correct the table file, run `azsqlcd gen --name <short_name>` again. A renamed column or table: `gen` infers no rename and writes DROP + ADD (the rows are lost); run `gen` with `--rename "column:[schema].[table].[old]=[new]"` or `--rename "table:[schema].[old]=[new]"`.
4. View, procedure, function or trigger changed: no migration. The file is the change.
5. Module deleted: add a `[[drop]]` entry to `schema/_tombstones.toml` in the same pull request. The finding `TMB001` prints the three lines to add.
6. Run `azsqlcd lint`, then `azsqlcd verify --base "$(git merge-base origin/main HEAD)"`. Open a
   pull request. Each finding has a code: "Lint and verify findings" in `docs/runbook.md`.
7. `main` moved and `migrations.sum` conflicts: merge `main`, keep the lines of both sides, then
   run `azsqlcd gen --resum`.

## What the checks mean

- `verify / verify` runs the lint, the chain rules, the module rules and, with `table_model = true`,
  the proof that the new migrations turn the old files into the new files. It uses no database.
  It does not prove `raw` batches; dev and sandbox are the rehearsal.
- Structural changes only: a `-- azsqlcd:data` batch is the error `DATA000` unless the owner sets `data_batches = true`.
- A code owner must approve. A new push removes the approval. A merged migration never changes. Merge with "Squash and merge" only: one pull request is one commit on `main` and one release.

## Promotion and approval

- A merge to `main` builds one release `r<n>` and runs dev > sandbox > test > preprod > prod.
  A stage starts only when the stage before it passed. A failed stage stops the promotion.
- dev and sandbox deploy with no plan job. test, preprod and prod run a plan job, then deploy that
  plan; preprod and prod wait for an approval. Open the compare link in the plan summary first.
- Fix a failed release with a new pull request; it starts at dev again. When a merged migration is the cause (also a view or procedure that its table change broke): withdraw and replace the migration (steps: `docs/setup.md` of the tool repository, "Withdraw and replace a merged migration").
- Promote a release again: `gh workflow run db.yml --ref main -f release=r<n> -f from_stage=<stage>`.
  The stages before `from_stage` only prove that they hold the release.
- Exit codes, reason codes and recovery: `docs/runbook.md` in the tool repository. A failed database command prints `log: <path>`; what to send: `docs/triage.md`. Add `.azsqlcd/` to `.gitignore`.
