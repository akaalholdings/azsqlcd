# azsqlcd: design choices, next to other families of tools

Read this first: **azsqlcd is not production-ready.** Version 0.1.0 was built on 2026-10-07. Its
live scripts and its command line ran on disposable Azure SQL Databases, and its workflows ran on
GitHub Actions up to the Azure login step. `README.md`, section "Status", says what ran and what
did not. Where this document and `README.md` differ, `README.md` wins.

What this document is:

- A list of the design choices of azsqlcd. For each choice it names a family of tools that makes
  another choice, and says in short how that family works.
- The limits of azsqlcd.

What this document is not:

- It is not a product review. It does not say what a named product can do or cannot do, and it
  gives no price, no edition and no version of another tool.
- A product name below is an example of a family. The sentence about a family is a short
  statement of its general approach, and this document gives no source for it. One product of a
  family can differ from it.
- No other tool was run for this document, and no vendor reviewed it.

The families:

- **Script runners**, such as Flyway or DbUp. A person writes ordered change scripts. The tool
  runs the scripts in order and records in a table of the database which ones ran.
- **State-based tools** that compare a model with a database, such as SqlPackage. The wanted
  schema is a model. At deploy time the tool compares the model with the target database and
  computes the change script.
- **Declarative tools** that plan against a development database, such as Atlas. The wanted
  schema is declared in files. The tool computes the change, and it uses a separate development
  database to work the change out and to check it.

## 1. The choices of azsqlcd

Each statement in the middle column is about the code of azsqlcd; the file is in `src/azsqlcd`.
`README.md` says which parts ran on a database.

| Subject | The choice of azsqlcd | A family that makes another choice |
|---|---|---|
| Where the truth is | Object files in git: one file for each object (schema, table, view, procedure, function, trigger, sequence, type, synonym). A pull request shows the new definition of the object, and the migration next to it | Script runners: the ordered scripts are the source. The schema is the result of all scripts that ran |
| How a table change is made | `gen` writes a migration from the difference between the table files at `origin/main` and in the working tree. A person reads it and can edit it. The migration is a versioned file in a chain (`migrations/migrations.sum`), and a merged migration never changes (`gen.py`, `chain.py`) | Script runners: a person writes the script. State-based tools: the tool computes the script at deploy time, for each target |
| How a migration is proven | `verify` replays the new migrations on a model of the old table files, in memory, and compares the result with the model of the new table files. It needs no database. A hand-edited migration is proven in the same way (`gen.py`) | Declarative tools that plan against a development database: the tool uses that database to compute and to check the change |
| Parser | An own lexer and parser in Python, with the standard library only. No Microsoft dac stack: no DacFx, no SqlPackage, no ScriptDom, no SMO (decision D3 of `docs/design.md`; `lex.py`, `parse.py`) | State-based tools such as SqlPackage: the tool is built on the DacFx library of Microsoft |
| How views, procedures, functions and triggers deploy | By checksum. A module whose file text changed is sent with `CREATE OR ALTER`. The tool sends the text of the author and never writes a module from a model (`modules.py`) | State-based tools: the module is a part of the model, and the tool computes the statement |
| Drops | A module is dropped only when a tombstone names it: a `[[drop]]` entry with a reason in `schema/_tombstones.toml`. A table is dropped by a migration statement with an allow line that gives the reason (`chain.py`, `lint.py`) | State-based tools: the comparison finds an object that the model does not hold, and an option of the tool decides what happens to it |
| Execution | One session, one unit of work: one transaction, or one non-transactional batch. After every batch a guard reads `@@TRANCOUNT`, `XACT_STATE()` and the transaction id. An application lock allows one writer. Under the lock the tool computes the plan again and runs only when its hash is the expected one (`STALE_PLAN` when it differs) (`runner.py`, `plan.py`) | Script runners: the scripts run in order, and the runner or its configuration decides the transaction |
| Drift | `drift` compares the managed objects of the database with the recorded state and ends with exit 30 when one differs. A deploy stops when an object that the release touches has drifted (`DRIFT_TOUCHED`). A person decides: `resolve` or a pull request | State-based tools: each deploy compares with the live database, so a difference in the target is an input of the script that is computed |
| Promotion | One release bundle with a digest is built once from a commit. The same bundle goes dev > sandbox > test > preprod > prod, with a plan for each environment, and an approval where the environment is gated (`release.py`) | State-based tools: the script is computed for each target at deploy time, so it can differ from one environment to the next |
| Triage log | Each database command writes a log of what the tool did, in order, with no SQL text, no data value and no token. `support-bundle` packs it with the reports of the run (`trace.py`, `docs/triage.md`) | No family is named for this row |

A row names a family only where its general approach differs in a plain way. A family that a row
does not name can make the same choice or another one. Two examples of the same choice: script
runners also run the same reviewed scripts in every environment, and they also keep a history in
the database of what ran.

## 2. What azsqlcd does not do

`docs/known-gaps.md` is the full list. The main limits:

- **Not production-ready.** No production use. On GitHub Actions no step after the Azure login
  ran: not the gate job, not an approval, not the OIDC subject against Entra, not a runner group.
  Nothing ran on Windows against a database (the unit tests pass on Windows runners).
- **Azure SQL Database only.** Any other engine edition is refused (decision D2 of
  `docs/design.md`).
- **Structural changes only.** Data batches are off by default (`DATA000`), and the proof does
  not model them.
- **Object types that the table model refuses**: ledger, memory-optimized, graph and external
  tables; partitioned tables, partition functions and partition schemes; XML, spatial, JSON,
  vector and full-text indexes; column sets, Always Encrypted, `FILESTREAM`, typed xml, CLR
  types; the enable and the disable of a constraint. `src/azsqlcd/parse.py` holds the list.
- **Modules that stay unmanaged at export**: an encrypted module, a signed module, a CLR module,
  a numbered procedure, an indexed view, a database DDL trigger, a module that is stored with
  `ANSI_NULLS` or `QUOTED_IDENTIFIER` off, a trigger with a first or last order
  (`src/azsqlcd/onboard.py`).
- **No table rebuild.** `gen` refuses a change that the engine cannot make with `ALTER`, for
  example an identity change or a new column that is not last. A person writes that migration.
- **The proof is on models in memory.** It does not show that the engine accepts a statement.
- **Users, roles and permissions** are out of scope (decision D7).
- **Not built**: undo migrations, the creation of a full schema in an empty database from the
  object files (`init`), the alignment of an environment that differs (`align`), Azure DevOps
  pipelines, a desktop or editor tool, style and naming rules in lint, sign-in to the database
  by another way than the token of the Azure CLI login.
- **No emergency path.** No stage can be skipped: a broken lower environment blocks a fix for
  prod (`docs/known-gaps.md`, sections 4 and 6). A failed non-transactional migration needs
  `resolve` by a person (`docs/runbook.md`).
- **No vendor and no support contract.** The tools named above are released products with a
  public version history; azsqlcd is one build of 2026-10-07.

Products change. Before you compare azsqlcd with a product, read the current documentation of
its vendor.
