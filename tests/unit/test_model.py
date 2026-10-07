"""The typed model: what is equal, what is different, and how a difference is named."""

import dataclasses
from typing import Any

import pytest

from azsqlcd import model as masking
from azsqlcd import names
from azsqlcd.model import (
    AddConstraint,
    AliasType,
    AlterColumn,
    Check,
    Column,
    Computed,
    CreateIndex,
    CreateTable,
    DefaultConstraint,
    DropTable,
    Expression,
    ForeignKey,
    Identity,
    Index,
    KeyColumn,
    Model,
    PrimaryKey,
    Rename,
    Schema,
    Sequence,
    SetSystemVersioning,
    Synonym,
    Table,
    TableType,
    Temporal,
    TypeRef,
    Unique,
    UnversionedTable,
    fold,
)


def expr(sql: str) -> Expression:
    return Expression.from_sql(sql)


ORDER = Table(
    "sales",
    "Order",
    (
        Column("OrderId", TypeRef("int"), False, identity=Identity(1, 1)),
        Column(
            "Status", TypeRef("tinyint"), False, default=DefaultConstraint("DF_Order_Status", expr("((0))"))
        ),
        Column("Note", TypeRef("nvarchar", length="max"), True, collation="Latin1_General_CI_AS"),
        Column("Twice", None, None, computed=Computed(expr("([OrderId] * 2)"), persisted=True)),
    ),
    (
        PrimaryKey("PK_Order", True, (KeyColumn("OrderId"),), (("FILLFACTOR", "90"),)),
        Unique("UQ_Order_Status", False, (KeyColumn("Status", descending=True),)),
        ForeignKey(
            "FK_Order_Customer", ("OrderId",), "dbo", "Customer", ("CustomerId",), "CASCADE", "NO ACTION"
        ),
        Check("CK_Order_Status", expr("([Status] < 9)")),
    ),
    (
        Index(
            "IX_Order_Status",
            unique=False,
            clustered=False,
            columns=(KeyColumn("Status"), KeyColumn("OrderId", descending=True)),
            included=("Note",),
            filter=expr("[Status] > 0"),
            options=(("DATA_COMPRESSION", "PAGE"),),
        ),
    ),
)
EVERY_KIND = (
    ORDER,
    Schema("sales", owner="dbo"),
    AliasType("dbo", "PhoneNumber", TypeRef("varchar", length=20), nullable=False),
    TableType(
        "dbo", "IdList", (Column("Id", TypeRef("int"), False),), (PrimaryKey(None, True, (KeyColumn("Id"),)),)
    ),
    Sequence(
        "sales", "OrderNo", TypeRef("decimal", precision=38, scale=0), 1, 1, 1, 10**38 - 1, False, True, 50
    ),
    Synonym("dbo", "LegacyOrder", "sales", "Order"),
)


def column(name: str, **changes: Any) -> Table:
    columns = tuple(dataclasses.replace(c, **changes) if c.name == name else c for c in ORDER.columns)
    return dataclasses.replace(ORDER, columns=columns)


def constraint(name: str, **changes: Any) -> Table:
    constraints = tuple(dataclasses.replace(c, **changes) if c.name == name else c for c in ORDER.constraints)
    return dataclasses.replace(ORDER, constraints=constraints)


def index(**changes: Any) -> Table:
    return dataclasses.replace(ORDER, indexes=(dataclasses.replace(ORDER.indexes[0], **changes),))


# ------------------------------------------------------------------ expressions
def test_an_expression_ignores_case_white_space_comments_and_brackets():
    written = expr("([Status] = 1 AND [Order Id] > 0x1F)")
    again = expr("( status=1\n  and /* any */ [ORDER ID]>0X1f ) -- done")
    assert written == again
    assert hash(written) == hash(again)
    assert written.comparison == ("(", "status", "=", "1", "and", "[order id]", ">", "0x1f", ")")


def test_an_expression_keeps_its_tokens_as_written():
    written = expr("( [Status]  =  N'It''s' )")
    assert written.tokens == ("(", "[Status]", "=", "N'It''s'", ")")


def test_string_literals_in_an_expression_compare_exactly():
    assert expr("([a] = 'Open')") != expr("([a] = 'open')")
    assert expr("([a] = 'x')") != expr("([a] = N'x')")
    assert expr("([a] = ' x')") != expr("([a] = 'x')")


def test_a_bracketed_reserved_word_is_a_name_and_not_the_keyword():
    assert expr("[select]") != expr("select")
    assert expr("[Status]") == expr("status") == expr('"STATUS"')
    assert expr("[1]") != expr("1")


def test_a_word_that_only_upper_cases_to_a_keyword_is_a_name():
    # LATIN SMALL LETTER LONG S. The lexer refuses it in an unquoted word; in brackets it is a name.
    with pytest.raises(ValueError, match="in an unquoted word"):
        expr("\u017felect")
    assert expr("[\u017felect]") != expr("select")
    with pytest.raises(ValueError):
        TypeRef("\u017fmallint")


def test_token_boundaries_matter_in_an_expression():
    assert expr("[a b]") != expr("a b")
    assert expr("a.b") != expr("[a.b]")


def test_the_comparison_form_reads_back_as_itself():
    written = expr("([Order Id] + [İd] + Ünit + [a]]b] > CONVERT([int], N'x') AND [select] IS NOT NULL)")
    assert Expression(written.comparison).comparison == written.comparison


def test_an_expression_must_be_made_of_whole_tokens():
    with pytest.raises(ValueError):
        Expression(())
    with pytest.raises(ValueError):
        Expression(("a b",))
    with pytest.raises(ValueError):
        Expression(("/* only a comment */",))
    with pytest.raises(ValueError):
        Expression(("'unterminated",))


# ------------------------------------------------------------------ names and equality
def test_names_compare_case_folded_and_are_kept_as_written():
    shouting = Table(
        "SALES", "ORDER", tuple(dataclasses.replace(c, name=c.name.upper()) for c in ORDER.columns)
    )
    quiet = Table("sales", "order", ORDER.columns)
    assert shouting == quiet
    assert hash(shouting) == hash(quiet)
    assert shouting.name == "ORDER"
    assert shouting.columns[0].name == "ORDERID"
    assert fold("Straße") == fold("STRASSE")


def test_a_built_in_type_name_is_not_case_sensitive_but_its_arguments_count():
    assert TypeRef("NVARCHAR", length=50) == TypeRef("nvarchar", length=50)
    assert TypeRef("nvarchar", length=50) != TypeRef("nvarchar", length=51)
    assert TypeRef("nvarchar", length=50) != TypeRef("nvarchar", length="max")
    assert TypeRef("varchar", length=50) != TypeRef("nvarchar", length=50)


def test_decimal_and_numeric_stay_distinct():
    assert TypeRef("decimal", precision=18, scale=4) != TypeRef("numeric", precision=18, scale=4)


def test_an_alias_type_is_not_the_built_in_type_of_the_same_name():
    assert TypeRef("sysname", schema="dbo") != TypeRef("sysname")


def test_a_type_outside_the_normal_form_cannot_enter_the_model():
    for build in (
        lambda: TypeRef("varchar"),  # no length
        lambda: TypeRef("decimal", precision=18),  # no scale
        lambda: TypeRef("datetime2"),  # no scale
        lambda: TypeRef("int", length=4),  # int takes none
        lambda: TypeRef("char", length="max"),  # max only for the var types
        lambda: TypeRef("integer"),  # synonym, not a catalog name
        lambda: TypeRef("PhoneNumber", schema="dbo", length=20),  # an alias type takes none
    ):
        with pytest.raises(ValueError):
            build()


def test_constraints_and_indexes_have_no_order_and_columns_have_one():
    reordered = dataclasses.replace(ORDER, constraints=ORDER.constraints[::-1])
    assert reordered == ORDER
    assert Model([reordered]).to_canonical_json() == Model([ORDER]).to_canonical_json()
    moved = dataclasses.replace(ORDER, columns=ORDER.columns[::-1])
    assert moved != ORDER
    assert Model([ORDER]).diff_paths(Model([moved])) == [(ORDER.key, "columns (order)")]


def test_included_columns_and_options_have_no_order_and_key_columns_have_one():
    a = Index(
        "IX",
        False,
        False,
        (KeyColumn("a"), KeyColumn("b")),
        ("c", "d"),
        None,
        (("FILLFACTOR", "9"), ("PAD_INDEX", "ON")),
    )
    b = Index(
        "IX",
        False,
        False,
        (KeyColumn("a"), KeyColumn("b")),
        ("D", "C"),
        None,
        (("PAD_INDEX", "ON"), ("FILLFACTOR", "9")),
    )
    assert a == b
    assert a != dataclasses.replace(a, columns=a.columns[::-1])


CHANGES = [
    (column("Status", type=TypeRef("smallint")), "columns[status].type.name"),
    (column("Note", type=TypeRef("nvarchar", length=50)), "columns[note].type.length"),
    (column("Status", nullable=True), "columns[status].nullable"),
    (column("OrderId", identity=Identity(5, 1)), "columns[orderid].identity.seed"),
    (column("OrderId", identity=Identity(1, 2)), "columns[orderid].identity.increment"),
    (column("OrderId", identity=None), "columns[orderid].identity"),
    (column("Status", default=DefaultConstraint("DF_Other", expr("((0))"))), "columns[status].default.name"),
    (
        column("Status", default=DefaultConstraint("DF_Order_Status", expr("((1))"))),
        "columns[status].default.expression",
    ),
    (column("Status", default=None), "columns[status].default"),
    (column("Note", collation=None), "columns[note].collation"),
    (
        column("Twice", computed=Computed(expr("([OrderId] * 2)"), persisted=False)),
        "columns[twice].computed.persisted",
    ),
    (
        column("Twice", computed=Computed(expr("([OrderId] * 3)"), persisted=True)),
        "columns[twice].computed.expression",
    ),
    (column("Twice", nullable=False), "columns[twice].nullable"),
    (dataclasses.replace(ORDER, columns=ORDER.columns[:3]), "columns[twice] (absent in other)"),
    (constraint("PK_Order", clustered=False), "constraints[pk_order].clustered"),
    (
        constraint("PK_Order", columns=(KeyColumn("OrderId", True),)),
        "constraints[pk_order].columns[orderid].descending",
    ),
    (constraint("PK_Order", options=(("FILLFACTOR", "80"),)), "constraints[pk_order].options[FILLFACTOR]"),
    (constraint("PK_Order", options=()), "constraints[pk_order].options[FILLFACTOR] (absent in other)"),
    (
        constraint("UQ_Order_Status", columns=(KeyColumn("Note"),)),
        "constraints[uq_order_status].columns[note] (absent in self)",
    ),
    (constraint("FK_Order_Customer", on_delete="SET NULL"), "constraints[fk_order_customer].on_delete"),
    (constraint("FK_Order_Customer", on_update="CASCADE"), "constraints[fk_order_customer].on_update"),
    (constraint("FK_Order_Customer", ref_schema="crm"), "constraints[fk_order_customer].ref_schema"),
    (constraint("FK_Order_Customer", ref_table="Client"), "constraints[fk_order_customer].ref_table"),
    (constraint("FK_Order_Customer", ref_columns=("Id",)), "constraints[fk_order_customer].ref_columns[0]"),
    (constraint("FK_Order_Customer", columns=("Status",)), "constraints[fk_order_customer].columns[0]"),
    (
        constraint("CK_Order_Status", expression=expr("([Status] < 10)")),
        "constraints[ck_order_status].expression",
    ),
    (
        dataclasses.replace(ORDER, constraints=ORDER.constraints[:3]),
        "constraints[ck_order_status] (absent in other)",
    ),
    (index(unique=True), "indexes[ix_order_status].unique"),
    (index(clustered=True), "indexes[ix_order_status].clustered"),
    (index(columns=ORDER.indexes[0].columns[::-1]), "indexes[ix_order_status].columns (order)"),
    (index(included=()), "indexes[ix_order_status].included (count)"),
    (index(included=("Twice",)), "indexes[ix_order_status].included[0]"),
    (index(filter=None), "indexes[ix_order_status].filter"),
    (index(filter=expr("[Status] > 1")), "indexes[ix_order_status].filter"),
    (index(options=(("DATA_COMPRESSION", "ROW"),)), "indexes[ix_order_status].options[DATA_COMPRESSION]"),
    (index(name="IX_Other"), "indexes[ix_order_status] (absent in other)"),
]


@pytest.mark.parametrize(("changed", "path"), CHANGES, ids=[path for _, path in CHANGES])
def test_a_change_of_any_property_makes_the_model_different_and_is_named(changed: Table, path: str):
    assert changed != ORDER
    assert Model([changed]) != Model([ORDER])
    assert (ORDER.key, path) in Model([ORDER]).diff_paths(Model([changed]))


def test_a_constraint_of_another_kind_under_the_same_name_is_a_difference():
    as_unique = tuple(
        Unique(c.name, c.clustered, c.columns, c.options) if isinstance(c, PrimaryKey) else c
        for c in ORDER.constraints
    )
    changed = dataclasses.replace(ORDER, constraints=as_unique)
    assert Model([ORDER]).diff_paths(Model([changed])) == [(ORDER.key, "constraints[pk_order].class")]


def test_diff_paths_never_holds_expression_text():
    changed = column("Status", default=DefaultConstraint("DF_Order_Status", expr("('hunter2')")))
    changed = dataclasses.replace(
        changed, indexes=(dataclasses.replace(ORDER.indexes[0], filter=expr("[Note] = N'hunter2'")),)
    )
    paths = Model([ORDER]).diff_paths(Model([changed]))
    assert paths == [
        (ORDER.key, "columns[status].default.expression"),
        (ORDER.key, "indexes[ix_order_status].filter"),
    ]


def test_diff_paths_without_expressions_compares_structure_only():
    changed = column("Status", default=DefaultConstraint("DF_Order_Status", expr("((1))")))
    assert Model([ORDER]).diff_paths(Model([changed]), expressions=False) == []
    dropped = column("Status", default=None)
    assert Model([ORDER]).diff_paths(Model([dropped]), expressions=False) == [
        (ORDER.key, "columns[status].default")
    ]


def test_diff_paths_names_objects_that_only_one_side_has():
    left, right = Model([ORDER, Schema("sales")]), Model([ORDER, Schema("audit")])
    assert left.diff_paths(right) == [
        ("SCHEMA:[audit]", "(absent in self)"),
        ("SCHEMA:[sales]", "(absent in other)"),
    ]
    assert left.diff_paths(left) == []


def test_an_alias_type_and_a_table_type_under_one_key_differ_in_class():
    alias = AliasType("dbo", "T", TypeRef("int"), True)
    as_table = TableType("dbo", "T", (Column("Id", TypeRef("int"), False),))
    assert alias.key == as_table.key == "TYPE:[dbo].[T]"
    assert Model([alias]).diff_paths(Model([as_table])) == [("TYPE:[dbo].[T]", "class")]


# ------------------------------------------------------------------ invariants of the classes
def test_a_column_is_either_typed_or_computed_and_a_typed_column_states_nullability():
    with pytest.raises(ValueError):
        Column("a", None, None)
    with pytest.raises(ValueError):
        Column("a", TypeRef("int"), None)
    with pytest.raises(ValueError):
        Column("a", TypeRef("int"), False, computed=Computed(expr("(1)")))
    with pytest.raises(ValueError):
        Column("a", None, None, computed=Computed(expr("(1)")), identity=Identity(1, 1))


def test_one_table_cannot_hold_two_things_of_one_name():
    a, b = Column("a", TypeRef("int"), False), Column("b", TypeRef("int"), False)
    with pytest.raises(ValueError, match="two columns"):
        Table("dbo", "T", (a, dataclasses.replace(b, name="A")))
    with pytest.raises(ValueError, match="two constraints"):
        Table("dbo", "T", (a,), (Check("X", expr("(1=1)")), Check("x", expr("(2=2)"))))
    with pytest.raises(ValueError, match="two constraints"):  # a default is a constraint object too
        named = dataclasses.replace(a, default=DefaultConstraint("X", expr("(0)")))
        Table("dbo", "T", (named,), (Check("x", expr("(1=1)")),))
    with pytest.raises(ValueError, match="two indexes"):  # a key owns the index of its name
        key = PrimaryKey("X", True, (KeyColumn("a"),))
        Table("dbo", "T", (a,), (key,), (Index("x", False, False, (KeyColumn("a"),)),))
    with pytest.raises(ValueError, match="no column"):
        Table("dbo", "T", ())


def test_a_table_has_only_named_constraints_and_a_table_type_has_no_foreign_key():
    a = Column("a", TypeRef("int"), False)
    with pytest.raises(ValueError, match="unnamed"):
        Table("dbo", "T", (a,), (PrimaryKey(None, True, (KeyColumn("a"),)),))
    with pytest.raises(ValueError, match="unnamed"):
        Table("dbo", "T", (dataclasses.replace(a, default=DefaultConstraint(None, expr("(0)"))),))
    with pytest.raises(ValueError, match="foreign key"):
        TableType("dbo", "L", (a,), (ForeignKey("FK", ("a",), "dbo", "P", ("a",)),))
    assert (
        TableType("dbo", "L", (a,), (PrimaryKey(None, True, (KeyColumn("a"),)),)).constraints[0].name is None
    )


def test_an_index_option_outside_the_closed_list_cannot_enter_the_model():
    key = (KeyColumn("a"),)
    for options in (
        (("ONLINE", "ON"),),  # an execution option, not a property
        (("FILL_FACTOR", "90"),),
        (("FILLFACTOR", "ninety"),),
        (("DATA_COMPRESSION", "COLUMNSTORE"),),
        (("PAD_INDEX", "on"),),  # values are upper case
        (("PAD_INDEX", "ON"), ("PAD_INDEX", "OFF")),
    ):
        with pytest.raises(ValueError):
            Index("IX", False, False, key, options=options)
        with pytest.raises(ValueError):
            PrimaryKey("PK", True, key, options)
        with pytest.raises(ValueError):
            Unique("UQ", False, key, options)


def test_a_foreign_key_action_is_one_of_the_four_and_the_column_lists_pair_up():
    with pytest.raises(ValueError):
        ForeignKey("FK", ("a",), "dbo", "P", ("a",), on_delete="RESTRICT")
    with pytest.raises(ValueError):
        ForeignKey("FK", ("a", "b"), "dbo", "P", ("a",))


def test_names_are_found_whatever_case_the_caller_uses():
    assert ORDER.column("STATUS") is ORDER.columns[1]
    assert ORDER.constraint("pk_order") is ORDER.constraints[0]
    assert ORDER.index("ix_ORDER_status") is ORDER.indexes[0]
    assert ORDER.column("Missing") is None


# ------------------------------------------------------------------ Model
def test_object_keys_come_from_names_object_key():
    assert [obj.key for obj in EVERY_KIND] == [
        names.object_key("TABLE", "sales", "Order"),
        names.object_key("SCHEMA", None, "sales"),
        names.object_key("TYPE", "dbo", "PhoneNumber"),
        names.object_key("TYPE", "dbo", "IdList"),
        names.object_key("SEQUENCE", "sales", "OrderNo"),
        names.object_key("SYNONYM", "dbo", "LegacyOrder"),
    ]


def test_iteration_is_in_key_order_whatever_the_order_of_insertion():
    forward, backward = Model(EVERY_KIND), Model(EVERY_KIND[::-1])
    assert list(forward) == list(backward) == sorted(forward, key=str.casefold)
    assert list(forward)[0] == "SCHEMA:[sales]"
    assert [obj.key for obj in forward.values()] == list(forward)


def test_lookup_ignores_the_case_of_the_key():
    model = Model(EVERY_KIND)
    assert model["table:[SALES].[order]"] is ORDER
    assert "TABLE:[sales].[ORDER]" in model
    assert model.get("TABLE:[sales].[Missing]") is None
    with pytest.raises(KeyError):
        model["TABLE:[sales].[Missing]"]


def test_add_remove_and_replace_return_a_new_model_and_leave_the_old_one_alone():
    empty = Model()
    one = empty.add(ORDER)
    assert (len(empty), len(one)) == (0, 1)
    changed = column("Status", nullable=True)
    two = one.replace(changed)
    assert one[ORDER.key] is ORDER and two[ORDER.key] is changed
    three = two.remove("table:[sales].[order]")
    assert (len(two), len(three)) == (1, 0)
    assert three == empty


def test_add_of_an_existing_key_and_remove_or_replace_of_a_missing_key_fail():
    model = Model([ORDER])
    with pytest.raises(ValueError, match="object exists"):
        model.add(Table("SALES", "ORDER", ORDER.columns))
    with pytest.raises(KeyError):
        model.remove("TABLE:[sales].[Missing]")
    with pytest.raises(KeyError):
        model.replace(Schema("sales"))
    with pytest.raises(ValueError, match="object exists"):
        Model([Schema("a"), Schema("A")])


def test_a_model_survives_a_canonical_json_round_trip():
    model = Model(EVERY_KIND)
    text = model.to_canonical_json()
    again = Model.from_canonical_json(text)
    assert again == model
    assert again.to_canonical_json() == text
    assert hash(again) == hash(model)
    assert len(again) == len(EVERY_KIND)
    assert isinstance(again["TABLE:[sales].[Order]"], Table)
    assert again["SEQUENCE:[sales].[OrderNo]"] == model["SEQUENCE:[sales].[OrderNo]"]


def test_canonical_json_is_sorted_compact_and_case_folded():
    model = Model([Synonym("dbo", "LegacyOrder", "sales", "Order"), Schema("Sales")])
    assert model.to_canonical_json() == (
        '{"schema:[sales]":{"class":"Schema","name":"sales","owner":null},'
        '"synonym:[dbo].[legacyorder]":{"class":"Synonym","name":"legacyorder","schema":"dbo",'
        '"target_name":"order","target_schema":"sales"}}'
    )


def test_canonical_json_keeps_integers_of_any_size_and_non_ascii_names():
    big = Sequence(
        "dbo", "Ünï", TypeRef("decimal", precision=38, scale=0), 1, 1, -(10**38) + 1, 10**38 - 1, False, False
    )
    again = Model.from_canonical_json(Model([big]).to_canonical_json())
    found = again["SEQUENCE:[dbo].[ünï]"]
    assert isinstance(found, Sequence) and found.maxvalue == 10**38 - 1 and found.minvalue == -(10**38) + 1


def test_text_that_is_not_canonical_model_json_is_rejected():
    column = '{"class":"KeyColumn","name":"a","descending":false}'
    for text in (
        "[]",
        '{"x": {"class": "Nope"}}',
        '{"x": {"class": "Schema", "nam": "a"}}',
        "{",
        f'{{"x": {column}}}',
        '{"x": 1}',
    ):
        with pytest.raises(ValueError):
            Model.from_canonical_json(text)
    with pytest.raises(ValueError, match="does not match"):
        Model.from_canonical_json('{"schema:[b]":{"class":"Schema","name":"a","owner":null}}')


# ------------------------------------------------------------------ operations
def test_operations_compare_like_the_model_they_carry():
    assert CreateTable(ORDER) == CreateTable(
        Table("SALES", "ORDER", ORDER.columns, ORDER.constraints[::-1], ORDER.indexes)
    )
    assert CreateTable(ORDER) != CreateTable(column("Status", nullable=True))
    assert DropTable("sales", "Order") == DropTable("SALES", "order")
    assert DropTable("sales", "Order") != DropTable("sales", "Orders")
    assert hash(DropTable("sales", "Order")) == hash(DropTable("SALES", "order"))


def test_execution_options_are_part_of_the_operation_and_have_no_order():
    ix = ORDER.indexes[0]
    online = CreateIndex("sales", "Order", ix, (("MAXDOP", "4"), ("ONLINE", "ON")))
    assert online == CreateIndex("sales", "Order", ix, (("ONLINE", "ON"), ("MAXDOP", "4")))
    assert online != CreateIndex("sales", "Order", ix)
    assert online.index == ix  # the index itself does not know how it was built
    assert AlterColumn("s", "t", "c", TypeRef("int"), False, exec_options=(("ONLINE", "ON"),)) != AlterColumn(
        "s", "t", "c", TypeRef("int"), False
    )


def test_a_default_constraint_is_added_for_a_column_and_nothing_else_is():
    default = DefaultConstraint("DF", expr("(0)"))
    assert AddConstraint("s", "t", default, for_column="c").for_column == "c"
    with pytest.raises(ValueError):
        AddConstraint("s", "t", default)
    with pytest.raises(ValueError):
        AddConstraint("s", "t", Check("CK", expr("(1=1)")), for_column="c")


def test_the_kind_of_a_rename_fixes_the_number_of_name_parts():
    assert Rename("column", ("sales", "Order", "Stat"), "Status") == Rename(
        "column", ("SALES", "ORDER", "STAT"), "status"
    )
    assert Rename("table", ("sales", "Order"), "Orders") != Rename("object", ("sales", "Order"), "Orders")
    for kind, old in (("column", ("sales", "Order")), ("table", ("sales", "Order", "x")), ("index", ("IX",))):
        with pytest.raises(ValueError):
            Rename(kind, old, "New")  # type: ignore[arg-type]


# ------------------------------------------------------------------ TQ-11: refusals with their reason
def test_an_expression_token_that_is_an_empty_identifier_is_refused():
    for text in ("[]", '""'):
        with pytest.raises(ValueError, match="an identifier cannot be empty"):
            Expression(("[a]", "=", text))


def test_the_length_of_a_type_is_a_number_or_the_word_max_in_lower_case():
    assert TypeRef("varchar", length="max").length == "max"
    for length in ("MAX", "8", "big"):
        with pytest.raises(ValueError, match="length is an integer or 'max'"):
            TypeRef("varchar", length=length)  # type: ignore[arg-type]


# ------------------------------------------------------------------ system-versioned temporal tables
DT2 = TypeRef("datetime2", scale=7)
PK_ID = PrimaryKey("PK_T", True, (KeyColumn("Id"),))
PLAIN_COLUMNS = (Column("Id", TypeRef("int"), False),)
PERIOD_COLUMNS = (
    Column("ValidFrom", DT2, False, generated="ROW_START"),
    Column("ValidTo", DT2, False, generated="ROW_END", hidden=True),
)
VERSIONED = Temporal("ValidFrom", "ValidTo", "s", "T_History")


def temporal_table(**changes: Any) -> Table:
    parts: dict[str, Any] = {
        "columns": PLAIN_COLUMNS + PERIOD_COLUMNS,
        "constraints": (PK_ID,),
        "temporal": VERSIONED,
    }
    return Table("s", "T", **{**parts, **changes})


def test_an_object_that_does_not_use_the_temporal_fields_keeps_its_exact_canonical_json():
    # the hash of every object that existed before this feature must not move: this text is the
    # canonical form as it was written before the fields generated, hidden and temporal existed
    plain = Model([Table("s", "T", PLAIN_COLUMNS, (PK_ID,))])
    assert plain.to_canonical_json() == (
        '{"table:[s].[t]":{"class":"Table","columns":[{"class":"Column","collation":null,"computed":null,'
        '"default":null,"identity":null,"name":"id","nullable":false,"type":{"class":"TypeRef","length":null,'
        '"name":"int","precision":null,"scale":null,"schema":null}}],"constraints":[{"class":"PrimaryKey",'
        '"clustered":true,"columns":[{"class":"KeyColumn","descending":false,"name":"id"}],"name":"pk_t",'
        '"options":[]}],"indexes":[],"name":"t","schema":"s"}}'
    )


def test_the_temporal_fields_are_in_the_canonical_json_when_used_and_read_back_as_an_equal_model():
    model = Model([temporal_table(temporal=dataclasses.replace(VERSIONED, retention=(6, "MONTHS")))])
    text = model.to_canonical_json()
    assert '"generated":"ROW_START"' in text and '"generated":"ROW_END"' in text
    assert text.count('"hidden":true') == 1 and '"hidden":false' not in text
    assert '"retention":[6,"MONTHS"]' in text and '"history_table":"t_history"' in text
    again = Model.from_canonical_json(text)
    assert again == model and again.to_canonical_json() == text


@pytest.mark.parametrize(
    ("changes", "says"),
    [
        ({"temporal": Temporal("ValidFrom", "validfrom", "s", "H")}, "two different columns"),
        ({"temporal": Temporal("ValidTo", "ValidFrom", "s", "H")}, "must be GENERATED ALWAYS AS ROW_START"),
        ({"temporal": Temporal("Id", "ValidTo", "s", "H")}, "must be GENERATED ALWAYS AS ROW_START"),
        ({"temporal": Temporal("Nope", "ValidTo", "s", "H")}, "must be GENERATED ALWAYS AS ROW_START"),
        ({"constraints": ()}, "needs a PRIMARY KEY"),
        ({"temporal": None}, "is not system-versioned"),
        (
            {
                "columns": PLAIN_COLUMNS
                + (PERIOD_COLUMNS[0], dataclasses.replace(PERIOD_COLUMNS[1], nullable=True))
            },
            "must be NOT NULL",
        ),
        (
            {
                "columns": PLAIN_COLUMNS
                + (dataclasses.replace(PERIOD_COLUMNS[0], type=TypeRef("datetime")), PERIOD_COLUMNS[1])
            },
            "must be datetime2",
        ),
        (
            {"columns": PLAIN_COLUMNS + PERIOD_COLUMNS + (Column("Third", DT2, False, generated="ROW_END"),)},
            "only the two period columns",
        ),
        (  # the engine wants one precision for both period columns (seen live: error 13513)
            {
                "columns": PLAIN_COLUMNS
                + (
                    dataclasses.replace(PERIOD_COLUMNS[0], type=TypeRef("datetime2", scale=0)),
                    PERIOD_COLUMNS[1],
                )
            },
            "must have one datetime2 scale",
        ),
    ],
)
def test_a_temporal_table_that_the_engine_would_refuse_is_not_a_table_of_the_model(changes, says):
    assert temporal_table() == temporal_table()  # the unchanged table is a table of the model
    with pytest.raises(ValueError, match=says):
        temporal_table(**changes)


@pytest.mark.parametrize(
    "make",
    [
        lambda: Column("A", DT2, False, hidden=True),  # HIDDEN goes with GENERATED ALWAYS only
        lambda: Column("A", DT2, False, generated="ALWAYS"),
        lambda: Temporal("a", "b", "s", "h", (6, "MONTH")),  # the model holds the plural
        lambda: Temporal("a", "b", "s", "h", (0, "DAYS")),
        lambda: Temporal("a", "b", "s", "h", (-1, "DAYS")),
        lambda: Temporal("a", "b", "s", "h", (2147483648, "DAYS")),  # sys.tables holds an int
        lambda: Temporal("a", "b", "s", "h", (True, "DAYS")),
        lambda: SetSystemVersioning("s", "T", True, "s", "H", (99999999999, "YEARS")),
        lambda: SetSystemVersioning("s", "T", True),  # ON names the history table
        lambda: SetSystemVersioning("s", "T", False, "s", "H"),
        lambda: SetSystemVersioning("s", "T", False, retention=(1, "DAYS")),
    ],
)
def test_temporal_values_that_have_no_sql_form_are_refused(make):
    with pytest.raises(ValueError):
        make()


def test_temporal_names_compare_case_folded_and_the_retention_is_part_of_the_table():
    assert temporal_table(temporal=Temporal("VALIDFROM", "validto", "S", "t_history")) == temporal_table()
    other_history = temporal_table(temporal=dataclasses.replace(VERSIONED, history_table="Other"))
    kept_a_year = temporal_table(temporal=dataclasses.replace(VERSIONED, retention=(1, "YEARS")))
    assert other_history != temporal_table() and kept_a_year != temporal_table()
    assert hash(temporal_table(temporal=Temporal("VALIDFROM", "validto", "S", "t_history"))) == hash(
        temporal_table()
    )
    paths = Model([temporal_table()]).diff_paths(Model([kept_a_year]))
    assert paths == [("TABLE:[s].[T]", "temporal.retention")]


def test_a_difference_in_a_field_that_only_one_side_writes_is_named_and_does_not_fail():
    plain = Model([Table("s", "T", PLAIN_COLUMNS, (PK_ID,))])
    paths = dict.fromkeys(path for _, path in plain.diff_paths(Model([temporal_table()])))
    assert "temporal" in paths and any(path.startswith("columns[validfrom]") for path in paths)


def test_a_table_with_versioning_switched_off_keeps_its_period_columns_and_equals_no_table():
    off = UnversionedTable("s", "T", PLAIN_COLUMNS + PERIOD_COLUMNS, (PK_ID,))
    assert [c.generated for c in off.columns] == [None, "ROW_START", "ROW_END"]
    assert off != temporal_table() and off.key == temporal_table().key
    with pytest.raises(ValueError, match="versioning is on"):
        UnversionedTable("s", "T", PLAIN_COLUMNS + PERIOD_COLUMNS, (PK_ID,), (), VERSIONED)
    assert Model.from_canonical_json(Model([off]).to_canonical_json()) == Model([off])


# ------------------------------------------------------------------ dynamic data masking
def test_a_column_without_a_mask_keeps_the_json_and_the_hash_it_had_before_masks_existed():
    plain = masking.Column("Mail", masking.TypeRef("nvarchar", length=320), False)
    table = masking.Table("dbo", "T", (plain,))

    assert "masked" not in masking.Model([table]).to_canonical_json()
    assert (
        "masked"
        in masking.Model(
            [masking.Table("dbo", "T", (dataclasses.replace(plain, masked="email()"),))]
        ).to_canonical_json()
    )


def test_a_mask_takes_part_in_equality_and_is_compared_as_text_with_its_letter_case():
    def column(function: str | None) -> masking.Column:
        return masking.Column("Phone", masking.TypeRef("varchar", length=20), True, masked=function)

    assert column('partial(1, "XX", 0)') == column('partial(1, "XX", 0)')
    assert column('partial(1, "XX", 0)') != column('partial(1, "xx", 0)')  # the padding is data, not a name
    assert column("default()") != column(None)
    assert hash(column("default()")) != hash(column("email()"))


def test_a_model_with_a_mask_comes_back_from_its_canonical_json():
    column = masking.Column(
        "Phone", masking.TypeRef("varchar", length=20), True, masked='partial(1, "X""x", 0)'
    )
    model = masking.Model([masking.Table("dbo", "T", (column,))])

    again = masking.Model.from_canonical_json(model.to_canonical_json())

    assert again == model


@pytest.mark.parametrize(
    "build",
    [
        lambda: masking.Column("C", masking.TypeRef("int"), True, masked=""),
        lambda: masking.Column("C", masking.TypeRef("int"), True, masked="  "),
        lambda: masking.Column(
            "C",
            None,
            None,
            computed=masking.Computed(masking.Expression.from_sql("([a] + 1)")),
            masked="default()",
        ),
        lambda: masking.Column(
            "C", masking.TypeRef("datetime2", scale=7), False, generated="ROW_START", masked="default()"
        ),
        lambda: masking.MaskColumn("dbo", "T", "C", ""),
        lambda: masking.MaskColumn("dbo", "T", "C", 'partial(1, "it\'s", 0)'),
        lambda: masking.Column("C", masking.TypeRef("int"), True, masked="default()'; DROP TABLE [x]; --"),
        lambda: masking.TableType(
            "dbo", "L", (masking.Column("C", masking.TypeRef("int"), True, masked="default()"),)
        ),
    ],
    ids=[
        "empty",
        "blank",
        "computed",
        "period column",
        "empty operation",
        "one quote",
        "end of literal",
        "table type",
    ],
)
def test_a_mask_that_the_engine_refuses_or_that_this_version_does_not_hold_is_a_value_error(build):
    with pytest.raises(ValueError, match="mask"):
        build()


def test_a_doubled_quote_in_a_mask_is_the_text_that_the_engine_stores_and_is_kept():
    function = "partial(1, \"it''s\", 0)"

    assert masking.Column("C", masking.TypeRef("varchar", length=9), True, masked=function).masked == function
    assert masking.MaskColumn("dbo", "T", "C", function).function == function


# every pair was read from sys.masked_columns.masking_function on Azure SQL Database
@pytest.mark.parametrize(
    ("written", "scale", "stored"),
    [
        ("DEFAULT()", None, "default()"),
        ("default( )", None, "default()"),
        ("EMAIL( )", None, "email()"),
        ('Partial(1,"xX",0)', None, 'partial(1, "xX", 0)'),
        ('partial( 1 , "x" , 2 )', None, 'partial(1, "x", 2)'),
        ('partial(01, "x", 2)', None, 'partial(1, "x", 2)'),
        ('partial(0,"",0)', None, 'partial(0, "", 0)'),
        ("RANDOM(1,12)", None, "random(1, 12)"),
        ("random(1,  12)", None, "random(1, 12)"),
        ("random(01, 12)", None, "random(1, 12)"),
        ("random(1, 12)", 2, "random(1.00, 12.00)"),
        ("random(1, 12.5)", 4, "random(1.0000, 12.5000)"),
        ("random(1, 12)", 0, "random(1, 12)"),
        ('datetime( "m" )', None, 'datetime("m")'),
    ],
)
def test_mask_spelling_gives_the_text_that_the_engine_stores(written: str, scale: int | None, stored: str):
    assert masking.mask_spelling(written, scale) == stored
    assert masking.mask_spelling(stored, scale) == stored


@pytest.mark.parametrize(
    "stored",
    [
        "default()",
        "email()",
        'partial(1, "XX\'X ,)", 0)',  # a comma and a parenthesis inside the padding
        'partial(1, "x""y", 2)',
        'partial(1, "a  b", 2)',  # spaces inside the padding are data
        "random(-5, 12)",
        "random(1.5, 12)",  # float: the engine keeps the digits as written
        "random(1.50, 12.00)",
        'datetime("Y")',
        "not a call",
    ],
)
def test_mask_spelling_leaves_a_text_in_the_spelling_of_the_engine_as_it_is(stored: str):
    assert masking.mask_spelling(stored) == stored


# ------------------------------------------------------------------ wider table coverage
GUID = TypeRef("uniqueidentifier")
NUMBER = TypeRef("int")


def test_a_new_field_is_in_the_canonical_form_only_when_the_object_uses_the_feature():
    plain = Table(
        "s",
        "T",
        (Column("Id", NUMBER, False, identity=Identity(1, 1)), Column("Other", NUMBER, True)),
        (
            PrimaryKey("PK_T", False, (KeyColumn("Id"),)),
            Check("CK_T", Expression.from_sql("([Id] > 0)")),
            ForeignKey("FK_T", ("Other",), "s", "T", ("Id",)),
        ),
        (Index("IX_T", False, False, (KeyColumn("Other"),)),),
    )
    text = Model([plain]).to_canonical_json()
    for word in ("not_for_replication", "rowguidcol", "sparse", "compression", "columnstore"):
        assert word not in text
    used = dataclasses.replace(
        plain,
        columns=(
            Column("Id", NUMBER, False, identity=Identity(1, 1, True)),
            Column("Other", NUMBER, True, sparse=True),
            Column("Guid", GUID, False, rowguidcol=True),
        ),
        constraints=(
            PrimaryKey("PK_T", False, (KeyColumn("Id"),)),
            Check("CK_T", Expression.from_sql("([Id] > 0)"), True),
            ForeignKey("FK_T", ("Other",), "s", "T", ("Id",), not_for_replication=True),
        ),
        indexes=(Index("CCI_T", False, False, (), ("Id",), columnstore=True),),
        compression="PAGE",
    )
    model = Model([used])
    again = Model.from_canonical_json(model.to_canonical_json())
    assert again == model and again.to_canonical_json() == model.to_canonical_json()
    assert Model([plain]).diff_paths(model) == [
        ("TABLE:[s].[T]", "columns[guid] (absent in self)"),
        ("TABLE:[s].[T]", "columns[id].identity.not_for_replication"),
        ("TABLE:[s].[T]", "columns[other].sparse"),
        ("TABLE:[s].[T]", "constraints[ck_t].not_for_replication"),
        ("TABLE:[s].[T]", "constraints[fk_t].not_for_replication"),
        ("TABLE:[s].[T]", "indexes[cci_t] (absent in self)"),
        ("TABLE:[s].[T]", "indexes[ix_t] (absent in other)"),
        ("TABLE:[s].[T]", "compression"),
    ]


@pytest.mark.parametrize(
    ("build", "says"),
    [
        (lambda: Column("a", NUMBER, False, rowguidcol=True), "uniqueidentifier"),
        (lambda: Column("a", TypeRef("Guid", "dbo"), False, rowguidcol=True), "uniqueidentifier"),
        (lambda: Column("a", NUMBER, False, sparse=True), "a SPARSE column is NULL"),
        (lambda: Column("a", NUMBER, True, identity=Identity(1, 1), sparse=True), "SPARSE"),
        (lambda: Column("a", GUID, True, rowguidcol=True, sparse=True), "SPARSE"),
        (
            lambda: Column("a", None, None, computed=Computed(Expression.from_sql("(1)")), sparse=True),
            "cannot have ROWGUIDCOL or SPARSE",
        ),
        (
            lambda: Table(
                "s",
                "T",
                (Column("a", GUID, False, rowguidcol=True), Column("b", GUID, False, rowguidcol=True)),
            ),
            "more than one ROWGUIDCOL column",
        ),
        (lambda: Table("s", "T", (Column("a", NUMBER, True),), compression="NONE"), "ROW or PAGE"),
        (
            lambda: Table(
                "s",
                "T",
                (Column("a", NUMBER, False),),
                (PrimaryKey("PK_T", True, (KeyColumn("a"),)),),
                compression="ROW",
            ),
            "the compression of a heap; state it on the clustered key or index [PK_T]",
        ),
        (
            lambda: Table(
                "s",
                "T",
                (Column("a", NUMBER, False),),
                indexes=(Index("CCI", False, True, (), columnstore=True),),
                compression="ROW",
            ),
            "the compression of a heap",
        ),
        (lambda: Index("I", True, False, (), ("a",), columnstore=True), "cannot be UNIQUE"),
        (
            lambda: Index("I", False, False, (KeyColumn("a", True),), ("a",), columnstore=True),
            "cannot be DESC",
        ),
        (lambda: Index("I", False, True, (), ("a",), columnstore=True), "no column list and no filter"),
        (lambda: Index("I", False, False, (), (), columnstore=True), "needs a column list"),
        (
            lambda: Index("I", False, True, (), options=(("FILLFACTOR", "90"),), columnstore=True),
            "index option",
        ),
        (
            lambda: Index("I", False, True, (), options=(("DATA_COMPRESSION", "PAGE"),), columnstore=True),
            "PAGE",
        ),
        (
            lambda: Index("I", False, False, (KeyColumn("a"),), options=(("COMPRESSION_DELAY", "5"),)),
            "index option",
        ),
        (
            lambda: Index("I", False, False, (KeyColumn("a"),), options=(("XML_COMPRESSION", "1"),)),
            "XML_COMPRESSION",
        ),
        (
            lambda: Table(
                "s",
                "T",
                (Column("a", NUMBER, False),),
                indexes=(
                    Index("C1", False, True, (), columnstore=True),
                    Index("C2", False, False, (), ("a",), columnstore=True),
                ),
            ),
            "more than one columnstore index",
        ),
        (
            lambda: TableType("s", "L", (Column("a", NUMBER, True, sparse=True),)),
            "cannot have a SPARSE column",
        ),
        (
            lambda: TableType("s", "L", (Column("a", NUMBER, False, identity=Identity(1, 1, True)),)),
            "cannot have NOT FOR REPLICATION",
        ),
        (
            lambda: TableType(
                "s",
                "L",
                (Column("a", NUMBER, False),),
                indexes=(Index("C", False, False, (), ("a",), columnstore=True),),
            ),
            "cannot have a columnstore index",
        ),
        (lambda: masking.AlterColumnProperty("s", "T", "c", True, "MASKED"), "not a column property"),
        (lambda: masking.RebuildTable("s", "T", "COLUMNSTORE"), "DATA_COMPRESSION of a table"),
    ],
)
def test_what_the_engine_refuses_of_the_wider_coverage_is_a_value_error(build, says: str):
    with pytest.raises(ValueError, match=says.replace("[", r"\[").replace("]", r"\]")):
        build()


def test_the_clustered_item_is_the_key_or_index_that_orders_the_table():
    heap = Table("s", "T", (Column("a", NUMBER, False),), compression="PAGE")
    assert heap.clustered_item() is None
    key = PrimaryKey("PK_T", True, (KeyColumn("a"),))
    assert dataclasses.replace(heap, constraints=(key,), compression=None).clustered_item() == key
    columnstore = Index("CCI", False, True, (), columnstore=True)
    assert dataclasses.replace(heap, indexes=(columnstore,), compression=None).clustered_item() == columnstore
    # a table type takes ROWGUIDCOL
    assert TableType("s", "L", (Column("a", GUID, False, rowguidcol=True),)).columns[0].rowguidcol


def test_a_retention_is_a_whole_number_from_1_that_the_catalog_can_hold():
    assert Temporal("a", "b", "s", "h", (1, "DAYS")).retention == (1, "DAYS")
    assert Temporal("a", "b", "s", "h", (2147483647, "DAYS")).retention == (2147483647, "DAYS")
    with pytest.raises(ValueError, match="1 <= n <= 2147483647"):
        Temporal("a", "b", "s", "h", (2147483648, "DAYS"))
