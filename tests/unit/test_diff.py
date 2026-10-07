"""The generator: diff(), what it refuses, the classifier and the touched objects.

Fixture pairs are in tests/fixtures/pairs (README.md there). expected.sql is the SQL that a
reviewer of a generated migration reads; expected_refusals.txt is what diff refuses. After an
intended change of behaviour, record them again and read the diff:
    AZSQLCD_UPDATE_GOLDEN=1 .venv/bin/python -m pytest tests/unit/test_diff.py -q
"""

import itertools
import os
import typing
from collections.abc import Sequence

import pytest

from azsqlcd import model as masking
from azsqlcd.diff import (
    REFUSALS,
    TEMPORAL_HINTS,
    GenRefused,
    RenameSpec,
    classify,
    diff,
    parse_rename_spec,
    touched_objects,
)
from azsqlcd.emit import emit_operation, token_roundtrip_differences
from azsqlcd.lex import split_batches
from azsqlcd.model import (
    AddColumn,
    AddConstraint,
    AlterColumn,
    AlterColumnProperty,
    AlterSequence,
    Check,
    Column,
    Constraint,
    CreateIndex,
    CreateSchema,
    CreateSequence,
    CreateSynonym,
    CreateTable,
    CreateType,
    DefaultConstraint,
    DropColumn,
    DropConstraint,
    DropIndex,
    DropSchema,
    DropSequence,
    DropSynonym,
    DropTable,
    DropType,
    Expression,
    ForeignKey,
    Index,
    Model,
    Operation,
    PrimaryKey,
    RebuildTable,
    Rename,
    SetSystemVersioning,
    Table,
    Unique,
    fold,
)
from azsqlcd.parse import ParseError, parse_statement
from azsqlcd.replay import ReplayError, apply, replay
from fixtures.pairs import loader

REFUSED = [case for case in loader.CASES if case.startswith("refuse_")]
ACCEPTED = [case for case in loader.CASES if case not in REFUSED]


def models(case: str) -> tuple[Model, Model, list[RenameSpec]]:
    return loader.model(case, "base"), loader.model(case, "head"), loader.renames(case)


def model_of(*statements: str) -> Model:
    """A model from CREATE statements, as written: nothing checks that the objects fit together."""
    objects = []
    for text in statements:
        match parse_statement(text):
            case CreateTable(table=obj) | CreateSchema(schema=obj) | CreateType(type=obj):
                objects.append(obj)
            case CreateSequence(sequence=obj) | CreateSynonym(synonym=obj):
                objects.append(obj)
            case other:
                raise AssertionError(f"not a CREATE of an object: {other!r}")
    return Model(objects)


def refusals_of(base: Model, head: Model, renames: Sequence[RenameSpec] = ()) -> list[tuple[str, str]]:
    with pytest.raises(GenRefused) as caught:
        diff(base, head, renames)
    return [(r.code, r.object_key) for r in caught.value.refusals]


def recorded_operations(case: str) -> list[Operation]:
    text = (loader.ROOT / case / "expected.sql").read_text(encoding="utf-8")
    return [parse_statement(batch.text) for batch in split_batches(text)]


# ------------------------------------------------------------------ the fixture pairs
def test_the_fixture_set_is_large_enough_to_cover_the_rule_set():
    assert len(loader.CASES) >= 25
    assert len(ACCEPTED) >= 20 and len(REFUSED) >= 10


@pytest.mark.parametrize("case", loader.CASES)
def test_fixture_object_files_are_in_normal_form(case: str):
    for side in ("base", "head"):
        files = loader.object_files(case, side)
        assert files, f"{case}/{side} has no object file"
        for path, text in files:
            assert token_roundtrip_differences(text, path) == [], f"{case}/{side}/{path}"


@pytest.mark.parametrize("case", loader.CASES)
def test_diff_gives_the_recorded_statements_or_the_recorded_refusals(case: str):
    base, head, renames = models(case)
    sql, refused = loader.ROOT / case / "expected.sql", loader.ROOT / case / "expected_refusals.txt"
    try:
        result = "".join(emit_operation(op) + "\nGO\n" for op in diff(base, head, renames))
        golden, other = sql, refused
    except GenRefused as e:
        result = "".join(f"{r.code} {r.object_key}\n" for r in e.refusals)
        golden, other = refused, sql
    if os.environ.get("AZSQLCD_UPDATE_GOLDEN"):
        golden.write_text(result, encoding="utf-8", newline="\n")
        other.unlink(missing_ok=True)
    assert not other.exists(), "a case has expected.sql or expected_refusals.txt, never both"
    assert (golden is refused) == case.startswith("refuse_"), "the refusal cases are named refuse_*"
    assert result == golden.read_text(encoding="utf-8")


def test_every_refusal_code_has_a_fixture():
    recorded = {
        line.split(" ", 1)[0]
        for case in REFUSED
        for line in (loader.ROOT / case / "expected_refusals.txt").read_text(encoding="utf-8").splitlines()
    }
    assert recorded == set(REFUSALS)


def test_every_operation_class_is_generated_by_some_fixture():
    generated = {type(op) for case in ACCEPTED for op in diff(*models(case))}
    assert generated == set(typing.get_args(Operation))


# The order of design (b), generation step 2, written once more and independently of diff.py:
# renames; drop FKs; drop CHECK/DEFAULT, indexes, PK/UNIQUE; drop columns; drop tables; create
# schemas, types, sequences; create tables; add columns; alter columns; add PK/UNIQUE; create
# indexes; add CHECK/DEFAULT; add FKs; synonyms; drop sequences, types. DROP SCHEMA is last.
def design_step(op: Operation, before: Model) -> int:
    match op:
        case Rename():
            return 1
        case DropConstraint():
            table = before[f"TABLE:[{op.schema}].[{op.table}]"]
            assert isinstance(table, Table)
            dropped = table.constraint(op.name)
            return 2 if isinstance(dropped, ForeignKey) else 3  # 3: CHECK, PK, UNIQUE or a DEFAULT
        case DropIndex():
            return 3
        case DropColumn():
            return 4
        case DropTable() | SetSystemVersioning():
            return 5  # versioning goes off directly before the DROP TABLE of a temporal table
        case CreateSchema() | CreateType() | CreateSequence() | AlterSequence():
            return 6
        case CreateTable():
            return 7
        case AddColumn():
            return 8
        case AlterColumnProperty() | RebuildTable():
            return 9  # DROP of a property before the ALTER of the column, ADD after it; then REBUILD
        case AlterColumn() | masking.MaskColumn() | masking.UnmaskColumn():
            return 9  # ADD MASKED and DROP MASKED are ALTER COLUMN statements, around the ALTER of the column
        case AddConstraint():
            constraint = op.constraint
            if isinstance(constraint, PrimaryKey | Unique):
                return 10
            return 12 if isinstance(constraint, Check | DefaultConstraint) else 13
        case CreateIndex():
            return 11
        case DropSynonym() | CreateSynonym():
            return 14
        case DropSequence() | DropType():
            return 15
        case DropSchema():
            return 16


@pytest.mark.parametrize("case", ACCEPTED)
def test_statements_come_in_the_fixed_order_of_the_design(case: str):
    base, head, renames = models(case)
    steps = []
    for op in diff(base, head, renames):
        steps.append(design_step(op, base))
        base = apply(base, op)
    assert steps == sorted(steps)


def test_one_fixture_has_a_statement_in_every_step_of_the_fixed_order():
    base, head, renames = models("every_step_in_the_fixed_order")
    steps = set()
    for op in diff(base, head, renames):
        steps.add(design_step(op, base))
        base = apply(base, op)
    assert steps == set(range(1, 17))


def test_inside_a_step_the_statements_are_in_key_order_then_name_order():
    ops = diff(*models("every_step_in_the_fixed_order"))
    dropped_keys = [(op.schema, op.table, op.name) for op in ops if isinstance(op, DropConstraint)][:3]
    assert dropped_keys == [
        ("old", "Part", "FK_Part_Thing"),
        ("old", "Thing", "FK_Thing_Part"),
        ("sales", "Order", "FK_Order_Customer"),
    ]
    indexes = [(op.table, op.index.name) for op in ops if isinstance(op, CreateIndex)]
    assert indexes == [("Invoice", "IX_Invoice_Customer"), ("Customer", "CIX_Customer_Name")]


def test_the_statements_do_not_depend_on_the_order_in_which_a_table_lists_its_parts():
    columns = "[A] int NOT NULL, [B] int NOT NULL, [C] int NOT NULL"
    parts = [
        "CONSTRAINT [PK_K] PRIMARY KEY NONCLUSTERED ([A])",
        "CONSTRAINT [AK_K] UNIQUE NONCLUSTERED ([B])",
        "CONSTRAINT [CK_K_2] CHECK ([C] > 0)",
        "CONSTRAINT [CK_K_1] CHECK ([B] > 0)",
        "INDEX [IX_K_2] NONCLUSTERED ([C])",
        "INDEX [IX_K_1] NONCLUSTERED ([B])",
        "INDEX [CIX_K] CLUSTERED ([C], [A])",
    ]
    empty = model_of(SCHEMA, f"CREATE TABLE [s].[K] ({columns})")
    listed = model_of(SCHEMA, f"CREATE TABLE [s].[K] ({columns}, {', '.join(parts)})")
    other_way = model_of(SCHEMA, f"CREATE TABLE [s].[K] ({columns}, {', '.join(reversed(parts))})")
    assert listed == other_way
    added = [emit_operation(op) for op in diff(empty, listed)]
    assert added == [emit_operation(op) for op in diff(empty, other_way)]
    assert added == [
        "ALTER TABLE [s].[K] ADD CONSTRAINT [AK_K] UNIQUE NONCLUSTERED ([B]);",
        "ALTER TABLE [s].[K] ADD CONSTRAINT [PK_K] PRIMARY KEY NONCLUSTERED ([A]);",
        "CREATE CLUSTERED INDEX [CIX_K] ON [s].[K] ([C], [A]);",
        "CREATE NONCLUSTERED INDEX [IX_K_1] ON [s].[K] ([B]);",
        "CREATE NONCLUSTERED INDEX [IX_K_2] ON [s].[K] ([C]);",
        "ALTER TABLE [s].[K] ADD CONSTRAINT [CK_K_1] CHECK ([B] > 0);",
        "ALTER TABLE [s].[K] ADD CONSTRAINT [CK_K_2] CHECK ([C] > 0);",
    ]
    dropped = [emit_operation(op) for op in diff(listed, empty)]
    assert dropped == [emit_operation(op) for op in diff(other_way, empty)]
    assert dropped == [
        "ALTER TABLE [s].[K] DROP CONSTRAINT [CK_K_1];",
        "ALTER TABLE [s].[K] DROP CONSTRAINT [CK_K_2];",
        "DROP INDEX [CIX_K] ON [s].[K];",
        "DROP INDEX [IX_K_1] ON [s].[K];",
        "DROP INDEX [IX_K_2] ON [s].[K];",
        "ALTER TABLE [s].[K] DROP CONSTRAINT [AK_K];",
        "ALTER TABLE [s].[K] DROP CONSTRAINT [PK_K];",
    ]


def test_the_clustered_key_and_the_clustered_index_of_a_table_come_before_its_others():
    columns = "[A] int NOT NULL, [B] int NOT NULL"
    keys = "CONSTRAINT [AK_K] UNIQUE NONCLUSTERED ([B]), CONSTRAINT [PK_K] PRIMARY KEY CLUSTERED ([A])"
    indexes = "INDEX [A_K] NONCLUSTERED ([B]), INDEX [Z_K] CLUSTERED ([A])"
    empty = model_of(SCHEMA, f"CREATE TABLE [s].[K] ({columns})")
    keyed = diff(empty, model_of(SCHEMA, f"CREATE TABLE [s].[K] ({columns}, {keys})"))
    assert [op.constraint.name for op in keyed if isinstance(op, AddConstraint)] == ["PK_K", "AK_K"]
    indexed = model_of(SCHEMA, f"CREATE TABLE [s].[K] ({columns}, {indexes})")
    for base in (empty, model_of(SCHEMA)):  # an index of a table that exists, and of a new table
        created = [op.index.name for op in diff(base, indexed) if isinstance(op, CreateIndex)]
        assert created == ["Z_K", "A_K"]


def test_equal_models_need_no_statement():
    for case in loader.CASES:
        for side in ("base", "head"):
            model = loader.model(case, side)
            assert diff(model, model) == []


# ------------------------------------------------------------------ renames are never inferred
def test_a_rename_happens_only_when_it_is_given():
    base, head, _ = models("rename_column")
    given = diff(base, head, [RenameSpec("column", ("sales", "Order", "Stat"), "Status")])
    assert given == [Rename("column", ("sales", "Order", "Stat"), "Status")]
    inferred_nothing = diff(base, head)
    assert not any(isinstance(op, Rename) for op in inferred_nothing)
    assert DropColumn("sales", "Order", "Stat") in inferred_nothing
    assert [op.column.name for op in inferred_nothing if isinstance(op, AddColumn)] == ["Status"]


def test_renames_are_applied_in_the_order_given_and_the_rest_is_compared_after_them():
    base, head, renames = models("rename_table_and_column_then_change")
    ops = diff(base, head, renames)
    assert ops[:2] == [
        Rename("table", ("sales", "Ordr"), "Order"),
        Rename("column", ("sales", "Order", "Note"), "Remark"),
    ]
    assert [type(op) for op in ops[2:]] == [AddColumn, AlterColumn, CreateIndex]


@pytest.mark.parametrize(
    "spec",
    [
        RenameSpec("table", ("sales", "Order"), "Order"),
        RenameSpec("column", ("sales", "Order", "OrderId"), "OrderId"),
        RenameSpec("index", ("sales", "Order", "PK_Order"), "PK_Order"),
        RenameSpec("constraint", ("sales", "PK_Order"), "PK_Order"),
    ],
    ids=lambda spec: spec.kind,
)
def test_a_rename_to_the_name_that_the_object_has_is_refused_and_writes_no_sp_rename(spec: RenameSpec):
    # the engine refuses sp_rename to the same name; touched_objects could not resolve what follows it
    model = model_of(
        "CREATE SCHEMA [sales]",
        "CREATE TABLE [sales].[Order] ([OrderId] int NOT NULL, "
        "CONSTRAINT [PK_Order] PRIMARY KEY CLUSTERED ([OrderId]))",
    )
    with pytest.raises(GenRefused) as caught:
        diff(model, model, [spec])
    (refusal,) = caught.value.refusals
    assert refusal.code == "RENAME_INVALID" and "the new name is the old name" in refusal.message


def test_a_rename_of_a_column_before_the_rename_of_its_table_is_refused_with_the_reason():
    base, head, renames = models("rename_table_and_column_then_change")
    column_first = [RenameSpec("column", ("sales", "Ordr", "Note"), "Remark"), renames[0]]
    with pytest.raises(GenRefused) as caught:
        diff(base, head, column_first)
    (refusal,) = caught.value.refusals
    assert (refusal.code, refusal.object_key) == ("RENAME_INVALID", "TABLE:[sales].[Ordr]")
    assert "the head revision has no table [sales].[Ordr]" in refusal.message


@pytest.mark.parametrize(
    ("spec", "key", "says"),
    [
        (RenameSpec("column", ("sales", "Order", "Stat"), "Note"), "TABLE:[sales].[Order]", "[Note] already"),
        (
            RenameSpec("column", ("sales", "Order", "Stat"), "Statsu"),
            "TABLE:[sales].[Order]",
            "no column [Statsu]",
        ),
        (RenameSpec("table", ("sales", "Order"), "Customer"), "TABLE:[sales].[Order]", "[Customer] is taken"),
        (RenameSpec("table", ("sales", "Ordr"), "Order"), "TABLE:[sales].[Ordr]", "does not exist"),
        (
            RenameSpec("index", ("sales", "Order", "IX_None"), "IX_Order_Stat"),
            "TABLE:[sales].[Order]",
            "no index",
        ),
        (
            RenameSpec("index", ("sales", "Order", "IX_Order_Stat"), "IX_New"),
            "TABLE:[sales].[Order]",
            "no index",
        ),
        (RenameSpec("constraint", ("sales", "PK_None"), "PK_Order"), "SCHEMA:[sales]", "no constraint"),
        (
            RenameSpec("constraint", ("sales", "PK_Order"), "PK_New"),
            "SCHEMA:[sales]",
            "no constraint [PK_New]",
        ),
    ],
    ids=lambda v: f"{v.kind}:{'.'.join(v.old)}={v.new_name}" if isinstance(v, RenameSpec) else None,
)
def test_a_rename_that_does_not_fit_base_and_head_is_refused(spec: RenameSpec, key: str, says: str):
    base, head, _ = models("rename_column")
    with pytest.raises(GenRefused) as caught:
        diff(base, head, [spec])
    (refusal,) = caught.value.refusals  # the comparison stops: no ORD001 noise from the rename that failed
    assert (refusal.code, refusal.object_key) == ("RENAME_INVALID", key)
    assert says in refusal.message


@pytest.mark.parametrize(
    ("text", "spec"),
    [
        (
            "column:[sales].[Order].[Stat]=[Status]",
            RenameSpec("column", ("sales", "Order", "Stat"), "Status"),
        ),
        (
            "index:[sales].[Order].[ix1]=[IX_Order]",
            RenameSpec("index", ("sales", "Order", "ix1"), "IX_Order"),
        ),
        ("table:[sales].[Ordr]=[Order]", RenameSpec("table", ("sales", "Ordr"), "Order")),
        (
            "constraint:[sales].[DF__1]=[DF_Order_Stat]",
            RenameSpec("constraint", ("sales", "DF__1"), "DF_Order_Stat"),
        ),
        ("COLUMN: sales.T.a = b", RenameSpec("column", ("sales", "T", "a"), "b")),
        ('column:"sales".[T].[a]]b]=[c d]', RenameSpec("column", ("sales", "T", "a]b"), "c d")),
        ("column:[a.b].[T=1].[x:y]=[z]", RenameSpec("column", ("a.b", "T=1", "x:y"), "z")),
    ],
)
def test_parse_rename_spec_reads_the_command_line_form(text: str, spec: RenameSpec):
    assert parse_rename_spec(text) == spec


@pytest.mark.parametrize(
    "text",
    [
        "",
        "[sales].[Order].[Stat]=[Status]",  # no kind
        "view:[sales].[v]=[w]",  # not a kind
        "object:[sales].[Order]=[Orders]",  # the parser's kind, not a command-line kind
        "column:[sales].[Order]=[Status]",  # a column needs the table
        "table:[sales].[Order].[Stat]=[Status]",  # a table has no third part
        "table:[Order]=[Orders]",  # no schema
        "column:[sales].[Order].[Stat]",  # no new name
        "column:[sales].[Order].[Stat]=",
        "column:[sales].[Order].[Stat]=[dbo].[Status]",  # the new name is one name
        "column:[sales].[Order].=[Status]",
        "column:[sales]..[Order].[Stat]=[Status]",
        "column:[sales].[Order].[Stat]=[Status] extra",
        "column:[sales].[Order].[Stat]=N'Status'",
        "column:[sales].[Order].[Stat=[Status]",  # the bracket does not close before the '='
        "column:[sales].[Order].[Stat]=[Status",  # the bracket does not close at all
    ],
)
def test_parse_rename_spec_refuses_every_other_text(text: str):
    with pytest.raises(ValueError, match="not a rename"):
        parse_rename_spec(text)


def test_a_rename_spec_made_in_code_is_held_to_the_same_shape():
    with pytest.raises(ValueError):
        RenameSpec("column", ("sales", "Order"), "Status")
    with pytest.raises(ValueError):
        RenameSpec("table", ("sales", "Order"), "")


# ------------------------------------------------------------------ refusals
@pytest.mark.parametrize("case", REFUSED)
def test_a_refusal_names_an_object_of_the_pair_and_carries_the_hand_write_hint(case: str):
    base, head, renames = models(case)
    with pytest.raises(GenRefused) as caught:
        diff(base, head, renames)
    assert caught.value.refusals
    for refusal in caught.value.refusals:
        assert refusal.object_key in base or refusal.object_key in head
        assert refusal.message
        if refusal.code == "TEMPORAL_CHANGE":  # the hint of the reason; REFUSALS holds the general text
            assert refusal.hint in TEMPORAL_HINTS.values()
        else:
            assert refusal.hint == REFUSALS[refusal.code]
        assert refusal.code in str(caught.value) and refusal.object_key in str(caught.value)


def test_every_refusal_of_one_comparison_is_collected():
    codes = [code for code, _ in refusals_of(*models("refuse_collects_every_refusal"))]
    assert len(codes) == 10
    assert set(codes) == set(REFUSALS) - {"RENAME_INVALID", "ORDER_BLOCKED"}


def test_a_refusal_message_holds_names_and_never_expression_text():
    with pytest.raises(GenRefused) as caught:
        diff(*models("refuse_computed_column_change"))
    (refusal,) = caught.value.refusals
    assert "[Total]" in refusal.message
    assert "Qty" not in refusal.message and "*" not in refusal.message


SCHEMA = "CREATE SCHEMA [s]"
PARENT = "CREATE TABLE [s].[P] ([Id] int NOT NULL, CONSTRAINT [PK_P] PRIMARY KEY CLUSTERED ([Id]))"
# one column for each kind of dependant
T = (
    "CREATE TABLE [s].[T] ([InIndex] int NOT NULL, [InInclude] int NOT NULL, [InFilter] int NULL, "
    "[InPk] int NOT NULL, [InUnique] int NOT NULL, [InFk] int NULL, [InCheck] int NOT NULL, "
    "[InComputed] int NOT NULL, [Referenced] int NOT NULL, "
    "[WithDefault] int NOT NULL CONSTRAINT [DF_T] DEFAULT ((0)), "
    "[Free] int NOT NULL, [Calc] AS ([InComputed] + 1), "
    "CONSTRAINT [PK_T] PRIMARY KEY CLUSTERED ([InPk]), CONSTRAINT [UQ_T] UNIQUE NONCLUSTERED ([InUnique]), "
    "CONSTRAINT [UQ_T_Referenced] UNIQUE NONCLUSTERED ([Referenced]), "
    "CONSTRAINT [FK_T_P] FOREIGN KEY ([InFk]) REFERENCES [s].[P] ([Id]), "
    "CONSTRAINT [CK_T] CHECK ([InCheck] > 0), "
    "INDEX [IX_T] NONCLUSTERED ([InIndex]) INCLUDE ([InInclude]) WHERE [InFilter] IS NOT NULL)"
)
U = (
    "CREATE TABLE [s].[U] ([Id] int NOT NULL, [TRef] int NOT NULL, "
    "CONSTRAINT [FK_U_T] FOREIGN KEY ([TRef]) REFERENCES [s].[T] ([Referenced]))"
)
CHILD = (
    "CREATE TABLE [s].[C] ([PId] int NOT NULL, "
    "CONSTRAINT [FK_C_P] FOREIGN KEY ([PId]) REFERENCES [s].[P] ([Id]))"
)


def widened(column: str, was: str = "int NOT NULL") -> Model:
    assert f"[{column}] {was}" in T
    return model_of(SCHEMA, PARENT, T.replace(f"[{column}] {was}", f"[{column}] bigint NOT NULL"), U)


@pytest.mark.parametrize(
    ("column", "was", "blocker"),
    [
        ("InIndex", "int NOT NULL", "index [IX_T]"),
        ("InInclude", "int NOT NULL", "index [IX_T]"),
        ("InFilter", "int NULL", "index [IX_T]"),
        ("InPk", "int NOT NULL", "PRIMARY KEY [PK_T]"),
        ("InUnique", "int NOT NULL", "UNIQUE [UQ_T]"),
        ("InFk", "int NULL", "FOREIGN KEY [FK_T_P]"),
        ("InCheck", "int NOT NULL", "CHECK [CK_T]"),
        ("InComputed", "int NOT NULL", "computed column [Calc]"),
        ("Referenced", "int NOT NULL", "FOREIGN KEY [FK_U_T] of [s].[U]"),
    ],
)
def test_alter_column_is_refused_when_a_modelled_dependant_uses_the_column(
    column: str, was: str, blocker: str
):
    with pytest.raises(GenRefused) as caught:
        diff(model_of(SCHEMA, PARENT, T, U), widened(column, was))
    (refusal,) = caught.value.refusals
    assert (refusal.code, refusal.object_key) == ("ALTER_COLUMN_DEPENDANTS", "TABLE:[s].[T]")
    assert f"[{column}]" in refusal.message and blocker in refusal.message


@pytest.mark.parametrize("column", ["Free", "WithDefault"])
def test_alter_column_is_written_when_nothing_but_a_default_uses_the_column(column: str):
    ops = diff(model_of(SCHEMA, PARENT, T, U), widened(column))
    assert [emit_operation(op) for op in ops] == [
        f"ALTER TABLE [s].[T] ALTER COLUMN [{column}] bigint NOT NULL;"
    ]


def test_alter_column_is_written_when_the_same_migration_drops_the_dependant_first():
    changed = T.replace("[InCheck] int NOT NULL", "[InCheck] bigint NOT NULL")
    without_check = changed.replace(", CONSTRAINT [CK_T] CHECK ([InCheck] > 0)", "")
    assert without_check != changed
    ops = diff(model_of(SCHEMA, PARENT, T, U), model_of(SCHEMA, PARENT, without_check, U))
    assert [emit_operation(op) for op in ops] == [
        "ALTER TABLE [s].[T] DROP CONSTRAINT [CK_T];",
        "ALTER TABLE [s].[T] ALTER COLUMN [InCheck] bigint NOT NULL;",
    ]


# One variable-length column for each user that lets a longer length pass (seen live), and [Code]
# under a foreign key of another table, which does not.
LENGTHS = (
    "CREATE TABLE [s].[L] ([Id] int NOT NULL, [InUnique] varchar(20) NOT NULL, [InCheck] nvarchar(5) NULL, "
    "[InIndex] varbinary(16) NOT NULL, [InInclude] varchar(10) NULL, [InFilter] varchar(10) NULL, "
    "CONSTRAINT [PK_L] PRIMARY KEY CLUSTERED ([Id]), CONSTRAINT [UQ_L] UNIQUE NONCLUSTERED ([InUnique]), "
    "CONSTRAINT [CK_L] CHECK (LEN([InCheck]) > 0), "
    "INDEX [IX_L] UNIQUE NONCLUSTERED ([InIndex]) INCLUDE ([InInclude]), "
    "INDEX [IX_L_Filter] NONCLUSTERED ([Id]) WHERE [InFilter] IS NOT NULL)"
)


@pytest.mark.parametrize(
    ("was", "now"),
    [
        ("[InUnique] varchar(20) NOT NULL", "[InUnique] varchar(40) NOT NULL"),
        ("[InCheck] nvarchar(5) NULL", "[InCheck] nvarchar(10) NULL"),
        ("[InIndex] varbinary(16) NOT NULL", "[InIndex] varbinary(32) NOT NULL"),
        ("[InInclude] varchar(10) NULL", "[InInclude] varchar(20) NULL"),
    ],
)
def test_a_longer_length_under_unique_check_or_index_is_one_alter_column_and_no_drop(was: str, now: str):
    assert was in LENGTHS
    ops = diff(model_of(SCHEMA, LENGTHS), model_of(SCHEMA, LENGTHS.replace(was, now)))
    assert [emit_operation(op) for op in ops] == [f"ALTER TABLE [s].[L] ALTER COLUMN {now};"]


@pytest.mark.parametrize(
    ("was", "now", "blocker"),
    [
        ("[InUnique] varchar(20) NOT NULL", "[InUnique] varchar(10) NOT NULL", "UNIQUE [UQ_L]"),
        ("[InUnique] varchar(20) NOT NULL", "[InUnique] nvarchar(40) NOT NULL", "UNIQUE [UQ_L]"),
        ("[InUnique] varchar(20) NOT NULL", "[InUnique] varchar(40) NULL", "UNIQUE [UQ_L]"),
        ("[InUnique] varchar(20) NOT NULL", "[InUnique] varchar(max) NOT NULL", "UNIQUE [UQ_L]"),
        ("[InFilter] varchar(10) NULL", "[InFilter] varchar(20) NULL", "index [IX_L_Filter]"),
    ],
)
def test_a_change_that_is_more_than_a_longer_length_is_refused_as_before(was: str, now: str, blocker: str):
    assert was in LENGTHS
    with pytest.raises(GenRefused) as caught:
        diff(model_of(SCHEMA, LENGTHS), model_of(SCHEMA, LENGTHS.replace(was, now)))
    (refusal,) = caught.value.refusals
    assert (refusal.code, refusal.object_key) == ("ALTER_COLUMN_DEPENDANTS", "TABLE:[s].[L]")
    assert blocker in refusal.message


def test_a_longer_key_column_is_refused_under_a_primary_key_and_under_a_foreign_key_that_references_it():
    parent = "CREATE TABLE [s].[K] ([Code] varchar(10) NOT NULL, {})"
    child = (
        "CREATE TABLE [s].[M] ([Ref] varchar(10) NOT NULL, "
        "CONSTRAINT [FK_M_K] FOREIGN KEY ([Ref]) REFERENCES [s].[K] ([Code]))"
    )
    for key, blocker in (
        ("CONSTRAINT [PK_K] PRIMARY KEY CLUSTERED ([Code])", "PRIMARY KEY [PK_K]"),
        ("CONSTRAINT [UQ_K] UNIQUE NONCLUSTERED ([Code])", "FOREIGN KEY [FK_M_K] of [s].[M]"),
    ):
        base = model_of(SCHEMA, parent.format(key), child)
        head = model_of(SCHEMA, parent.format(key).replace("varchar(10)", "varchar(20)"), child)
        with pytest.raises(GenRefused) as caught:
            diff(base, head)
        (refusal,) = caught.value.refusals
        assert refusal.code == "ALTER_COLUMN_DEPENDANTS" and blocker in refusal.message


def test_the_widening_pair_is_one_alter_column_for_each_column_and_touches_no_constraint_or_index():
    ops = diff(*models("alter_column_widen_under_unique_check_and_index"))
    assert {type(op) for op in ops} == {AlterColumn} and len(ops) == 3


def test_alter_column_writes_the_collation_of_the_head_so_that_the_engine_does_not_reset_it():
    before = "CREATE TABLE [s].[W] ([Text] varchar(10) COLLATE Latin1_General_BIN2 NULL)"
    after = before.replace("varchar(10)", "varchar(50)")
    (op,) = diff(model_of(SCHEMA, before), model_of(SCHEMA, after))
    assert (
        emit_operation(op)
        == "ALTER TABLE [s].[W] ALTER COLUMN [Text] varchar(50) COLLATE Latin1_General_BIN2 NULL;"
    )


def test_a_new_computed_column_that_uses_an_altered_column_blocks_the_alter_column():
    # ADD comes before ALTER COLUMN in the fixed order, so the new column is a dependant
    before = "CREATE TABLE [s].[W] ([N] int NOT NULL)"
    after = "CREATE TABLE [s].[W] ([N] bigint NOT NULL, [Twice] AS ([N] * 2))"
    assert refusals_of(model_of(SCHEMA, before), model_of(SCHEMA, after)) == [
        ("ALTER_COLUMN_DEPENDANTS", "TABLE:[s].[W]")
    ]


def test_a_new_column_in_any_place_but_the_end_is_ord001_and_the_message_names_it():
    with pytest.raises(GenRefused) as caught:
        diff(*models("refuse_new_column_in_the_middle"))
    (refusal,) = caught.value.refusals
    assert refusal.code == "ORD001" and "[Mid]" in refusal.message


def test_what_the_fixed_order_cannot_do_is_refused_and_not_written():
    # a table and a synonym share one namespace; the fixed order creates the table before it drops the synonym
    base = model_of(SCHEMA, "CREATE SYNONYM [s].[Thing] FOR [s].[Other]")
    head = model_of(SCHEMA, "CREATE TABLE [s].[Thing] ([Id] int NOT NULL)")
    with pytest.raises(GenRefused) as caught:
        diff(base, head)
    (refusal,) = caught.value.refusals
    assert (refusal.code, refusal.object_key) == ("ORDER_BLOCKED", "TABLE:[s].[Thing]")
    assert (
        "statement 1 of 2" in refusal.message and "[Thing] is taken by SYNONYM:[s].[Thing]" in refusal.message
    )


def test_an_unchanged_foreign_key_is_added_again_only_when_its_key_is_dropped():
    ops = diff(*models("primary_key_change_adds_its_foreign_key_again"))
    assert [type(op) for op in ops] == [DropConstraint, DropConstraint, AddConstraint, AddConstraint]
    assert isinstance(ops[0], DropConstraint) and ops[0].name == "FK_Order_Customer"
    # near miss: the key that changes is on other columns than the foreign key
    other_key = PARENT[:-1].replace("[Id] int NOT NULL", "[Id] int NOT NULL, [Code] int NOT NULL")
    other_key += ", CONSTRAINT [UQ_P] UNIQUE NONCLUSTERED ([Code]))"
    base = model_of(SCHEMA, other_key, CHILD)
    head = model_of(SCHEMA, other_key.replace("[UQ_P] UNIQUE", "[UQ_P2] UNIQUE"), CHILD)
    assert [emit_operation(op) for op in diff(base, head)] == [
        "ALTER TABLE [s].[P] DROP CONSTRAINT [UQ_P];",
        "ALTER TABLE [s].[P] ADD CONSTRAINT [UQ_P2] UNIQUE NONCLUSTERED ([Code]);",
    ]


TWO_KEYS = PARENT[:-1] + ", INDEX [UX_P] UNIQUE NONCLUSTERED ([Id]))"
PK_P = "CONSTRAINT [PK_P] PRIMARY KEY CLUSTERED ([Id]), "
DROP_FK_C_P = "ALTER TABLE [s].[C] DROP CONSTRAINT [FK_C_P];"
ADD_FK_C_P = "ALTER TABLE [s].[C] ADD CONSTRAINT [FK_C_P] FOREIGN KEY ([PId]) REFERENCES [s].[P] ([Id]);"


def test_dropping_one_of_two_keys_on_the_same_columns_drops_and_adds_the_foreign_key_again():
    # PP-6. The engine binds a foreign key to one index and refuses to drop that one (error
    # 3725), also when another key has the same columns. The model does not hold which one it
    # is: the foreign key goes first and comes back last, in both cases.
    base = model_of(SCHEMA, TWO_KEYS, CHILD)
    without_pk = model_of(SCHEMA, TWO_KEYS.replace(PK_P, ""), CHILD)
    assert [emit_operation(op) for op in diff(base, without_pk)] == [
        DROP_FK_C_P,
        "ALTER TABLE [s].[P] DROP CONSTRAINT [PK_P];",
        ADD_FK_C_P,
    ]
    without_index = model_of(SCHEMA, PARENT, CHILD)
    assert [emit_operation(op) for op in diff(base, without_index)] == [
        DROP_FK_C_P,
        "DROP INDEX [UX_P] ON [s].[P];",
        ADD_FK_C_P,
    ]
    changed = model_of(SCHEMA, TWO_KEYS.replace("KEY CLUSTERED", "KEY NONCLUSTERED"), CHILD)
    assert [emit_operation(op) for op in diff(base, changed)] == [
        DROP_FK_C_P,
        "ALTER TABLE [s].[P] DROP CONSTRAINT [PK_P];",
        "ALTER TABLE [s].[P] ADD CONSTRAINT [PK_P] PRIMARY KEY NONCLUSTERED ([Id]);",
        ADD_FK_C_P,
    ]
    # a table that is dropped loses its foreign key before the key goes, not with DROP TABLE
    assert [emit_operation(op) for op in diff(base, model_of(SCHEMA, TWO_KEYS.replace(PK_P, "")))] == [
        DROP_FK_C_P,
        "ALTER TABLE [s].[P] DROP CONSTRAINT [PK_P];",
        "DROP TABLE [s].[C];",
    ]


def test_a_dropped_table_loses_its_foreign_key_first_when_the_key_it_references_is_dropped_too():
    child = CHILD
    base = model_of(SCHEMA, PARENT, child)
    head = model_of(SCHEMA, PARENT.replace("KEY CLUSTERED", "KEY NONCLUSTERED"))
    assert [emit_operation(op) for op in diff(base, head)] == [
        "ALTER TABLE [s].[C] DROP CONSTRAINT [FK_C_P];",
        "ALTER TABLE [s].[P] DROP CONSTRAINT [PK_P];",
        "DROP TABLE [s].[C];",
        "ALTER TABLE [s].[P] ADD CONSTRAINT [PK_P] PRIMARY KEY NONCLUSTERED ([Id]);",
    ]
    # near miss: the key stays, so DROP TABLE alone is enough
    assert [emit_operation(op) for op in diff(base, model_of(SCHEMA, PARENT))] == ["DROP TABLE [s].[C];"]


# ------------------------------------------------------------------ classify
BASE = model_of(
    SCHEMA,
    "CREATE TABLE [s].[T] ([Id] int NOT NULL, [Name] varchar(50) NULL, [Uni] nvarchar(50) NOT NULL, "
    "[Big] varchar(max) NULL, [Amount] decimal(10, 2) NOT NULL, [At] datetime2(7) NOT NULL, "
    "[Word] varchar(20) COLLATE Latin1_General_CI_AS NOT NULL)",
)
ALTER = "ALTER TABLE [s].[T] ALTER COLUMN "
FK_T = "CONSTRAINT [FK_T] FOREIGN KEY ([Id]) REFERENCES [s].[P] ([Id])"


@pytest.mark.parametrize(
    ("statement", "codes"),
    [
        # by the statement alone
        ("DROP TABLE [s].[T]", ["DROP_TABLE"]),
        ("ALTER TABLE [s].[T] DROP COLUMN [Name]", ["DROP_COLUMN"]),
        ("DROP SEQUENCE [s].[Seq]", ["DROP_SEQUENCE"]),
        ("DROP TYPE [s].[Phone]", ["DROP_TYPE"]),
        ("DROP SCHEMA [s]", ["DROP_SCHEMA"]),
        ("EXEC sys.sp_rename N'[s].[T].[Name]', N'Title', N'COLUMN'", ["RENAME"]),
        ("EXEC sys.sp_rename N'[s].[T]', N'T2', N'OBJECT'", ["RENAME"]),
        # near misses: a drop that loses no data, a create, a change that is not a rename
        ("ALTER TABLE [s].[T] DROP CONSTRAINT [CK_T]", []),
        ("DROP INDEX [IX_T] ON [s].[T]", []),
        ("DROP SYNONYM [s].[Syn]", []),
        ("ALTER SEQUENCE [s].[Seq] RESTART WITH 1", []),
        ("CREATE SCHEMA [x]", []),
        ("CREATE SYNONYM [s].[Syn] FOR [s].[T]", []),
        ("CREATE TABLE [s].[New] ([Id] int NOT NULL)", []),
        ("ALTER TABLE [s].[T] ADD [More] int NULL", []),
        # ALTER_COLUMN_LOSSY: narrowing
        (ALTER + "[Name] varchar(20) NULL", ["ALTER_COLUMN_LOSSY", "LONG_LOCK"]),
        (ALTER + "[Big] varchar(8000) NULL", ["ALTER_COLUMN_LOSSY", "LONG_LOCK"]),
        (ALTER + "[Amount] decimal(8, 2) NOT NULL", ["ALTER_COLUMN_LOSSY", "LONG_LOCK"]),
        (ALTER + "[Amount] decimal(10, 4) NOT NULL", ["ALTER_COLUMN_LOSSY", "LONG_LOCK"]),  # 8 digits to 6
        (ALTER + "[Amount] decimal(12, 1) NOT NULL", ["ALTER_COLUMN_LOSSY", "LONG_LOCK"]),  # scale 2 to 1
        (ALTER + "[At] datetime2(3) NOT NULL", ["ALTER_COLUMN_LOSSY", "LONG_LOCK"]),
        # ALTER_COLUMN_LOSSY: another type, Unicode to non-Unicode, another collation
        (ALTER + "[Id] bigint NOT NULL", ["ALTER_COLUMN_LOSSY", "LONG_LOCK"]),
        (ALTER + "[Amount] numeric(10, 2) NOT NULL", ["ALTER_COLUMN_LOSSY", "LONG_LOCK"]),
        (ALTER + "[Uni] varchar(50) NOT NULL", ["ALTER_COLUMN_LOSSY", "LONG_LOCK"]),
        (ALTER + "[Name] [s].[Phone] NULL", ["ALTER_COLUMN_LOSSY", "LONG_LOCK"]),
        (ALTER + "[Word] varchar(20) COLLATE Greek_CI_AS NOT NULL", ["ALTER_COLUMN_LOSSY", "LONG_LOCK"]),
        (ALTER + "[Word] varchar(20) NOT NULL", ["ALTER_COLUMN_LOSSY", "LONG_LOCK"]),  # back to the default
        # near misses: wider is a type change (LONG_LOCK) and loses nothing
        (ALTER + "[Name] varchar(100) NULL", ["LONG_LOCK"]),
        (ALTER + "[Name] varchar(max) NULL", ["LONG_LOCK"]),
        (ALTER + "[Amount] decimal(12, 4) NOT NULL", ["LONG_LOCK"]),
        (ALTER + "[At] datetime2(7) NULL", []),
        (ALTER + "[Word] varchar(20) COLLATE latin1_general_ci_as NOT NULL", []),  # the same collation
        # SET_NOT_NULL and its near misses
        (ALTER + "[Name] varchar(50) NOT NULL", ["SET_NOT_NULL"]),
        (ALTER + "[Name] varchar(20) NOT NULL", ["ALTER_COLUMN_LOSSY", "SET_NOT_NULL", "LONG_LOCK"]),
        (ALTER + "[Name] varchar(50) NULL", []),
        (ALTER + "[Uni] nvarchar(50) NOT NULL", []),
        (ALTER + "[Uni] nvarchar(50) NULL", []),
        # a column that the model does not hold: nothing can be ruled out
        (ALTER + "[Unknown] int NOT NULL", ["ALTER_COLUMN_LOSSY", "SET_NOT_NULL", "LONG_LOCK"]),
        (ALTER + "[Unknown] int NULL", ["ALTER_COLUMN_LOSSY", "LONG_LOCK"]),
        # LONG_LOCK: the engine reads every row of a table that exists
        ("CREATE NONCLUSTERED INDEX [IX_T] ON [s].[T] ([Name])", ["LONG_LOCK"]),
        ("CREATE NONCLUSTERED INDEX [IX_T] ON [s].[T] ([Name]) WITH (ONLINE = ON)", ["LONG_LOCK"]),
        ("ALTER TABLE [s].[T] ADD CONSTRAINT [PK_T] PRIMARY KEY CLUSTERED ([Id])", ["LONG_LOCK"]),
        ("ALTER TABLE [s].[T] ADD CONSTRAINT [UQ_T] UNIQUE NONCLUSTERED ([Name])", ["LONG_LOCK"]),
        ("ALTER TABLE [s].[T] ADD CONSTRAINT [CK_T] CHECK ([Id] > 0)", ["LONG_LOCK"]),
        ("ALTER TABLE [s].[T] WITH CHECK ADD CONSTRAINT [CK_T] CHECK ([Id] > 0)", ["LONG_LOCK"]),
        ("ALTER TABLE [s].[T] ADD " + FK_T, ["LONG_LOCK"]),
        ("ALTER TABLE [s].[T] WITH CHECK ADD " + FK_T, ["LONG_LOCK"]),
        # near misses: no row is read, or the table is new
        ("ALTER TABLE [s].[T] WITH NOCHECK ADD CONSTRAINT [CK_T] CHECK ([Id] > 0)", []),
        ("ALTER TABLE [s].[T] WITH NOCHECK ADD " + FK_T, []),
        ("ALTER TABLE [s].[T] ADD CONSTRAINT [DF_T] DEFAULT ((0)) FOR [Id]", []),
        ("CREATE NONCLUSTERED INDEX [IX_New] ON [s].[New] ([Id])", []),
        ("ALTER TABLE [s].[New] ADD CONSTRAINT [PK_New] PRIMARY KEY CLUSTERED ([Id])", []),
        ("ALTER TABLE [s].[New] ADD CONSTRAINT [CK_New] CHECK ([Id] > 0)", []),
    ],
)
def test_classify_gives_the_allow_codes_that_a_statement_needs(statement: str, codes: list[str]):
    assert classify(parse_statement(statement), BASE) == codes


def test_classify_reads_a_table_that_the_same_change_created_as_new():
    index = parse_statement("CREATE NONCLUSTERED INDEX [IX_T] ON [s].[T] ([Name])")
    retype = parse_statement(ALTER + "[Id] bigint NOT NULL")
    assert classify(index, BASE) == ["LONG_LOCK"]
    assert classify(index, BASE, created=["TABLE:[S].[t]"]) == []
    assert classify(index, BASE, created=["TABLE:[s].[Other]"]) == ["LONG_LOCK"]
    # the rows of a new table are still rows: only the lock code goes
    assert classify(retype, BASE, created=["TABLE:[s].[T]"]) == ["ALTER_COLUMN_LOSSY"]


def test_classify_is_exact_for_a_hand_edit_when_it_gets_the_model_before_the_statement():
    # ADD NULL, backfill, ALTER COLUMN NOT NULL: against the base revision the column is unknown
    add = parse_statement("ALTER TABLE [s].[T] ADD [Status] tinyint NULL")
    set_not_null = parse_statement(ALTER + "[Status] tinyint NOT NULL")
    assert classify(set_not_null, BASE) == ["ALTER_COLUMN_LOSSY", "SET_NOT_NULL", "LONG_LOCK"]
    assert classify(set_not_null, apply(BASE, add)) == ["SET_NOT_NULL"]


def test_classify_of_generated_operations_marks_exactly_the_destructive_and_locking_ones():
    base, head, renames = models("every_step_in_the_fixed_order")
    marked = {emit_operation(op): classify(op, base) for op in diff(base, head, renames)}
    assert {sql: codes for sql, codes in marked.items() if codes} == {
        "EXEC sys.sp_rename N'[sales].[Customer].[Nme]', N'Name', N'COLUMN';": ["RENAME"],
        "ALTER TABLE [sales].[Customer] DROP COLUMN [Fax];": ["DROP_COLUMN"],
        "DROP TABLE [old].[Part];": ["DROP_TABLE"],
        "DROP TABLE [old].[Thing];": ["DROP_TABLE"],
        "ALTER TABLE [sales].[Customer] ALTER COLUMN [Email] varchar(320) NULL;": ["LONG_LOCK"],
        "ALTER TABLE [sales].[Customer] ADD CONSTRAINT [PK_Customer] "
        "PRIMARY KEY NONCLUSTERED ([CustomerId]);": ["LONG_LOCK"],
        "ALTER TABLE [sales].[Customer] ADD CONSTRAINT [UQ_Customer_Mail] UNIQUE NONCLUSTERED ([Email]);": [
            "LONG_LOCK"
        ],
        "CREATE CLUSTERED INDEX [CIX_Customer_Name] ON [sales].[Customer] ([Name]);": ["LONG_LOCK"],
        "ALTER TABLE [sales].[Customer] ADD CONSTRAINT [CK_Customer_Tier] CHECK ([Tier] BETWEEN 1 AND 5);": [
            "LONG_LOCK"
        ],
        "ALTER TABLE [sales].[Order] ADD CONSTRAINT [FK_Order_Customer] FOREIGN KEY ([CustomerId]) "
        "REFERENCES [sales].[Customer] ([CustomerId]);": ["LONG_LOCK"],
        "DROP SEQUENCE [old].[Seq];": ["DROP_SEQUENCE"],
        "DROP TYPE [old].[FlagList];": ["DROP_TYPE"],
        "DROP TYPE [old].[Flag];": ["DROP_TYPE"],
        "DROP SCHEMA [old];": ["DROP_SCHEMA"],
    }


# ------------------------------------------------------------------ touched objects
def test_touched_objects_are_the_keys_that_the_statements_name_in_key_order_without_repeats():
    ops = [
        parse_statement(text)
        for text in (
            "CREATE NONCLUSTERED INDEX [IX_T] ON [s].[T] ([Name])",
            "ALTER TABLE [S].[t] ADD [More] int NULL",
            "DROP TABLE [s].[Gone]",
            "CREATE TABLE [s].[New] ([Id] int NOT NULL, " + FK_T + ")",
            "ALTER SEQUENCE [s].[Seq] RESTART WITH 1",
            "DROP TYPE [dbo].[Phone]",
            "CREATE SYNONYM [dbo].[Syn] FOR [s].[T]",
            "DROP SCHEMA [old]",
            "EXEC sys.sp_rename N'[s].[T].[IX_Old]', N'IX_New', N'INDEX'",
        )
    ]
    assert touched_objects(ops) == [
        "SCHEMA:[old]",
        "SEQUENCE:[s].[Seq]",
        "SYNONYM:[dbo].[Syn]",
        "TABLE:[s].[Gone]",
        "TABLE:[s].[New]",
        "TABLE:[s].[T]",
        "TYPE:[dbo].[Phone]",
    ]
    assert touched_objects([]) == []


def test_every_fixture_migration_touches_the_objects_whose_model_changes():
    # and nothing more, but for a table whose unchanged foreign key is dropped and added again
    more = {"primary_key_change_adds_its_foreign_key_again": {"table:[sales].[order]"}}
    for case in ACCEPTED:
        base, head, renames = models(case)
        changed = {key.casefold() for key, _ in base.diff_paths(head)}
        for ops in (
            diff(base, head, renames),
            recorded_operations(case),
        ):  # as generated, and as parsed from SQL
            for model in (base, head):
                touched = {key.casefold() for key in touched_objects(ops, model)}
                assert touched - changed == more.get(case, set()), case
                assert changed <= touched, case


def test_a_rename_touches_the_tables_whose_foreign_keys_point_at_the_renamed_table():
    base, head, _ = models("rename_table")
    rename = parse_statement("EXEC sys.sp_rename N'[sales].[Ordr]', N'Order', N'OBJECT'")
    expected = ["TABLE:[sales].[Order]", "TABLE:[sales].[OrderLine]", "TABLE:[sales].[Ordr]"]
    assert touched_objects([rename], base) == expected
    assert touched_objects([rename], head) == expected


def test_a_column_rename_touches_only_the_tables_whose_foreign_keys_name_that_column():
    model = model_of(
        SCHEMA,
        "CREATE TABLE [s].[K] ([Id] int NOT NULL, [Code] int NOT NULL, [Note] int NULL, "
        "CONSTRAINT [PK_K] PRIMARY KEY CLUSTERED ([Id]), CONSTRAINT [UQ_K] UNIQUE NONCLUSTERED ([Code]))",
        "CREATE TABLE [s].[ById] ([K] int NOT NULL, "
        "CONSTRAINT [FK_1] FOREIGN KEY ([K]) REFERENCES [s].[K] ([Id]))",
        "CREATE TABLE [s].[ByCode] ([K] int NOT NULL, "
        "CONSTRAINT [FK_2] FOREIGN KEY ([K]) REFERENCES [s].[K] ([Code]))",
    )
    rename = "EXEC sys.sp_rename N'[s].[K].[{}]', N'{}', N'COLUMN'"
    assert touched_objects([parse_statement(rename.format("Code", "Kode"))], model) == [
        "TABLE:[s].[ByCode]",
        "TABLE:[s].[K]",
    ]
    assert touched_objects([parse_statement(rename.format("Kode", "Code"))], model) == [  # the model after
        "TABLE:[s].[ByCode]",
        "TABLE:[s].[K]",
    ]
    assert touched_objects([parse_statement(rename.format("Note", "Remark"))], model) == ["TABLE:[s].[K]"]


def test_a_constraint_rename_touches_the_table_that_has_the_constraint():
    base, head, _ = models("rename_index_and_constraints")
    for name, new in (
        ("PK__Account__3214EC07", "PK_Account"),
        ("DF__Account__Balance", "DF_Account_Balance"),
    ):
        rename = parse_statement(f"EXEC sys.sp_rename N'[sales].[{name}]', N'{new}', N'OBJECT'")
        assert touched_objects([rename], base) == ["TABLE:[sales].[Account]"]
        assert touched_objects([rename], head) == ["TABLE:[sales].[Account]"]


def test_touched_objects_follows_a_rename_whose_new_name_a_later_statement_removes():
    # PP-8. plan gives touched_objects the head model. A constraint or table that one release
    # renames and then drops (or renames again) has neither name in it; the statements say enough.
    base = model_of(
        SCHEMA,
        "CREATE TABLE [s].[T] ([Id] int NOT NULL, [Note] int NULL CONSTRAINT [DF_Old] DEFAULT ((0)))",
        "CREATE TABLE [s].[Old] ([Id] int NOT NULL)",
        "CREATE TABLE [s].[A] ([Id] int NOT NULL, CONSTRAINT [CK_A] CHECK ([Id] > 0))",
    )
    ops = [
        parse_statement(text)
        for text in (
            "EXEC sys.sp_rename N'[s].[DF_Old]', N'DF_Mid', N'OBJECT'",
            "EXEC sys.sp_rename N'[s].[DF_Mid]', N'DF_New', N'OBJECT'",
            "ALTER TABLE [s].[T] DROP CONSTRAINT [DF_New]",
            "EXEC sys.sp_rename N'[s].[Old]', N'New', N'OBJECT'",
            "DROP TABLE [s].[New]",
            "EXEC sys.sp_rename N'[s].[A]', N'B', N'OBJECT'",
            "EXEC sys.sp_rename N'[s].[B]', N'C', N'OBJECT'",
            "EXEC sys.sp_rename N'[s].[CK_A]', N'CK_B', N'OBJECT'",
            "EXEC sys.sp_rename N'[s].[CK_B]', N'CK_C', N'OBJECT'",
        )
    ]
    head = replay(base, ops)
    assert "TABLE:[s].[C]" in head and "TABLE:[s].[T]" in head and "TABLE:[s].[New]" not in head
    expected = [
        "TABLE:[s].[A]",
        "TABLE:[s].[B]",
        "TABLE:[s].[C]",
        "TABLE:[s].[New]",
        "TABLE:[s].[Old]",
        "TABLE:[s].[T]",
    ]
    assert touched_objects(ops, head) == expected
    assert touched_objects(ops, base) == expected
    # a constraint that is renamed and goes with its table: no statement and no model names the table
    lost = [*ops[:1], parse_statement("DROP TABLE [s].[T]")]
    with pytest.raises(ValueError, match="cannot find the table"):
        touched_objects(lost, replay(base, lost))


def test_a_rename_that_cannot_be_resolved_fails_loud():
    base, _, _ = models("rename_table")
    table = parse_statement("EXEC sys.sp_rename N'[sales].[Ordr]', N'Order', N'OBJECT'")
    column = parse_statement("EXEC sys.sp_rename N'[sales].[Ordr].[Stat]', N'Status', N'COLUMN'")
    unknown = parse_statement("EXEC sys.sp_rename N'[sales].[Nothing]', N'Something', N'OBJECT'")
    for rename in (table, column):
        with pytest.raises(ValueError, match="needs the model"):
            touched_objects([rename])
    with pytest.raises(ValueError, match="cannot find the table"):
        touched_objects([unknown], base)


# ------------------------------------------------------------------ statement order, read without replay
# replay.py holds the preconditions of the proof. These tests do not ask it: they keep their own
# small book of what stands on what, so a precondition that replay lost would show here.
class OrderBook:
    """Tables, their columns, what stands on each column, their keys and what references them."""

    def __init__(self, model: Model) -> None:
        self.columns: dict[tuple[str, str], set[str]] = {}
        self.users: dict[tuple[str, str], dict[str, set[str]]] = {}  # index, constraint, default -> columns
        self.keys: dict[tuple[str, str], dict[str, frozenset[str]]] = {}  # key or unique index -> columns
        self.refs: dict[tuple[str, str], dict[str, tuple[tuple[str, str], frozenset[str]]]] = {}
        tables = [obj for obj in model.values() if isinstance(obj, Table)]
        for table in tables:
            self.create(table, keys_only=True)
        for table in tables:  # a foreign key of the model can point at any table of the model
            for constraint in table.constraints:
                if isinstance(constraint, ForeignKey):
                    self.constraint(self.at(table.schema, table.name), constraint)

    @staticmethod
    def at(schema: str, table: str) -> tuple[str, str]:
        return fold(schema), fold(table)

    def named(self, at: tuple[str, str], expression: Expression | None) -> set[str]:
        tokens = set(expression.comparison) if expression is not None else set()
        return {c for c in self.columns[at] if c in tokens or f"[{c}]" in tokens}

    def create(self, table: Table, keys_only: bool = False) -> None:
        at = self.at(table.schema, table.name)
        assert at not in self.columns, f"{at} is created twice"
        self.columns[at], self.users[at], self.keys[at], self.refs[at] = set(), {}, {}, {}
        for column in table.columns:
            self.column(at, column)
        ordered = sorted(table.constraints, key=lambda c: isinstance(c, ForeignKey))
        for constraint in ordered:
            if not (keys_only and isinstance(constraint, ForeignKey)):
                self.constraint(at, constraint)
        for index in table.indexes:
            self.index(at, index)

    def column(self, at: tuple[str, str], column: Column) -> None:
        name = fold(column.name)
        assert name not in self.columns[at], f"{at} gets column {name} twice"
        self.columns[at].add(name)
        if column.default is not None and column.default.name is not None:
            self.users[at][fold(column.default.name)] = {name}
        if column.computed is not None:
            self.users[at][f"computed column {name}"] = self.named(at, column.computed.expression) - {name}

    def constraint(
        self, at: tuple[str, str], constraint: Constraint | DefaultConstraint, for_column: str | None = None
    ) -> None:
        assert constraint.name is not None
        name = fold(constraint.name)
        match constraint:
            case PrimaryKey() | Unique():
                columns = {fold(k.name) for k in constraint.columns}
                self.keys[at][name] = frozenset(columns)
            case ForeignKey():
                columns = {fold(c) for c in constraint.columns}
                target = self.at(constraint.ref_schema, constraint.ref_table)
                wanted = frozenset(fold(c) for c in constraint.ref_columns)
                assert target in self.columns, f"foreign key {name} is added before its table {target}"
                assert wanted in self.keys[target].values(), (
                    f"foreign key {name} is added before a key on {sorted(wanted)} of {target} exists"
                )
                self.refs[at][name] = (target, wanted)
            case Check():
                columns = self.named(at, constraint.expression)
            case DefaultConstraint():
                assert for_column is not None
                columns = {fold(for_column)}
        assert columns <= self.columns[at], f"{name} is added before its columns {sorted(columns)}"
        self.users[at][name] = columns

    def index(self, at: tuple[str, str], index: Index) -> None:
        columns = {fold(k.name) for k in index.columns} | {fold(c) for c in index.included}
        assert columns <= self.columns[at], f"index {index.name} is created before its columns"
        self.users[at][fold(index.name)] = columns | self.named(at, index.filter)
        if index.unique:
            self.keys[at][fold(index.name)] = frozenset(fold(k.name) for k in index.columns)

    def drop_user(self, at: tuple[str, str], name: str) -> None:
        name = fold(name)
        assert name in self.users[at], f"{name} of {at} is dropped and is not there"
        del self.users[at][name]
        self.refs[at].pop(name, None)
        key = self.keys[at].pop(name, None)
        if key is not None and key not in self.keys[at].values():
            still = [fk for refs in self.refs.values() for fk, ref in refs.items() if ref == (at, key)]
            assert not still, f"key {name} of {at} is dropped before foreign key {still} that references it"

    def rename(self, op: Rename) -> None:
        new = fold(op.new_name)
        if op.kind == "column":
            at, old = self.at(op.old[0], op.old[1]), fold(op.old[2])
            self.columns[at] = {new if c == old else c for c in self.columns[at]}
            for users in (self.users[at], self.keys[at]):
                for name, columns in users.items():
                    users[name] = type(columns)(new if c == old else c for c in columns)  # type: ignore[assignment]
            for refs in self.refs.values():
                for fk, (target, columns) in refs.items():
                    if target == at:
                        refs[fk] = (target, frozenset(new if c == old else c for c in columns))
            return
        at = self.at(op.old[0], op.old[1])
        if op.kind != "index" and at in self.columns:  # a table
            moved = (at[0], new)
            for book in (self.columns, self.users, self.keys, self.refs):
                book[moved] = book.pop(at)  # type: ignore[index]
            for refs in self.refs.values():
                for fk, (target, columns) in refs.items():
                    if target == at:
                        refs[fk] = (moved, columns)
            return
        old = fold(op.old[2] if op.kind == "index" else op.old[1])
        owners = (
            [at] if op.kind == "index" else [t for t in self.users if t[0] == at[0] and old in self.users[t]]
        )
        (owner,) = owners
        for book in (self.users[owner], self.keys[owner], self.refs[owner]):
            if old in book:
                book[new] = book.pop(old)  # type: ignore[index]

    def run(self, op: Operation) -> None:
        match op:
            case CreateTable():
                self.create(op.table)
            case DropTable():
                at = self.at(op.schema, op.name)
                others = [
                    fk for t, refs in self.refs.items() if t != at for fk, ref in refs.items() if ref[0] == at
                ]
                assert not others, f"{at} is dropped before foreign key {others} that references it"
                for book in (self.columns, self.users, self.keys, self.refs):
                    del book[at]  # type: ignore[arg-type]
            case AddColumn():
                self.column(self.at(op.schema, op.table), op.column)
            case DropColumn():
                at, name = self.at(op.schema, op.table), fold(op.column)
                on_it = sorted(user for user, columns in self.users[at].items() if name in columns)
                assert not on_it, f"column {name} of {at} is dropped before {on_it} on it"
                assert name in self.columns[at], f"column {name} of {at} is dropped and is not there"
                self.columns[at].remove(name)
                self.users[at].pop(f"computed column {name}", None)
            case AddConstraint():
                self.constraint(self.at(op.schema, op.table), op.constraint, op.for_column)
            case DropConstraint():
                self.drop_user(self.at(op.schema, op.table), op.name)
            case CreateIndex():
                self.index(self.at(op.schema, op.table), op.index)
            case DropIndex():
                self.drop_user(self.at(op.schema, op.table), op.name)
            case Rename():
                self.rename(op)
            case _:
                pass  # schemas, types, sequences, synonyms and ALTER COLUMN move no column and no key


def order_problem(base: Model, ops: Sequence[Operation]) -> str | None:
    book = OrderBook(base)
    for number, op in enumerate(ops, start=1):
        try:
            book.run(op)
        except AssertionError as e:
            return f"statement {number}: {e}"
    return None


@pytest.mark.parametrize("case", ACCEPTED)
def test_no_golden_migration_drops_a_column_under_its_users_or_adds_a_foreign_key_before_its_key(case: str):
    assert order_problem(loader.model(case, "base"), recorded_operations(case)) is None


def test_the_order_check_of_these_tests_sees_the_two_orders_that_the_engine_refuses():
    # the check is worth something only when it fails on a wrong order: move one statement of a golden file
    case = "drop_column_and_what_uses_it"
    ops = recorded_operations(case)
    drop = next(i for i, op in enumerate(ops) if isinstance(op, DropColumn))
    assert drop > 0
    early = [ops[drop], *ops[:drop], *ops[drop + 1 :]]
    problem = order_problem(loader.model(case, "base"), early)
    assert (
        problem is not None
        and problem.startswith("statement 1: column ")
        and " is dropped before " in problem
    )

    case = "keys_added_and_dropped_then_foreign_key_on_the_new_key"
    ops = recorded_operations(case)
    add = next(
        i
        for i, op in enumerate(ops)
        if isinstance(op, AddConstraint) and isinstance(op.constraint, ForeignKey)
    )
    early = [ops[add], *ops[:add], *ops[add + 1 :]]
    problem = order_problem(loader.model(case, "base"), early)
    assert problem is not None and "is added before a key on" in problem

    case = "drop_referenced_table_and_the_column_that_pointed_at_it"
    ops = recorded_operations(case)
    table = next(i for i, op in enumerate(ops) if isinstance(op, DropTable))
    early = [ops[table], *ops[:table], *ops[table + 1 :]]
    problem = order_problem(loader.model(case, "base"), early)
    assert problem is not None and "is dropped before foreign key" in problem


def test_no_generated_migration_between_two_fixture_models_has_a_wrong_statement_order():
    """Every pair of fixture models and the empty model: thousands of migrations that nobody wrote."""
    sides = [Model(), *(loader.model(case, side) for case in loader.CASES for side in ("base", "head"))]
    checked = 0
    for source, target in itertools.product(sides, repeat=2):
        try:
            ops = diff(source, target)
        except GenRefused:
            continue
        assert order_problem(source, ops) is None
        checked += 1
    assert checked > 5000


# ------------------------------------------------------------------ system-versioned temporal tables
def temporal(
    name: str = "V", history: str = "V_History", extra: str = "", options: str = "", scale: int = 7
) -> str:
    return (
        f"CREATE TABLE [s].[{name}] ([Id] int NOT NULL, [Name] varchar(50) NULL, "
        f"[From] datetime2({scale}) GENERATED ALWAYS AS ROW START NOT NULL, "
        f"[To] datetime2({scale}) GENERATED ALWAYS AS ROW END NOT NULL, {extra}"
        f"CONSTRAINT [PK_{name}] PRIMARY KEY CLUSTERED ([Id]), PERIOD FOR SYSTEM_TIME ([From], [To])) "
        f"WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [s].[{history}]{options}))"
    )


NOT_VERSIONED = (
    "CREATE TABLE [s].[V] ([Id] int NOT NULL, [Name] varchar(50) NULL, "
    "CONSTRAINT [PK_V] PRIMARY KEY CLUSTERED ([Id]))"
)


def written(base: Model, head: Model, renames: Sequence[RenameSpec] = ()) -> list[str]:
    return [emit_operation(op) for op in diff(base, head, renames)]


def test_a_new_temporal_table_is_one_create_table_with_its_period_and_history_table():
    (statement,) = written(model_of(SCHEMA), model_of(SCHEMA, temporal()))
    assert statement.startswith("CREATE TABLE [s].[V] (")
    assert "PERIOD FOR SYSTEM_TIME ([From], [To])" in statement
    assert statement.endswith("WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [s].[V_History]));")


def test_a_dropped_temporal_table_gets_versioning_off_directly_before_its_drop_table():
    base = model_of(SCHEMA, temporal(), temporal("W", "W_History"))
    ops = diff(base, model_of(SCHEMA))
    assert [emit_operation(op) for op in ops] == [
        "ALTER TABLE [s].[V] SET (SYSTEM_VERSIONING = OFF);",
        "DROP TABLE [s].[V];",
        "ALTER TABLE [s].[W] SET (SYSTEM_VERSIONING = OFF);",
        "DROP TABLE [s].[W];",
    ]
    # the author must allow both: the history stops, and the history table stays behind unmanaged
    assert [classify(op, base) for op in ops[:2]] == [["TEMPORAL_OFF"], ["DROP_TABLE"]]


def test_classify_asks_for_temporal_off_only_when_versioning_goes_off():
    base = model_of(SCHEMA, temporal())
    on = parse_statement("ALTER TABLE [s].[V] SET (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [s].[V_History]))")
    assert classify(parse_statement("ALTER TABLE [s].[V] SET (SYSTEM_VERSIONING = OFF)"), base) == [
        "TEMPORAL_OFF"
    ]
    assert classify(on, base) == []
    assert classify(parse_statement("ALTER TABLE [s].[V] DROP COLUMN [Name]"), base) == ["DROP_COLUMN"]
    assert touched_objects([on]) == ["TABLE:[s].[V]"]


def test_columns_constraints_and_indexes_of_a_temporal_table_change_as_on_any_table():
    base = model_of(SCHEMA, temporal())
    head = model_of(
        SCHEMA,
        temporal(
            extra="[Qty] int NULL, CONSTRAINT [CK_V] CHECK ([Id] > 0), INDEX [IX_V] NONCLUSTERED ([Id]), "
        ).replace("[Name] varchar(50) NULL", "[Name] varchar(80) NULL"),
    )
    assert written(base, head) == [
        "ALTER TABLE [s].[V] ADD [Qty] int NULL;",
        "ALTER TABLE [s].[V] ALTER COLUMN [Name] varchar(80) NULL;",
        "CREATE NONCLUSTERED INDEX [IX_V] ON [s].[V] ([Id]);",
        "ALTER TABLE [s].[V] ADD CONSTRAINT [CK_V] CHECK ([Id] > 0);",
    ]
    assert written(head, model_of(SCHEMA, temporal().replace("[Name] varchar(50) NULL, ", ""))) == [
        "ALTER TABLE [s].[V] DROP CONSTRAINT [CK_V];",
        "DROP INDEX [IX_V] ON [s].[V];",
        "ALTER TABLE [s].[V] DROP COLUMN [Name];",
        "ALTER TABLE [s].[V] DROP COLUMN [Qty];",
    ]


@pytest.mark.parametrize(
    ("base", "head", "says"),
    [
        (NOT_VERSIONED, temporal(), "system-versioned at the head revision and not at the base"),
        (temporal(), NOT_VERSIONED, "system-versioned at the base revision and not at the head"),
        (temporal(), temporal(history="Other"), "the history table differs"),
        (temporal(), temporal().replace("[s].[V_History]", "[x].[V_History]"), "the history table differs"),
        (
            temporal(),
            temporal(options=", HISTORY_RETENTION_PERIOD = 1 YEAR"),
            "HISTORY_RETENTION_PERIOD differs",
        ),
        (temporal(), temporal(scale=3), "the definition of period column [From] differs"),
        (temporal(), temporal().replace("ROW END NOT", "ROW END HIDDEN NOT"), "period column [To] differs"),
        (
            temporal(),
            temporal().replace("[To]", "[Until]"),
            "the period columns of PERIOD FOR SYSTEM_TIME differ",
        ),
        (temporal(), temporal().replace("CLUSTERED ([Id])", "NONCLUSTERED ([Id])"), "PRIMARY KEY [PK_V]"),
    ],
)
def test_a_change_of_the_versioning_itself_is_refused_and_no_statement_is_written(base, head, says):
    with pytest.raises(GenRefused) as caught:
        diff(model_of(SCHEMA, "CREATE SCHEMA [x]", base), model_of(SCHEMA, "CREATE SCHEMA [x]", head))
    (refusal,) = caught.value.refusals  # one refusal for the table, not one for each column of the period
    assert (refusal.code, refusal.object_key) == ("TEMPORAL_CHANGE", "TABLE:[s].[V]")
    assert says in refusal.message and refusal.hint in TEMPORAL_HINTS.values()


OFF = "ALTER TABLE [s].[V] SET (SYSTEM_VERSIONING = OFF)"
ON = "ALTER TABLE [s].[V] SET (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [s].[{}]{}))"
# (base, head, the hand-written statements between OFF and ON, the history table and options of ON)
PROVEN_BY_HAND = [
    (
        temporal(),
        temporal().replace("CLUSTERED ([Id])", "NONCLUSTERED ([Id])"),
        [
            "ALTER TABLE [s].[V] DROP CONSTRAINT [PK_V]",
            "ALTER TABLE [s].[V] ADD CONSTRAINT [PK_V] PRIMARY KEY NONCLUSTERED ([Id])",
        ],
        ("V_History", ""),
    ),
    (temporal(), temporal(history="Other"), [], ("Other", "")),
    (
        temporal(),
        temporal(options=", HISTORY_RETENTION_PERIOD = 1 YEAR"),
        [],
        ("V_History", ", HISTORY_RETENTION_PERIOD = 1 YEAR"),
    ),
    (
        temporal(),
        temporal(extra="[Z] AS ([Id] + 1), "),
        ["ALTER TABLE [s].[V] ADD [Z] AS ([Id] + 1)"],
        ("V_History", ""),
    ),
    (
        temporal(),
        temporal(extra="[Z] int IDENTITY(1, 1) NOT NULL, "),
        ["ALTER TABLE [s].[V] ADD [Z] int IDENTITY(1, 1) NOT NULL"],
        ("V_History", ""),
    ),
]


@pytest.mark.parametrize(("base", "head", "between", "on"), PROVEN_BY_HAND)
def test_a_hint_that_says_hand_write_names_a_route_that_the_proof_accepts(base, head, between, on):
    before, after = model_of(SCHEMA, base), model_of(SCHEMA, head)
    with pytest.raises(GenRefused) as caught:
        diff(before, after)
    (refusal,) = caught.value.refusals
    assert refusal.code == "TEMPORAL_CHANGE"
    hint = refusal.hint
    assert hint.startswith("Hand-write the migration") and "not supported" not in hint
    assert hint.index("SYSTEM_VERSIONING = OFF") < hint.index("SYSTEM_VERSIONING = ON")
    assert "TEMPORAL_OFF" in hint  # the allow line that the route needs
    # the route of the hint is true: these statements give the head model in the replay
    route = [OFF, *between, ON.format(*on)]
    assert replay(before, [parse_statement(text) for text in route]) == after


@pytest.mark.parametrize(
    ("base", "head", "route"),
    [
        # converting a table: the statement that adds the period is not read
        (
            NOT_VERSIONED,
            temporal(),
            ["ALTER TABLE [s].[V] ADD [From] datetime2(7) GENERATED ALWAYS AS ROW START NOT NULL"],
        ),
        # dropping the period
        (temporal(), NOT_VERSIONED, [OFF, "ALTER TABLE [s].[V] DROP PERIOD FOR SYSTEM_TIME"]),
        (temporal(), NOT_VERSIONED, [OFF, "ALTER TABLE [s].[V] DROP COLUMN [From]"]),
        # the definition of a period column
        (
            temporal(),
            temporal(scale=3),
            [OFF, "ALTER TABLE [s].[V] ALTER COLUMN [From] datetime2(3) NOT NULL"],
        ),
        (
            temporal(),
            temporal().replace("ROW END NOT", "ROW END HIDDEN NOT"),
            [OFF, "ALTER TABLE [s].[V] ALTER COLUMN [To] ADD HIDDEN"],
        ),
        # another period column
        (
            temporal(),
            temporal().replace("[To]", "[Until]"),
            [OFF, "EXEC sys.sp_rename N'[s].[V].[To]', N'Until', N'COLUMN'"],
        ),
    ],
)
def test_a_change_that_no_migration_of_this_version_can_make_does_not_say_hand_write(base, head, route):
    before = model_of(SCHEMA, base)
    with pytest.raises(GenRefused) as caught:
        diff(before, model_of(SCHEMA, head))
    (refusal,) = caught.value.refusals
    hint = refusal.hint
    assert hint == TEMPORAL_HINTS["not supported"]
    assert "not supported in this version" in hint
    assert "changed outside the tool and then exported again" in hint
    assert "hand-write" not in hint.lower()
    # and it is true: the parser or the replay refuses the statement that the old hint asked for
    with pytest.raises((ParseError, ReplayError)):
        replay(before, [parse_statement(text) for text in route])


def test_the_rename_of_a_period_column_does_not_say_hand_write():
    head = model_of(SCHEMA, temporal().replace("[To]", "[Until]"))
    with pytest.raises(GenRefused) as caught:
        diff(model_of(SCHEMA, temporal()), head, [RenameSpec("column", ("s", "V", "To"), "Until")])
    (refusal,) = caught.value.refusals
    assert refusal.hint == TEMPORAL_HINTS["not supported"]


def test_the_general_text_of_temporal_change_promises_no_route():
    assert "hand-write" not in REFUSALS["TEMPORAL_CHANGE"].lower()
    assert set(TEMPORAL_HINTS) == {"not supported", "off and on", "history table", "add column", "schema"}


@pytest.mark.parametrize(
    ("extra", "says"),
    [("[Z] AS ([Id] + 1), ", "computed"), ("[Z] int IDENTITY(1, 1) NOT NULL, ", "IDENTITY")],
)
def test_a_new_computed_or_identity_column_of_a_temporal_table_is_refused_and_not_written(extra, says):
    # the engine refuses ALTER TABLE ADD of both while SYSTEM_VERSIONING is ON (seen live)
    with pytest.raises(GenRefused) as caught:
        diff(model_of(SCHEMA, temporal()), model_of(SCHEMA, temporal(extra=extra)))
    (refusal,) = caught.value.refusals
    assert (refusal.code, refusal.object_key) == ("TEMPORAL_CHANGE", "TABLE:[s].[V]")
    assert "[Z]" in refusal.message and says in refusal.message
    assert refusal.hint == TEMPORAL_HINTS["add column"] and "history table" in refusal.hint
    # on a table that is not versioned the same column is written
    plain = NOT_VERSIONED.replace("CONSTRAINT", extra + "CONSTRAINT")
    assert written(model_of(SCHEMA, NOT_VERSIONED), model_of(SCHEMA, plain)) == [
        f"ALTER TABLE [s].[V] ADD {extra[:-2]};"
    ]


def test_a_rename_of_a_period_column_is_refused_as_a_temporal_change():
    head = model_of(SCHEMA, temporal().replace("[To]", "[Until]"))
    spec = RenameSpec("column", ("s", "V", "To"), "Until")
    assert refusals_of(model_of(SCHEMA, temporal()), head, [spec]) == [("TEMPORAL_CHANGE", "TABLE:[s].[V]")]
    # another column of the same table is renamed like any column
    name = RenameSpec("column", ("s", "V", "Name"), "Title")
    assert written(
        model_of(SCHEMA, temporal()), model_of(SCHEMA, temporal().replace("[Name]", "[Title]")), [name]
    ) == ["EXEC sys.sp_rename N'[s].[V].[Name]', N'Title', N'COLUMN';"]


def test_a_dropped_schema_that_would_keep_a_history_table_is_refused_before_any_statement():
    # live: DROP TABLE leaves the history table, and the engine then refuses DROP SCHEMA
    base = model_of(SCHEMA, temporal())
    with pytest.raises(GenRefused) as caught:
        diff(base, Model())
    (refusal,) = caught.value.refusals
    assert (refusal.code, refusal.object_key) == ("TEMPORAL_CHANGE", "TABLE:[s].[V]")
    assert "[s].[V_History] stays in a schema" in refusal.message
    # the history table in a schema that stays: the drop is written
    elsewhere = model_of(
        SCHEMA, "CREATE SCHEMA [x]", temporal().replace("[s].[V_History]", "[x].[V_History]")
    )
    assert written(elsewhere, model_of("CREATE SCHEMA [x]"))[-1] == "DROP SCHEMA [s];"


# ------------------------------------------------------------------ dynamic data masking
MASKED_TABLE = "CREATE TABLE [s].[M] ([Id] int NOT NULL, [Mail] varchar(100){} NULL, [N] int{} NOT NULL)"


def masked_model(mail: str | None, n: str | None, mail_type: str = "varchar(100)") -> Model:
    def clause(function: str | None) -> str:
        return "" if function is None else f" MASKED WITH (FUNCTION = '{function}')"

    return model_of(
        "CREATE SCHEMA [s]", MASKED_TABLE.format(clause(mail), clause(n)).replace("varchar(100)", mail_type)
    )


def test_a_mask_that_comes_or_changes_is_add_masked_and_a_mask_that_goes_is_drop_masked():
    assert diff(masked_model(None, None), masked_model("email()", None)) == [
        masking.MaskColumn("s", "M", "Mail", "email()")
    ]
    assert diff(masked_model("email()", "default()"), masked_model("default()", "default()")) == [
        masking.MaskColumn("s", "M", "Mail", "default()")
    ]
    assert diff(masked_model("email()", "default()"), masked_model("email()", None)) == [
        masking.UnmaskColumn("s", "M", "N")
    ]
    assert diff(masked_model("email()", "default()"), masked_model("email()", "default()")) == []


def test_the_mask_is_written_again_after_an_alter_column_because_the_engine_removes_it():
    # Azure SQL Database: ALTER COLUMN to a wider type, to another type, to NOT NULL or to another
    # collation left the column without its mask every time
    kept = diff(masked_model("email()", None), masked_model("email()", None, "varchar(200)"))
    changed = diff(masked_model("email()", None), masked_model("default()", None, "varchar(200)"))
    new = diff(masked_model(None, None), masked_model("email()", None, "varchar(200)"))

    assert [type(op).__name__ for op in kept] == ["AlterColumn", "MaskColumn"]
    assert kept[1] == masking.MaskColumn("s", "M", "Mail", "email()")
    assert changed[1] == masking.MaskColumn("s", "M", "Mail", "default()")
    assert [type(op).__name__ for op in new] == ["AlterColumn", "MaskColumn"]


def test_a_mask_that_goes_with_a_type_change_is_dropped_by_its_own_statement_before_the_alter_column():
    # ALTER COLUMN alone would remove the mask too, without the word UNMASK in the migration
    ops = diff(masked_model("email()", None), masked_model(None, None, "varchar(200)"))

    assert [type(op).__name__ for op in ops] == ["UnmaskColumn", "AlterColumn"]
    assert [classify(op, masked_model("email()", None)) for op in ops] == [["UNMASK"], ["LONG_LOCK"]]


def test_unmask_needs_the_allow_code_unmask_and_a_new_mask_needs_none():
    base = masked_model("email()", None)

    assert classify(masking.UnmaskColumn("s", "M", "Mail"), base) == ["UNMASK"]
    assert classify(masking.MaskColumn("s", "M", "Mail", "default()"), base) == []
    assert classify(masking.MaskColumn("s", "M", "N", "default()"), base) == []
    removed = diff(*models("mask_removed_needs_allow_unmask")[:2])
    assert removed and all(
        classify(op, loader.model("mask_removed_needs_allow_unmask", "base")) == ["UNMASK"] for op in removed
    )
    added = diff(*models("mask_added_to_columns")[:2])
    assert added and all(classify(op, loader.model("mask_added_to_columns", "base")) == [] for op in added)


def test_a_masking_operation_touches_its_table():
    ops = [masking.MaskColumn("s", "M", "Mail", "email()"), masking.UnmaskColumn("s", "M", "N")]

    assert touched_objects(ops, masked_model(None, "default()")) == ["TABLE:[s].[M]"]


def test_a_new_table_and_a_new_column_carry_their_masks_in_the_create_and_add_statements():
    empty = model_of("CREATE SCHEMA [s]")
    one = masked_model("email()", None)
    wide = model_of(
        "CREATE SCHEMA [s]",
        MASKED_TABLE.format(" MASKED WITH (FUNCTION = 'email()')", "").replace(
            " NOT NULL)", " NOT NULL, [Tax] varchar(20) MASKED WITH (FUNCTION = 'default()') NULL)"
        ),
    )

    (create,) = diff(empty, one)
    (add,) = diff(one, wide)

    assert isinstance(create, CreateTable) and [c.masked for c in create.table.columns] == [
        None,
        "email()",
        None,
    ]
    assert isinstance(add, masking.AddColumn) and add.column.masked == "default()"
