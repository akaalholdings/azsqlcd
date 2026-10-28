"""Set-aside migrations (set_aside.py): parked from the facts of the state tables, and independence.

A migration that is set aside in an environment does not run there. What runs past it must be
independent of it: it touches nothing that the set-aside migration touches or names, and it names
nothing that the set-aside migration touches. The rule is the same for every reason.
"""

import pytest

from azsqlcd import chain, set_aside
from azsqlcd.errors import Exit, ToolError
from azsqlcd.modules import read_module
from azsqlcd.state import FailedRun, StepRow


def migration(file: str, *batches: str) -> chain.Migration:
    header = f"-- azsqlcd:migration {file.removesuffix('.sql')}\n-- azsqlcd:mode tx\n"
    return chain.parse_migration(header + "\nGO\n".join(batches) + "\n", file)


M2 = migration("0003__order_priority.sql", "ALTER TABLE [sales].[Order] ADD [Priority] tinyint NOT NULL;")
PARKED_M2 = set_aside.SetAside(M2.file, set_aside.PARKED, "run 4, BATCH_FAILED")


def failed(run_id: int, step: str, reason: str | None = "BATCH_FAILED", status: str = "failed") -> FailedRun:
    return FailedRun(run_id, 3, status, step, reason)


def resolve_step(run_id: int, note: str) -> StepRow:
    return StepRow(run_id * 10, run_id, "resolve", None, None, "ok", note)


# ------------------------------------------------------------------ parked
def test_a_run_that_failed_on_a_batch_of_a_migration_parks_that_migration():
    found = set_aside.parked((failed(4, f"{M2.file}#1"),), ())

    assert found.active == {M2.file: PARKED_M2} and found.released == frozenset()
    assert PARKED_M2.note == f"parked: {M2.file} (run 4, BATCH_FAILED)"


@pytest.mark.parametrize(
    ("step", "reason", "status"),
    [
        ("refresh:VIEW:[sales].[vw_OpenOrders]", "DEPENDANT_BROKEN", "failed"),  # a module, not the batch
        ("0003__order_priority.sql@4:deploy:VIEW:[sales].[v]", "BATCH_FAILED", "failed"),  # a directive
        ("commit", "BATCH_FAILED", "failed"),
        ("0003__order_priority.sql#1", "LOCK_TIMEOUT", "failed"),  # start again: no park
        ("0003__order_priority.sql#1", "CONNECTION_LOST_NONTX", "unknown"),  # nothing known of the batch
        ("0003__order_priority.sql#1", None, "failed"),
    ],
)
def test_only_an_engine_error_of_the_batch_itself_parks(step, reason, status):
    assert set_aside.parked((failed(4, step, reason, status),), ()) == set_aside.NOTHING


def test_a_nontx_batch_that_failed_parks_its_migration():
    found = set_aside.parked((failed(4, f"{M2.file}#1", "NONTX_FAILED", "unknown"),), ())
    assert list(found.active) == [M2.file]


def test_an_unpark_step_after_the_run_ends_the_park_and_a_later_failure_parks_again():
    unpark = resolve_step(5, set_aside.unpark_note(M2.file))

    ended = set_aside.parked((failed(4, f"{M2.file}#1"),), (unpark,))
    again = set_aside.parked((failed(4, f"{M2.file}#1"), failed(6, f"{M2.file}#1")), (unpark,))

    assert ended.active == {} and ended.released == {M2.file}
    assert again.active[M2.file].why == "run 6, BATCH_FAILED"
    # an operator's reason text is no unpark note: the note is exact
    typed = resolve_step(5, f"mark-applied: {set_aside.unpark_note(M2.file)}")
    assert list(set_aside.parked((failed(4, f"{M2.file}#1"),), (typed,)).active) == [M2.file]


# ------------------------------------------------------------------ independence
def test_a_migration_on_another_table_runs_past_a_set_aside_one():
    other = migration(
        "0004__customer_email.sql", "ALTER TABLE [sales].[Customer] ADD [Email] nvarchar(200) NULL;"
    )
    assert set_aside.check((other,), ((PARKED_M2, M2),), ()) == []


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE [sales].[Order] ADD [Note] nvarchar(200) NULL;",  # touches what M2 touches
        # names what M2 touches: a key to the table
        "CREATE TABLE [sales].[Line] ([LineId] int NOT NULL, [OrderId] int NOT NULL, CONSTRAINT [PK_Line] "
        "PRIMARY KEY CLUSTERED ([LineId]), CONSTRAINT [FK_Line_Order] FOREIGN KEY ([OrderId]) REFERENCES "
        "[sales].[Order] ([OrderId]));",
    ],
)
def test_a_migration_that_shares_an_object_with_a_set_aside_one_is_refused(statement):
    other = migration("0004__order_more.sql", statement)

    with pytest.raises(ToolError) as e:
        set_aside.check((other,), ((PARKED_M2, M2),), ())

    assert (e.value.exit_code, e.value.reason_code) == (Exit.REFUSED, "SET_ASIDE_DEPENDENCY")
    assert e.value.detail == {
        "blocked": other.file,
        "set_aside": M2.file,
        "reason": "parked",
        "object": "[sales].[order]",
    }
    assert (
        f"azsqlcd resolve --unpark {M2.file}" in e.value.message and "run 4, BATCH_FAILED" in e.value.message
    )
    # after the unpark it runs at its place among the pending ones, which can be after the blocked one
    assert "at its place in the chain" in e.value.message and "runs first" not in e.value.message


def test_a_set_aside_migration_that_names_what_a_pending_one_touches_is_a_dependency_too():
    # the set-aside migration adds a key to [sales].[Customer]; the pending one changes that table
    keyed = migration(
        "0003__order_customer_key.sql",
        "ALTER TABLE [sales].[Order] ADD CONSTRAINT [FK_Order_Customer2] FOREIGN KEY ([CustomerId]) "
        "REFERENCES [sales].[Customer] ([CustomerId]);",
    )
    other = migration(
        "0004__customer_email.sql", "ALTER TABLE [sales].[Customer] ADD [Email] nvarchar(200) NULL;"
    )
    aside = set_aside.SetAside(keyed.file, set_aside.PARKED, "run 4, BATCH_FAILED")

    with pytest.raises(ToolError) as e:
        set_aside.check((other,), ((aside, keyed),), ())
    assert e.value.detail["object"] == "[sales].[customer]"


def test_a_raw_batch_with_no_shared_name_runs_with_a_note_and_one_with_a_shared_name_is_refused():
    raw = (
        "-- azsqlcd:raw TABLE:[audit].[Log] reason: not modelled\nALTER TABLE [audit].[Log] ADD [x] int NULL;"
    )
    alone = migration("0004__audit_log.sql", raw)

    assert set_aside.check((alone,), ((PARKED_M2, M2),), ()) == [
        "independence not proven for a raw batch: 0004__audit_log.sql"
    ]
    shared = migration(
        "0004__audit_log.sql",
        raw.replace("ADD [x] int NULL;", "ADD [x] int NULL; SELECT 1 FROM [sales].[Order];"),
    )
    with pytest.raises(ToolError, match="SET_ASIDE_DEPENDENCY|depends") as e:
        set_aside.check((shared,), ((PARKED_M2, M2),), ())
    assert e.value.reason_code == "SET_ASIDE_DEPENDENCY"


def test_a_module_whose_text_names_what_a_set_aside_migration_touches_is_refused():
    view = read_module(
        "schema/views/sales.vw_OpenOrders.sql",
        b"CREATE OR ALTER VIEW [sales].[vw_OpenOrders] AS\nSELECT [OrderId] FROM [sales].[Order];\n",
    )
    other = read_module(
        "schema/views/sales.vw_Customers.sql",
        b"CREATE OR ALTER VIEW [sales].[vw_Customers] AS\nSELECT [CustomerId] FROM [sales].[Customer];\n",
    )

    assert set_aside.check((), ((PARKED_M2, M2),), (other,)) == []
    with pytest.raises(ToolError) as e:
        set_aside.check((), ((PARKED_M2, M2),), (view,))
    assert e.value.detail["blocked"] == "VIEW:[sales].[vw_OpenOrders]"


def test_every_reason_has_a_way_out():
    assert set(set_aside.REASONS) == set(set_aside.WAY_OUT)


def test_a_park_is_in_force_until_its_migration_is_applied_or_withdrawn():
    """drift and resolve --unpark list only the parks that still hold."""
    found = set_aside.parked((failed(4, f"{M2.file}#1"),), ())
    chain_line = f"azsqlcd-sum 1\n{M2.file} sha256:{'0' * 64} tx\n"
    files = {chain.SUM_PATH: chain_line.encode()}

    assert set_aside.in_force(found, (), files) == (PARKED_M2,)
    assert set_aside.in_force(found, (), {}) == (PARKED_M2,)
    applied = StepRow(50, 5, "migration", M2.file, "0" * 64, "ok", None)
    assert set_aside.in_force(found, (applied,), files) == ()
    withdrawn = {chain.SUM_PATH: chain_line.replace(" tx", " tx withdrawn").encode()}
    assert set_aside.in_force(found, (), withdrawn) == ()


# ------------------------------------------------------------------ review: renames and one-part names
RENAMED = migration(
    "0003__order_reference.sql", "EXEC sys.sp_rename N'[sales].[Order].[Reference]', N'Ref', N'COLUMN';"
)
PARKED_RENAME = set_aside.SetAside(RENAMED.file, set_aside.PARKED, "run 4, BATCH_FAILED")


def test_a_parked_column_rename_touches_its_table_also_without_the_model():
    """The names of sp_rename are in string literals: the token scan of the text does not see them.
    The rename statement says its table, so a change of that table and a module that names it wait."""
    note = migration("0004__order_note.sql", "ALTER TABLE [sales].[Order] ADD [Note] nvarchar(200) NULL;")
    finder = read_module(
        "schema/procedures/sales.usp_FindOrder.sql",
        b"CREATE OR ALTER PROCEDURE [sales].[usp_FindOrder] AS\nSELECT [OrderId] FROM [sales].[Order];\n",
    )

    for pending, sent in (((note,), ()), ((), (finder,))):
        with pytest.raises(ToolError) as e:
            set_aside.check(pending, ((PARKED_RENAME, RENAMED),), sent)
        assert (e.value.reason_code, e.value.detail["object"]) == ("SET_ASIDE_DEPENDENCY", "[sales].[order]")


def test_a_parked_table_rename_touches_the_old_and_the_new_name():
    renamed = migration(
        "0003__order_rename.sql", "EXEC sys.sp_rename N'[sales].[Order]', N'Orders', N'OBJECT';"
    )
    found = set_aside.footprint(renamed)
    assert {("sales", "order"), ("sales", "orders")} <= found.touched


def test_with_the_model_a_parked_rename_also_touches_the_tables_with_a_key_to_it():
    from azsqlcd.model import Model
    from azsqlcd.parse import parse_object_file

    tables = (
        (
            "Order",
            "CREATE TABLE [sales].[Order] ([OrderId] int NOT NULL, [Reference] nvarchar(50) NULL, "
            "CONSTRAINT [PK_Order] PRIMARY KEY CLUSTERED ([OrderId]));\n",
        ),
        (
            "Line",
            "CREATE TABLE [sales].[Line] ([LineId] int NOT NULL, [OrderId] int NOT NULL, "
            "CONSTRAINT [PK_Line] PRIMARY KEY CLUSTERED ([LineId]), CONSTRAINT [FK_Line_Order] "
            "FOREIGN KEY ([OrderId]) REFERENCES [sales].[Order] ([OrderId]));\n",
        ),
    )
    model = Model(
        [obj for name, text in tables for obj in parse_object_file(text, f"schema/tables/sales.{name}.sql")]
    )
    renamed = migration(
        "0003__order_id.sql", "EXEC sys.sp_rename N'[sales].[Order].[OrderId]', N'Id', N'COLUMN';"
    )

    assert set_aside.footprint(renamed, model).touched == {("sales", "order"), ("sales", "line")}
    assert set_aside.footprint(renamed, model).unproven == ()


def test_a_module_that_names_a_set_aside_table_without_its_schema_is_refused():
    """A one-part name is dbo, as for the dependency scan of modules (A17)."""
    orders = migration("0003__orders_rank.sql", "ALTER TABLE [dbo].[Orders] ADD [Rank] tinyint NOT NULL;")
    aside = set_aside.SetAside(orders.file, set_aside.PARKED, "run 4, BATCH_FAILED")
    view = read_module(
        "schema/views/dbo.vw_Orders.sql",
        b"CREATE OR ALTER VIEW [dbo].[vw_Orders] AS\nSELECT [OrderId] FROM Orders;\n",
    )

    with pytest.raises(ToolError) as e:
        set_aside.check((), ((aside, orders),), (view,))
    assert (e.value.reason_code, e.value.detail["object"]) == ("SET_ASIDE_DEPENDENCY", "[dbo].[orders]")
