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
