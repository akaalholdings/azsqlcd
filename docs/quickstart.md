# Quickstart: a first proven migration in 15 minutes

You change one table of the demo database on your own machine. The tool writes the migration, you
read it, and the tool proves that the migration gives the table file. No database, no network, no
Azure account.

Status of the tool: the commands of this document ran on a developer machine; section 10 says how.
The commands that write to a database (`baseline`, `deploy`, `resolve`) ran through the real
command line on dummy databases of Azure SQL Database on 2026-10-07; `export` also ran read-only
on a sample database (`README.md`, Status). The workflows ran on GitHub Actions up to the Azure
login step; no job passed the login, so no workflow reached a database. Nothing ran in
production. This document ends where a database would start.

You need git, [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer. The shell lines are for
bash or zsh.

A test, `tests/unit/test_example_repo.py`, makes the steps 2 to 9 again on every test run. It
compares the edits, the migration that `gen` writes and the output lines of `gen`, `lint`,
`verify` and `targets` with the text of this document.

## 1. Install the tool (2 minutes)

```
git clone https://github.com/akaalholdings/azsqlcd
cd azsqlcd
uv sync --frozen
export TOOL="$PWD"
azsqlcd() { uv run --project "$TOOL" --no-sync azsqlcd "$@"; }
azsqlcd --version
```

```
azsqlcd 0.1.0
```

The offline commands use the Python standard library only. `uv sync --frozen` also installs the
test tools of the repository; add `--no-dev` to leave them out. The function `azsqlcd` runs the
tool from the checkout in the directory where you call it.

## 2. Make a database repository from the example (1 minute)

One database has one repository. Copy the example into a new one:

```
mkdir ../db-demo && cd ../db-demo
cp -R "$TOOL/examples/demo-db/." .
git init -b main
git add -A
git commit -m "Demo sales database"
git update-ref refs/remotes/origin/main HEAD
```

The last line is for this machine only. `gen` and `build` read the ref `origin/main`, and a
repository with no remote has none. The line makes the ref point at your commit, as a push and a
fetch would.

```
azsqlcd lint
```

```
0 error(s), 0 warning(s)
```

The repository holds 12 tables, 6 views, 4 functions, 8 procedures, 2 triggers and the first
migration. `README.md` in it lists them.

## 3. Change a table and a procedure (3 minutes)

The change: each customer gets a loyalty tier. Work on a branch:

```
git switch -c feat/loyalty-tier
```

Edit `schema/tables/sales.Customer.sql`. Add the column after the last column, `[CreatedUtc]`:

```sql
    [LoyaltyTier] tinyint NOT NULL CONSTRAINT [DF_Customer_LoyaltyTier] DEFAULT (0),
```

Add the index at the end of the same file:

```sql
GO
CREATE NONCLUSTERED INDEX [IX_Customer_LoyaltyTier] ON [sales].[Customer] ([LoyaltyTier])
    INCLUDE ([DisplayName]);
```

Edit `schema/procedures/sales.usp_CreateCustomer.sql`. Add the parameter, and use it in the INSERT:

```sql
    @CountryCode char(2),
    @LoyaltyTier tinyint = 0,
```

```sql
    INSERT INTO [sales].[Customer] ([Email], [DisplayName], [CountryCode], [LoyaltyTier])
    VALUES (@Email, @DisplayName, UPPER(@CountryCode), @LoyaltyTier);
```

You edit the files that say what the database is. You write no ALTER statement.

## 4. Let the tool write the migration (1 minute)

```
azsqlcd gen --name customer_loyalty_tier
```

```
wrote migrations/0002__customer_loyalty_tier.sql
wrote migrations/migrations.sum
1 allow line(s) need a reason: replace TODO, then run azsqlcd gen --resum
```

`gen` reads the table files at `origin/main` from git and the table files of your working tree,
and writes the statements that change the first into the second. It uses no database.

## 5. Read the migration (2 minutes)

```
cat migrations/0002__customer_loyalty_tier.sql
```

```sql
-- azsqlcd:migration 0002__customer_loyalty_tier
-- azsqlcd:mode tx
ALTER TABLE [sales].[Customer] ADD [LoyaltyTier] tinyint NOT NULL CONSTRAINT [DF_Customer_LoyaltyTier] DEFAULT (0);
GO
-- azsqlcd:allow LONG_LOCK [sales].[Customer] reason: TODO
CREATE NONCLUSTERED INDEX [IX_Customer_LoyaltyTier] ON [sales].[Customer] ([LoyaltyTier]) INCLUDE ([DisplayName]);
GO
```

- `mode tx`: the whole file runs in one transaction. A failure rolls all of it back.
- One statement in each batch. `GO` ends a batch.
- The procedure is not in the file. A view, procedure, function or trigger has no migration: its
  file is the change, and the deploy sends the file when its checksum differs.
- `allow LONG_LOCK`: an index build on a table that exists holds a lock for as long as the build
  takes. The tool does not decide that for you. It asks for a reason that a reviewer can read.

This file is the SQL that runs in every environment, from dev to prod. A reviewer of the pull
request reads this file.

## 6. Write the reason (2 minutes)

`lint` does not take `TODO` for a reason:

```
azsqlcd lint
```

```
migrations/0002__customer_loyalty_tier.sql:5: error ALLOW_REASON: TODO is not a reason; write the reason
1 error(s), 0 warning(s)
LINT_FAILED: 1 error(s) in the files; first: ALLOW_REASON at migrations/0002__customer_loyalty_tier.sql:5
azsqlcd lint: exit 22
```

Edit line 5 of the migration:

```sql
-- azsqlcd:allow LONG_LOCK [sales].[Customer] reason: the table has under 100000 rows; the index build takes seconds
```

`migrations/migrations.sum` holds a checksum of each migration file, so `lint` now sees the edit:

```
azsqlcd lint
```

```
migrations/migrations.sum:3: error CHN004: the sha256 of this line is not the sha256 of migrations/0002__customer_loyalty_tier.sql. If the migration is not merged yet, run azsqlcd gen --resum; a merged migration never changes
1 error(s), 0 warning(s)
LINT_FAILED: 1 error(s) in the files; first: CHN004 at migrations/migrations.sum:3
azsqlcd lint: exit 22
```

Write the line of the new migration again, then check:

```
azsqlcd gen --resum
azsqlcd lint
```

```
wrote migrations/migrations.sum
0 error(s), 0 warning(s)
```

Run `gen --resum` after every hand edit of a migration that is not merged. A merged migration
never changes.

If the table is large, a reason is not enough. Put the index in a pull request of its own and
change its migration by hand: the mode line, the statement, and remove the `allow LONG_LOCK`
line (in a non-transactional migration that line matches nothing, and lint fails with
`ALLOW_UNUSED`). The file is then its `-- azsqlcd:migration` line and these two lines; run
`azsqlcd gen --resum` after the edit:

```sql
-- azsqlcd:mode nontx expected-minutes: 10
CREATE NONCLUSTERED INDEX [IX_Customer_LoyaltyTier] ON [sales].[Customer] ([LoyaltyTier]) INCLUDE ([DisplayName]) WITH (ONLINE = ON (WAIT_AT_LOW_PRIORITY (MAX_DURATION = 5 MINUTES, ABORT_AFTER_WAIT = SELF)));
```

Such a migration runs outside a transaction and is the only change of its pull request
(`NTX003`). `lint` and `verify` accepted a migration of this form for another index of the same
table; no database has run one.

## 7. Prove the change (1 minute)

```
azsqlcd verify --base "$(git merge-base origin/main HEAD)"
```

```
0 error(s), 0 warning(s)
```

`verify` is the check of the pull request. It runs the lint, the rules of the migration chain and
of the module files, and the proof: it applies the new migrations, statement by statement, to the
model of the table files at the base revision, and requires the model of the table files of your
branch. The proof is on models in memory. It does not prove that the engine accepts a statement,
and it does not read data batches: the first deploy to dev is the first proof of both.

## 8. See the proof refuse a wrong migration (2 minutes)

You can edit a migration by hand, and the proof must still hold. In line 3 of the migration,
change `tinyint` to `smallint`. Then:

```
azsqlcd gen --resum
azsqlcd verify --base "$(git merge-base origin/main HEAD)"
```

```
wrote migrations/migrations.sum
schema/tables/sales.Customer.sql:1: error PRF001: TABLE:[sales].[Customer]: columns[loyaltytier].type.name: the migrations give another value than the object file
1 error(s), 0 warning(s)
VERIFY_FAILED: 1 error(s) in the files; first: PRF001 at schema/tables/sales.Customer.sql:1
azsqlcd verify: exit 22
```

The finding names the object and the property. Change `smallint` back to `tinyint`, run
`azsqlcd gen --resum` and the `verify` line again: `0 error(s), 0 warning(s)`.

## 9. After the merge (1 minute)

Commit, and merge the branch into `main` as a pull request would:

```
git add -A
git commit -m "Add the loyalty tier of a customer"
git switch main
git merge --ff-only feat/loyalty-tier
git update-ref refs/remotes/origin/main HEAD
```

On GitHub a merge starts the release job. It runs `build` on the merged commit:

```
azsqlcd build --commit HEAD --out ../dist
```

```
0 error(s), 0 warning(s)
release r2 of commit f516bacb029dc3cf3dc41daf6b841a8a5bd6c4d4: 45 file(s)
digest 8168693e6bf24545f29f88d19e8abe643a5121d201778b5724927102edb93b0f
```

Your commit id and your digest differ from these: both come from your commit. `build` reads the
files from git, not from the working tree, and refuses a commit that is not on `origin/main`.
`../dist` holds `bundle.tar` and `manifest.json`. The digest names this release in every later
job, and a job refuses a bundle that does not have it.

Each stage then asks which databases it has:

```
azsqlcd targets --bundle ../dist --digest <the digest that build printed> --env dev
```

```
demo-dev: demo_sales on replace-me-dev.database.windows.net
job timeout: 120 min
```

What follows needs a database, and this document stops here. The design is:

1. The release `r2` goes to dev, sandbox, test, preprod, prod, in this order. A stage starts only
   when the stage before it passed.
2. dev and sandbox: `deploy --inline-plan`. test, preprod, prod: a `plan` job that only reads,
   then `deploy --expect-plan-file` of that plan. preprod and prod wait for an approval.
3. `deploy` takes a lock, computes the plan again and stops when its hash is not the hash that was
   approved. It then runs one transaction: the migration, the changed procedure, a read-back of
   the table against the model, the history rows. After every batch it checks that the
   transaction is still the one that it opened.
4. The exit code and a reason code say what happened to the database. `docs/runbook.md` has one
   row for each reason code.

Steps 1 to 4 ran through the real command line on a disposable Azure SQL Database on 2026-10-07.
On GitHub the workflows ran up to the Azure login step, so no step that needs a database ran
there. `docs/live-testing.md` is the list of tests that prove them, and `docs/known-gaps.md` is
the list of what is not built or not proven.

## If a command stops

Each line is a message that the tool printed while this document was made, with what to do.

| Message | Cause | Do this |
|---|---|---|
| `GIT_FAILED: git rev-parse failed (exit 128): fatal: Needed a single revision` | The repository has no ref `origin/main` | Run the `git update-ref` line of step 2, or give `gen --base <commit>` |
| `CONFIG_INVALID: azsqlcd.toml does not exist` | The command did not run in the root of the database repository | `cd` to the root, or give `--root DIR` |
| `PRF002 ... the pull request adds no migration` | A table file changed and `gen` did not run | Run `azsqlcd gen --name <name>`. The message shows the statements |
| `GEN_REFUSED ... ORD001 ... new column [LoyaltyTier] is not the last column` | The new column is between two columns | Put it after the last column. The engine cannot move a column |
| `NF002: the DEFAULT of column [LoyaltyTier] needs a name` | A table file has `DEFAULT 0` with no constraint name | Write `CONSTRAINT [DF_...] DEFAULT (0)` |
| `CHN004: the sha256 of this line is not the sha256 of ... If the migration is not merged yet, run azsqlcd gen --resum; a merged migration never changes` | A new migration was edited by hand | Run `azsqlcd gen --resum` |
| `TMB001: this module file is removed and schema/_tombstones.toml has no [[drop]] for it. Add to that file the three lines: [[drop]] / object = "<KIND>:[schema].[name]" / reason = "<why the module is dropped>"` | A view, procedure, function or trigger file was deleted | Add the three lines of the message to `schema/_tombstones.toml` |
| A new migration is wrong and you want to write it again | The table file was not right when `gen` ran | Delete the file of the new migration under `migrations/`. Run `azsqlcd gen --resum`: it drops the chain line of a new migration whose file is gone (a merged migration is never dropped). Correct the table file. Run `azsqlcd gen --name <name>` again |
| The migration has `DROP` and `ADD` for a column or a table that was renamed | `gen` infers no rename, and the rows of the column would be lost | Run `gen` with `--rename "column:[schema].[table].[old]=[new]"` or `--rename "table:[schema].[old]=[new]"`. `gen` prints the exact argument when one column is gone and one is new with the same data type |

Every other finding code: `docs/runbook.md`, section "Lint and verify findings".

## 10. How this document was checked

- Machine: macOS, git 2.50.1, uv 0.10.4, Python 3.12.12. Not run on Windows or Linux.
- The minutes in the headings are estimates. Nobody who is new to the tool was timed.
- Step 1: `uv sync --frozen` ran on a copy of the checkout, from the uv cache with no network. The
  `git clone` line was not run.
- Steps 2 to 9: each `azsqlcd` line ran as written here, on a terminal. Each output block is the
  text that the tool printed, complete, except the commit id and the digest of step 9, which
  differ on every machine. Output of git is not shown.
- The messages of "If a command stops" came from separate runs on other copies of the example.
- On a pipe, for example in a CI log, the two lines of a failure (`LINT_FAILED: ...` and
  `azsqlcd lint: exit 22`) come before the findings, not after them.
