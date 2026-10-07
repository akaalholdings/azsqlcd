# Demo sales database

A small sales database as a database repository of `azsqlcd`. It is `templates/db-repo/` with the
files filled in. Use it to try the offline commands, and as the first release of a disposable
database in a live test.

Status: the offline commands (`lint`, `gen`, `verify`, `build`, `targets`, `setup-sql`) ran on this
repository; `tests/unit/test_example_repo.py` runs them again on every test run, and sends the
release through `deploy` on the fake database of the unit tests. No statement of this repository
was sent to a database. The first deploy to a disposable Azure SQL Database is the first proof
that the engine accepts the SQL.

## What is in it

| Kind | Count | Files |
|---|---|---|
| Schema | 3 | `schema/schemas/`: `product`, `sales`, `stock` |
| Table | 12 | `schema/tables/` |
| Sequence | 2 | `schema/sequences/`: `sales.OrderNumberSeq`, `stock.MovementIdSeq` |
| Alias type | 1 | `schema/types/product.Sku.sql` |
| Table type | 1 | `schema/types/sales.OrderLineInput.sql` |
| Synonym | 1 | `schema/synonyms/dbo.LegacyOrder.sql` |
| View | 6 | `schema/views/`; `sales.vw_OrderTotals` is schema-bound |
| Function | 4 | `schema/functions/`: two scalar, one inline table-valued, one multi-statement |
| Procedure | 8 | `schema/procedures/` |
| Trigger | 2 | `schema/triggers/` |
| Migration | 1 | `migrations/0001__initial_schema.sql`, written by `azsqlcd gen` |

Where to look for a feature of a table file:

| Feature | File |
|---|---|
| IDENTITY, named DEFAULT, CHECK, UNIQUE, a foreign key to the same table | `schema/tables/product.Category.sql` |
| A column of an alias type, a CHECK that calls a function, a filtered index with INCLUDE | `schema/tables/product.Product.sql` |
| A DEFAULT that takes the next value of a sequence, an index with a descending key, a filtered index | `schema/tables/sales.Order.sql` |
| A persisted computed column, a composite primary key, ON DELETE CASCADE | `schema/tables/sales.OrderLine.sql` |
| A nonclustered primary key with a clustered index of another key | `schema/tables/sales.OrderStatusHistory.sql` |
| A computed column that is not persisted | `schema/tables/stock.StockLevel.sql` |
| A foreign key of two columns, an index option (`DATA_COMPRESSION`) | `schema/tables/stock.StockMovement.sql` |

Each table-class file is in the canonical form: the text that the tool writes for the object.
`azsqlcd lint` refuses a table file that says something the model does not hold (`NF000`).
The guide for the people who change a schema is `templates/db-repo/README.md` of the tool
repository.

## Commands

`TOOL` is the path of a checkout of the tool. Run the commands in a copy of this directory that is
a git repository of its own; `docs/quickstart.md` of the tool repository has every step.

```
azsqlcd() { uv run --project "$TOOL" --no-sync azsqlcd "$@"; }

azsqlcd lint                                                # file rules of the working tree
azsqlcd gen --name <short_name>                             # write the migration for a table change
azsqlcd gen --resum                                         # after a hand edit of a new migration
azsqlcd verify --base "$(git merge-base origin/main HEAD)"  # the check of the pull request
azsqlcd build --commit HEAD --out ../dist                   # the release of a commit of main
azsqlcd targets --bundle ../dist --digest <digest> --env dev
azsqlcd setup-sql --env dev --target demo-dev               # prints the script of the administrator
```

These commands use no database and no network. The database commands (`plan`, `deploy`, `drift`,
`export`, `baseline`, `resolve`) need real values in `azsqlcd.toml` and the `db` extra:
`docs/setup.md` and `docs/live-testing.md` of the tool repository.

## The first migration

`migrations/0001__initial_schema.sql` creates every table-class object of the repository on a
database that has none of them. Nobody wrote it by hand: on a base revision with no object file,
`azsqlcd gen --name initial_schema` wrote the file and the line in `migrations/migrations.sum`.
The order is fixed: schemas, types, sequences, tables without their foreign keys, indexes, foreign
keys, synonyms.

One line of the file is a directive:

```
-- azsqlcd:deploy-module [product].[fn_IsValidSku]
CREATE TABLE [product].[Product] (
```

The CHECK constraint `[CK_Product_Sku]` calls the function, so the function must exist before the
table. `gen` found the name in the expression and wrote the line; the deploy sends the function
file at that position, inside the transaction of the migration. Every other view, function,
procedure and trigger is sent after the migration, in dependency order, in the same transaction.

## Before a live run

- Replace every value of `azsqlcd.toml`: the tenant, the six client ids, the five servers and the
  database name. The values in the file are examples and name nothing.
- No schema file states `AUTHORIZATION`. The principal that runs the deploy then owns the schemas
  `product`, `sales` and `stock`. The tool does not change the owner of a schema later
  (`SCHEMA_OWNER_CHANGE`): a database administrator does.
- The tool manages no user, role or permission. Grant rights on the three schemas by hand.
- `.github/` is the copy of the template. The workflows run only in a repository of their own.
