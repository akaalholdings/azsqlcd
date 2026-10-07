"""The proof: replay(base, diff(base, head)) == head, and what a hand edit may and may not do.

`verify` trusts replay and nothing else. These tests hold the generator to the proof over every
fixture pair of tests/fixtures/pairs, in both directions and across the cases, and they show
that the proof fails when a migration does not give the head model.
"""

import itertools

import pytest

from azsqlcd.diff import GenRefused, diff
from azsqlcd.emit import emit_create_script, emit_operation
from azsqlcd.lex import split_batches
from azsqlcd.model import (
    AlterColumn,
    DropConstraint,
    DropTable,
    MaskColumn,
    Model,
    Operation,
    Table,
    UnmaskColumn,
)
from azsqlcd.parse import parse_statement
from azsqlcd.replay import ReplayError, replay
from fixtures.pairs import loader

ACCEPTED = [case for case in loader.CASES if not case.startswith("refuse_")]
ADD_PRIORITY = "ALTER TABLE [sales].[Order] ADD [Priority] tinyint"


def reversible(case: str) -> bool:
    try:
        diff(loader.model(case, "head"), loader.model(case, "base"))
    except GenRefused:
        return False
    return True


REVERSIBLE = [case for case in loader.CASES if reversible(case)]


def as_written(ops: list[Operation]) -> list[Operation]:
    """The operations as `verify` gets them: through the SQL text of the migration file."""
    return [parse_statement(emit_operation(op)) for op in ops]


def statements(*texts: str) -> list[Operation]:
    return [parse_statement(text) for text in texts]


# ------------------------------------------------------------------ the property
@pytest.mark.parametrize("case", ACCEPTED)
def test_the_generated_migration_replays_to_the_head_model(case: str):
    base, head = loader.model(case, "base"), loader.model(case, "head")
    ops = diff(base, head, loader.renames(case))
    assert replay(base, ops) == head
    assert replay(base, as_written(ops)) == head
    assert replay(base, ops).to_canonical_json() == head.to_canonical_json()


@pytest.mark.parametrize("case", ACCEPTED)
def test_the_recorded_sql_of_a_fixture_replays_to_the_head_model(case: str):
    text = (loader.ROOT / case / "expected.sql").read_text(encoding="utf-8")
    ops = [parse_statement(batch.text) for batch in split_batches(text)]
    assert replay(loader.model(case, "base"), ops) == loader.model(case, "head")


@pytest.mark.parametrize("case", REVERSIBLE)
def test_the_migration_back_replays_to_the_base_model(case: str):
    base, head = loader.model(case, "base"), loader.model(case, "head")
    back = diff(head, base)  # no rename is given: the way back drops and creates
    assert replay(head, back) == base
    assert replay(head, as_written(back)) == base


def test_the_way_back_is_proven_for_most_fixtures_and_refused_for_the_rest():
    assert len(REVERSIBLE) >= 25
    refused_back = set(loader.CASES) - set(REVERSIBLE)
    assert "add_columns_last" in REVERSIBLE and "rename_table" in REVERSIBLE
    # a column that comes back in the middle of its table is ORD001; the way there was a drop
    assert "drop_referenced_table_and_the_column_that_pointed_at_it" in refused_back


def test_every_pair_of_fixture_models_replays_to_its_target_or_is_refused():
    """Models of different cases share table names with other shapes: thousands of migrations that
    nobody wrote by hand. The empty model is one of them: create everything, drop everything."""
    models = [Model(), *(loader.model(case, side) for case in loader.CASES for side in ("base", "head"))]
    proven = refused = 0
    for source, target in itertools.product(models, repeat=2):
        try:
            ops = diff(source, target)
        except GenRefused as e:
            refused += 1
            for refusal in e.refusals:
                # the one thing in the fixtures that the fixed order cannot do; all else is a defect of diff
                assert refusal.code != "ORDER_BLOCKED" or "is the only column of the table" in refusal.message
            continue
        assert replay(source, as_written(ops)) == target
        proven += 1
    assert proven + refused == len(models) ** 2
    assert proven > 5000 and refused > 500


def keeps_a_history_table_in_a_schema_of_its_own(model: Model) -> bool:
    return any(
        isinstance(obj, Table)
        and obj.temporal is not None
        and f"SCHEMA:[{obj.temporal.history_schema}]" in model
        for obj in model.values()
    )


@pytest.mark.parametrize("case", loader.CASES)
def test_every_fixture_model_is_created_from_nothing_and_dropped_to_nothing(case: str):
    for side in ("base", "head"):
        model = loader.model(case, side)
        assert replay(Model(), as_written(diff(Model(), model))) == model
        if keeps_a_history_table_in_a_schema_of_its_own(model):
            # DROP TABLE leaves the history table and the engine then refuses DROP SCHEMA (seen live)
            with pytest.raises(GenRefused) as caught:
                diff(model, Model())
            assert {refusal.code for refusal in caught.value.refusals} == {"TEMPORAL_CHANGE"}
            continue
        assert replay(model, as_written(diff(model, Model()))) == Model()


@pytest.mark.parametrize("case", loader.CASES)
def test_the_create_script_of_the_emitter_passes_the_preconditions_and_gives_the_model(case: str):
    # emit_create_script is the order for an empty database; the replay checks that order
    for side in ("base", "head"):
        model = loader.model(case, side)
        assert replay(Model(), statements(*emit_create_script(model))) == model


@pytest.mark.parametrize(
    "case", [case for case in ACCEPTED if case != "no_change_when_only_letter_case_differs"]
)
def test_a_migration_that_lacks_one_statement_does_not_pass_the_proof(case: str):
    base, head = loader.model(case, "base"), loader.model(case, "head")
    ops = diff(base, head, loader.renames(case))
    assert ops
    for missing, op in enumerate(ops):
        try:
            result = replay(base, ops[:missing] + ops[missing + 1 :])
        except ReplayError:
            continue
        # The one statement that the proof does not need: a foreign key of a table that the same
        # migration drops. Of two tables that reference each other, one drop is enough.
        if isinstance(op, DropConstraint) and DropTable(op.schema, op.table) in ops:
            continue
        # The other one: DROP MASKED directly before the ALTER COLUMN of the same column. ALTER
        # COLUMN removes the mask by itself; diff writes DROP MASKED so that no mask goes without
        # the allow code UNMASK.
        after = ops[missing + 1] if missing + 1 < len(ops) else None
        if (
            isinstance(op, UnmaskColumn)
            and isinstance(after, AlterColumn)
            and (after.schema, after.table, after.column) == (op.schema, op.table, op.column)
        ):
            continue
        assert result != head, f"statement {missing + 1} of {case} makes no difference"
        assert result.diff_paths(head)


# ------------------------------------------------------------------ hand edits (design (b), "Human edit")
def test_add_not_null_split_into_add_null_then_alter_column_gives_the_same_model():
    base, head = loader.model("add_columns_last", "base"), loader.model("add_columns_last", "head")
    generated = diff(base, head)
    assert emit_operation(generated[1]) == (
        f"{ADD_PRIORITY} NOT NULL CONSTRAINT [DF_Order_Priority] DEFAULT ((0));"
    )
    by_hand = statements(
        "ALTER TABLE [sales].[Order] ADD [ShippedUtc] datetime2(3) NULL;",
        f"{ADD_PRIORITY} NULL CONSTRAINT [DF_Order_Priority] DEFAULT ((0));",
        # a data batch fills the column here; the model does not see it
        "ALTER TABLE [sales].[Order] ALTER COLUMN [Priority] tinyint NOT NULL;",
    )
    assert replay(base, by_hand) == head == replay(base, generated)


def test_drop_and_add_replaced_by_one_rename_gives_the_same_model():
    case = "no_rename_given_is_drop_and_add"
    base, head = loader.model(case, "base"), loader.model(case, "head")
    generated = diff(base, head)
    assert "DROP COLUMN [Stat]" in emit_operation(generated[1])
    by_hand = statements("EXEC sys.sp_rename N'[sales].[Order].[Stat]', N'Status', N'COLUMN';")
    assert replay(base, by_hand) == head == replay(base, generated)


def test_an_index_build_moved_out_of_the_migration_leaves_a_model_that_lacks_the_index():
    case = "new_schema_table_indexes_foreign_key"
    base, head = loader.model(case, "base"), loader.model(case, "head")
    ops = diff(base, head)
    moved = [op for op in ops if "[IX_Invoice_Customer]" in emit_operation(op)]
    kept = [op for op in ops if op not in moved]
    after_first = replay(base, kept)
    assert after_first.diff_paths(head) == [
        ("TABLE:[billing].[Invoice]", "indexes[ix_invoice_customer] (absent in self)")
    ]
    assert replay(after_first, moved) == head  # the second migration brings the head model


def test_a_wrong_hand_edit_fails_the_proof_and_the_difference_names_object_and_property():
    base, head = loader.model("add_columns_last", "base"), loader.model("add_columns_last", "head")
    forgot_the_alter = statements(
        "ALTER TABLE [sales].[Order] ADD [ShippedUtc] datetime2(3) NULL;",
        f"{ADD_PRIORITY} NULL CONSTRAINT [DF_Order_Priority] DEFAULT ((0));",
    )
    result = replay(base, forgot_the_alter)
    assert result != head
    assert result.diff_paths(head) == [("TABLE:[sales].[Order]", "columns[priority].nullable")]

    other_default = statements(
        "ALTER TABLE [sales].[Order] ADD [ShippedUtc] datetime2(3) NULL;",
        f"{ADD_PRIORITY} NOT NULL CONSTRAINT [DF_Order_Priority] DEFAULT ((1));",
    )
    assert replay(base, other_default).diff_paths(head) == [
        ("TABLE:[sales].[Order]", "columns[priority].default.expression")
    ]

    other_order = list(reversed(forgot_the_alter[:1] + statements(emit_operation(diff(base, head)[1]))))
    assert replay(base, other_order).diff_paths(head) == [("TABLE:[sales].[Order]", "columns (order)")]


def test_a_hand_edit_that_the_engine_would_refuse_fails_with_the_statement_the_object_and_the_reason():
    case = "rename_column"
    base = loader.model(case, "base")
    wrong_column = statements(
        "ALTER TABLE [sales].[Order] ADD [ClosedUtc] datetime2(3) NULL;",
        "EXEC sys.sp_rename N'[sales].[Order].[State]', N'Status', N'COLUMN';",
    )
    with pytest.raises(ReplayError) as caught:
        replay(base, wrong_column)
    error = caught.value
    assert (error.op_index, error.object_key) == (1, "TABLE:[sales].[Order]")
    assert error.message == "the table has no column [State]"

    drop_without_the_index = statements("ALTER TABLE [sales].[Order] DROP COLUMN [Stat];")
    with pytest.raises(ReplayError) as caught:
        replay(base, drop_without_the_index)
    assert caught.value.object_key == "TABLE:[sales].[Order]"
    assert caught.value.message == "DROP COLUMN [Stat] is blocked by index [IX_Order_Stat]"


def test_equal_models_in_other_letters_are_one_model_for_the_proof():
    case = "no_change_when_only_letter_case_differs"
    base, head = loader.model(case, "base"), loader.model(case, "head")
    assert diff(base, head) == [] and replay(base, []) == head
    assert isinstance(head, Model) and list(base) != list(head)  # the keys are written in other letters


# ------------------------------------------------------------------ system-versioned temporal tables
TEMPORAL_PAIRS = ["temporal_table_new", "temporal_table_add_column_check_and_index", "temporal_table_drop"]


def test_the_temporal_pairs_are_proven_and_two_of_them_have_a_proven_way_back():
    assert set(TEMPORAL_PAIRS) <= set(ACCEPTED)
    assert {"temporal_table_new", "temporal_table_drop"} <= set(REVERSIBLE)
    # the way back of a new column is a drop of a column in the middle of nothing: it is written too
    for case in TEMPORAL_PAIRS:
        base, head = loader.model(case, "base"), loader.model(case, "head")
        assert replay(base, as_written(diff(base, head))) == head


def test_a_changed_history_table_is_refused_by_the_generator_and_proven_when_written_by_hand():
    case = "refuse_temporal_history_table_change"
    base, head = loader.model(case, "base"), loader.model(case, "head")
    with pytest.raises(GenRefused):
        diff(base, head)
    by_hand = statements(
        "ALTER TABLE [dbo].[Team] SET (SYSTEM_VERSIONING = OFF)",
        "ALTER TABLE [dbo].[Team] SET (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [dbo].[Team_Archive], "
        "DATA_CONSISTENCY_CHECK = ON))",
    )
    assert replay(base, by_hand) == head
    # the same statements with the old history table name are not a proof of the head revision
    wrong = statements(
        "ALTER TABLE [dbo].[Team] SET (SYSTEM_VERSIONING = OFF)",
        "ALTER TABLE [dbo].[Team] SET (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [dbo].[Team_History]))",
    )
    assert replay(base, wrong).diff_paths(head) == [("TABLE:[dbo].[Team]", "temporal.history_table")]


def test_versioning_off_for_a_table_that_stays_has_no_proof_in_this_version():
    # The head revision has no period columns. Versioning off alone leaves them, and this version
    # reads no statement that drops a period: the proof fails and names the table. A known limit.
    case = "refuse_temporal_versioning_off_for_a_table_that_stays"
    base, head = loader.model(case, "base"), loader.model(case, "head")
    off = replay(base, statements("ALTER TABLE [dbo].[Team] SET (SYSTEM_VERSIONING = OFF)"))
    assert off != head and off.diff_paths(head)[0][0] == "TABLE:[dbo].[Team]"
    with pytest.raises(ReplayError, match="it is a period column"):
        replay(
            base,
            statements(
                "ALTER TABLE [dbo].[Team] SET (SYSTEM_VERSIONING = OFF)",
                "ALTER TABLE [dbo].[Team] DROP COLUMN [ValidTo]",
            ),
        )


# ------------------------------------------------------------------ dynamic data masking
MASK_CASES = [case for case in ACCEPTED if case.startswith("mask_")]


def test_the_pairs_hold_a_mask_that_comes_one_that_changes_one_that_goes_and_one_over_alter_column():
    assert MASK_CASES == [
        "mask_added_to_columns",
        "mask_changed",
        "mask_removed_needs_allow_unmask",
        "mask_written_again_after_alter_column",
    ]


@pytest.mark.parametrize("case", MASK_CASES)
def test_a_migration_of_masks_without_one_of_its_masking_statements_leaves_another_mask_than_the_head(
    case: str,
):
    base, head = loader.model(case, "base"), loader.model(case, "head")
    ops = diff(base, head, loader.renames(case))
    assert replay(base, ops) == head
    masks = [n for n, op in enumerate(ops) if isinstance(op, MaskColumn)]
    assert masks or any(isinstance(op, UnmaskColumn) for op in ops)
    for n in masks:
        result = replay(base, ops[:n] + ops[n + 1 :])
        column = ops[n].column.casefold()
        assert result.diff_paths(head) == [("TABLE:[sales].[Buyer]", f"columns[{column}].masked")]


def test_the_mask_of_the_head_is_what_stops_a_hand_written_alter_column_that_removes_a_mask():
    # the engine removes the mask with ALTER COLUMN; the proof shows it because the replay does too
    case = "mask_written_again_after_alter_column"
    base, head = loader.model(case, "base"), loader.model(case, "head")
    hand = [op for op in diff(base, head) if not (isinstance(op, MaskColumn) and op.column == "Mail")]

    result = replay(base, hand)

    assert result != head
    assert result.diff_paths(head) == [("TABLE:[sales].[Buyer]", "columns[mail].masked")]
