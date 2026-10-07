"""The table model against a database: hooks of plan and deploy, export, baseline compare.

The live catalog is FakeSession with the rows that test_catalog_tables.rows_from_model writes for a
model; a test changes single facts of those rows. A release is a small in-memory bundle whose table
files are the canonical text of a model. No test runs T-SQL.
"""

import dataclasses
import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

import pytest

from azsqlcd import (
    catalog,
    catalog_tables,
    chain,
    emit,
    lex,
    modules,
    names,
    plan,
    release,
    runner,
    state,
    tables,
)
from azsqlcd.catalog import Difference
from azsqlcd.catalog_tables import SystemNamed, Unsupported, read_model
from azsqlcd.config import Config, load_config
from azsqlcd.errors import Exit, ToolError
from azsqlcd.model import (
    AliasType,
    Check,
    Column,
    Computed,
    DefaultConstraint,
    Expression,
    ForeignKey,
    Index,
    KeyColumn,
    Model,
    ModelObject,
    PrimaryKey,
    Schema,
    Sequence,
    Synonym,
    Table,
    TableType,
    TypeRef,
)
from azsqlcd.parse import parse_object_file
from azsqlcd.plan import ModuleChange, TableFindings, Work
from azsqlcd.release import Bundle, Manifest
from azsqlcd.sqlerrors import sql_error
from azsqlcd.state import Meta, ObjectRow, State
from azsqlcd.tables import (
    DEPENDS_ON_UNMANAGED,
    DIFFERS,
    EQUAL,
    MISSING_HERE,
    ONLY_HERE,
    ROUNDTRIP,
    BaselineItem,
    Hooks,
    compare_with_snapshot,
    export_tables,
)
from fixtures.pairs import loader
from support.fake_session import FakeSession
from unit.test_catalog_tables import (
    CUSTOMER,
    CUSTOMER_KEY,
    ORDER,
    ORDER_KEY,
    PRICE,
    PRICE_HISTORY_KEY,
    PRICE_KEY,
    QUOTE,
    SALES,
    CatalogRows,
    only,
    price,
    price_rows,
    rows_from_model,
    session_for,
    versioning_off,
)

M1, M2, M3 = "0001__a.sql", "0002__b.sql", "0003__c.sql"
SECRET = "N'never-in-a-report'"
WRITES = frozenset("INSERT UPDATE DELETE MERGE EXEC EXECUTE CREATE ALTER DROP INTO SET".split())
VIEW = "VIEW:[rpt].[vMargin]"
TOML = """
[project]
name = "sales"
tenant_id = "11111111-1111-1111-1111-111111111111"
table_model = {table_model}
module_chunk = 100
min_token_minutes = 20

[identities]
plan = "22222222-2222-2222-2222-222222222222"
deploy = "33333333-3333-3333-3333-333333333333"

[env.dev]
plan_identity = "plan"
deploy_identity = "deploy"
drift = "report"
lock_timeout_ms = 30000
applock_wait_s = 600
job_timeout_minutes = 120
targets = [{{ id = "sales-dev", server = "sql-sales-dev.database.windows.net", database = "sales" }}]

[unmanaged]
objects = {unmanaged}

[ack]
unmanaged_dependants = {ack}
"""
MODEL = Model([SALES, ORDER, CUSTOMER])
TEMPORAL = Model([SALES, PRICE, QUOTE])  # a system-versioned table and a table that references it


def config(
    *, ack: list[str] | None = None, unmanaged: list[str] | None = None, table_model: bool = True
) -> Config:
    text = TOML.format(
        table_model=str(table_model).lower(), unmanaged=json.dumps(unmanaged or []), ack=json.dumps(ack or [])
    )
    return load_config(text)


# ------------------------------------------------------------------ a release, a state, a database
def mig(file: str, *batches: str) -> tuple[str, str]:
    header = f"-- azsqlcd:migration {file.removesuffix('.sql')}\n-- azsqlcd:mode tx\n"
    return file, header + "\nGO\n".join(batches) + "\nGO\n"


def bundle(model: Model = MODEL, *migrations: tuple[str, str], withdrawn: tuple[str, ...] = ()) -> Bundle:
    """A release whose table files are the canonical text of the model."""
    files: dict[str, bytes] = {release.CONFIG_PATH: b""}
    for obj in model.values():
        schema = None if isinstance(obj, Schema) else obj.schema
        files[names.path_for(obj.kind, schema, obj.name)] = emit.emit_object_file(obj).encode()
    entries = []
    for file, text in migrations:
        files[f"migrations/{file}"] = text.encode()
        entries.append(chain.ChainEntry(file, chain.file_sha256(text.encode()), "tx", file in withdrawn))
    files[chain.SUM_PATH] = chain.format_sum(chain.Chain(True, tuple(entries))).encode()
    listed = tuple(sorted((path, hashlib.sha256(data).hexdigest()) for path, data in files.items()))
    return Bundle(Manifest("c" * 40, 7, listed, {file: 7 for file, _ in migrations}), files)


def pending(*migrations: tuple[str, str], **more: Any) -> Work:
    return Work("work", (), tuple(chain.parse_migration(text, file) for file, text in migrations), **more)


def hooks(model: Model = MODEL, *migrations: tuple[str, str]) -> Hooks:
    return Hooks(bundle(model, *migrations), config())


def captures_of(model: Model) -> dict[str, dict[str, Any]]:
    """What a baseline of a database that holds exactly this model records."""
    return read_model(session_for(rows_from_model(model))).captures


def module_row(type_code: str) -> ObjectRow:
    capture = {"type": type_code, "definition": "..."}
    return ObjectRow("managed", "0" * 64, 1, capture, state.capture_sha256(capture))


def recorded(model: Model = MODEL, modules: dict[str, str] | None = None) -> State:
    """The state after a baseline of the model. modules: key of a managed module -> sys.objects.type."""
    objects = {
        key: ObjectRow("managed", None, 1, capture, state.capture_sha256(capture))
        for key, capture in captures_of(model).items()
    }
    objects |= {key: module_row(code) for key, code in (modules or {}).items()}
    return State(Meta(1, "sales", "dev"), (), None, (), objects)


def database(
    live: Model | CatalogRows = MODEL,
    stored: State | None = None,
    *,
    blockers: list[tuple[str, str, str, str]] | None = None,
    dependants: list[tuple[str, str, str, int]] | None = None,
    broken: tuple[str, ...] = (),
    present: list[int] | None = None,
) -> FakeSession:
    """A session on a database with this catalog and this recorded state.

    blockers, dependants, present: the rows of azsqlcd:table_blockers, azsqlcd:dependants_of and
    azsqlcd:names_present. broken: names of modules that do not bind (azsqlcd:broken_references).
    """
    db = session_for(rows_from_model(live) if isinstance(live, Model) else live)
    stored = stored or recorded()
    db.respond("azsqlcd:read_state.tables", [[(table, 1) for table in state.TABLES]])
    db.respond("azsqlcd:read_state.meta", [[(1, "sales", "dev")]])
    db.respond(
        "azsqlcd:read_state.objects",
        [
            [
                (key, row.status, row.source_sha256, 1, state.capture_json(row.capture), row.catalog_sha256)
                for key, row in stored.objects.items()
            ]
        ],
    )
    db.respond("azsqlcd:table_blockers", [blockers or []])
    db.respond("azsqlcd:dependants_of", [dependants or []])
    unresolved = [("sales", "Gone", None, None, 0, 1, 0, None)]
    db.respond(
        lambda batch: "azsqlcd:broken_references" in batch and any(n in batch for n in broken), [unresolved]
    )
    db.respond("azsqlcd:names_present", [[(n,) for n in present or []]])
    return db


def refusal(call: Callable[..., object], *args: Any, **kwargs: Any) -> ToolError:
    with pytest.raises(ToolError) as caught:
        call(*args, **kwargs)
    return caught.value


def with_columns(table: Table, *columns: Column, drop: tuple[str, ...] = ()) -> Table:
    kept = tuple(c for c in table.columns if c.name not in drop)
    return dataclasses.replace(table, columns=(*kept, *columns))


def with_parts(table: Table, *, constraints: tuple = (), indexes: tuple = ()) -> Table:
    return dataclasses.replace(
        table, constraints=(*table.constraints, *constraints), indexes=(*table.indexes, *indexes)
    )


IX_DBA = Index("IX_dba", False, False, (KeyColumn("Note"),))


# ------------------------------------------------------------------ the release
def test_hooks_are_only_for_a_project_with_a_table_model():
    with pytest.raises(ValueError, match="table_model = true"):
        Hooks(bundle(), config(table_model=False))


def test_the_head_model_is_what_the_table_files_of_the_release_say():
    assert hooks().model == MODEL
    assert hooks(loader.model("every_step_in_the_fixed_order", "head")).model == loader.model(
        "every_step_in_the_fixed_order", "head"
    )


def test_module_files_and_tombstones_of_the_release_are_not_part_of_the_table_model():
    b = bundle()
    b.files["schema/views/rpt.vMargin.sql"] = b"CREATE OR ALTER VIEW [rpt].[vMargin] AS SELECT 1 AS [x];\n"
    b.files[chain.TOMBSTONES_PATH] = b""

    assert Hooks(b, config()).model == MODEL


def test_a_table_file_that_does_not_parse_stops_the_hooks_with_file_and_line():
    b = bundle()
    b.files["schema/tables/sales.Order.sql"] = b"CREATE TABLE [sales].[Order] (\n    [a] int\n);\n"

    error = refusal(Hooks, b, config())

    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "MODEL_INVALID")
    assert error.detail == {"path": "schema/tables/sales.Order.sql", "line": 2}
    assert "NF001" in error.message


# ------------------------------------------------------------------ what a release touches
NEW_SEQUENCE = (
    "CREATE SEQUENCE [sales].[No] AS int START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9 NO CYCLE NO CACHE;"
)


def test_touched_objects_come_from_the_model_batches_of_the_pending_migrations():
    work = pending(
        mig(
            M1,
            "ALTER TABLE [SALES].[order] ADD [Extra] int NULL;",
            "-- azsqlcd:data\nUPDATE [sales].[Customer] SET [CustomerId] = [CustomerId];",
        ),
        mig(M2, "DROP TABLE [sales].[OLD];", NEW_SEQUENCE),
    )

    touched = hooks().touched_table_objects(work)

    # the spelling of the files where the release holds the object; a data batch is not modelled
    assert touched == ["SEQUENCE:[sales].[No]", "TABLE:[sales].[OLD]", ORDER_KEY]


def test_a_rename_of_a_constraint_is_followed_with_the_head_model():
    renamed = dataclasses.replace(
        CUSTOMER, constraints=(PrimaryKey("PK_Customer_New", True, (KeyColumn("CustomerId"),)),)
    )
    work = pending(mig(M1, "EXEC sys.sp_rename N'[sales].[PK_Customer]', N'PK_Customer_New', N'OBJECT';"))

    assert hooks(Model([SALES, ORDER, renamed])).touched_table_objects(work) == [CUSTOMER_KEY]


def test_a_rename_that_the_release_cannot_follow_is_refused_not_guessed():
    work = pending(mig(M1, "EXEC sys.sp_rename N'[sales].[CK_gone]', N'CK_new', N'OBJECT';"))

    error = refusal(hooks().touched_table_objects, work)

    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "RENAME_NOT_RESOLVED")


def test_a_model_batch_outside_the_grammar_is_refused_with_file_and_line():
    work = pending(
        mig(M1, "ALTER TABLE [sales].[Order] ADD [a] int NULL;", "ALTER TABLE [sales].[Order]\nREBUILD;")
    )

    error = refusal(hooks().touched_table_objects, work)

    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "MIGRATION_INVALID")
    assert error.detail == {"file": M1, "line": 6}


def test_an_object_of_the_release_with_no_row_and_no_migration_that_creates_it_needs_a_baseline():
    code = AliasType("sales", "Code", TypeRef("varchar", length=10), False)
    orders = dataclasses.replace(ORDER, name="Orders")
    h = hooks(Model([SALES, orders, CUSTOMER, code, Schema("audit")]))
    work = pending(
        mig(
            M1,
            "CREATE SCHEMA [audit];",
            "EXEC sys.sp_rename N'[sales].[Order]', N'Orders', N'OBJECT';",
            "ALTER TABLE [sales].[Orders] ADD [Extra] int NULL;",
        )
    )

    # the type has a file and no row, and nothing pending creates it: it was never compared
    assert h.unrecorded_objects(work, recorded()) == [code.key]
    assert h.unrecorded_objects(pending(), recorded()) == ["SCHEMA:[audit]", orders.key, code.key]
    assert hooks().unrecorded_objects(pending(), recorded()) == []


# ------------------------------------------------------------------ drift
def test_a_database_that_equals_its_recorded_captures_has_no_drift():
    db = database()

    assert hooks().table_drift(db, recorded(), list(MODEL)) == {}
    (batch,) = db.sent("azsqlcd:read_tabulars")
    assert "OBJECT_ID(N'[sales].[Order]')" in batch  # only the asked objects are read


def test_drift_names_the_sub_object_and_gives_hashes_not_values():
    rows = rows_from_model(MODEL)
    only(rows, "columns", name="Note")["max_length"] = 200
    only(rows, "checks", name="CK_Order_Status")["definition"] = f"([Status]<len({SECRET}))"

    drift = hooks().table_drift(database(rows), recorded(), list(MODEL))

    assert list(drift) == [ORDER_KEY]
    assert [d.property for d in drift[ORDER_KEY]] == ["column [Note]", "constraint [CK_Order_Status]"]
    for difference in drift[ORDER_KEY]:
        assert re.fullmatch("[0-9a-f]{64}", difference.stored_sha256)
        assert re.fullmatch("[0-9a-f]{64}", difference.live_sha256 or "")
        assert difference.stored_sha256 != difference.live_sha256
    assert "never-in-a-report" not in repr(drift)


def test_a_live_sub_object_that_is_not_recorded_is_not_drift_and_a_recorded_one_that_is_gone_is():
    extra = with_parts(ORDER, indexes=(IX_DBA,), constraints=(Check("CK_dba", Expression.from_sql("(1=1)")),))
    assert hooks().table_drift(database(Model([SALES, extra, CUSTOMER])), recorded(), [ORDER_KEY]) == {}

    fewer = dataclasses.replace(ORDER, indexes=())
    drift = hooks().table_drift(database(Model([SALES, fewer, CUSTOMER])), recorded(), [ORDER_KEY])

    assert [(d.property, d.live_sha256) for d in drift[ORDER_KEY]] == [("index [IX_Order_Status]", None)]


def test_the_start_value_of_a_sequence_is_not_drift_and_every_other_property_of_it_is():
    # RO-3: on Azure SQL Database sys.sequences.start_value equals current_value on every sequence
    # of a database that reseeds with ALTER SEQUENCE ... RESTART WITH. Until the live spike says
    # whether RESTART WITH moves start_value, a difference of it is not reported as drift.
    number = Sequence("sales", "No", TypeRef("bigint"), 1, 1, 1, 9_000_000, False, True, 50)
    model = Model([SALES, number])
    stored = recorded(model)
    assert stored.objects[number.key].capture["start_value"] == 1  # the capture still holds the value

    def drift_of(**change: Any) -> dict[str, list[Difference]]:
        live = Model([SALES, dataclasses.replace(number, **change)])
        return hooks(model).table_drift(database(live, stored), stored, [number.key])

    assert drift_of(start=73596) == {}
    assert [d.property for d in drift_of(start=73596, increment=5)[number.key]] == ["increment"]
    assert [d.property for d in drift_of(maxvalue=10_000_000)[number.key]] == ["maximum_value"]
    assert catalog_tables.NOT_IN_DRIFT == {"SEQUENCE": frozenset({"start_value"})}
    # the model keeps START WITH: a read-back against the file of the release still compares it
    reseeded = Model([SALES, dataclasses.replace(number, start=73596)])
    error = refusal(hooks(model).read_back, database(reseeded, stored), bundle(model), [number.key], None)
    assert error.reason_code == "READBACK_MISMATCH" and error.detail["objects"][0]["properties"] == ["start"]


def test_a_property_named_start_value_of_another_kind_of_object_is_compared():
    capture = {"kind": "SYNONYM", "base_object_name": "[dbo].[Legacy]", "start_value": 1}
    row = ObjectRow("managed", None, 1, capture, state.capture_sha256(capture))
    key = "SYNONYM:[sales].[Old]"
    stored = State(Meta(1, "sales", "dev"), (), None, (), {key: row})
    live = rows_from_model(Model([SALES, Synonym("sales", "Old", "dbo", "Legacy")]))

    drift = Hooks(bundle(Model([SALES])), config()).table_drift(database(live, stored), stored, [key])

    assert [d.property for d in drift[key]] == ["start_value"]


def test_a_column_that_only_the_database_has_is_drift():
    wider = with_columns(ORDER, Column("Extra", TypeRef("int"), True))

    drift = hooks().table_drift(database(Model([SALES, wider, CUSTOMER])), recorded(), [ORDER_KEY])

    assert [d.property for d in drift[ORDER_KEY]] == ["columns"]


def test_a_recorded_object_that_is_gone_or_outside_the_model_is_a_difference():
    stored = recorded()
    gone = hooks().table_drift(database(Model([SALES, CUSTOMER])), stored, [ORDER_KEY, CUSTOMER_KEY])
    rows = rows_from_model(MODEL)
    only(rows, "tabulars", name="Order")["ledger_type"] = 2
    outside = hooks().table_drift(database(rows), stored, [ORDER_KEY])

    whole = stored.objects[ORDER_KEY].catalog_sha256
    assert gone == {ORDER_KEY: [Difference("exists", whole, None)]}
    assert outside == {ORDER_KEY: [Difference("unsupported", whole, None)]}


def test_drift_sees_versioning_switched_off():
    stored = recorded(TEMPORAL)
    whole = stored.objects[PRICE_KEY].catalog_sha256
    assert hooks(TEMPORAL).table_drift(database(price_rows(), stored), stored, list(TEMPORAL)) == {}

    # SET (SYSTEM_VERSIONING = OFF): the period stays, and no table file can say that
    off = hooks(TEMPORAL).table_drift(database(versioning_off(price_rows()), stored), stored, list(TEMPORAL))
    # ... and DROP PERIOD FOR SYSTEM_TIME: a plain table again
    plain = versioning_off(price_rows(), drop_period=True)
    dropped = hooks(TEMPORAL).table_drift(database(plain, stored), stored, list(TEMPORAL))

    assert off == {PRICE_KEY: [Difference("unsupported", whole, None)]}
    assert [d.property for d in dropped[PRICE_KEY]] == ["column [ValidFrom]", "column [ValidTo]", "temporal"]
    assert list(dropped) == [PRICE_KEY]
    assert dropped[PRICE_KEY][2].live_sha256 is None  # the live capture has no 'temporal'


@pytest.mark.parametrize(
    "change",
    [
        {"history_table": "Price_Old"},
        {"history_retention_period": 6, "history_retention_period_unit": 5},
    ],
)
def test_drift_sees_another_history_table_and_another_retention(change: dict[str, Any]):
    stored = recorded(TEMPORAL)
    rows = price_rows()
    only(rows, "tabulars", name="Price").update(change)

    drift = hooks(TEMPORAL).table_drift(database(rows, stored), stored, list(TEMPORAL))

    assert [d.property for d in drift[PRICE_KEY]] == ["temporal"] and list(drift) == [PRICE_KEY]


def test_no_key_to_compare_sends_no_batch():
    db = database()

    assert hooks().table_drift(db, recorded(), []) == {}
    assert db.batches == []


# ------------------------------------------------------------------ read-back
def read_back(
    live: Model | CatalogRows, model: Model = MODEL, keys: list[str] | None = None
) -> dict[str, Any]:
    h = hooks(model)
    return h.read_back(database(live), bundle(model), keys or list(model), None)


def mismatch(live: Model | CatalogRows, model: Model = MODEL, keys: list[str] | None = None) -> ToolError:
    error = refusal(read_back, live, model, keys)
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "READBACK_MISMATCH")
    return error


def test_a_database_that_holds_the_model_reads_back_and_gives_the_captures_to_record():
    captures = read_back(MODEL, keys=[*MODEL, "TABLE:[sales].[Dropped]"])

    assert captures == captures_of(MODEL) | {"TABLE:[sales].[Dropped]": None}


def test_a_temporal_table_reads_back_and_its_capture_holds_the_temporal_facts():
    captures = read_back(TEMPORAL, TEMPORAL)

    assert captures == captures_of(TEMPORAL)
    assert captures[PRICE_KEY]["temporal"]["history_table"] == "Price_History"
    assert PRICE_HISTORY_KEY not in captures


@pytest.mark.parametrize(
    ("live", "path"),
    [
        (price(retention=(6, "MONTHS")), "temporal.retention"),
        (price(history=("sales", "Price_Old")), "temporal.history_table"),
        (price(hidden=True), "columns[validfrom].hidden"),
    ],
)
def test_read_back_compares_the_temporal_fields(live: Table, path: str):
    error = mismatch(Model([SALES, live, QUOTE]), TEMPORAL)

    (problem,) = error.detail["objects"]
    assert problem["object"] == PRICE_KEY and path in problem["properties"]


def test_read_back_fails_when_the_model_says_temporal_and_the_database_table_is_not():
    plain = mismatch(versioning_off(price_rows(), drop_period=True), TEMPORAL)
    off = mismatch(versioning_off(price_rows()), TEMPORAL)

    (problem,) = plain.detail["objects"]
    assert problem["object"] == PRICE_KEY
    assert any(path.startswith("temporal") for path in problem["properties"])
    assert any(path.startswith("columns[validfrom].generated") for path in problem["properties"])
    assert [(p["object"], p["properties"]) for p in off.detail["objects"]] == [(PRICE_KEY, ["unsupported"])]


def test_read_back_fails_when_the_database_table_is_temporal_and_the_model_does_not_say_so():
    declared = Table(
        "sales",
        "Price",
        tuple(dataclasses.replace(c, generated=None, hidden=False) for c in PRICE.columns),
        PRICE.constraints,
    )

    error = mismatch(TEMPORAL, Model([SALES, declared, QUOTE]))

    (problem,) = error.detail["objects"]
    assert problem["object"] == PRICE_KEY and any(p.startswith("temporal") for p in problem["properties"])


def test_read_back_reports_object_property_and_hashes_never_text():
    rows = rows_from_model(MODEL)
    only(rows, "columns", name="Note")["max_length"] = 120
    only(rows, "defaults", name="DF_Order_Status")["definition"] = f"(len({SECRET}))"

    error = mismatch(rows)

    (problem,) = error.detail["objects"]
    assert problem["object"] == ORDER_KEY
    assert problem["properties"] == ["columns[note].type.length"]
    assert re.fullmatch("[0-9a-f]{64}", problem["expected_sha256"])
    assert re.fullmatch("[0-9a-f]{64}", problem["live_sha256"])
    assert problem["expected_sha256"] != problem["live_sha256"]
    assert ORDER_KEY in error.message and "columns[note].type.length" in error.message
    assert "never-in-a-report" not in error.message + json.dumps(error.detail)


def test_expression_text_is_captured_and_not_compared_with_the_file():
    rows = rows_from_model(MODEL)
    # the engine stores another text than the file holds
    only(rows, "defaults", name="DF_Order_Status")["definition"] = "(CONVERT([tinyint],(0)))"
    only(rows, "checks", name="CK_Order_Status")["definition"] = "([Status]<(9) AND [Status]>=(0))"

    captures = read_back(rows)

    assert captures[ORDER_KEY]["constraint [DF_Order_Status]"]["expression"] == "(CONVERT([tinyint],(0)))"


def test_read_back_fails_for_every_object_that_differs_in_one_error():
    rows = rows_from_model(MODEL)
    only(rows, "columns", name="Note")["is_nullable"] = 0
    only(rows, "indexes", name="PK_Customer")["type"] = 2

    error = mismatch(rows)

    assert [(p["object"], p["properties"]) for p in error.detail["objects"]] == [
        (CUSTOMER_KEY, ["constraints[pk_customer].clustered"]),
        (ORDER_KEY, ["columns[note].nullable"]),
    ]


def test_a_sub_object_that_the_model_holds_and_the_database_does_not_fails_the_read_back():
    fewer = dataclasses.replace(ORDER, indexes=(), constraints=ORDER.constraints[:2])

    error = mismatch(Model([SALES, fewer, CUSTOMER]))

    assert error.detail["objects"][0]["properties"] == [
        "constraints[ck_order_status] (absent in other)",
        "constraints[fk_order_customer] (absent in other)",
        "indexes[ix_order_status] (absent in other)",
    ]


def test_a_column_that_only_the_database_has_fails_the_read_back():
    wider = with_columns(ORDER, Column("Extra", TypeRef("int"), True))

    error = mismatch(Model([SALES, wider, CUSTOMER]))

    assert error.detail["objects"][0]["properties"] == ["columns[extra] (absent in self)"]


def test_a_live_sub_object_that_the_model_does_not_hold_is_not_compared_and_not_recorded():
    extra_default = DefaultConstraint("DF_dba", Expression.from_sql("(N'x')"))
    live = with_parts(ORDER, indexes=(IX_DBA,), constraints=(Check("CK_dba", Expression.from_sql("(1=1)")),))
    live = with_columns(
        live,
        Column("Note", TypeRef("nvarchar", length=50), True, default=extra_default),
        drop=("Note", "Total"),
    )
    live = with_columns(live, ORDER.columns[-1])

    captures = read_back(Model([SALES, live, CUSTOMER]))

    assert captures[ORDER_KEY] == captures_of(MODEL)[ORDER_KEY]
    assert not {"index [IX_dba]", "constraint [CK_dba]", "constraint [DF_dba]"} & set(captures[ORDER_KEY])


def test_a_constraint_that_the_engine_named_is_matched_to_the_model_by_its_shape():
    rows = rows_from_model(MODEL)
    only(rows, "defaults", name="DF_Order_Status").update(name="DF__1", is_system_named=1)
    only(rows, "checks", name="CK_Order_Status").update(name="CK__1", is_system_named=1)
    only(rows, "indexes", name="UQ_Order_Note").update(name="UQ__1", constraint_is_system_named=1)
    only(rows, "foreign_keys", name="FK_Order_Customer").update(name="FK__1", is_system_named=1)

    captures = read_back(rows)

    # recorded under the fixed names: a capture never holds a name that the engine made
    assert {label for label in captures[ORDER_KEY] if label.startswith("constraint ")} == {
        "constraint [CK_Order_1]",
        "constraint [DF_Order_Status]",
        "constraint [FK_Order_Customer_1]",
        "constraint [PK_Order]",
        "constraint [UQ_Order_Note_Status]",
    }


def test_an_engine_named_constraint_of_another_shape_is_not_the_constraint_of_the_model():
    rows = rows_from_model(MODEL)
    unique = only(rows, "indexes", name="UQ_Order_Note")
    unique.update(name="UQ__1", constraint_is_system_named=1)
    only(rows, "index_columns", index_id=unique["index_id"], column_name="Status")["is_descending_key"] = 0

    error = mismatch(rows)

    # the live key (fixed name UQ_Order_Note_Status) is an unmanaged sub-object; the model's key is absent
    assert error.detail["objects"][0]["properties"] == ["constraints[uq_order_note] (absent in other)"]


def test_an_engine_named_default_on_another_column_is_not_the_default_of_the_model():
    rows = rows_from_model(MODEL)
    note = only(rows, "columns", name="Note")["column_id"]
    only(rows, "defaults", name="DF_Order_Status").update(
        name="DF__1", is_system_named=1, parent_column_id=note
    )

    error = mismatch(rows)

    assert error.detail["objects"][0]["properties"] == ["columns[status].default"]


def test_a_human_named_constraint_with_another_name_is_not_matched_by_shape():
    rows = rows_from_model(MODEL)
    only(rows, "checks", name="CK_Order_Status")["name"] = "CK_other"

    error = mismatch(rows)

    assert error.detail["objects"][0]["properties"] == ["constraints[ck_order_status] (absent in other)"]


def options_table(*options: tuple[str, str]) -> Model:
    index = Index("IX_T", False, False, (KeyColumn("a"),), options=options)
    return Model([Table("dbo", "T", (Column("a", TypeRef("int"), False),), (), (index,))])


def test_read_back_compares_the_options_against_the_engine_defaults():
    live = options_table(("FILLFACTOR", "80"))

    # a stated engine default equals a catalog that holds the default
    assert read_back(options_table(), options_table(("FILLFACTOR", "100"), ("PAD_INDEX", "OFF"))) != {}
    error = mismatch(live, options_table(("FILLFACTOR", "90")))
    assert error.detail["objects"][0]["properties"] == ["indexes[ix_t].options[FILLFACTOR]"]


@pytest.mark.parametrize(
    "option",
    [
        ("IGNORE_DUP_KEY", "ON"),  # duplicates are discarded with a warning instead of an error
        ("FILLFACTOR", "10"),
        ("DATA_COMPRESSION", "PAGE"),
        ("ALLOW_ROW_LOCKS", "OFF"),
        ("STATISTICS_NORECOMPUTE", "ON"),
    ],
)
def test_read_back_fails_when_a_live_key_or_index_has_a_non_default_option_that_the_file_does_not_state(
    option: tuple[str, str],
):
    # A14 (OM-03): options are a field of the model; what the file does not state is the engine default
    error = mismatch(options_table(option), options_table())
    (path,) = error.detail["objects"][0]["properties"]
    assert path.startswith(f"indexes[ix_t].options[{option[0]}]")

    def keyed(*options: tuple[str, str]) -> Model:
        key = PrimaryKey("PK_T", True, (KeyColumn("a"),), options)
        return Model([Table("dbo", "T", (Column("a", TypeRef("int"), False),), (key,))])

    error = mismatch(keyed(option), keyed())
    (path,) = error.detail["objects"][0]["properties"]
    assert path.startswith(f"constraints[pk_t].options[{option[0]}]")
    assert read_back(keyed(option), keyed(option)) != {}


def collated(collation: str | None) -> Model:
    name = Column("Name", TypeRef("nvarchar", length=50), False, collation=collation)
    return Model([Table("dbo", "T", (name,))])


def test_read_back_passes_for_a_column_whose_declared_collation_is_the_database_collation():
    # OM-04: the reader gives no collation for a column that has the collation of the database
    def read(model: Model, database_collation: str | None) -> dict[str, Any]:
        db = database(collated(None))
        if database_collation is not None:
            db.respond("azsqlcd:database_collation", [[(database_collation,)]])
        return hooks(model).read_back(db, bundle(model), list(model), None)

    stated = collated("SQL_Latin1_General_CP1_CI_AS")
    assert read(stated, "SQL_Latin1_General_CP1_CI_AS") != {}
    assert read(stated, "sql_latin1_general_cp1_ci_as") != {}  # a collation name has no letter case
    for database_collation in ("Latin1_General_100_BIN2", None):  # another default, and no answer
        error = refusal(read, stated, database_collation)
        assert error.reason_code == "READBACK_MISMATCH"
        assert error.detail["objects"][0]["properties"] == ["columns[name].collation"]
    # a file that states no collation asks nothing
    db = database(collated(None))
    hooks(collated(None)).read_back(db, bundle(collated(None)), list(collated(None)), None)
    assert db.sent("azsqlcd:database_collation") == []


def computed_table(nullable: bool | None) -> Model:
    total = Column("Total", None, nullable, computed=Computed(Expression.from_sql("([a]*(2))"), True))
    return Model([Table("dbo", "T", (Column("a", TypeRef("int"), False), total))])


def test_a_computed_column_is_compared_on_nullability_only_when_the_file_says_not_null():
    # the engine can derive NOT NULL from the expression: the file did not say it, so it is not compared
    assert read_back(computed_table(False), computed_table(None)) != {}
    assert read_back(computed_table(False), computed_table(False)) != {}

    error = mismatch(computed_table(None), computed_table(False))

    assert error.detail["objects"][0]["properties"] == ["columns[total].nullable"]


def test_an_option_that_the_file_states_and_the_database_does_not_have_fails_the_read_back():
    error = mismatch(options_table(), options_table(("DATA_COMPRESSION", "PAGE")))

    assert error.detail["objects"][0]["properties"] == ["indexes[ix_t].options[DATA_COMPRESSION]"]


def test_the_owner_of_a_schema_is_read_back_only_when_the_file_states_it():
    rows = rows_from_model(Model([Schema("audit")]))
    only(rows, "schemas", name="audit")["owner_name"] = "azsqlcd-sales-dev-deploy"

    assert read_back(rows, Model([Schema("audit")])) == {
        "SCHEMA:[audit]": {"kind": "SCHEMA", "owner": "azsqlcd-sales-dev-deploy"}
    }
    error = mismatch(rows, Model([Schema("audit", "dbo")]))
    assert error.detail["objects"][0]["properties"] == ["owner"]


def test_an_object_that_exists_on_one_side_only_fails_the_read_back():
    absent = mismatch(Model([SALES, CUSTOMER]))
    extra = mismatch(MODEL, Model([SALES, CUSTOMER]), [ORDER_KEY])

    (problem,) = absent.detail["objects"]
    assert (problem["object"], problem["properties"], problem["live_sha256"]) == (ORDER_KEY, ["exists"], None)
    (problem,) = extra.detail["objects"]
    assert (problem["object"], problem["properties"], problem["expected_sha256"]) == (
        ORDER_KEY,
        ["exists"],
        None,
    )


def test_an_object_that_left_the_model_of_the_reader_fails_the_read_back():
    rows = rows_from_model(MODEL)
    only(rows, "columns", name="Note")["is_masked"] = 1

    error = mismatch(rows)

    assert [(p["object"], p["properties"]) for p in error.detail["objects"]] == [(ORDER_KEY, ["unsupported"])]


def test_read_back_is_for_the_release_of_the_hooks():
    other = bundle(Model([SALES, CUSTOMER]))

    with pytest.raises(ValueError, match="another release"):
        hooks().read_back(database(), other, [ORDER_KEY], None)


ADD_TO_ORDER = "ALTER TABLE [sales].[Order] ADD [Extra] int NULL;"
ADD_TO_CUSTOMER = "ALTER TABLE [sales].[Customer] ADD [Extra] int NULL;"
HEAD = Model(
    [
        SALES,
        with_columns(ORDER, Column("Extra", TypeRef("int"), True)),
        with_columns(CUSTOMER, Column("Extra", TypeRef("int"), True)),
    ]
)


def test_read_back_through_a_migration_uses_the_head_model_when_nothing_later_touches_the_object():
    b = bundle(HEAD, mig(M1, ADD_TO_ORDER), mig(M2, ADD_TO_CUSTOMER))
    live = Model([SALES, HEAD[ORDER_KEY], CUSTOMER])  # M1 is applied, M2 is not

    captures = Hooks(b, config()).read_back(database(live), b, [ORDER_KEY], M1)

    assert captures == {ORDER_KEY: captures_of(HEAD)[ORDER_KEY]}


def test_read_back_through_a_migration_is_refused_when_a_later_pending_migration_touches_the_object():
    b = bundle(HEAD, mig(M1, ADD_TO_ORDER), mig(M2, "ALTER TABLE [sales].[Order] ADD [Later] int NULL;"))
    db = database(HEAD)

    error = refusal(Hooks(b, config()).read_back, db, b, [ORDER_KEY], M1)

    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "READBACK_NOT_COMPUTABLE")
    assert error.detail == {"migration": M1, "later": M2, "objects": [ORDER_KEY]}
    assert db.batches == []  # refused before the catalog is read


def test_a_later_migration_that_is_withdrawn_does_not_stand_in_the_way():
    b = bundle(HEAD, mig(M1, ADD_TO_ORDER), mig(M2, "DROP TABLE [sales].[Order];"), withdrawn=(M2,))

    assert ORDER_KEY in Hooks(b, config()).read_back(database(HEAD), b, [ORDER_KEY], M1)


def test_read_back_through_a_migration_that_the_release_does_not_hold_is_refused():
    b = bundle(HEAD, mig(M1, ADD_TO_ORDER))

    error = refusal(Hooks(b, config()).read_back, database(HEAD), b, [ORDER_KEY], "0009__other.sql")

    assert error.reason_code == "READBACK_NOT_COMPUTABLE"


# ------------------------------------------------------------------ blockers
DROP_NOTE = mig(
    M1, "DROP INDEX [IX_Order_Status] ON [sales].[Order];", "ALTER TABLE [sales].[Order] DROP COLUMN [Note];"
)
RECORDED_INDEX = ("INDEX", "sales", "Order", "IX_Order_Status")


def findings(work: Work, db: FakeSession, cfg: Config | None = None) -> TableFindings:
    return hooks().blockers_and_dependants(db, work, cfg or config())


def test_an_unrecorded_index_on_a_dropped_column_blocks_the_plan():
    db = database(blockers=[RECORDED_INDEX, ("INDEX", "sales", "Order", "IX_dba")])

    found = findings(pending(DROP_NOTE), db)

    (blocker,) = found.blockers
    assert "INDEX [sales].[Order].[IX_dba]" in blocker and "[sales].[Order].[Note]" in blocker
    assert "IX_Order_Status" not in blocker  # recorded: the migration of the release knows it
    (scan,) = db.sent("azsqlcd:table_blockers")
    assert "COLUMNPROPERTY(OBJECT_ID(N'[sales].[Order]'), N'Note', N'ColumnId')" in scan


def test_statistics_that_a_user_made_block_and_a_recorded_foreign_key_does_not():
    line = Table(
        "sales",
        "Line",
        (Column("OrderId", TypeRef("int"), False),),
        (ForeignKey("FK_Line_Order", ("OrderId",), "sales", "Order", ("OrderId",)),),
    )
    stored = recorded(Model([SALES, ORDER, CUSTOMER, line]))
    rows = [
        ("STATISTICS", "sales", "Order", "st_dba"),
        ("FOREIGN KEY", "sales", "Line", "FK_Line_Order"),
        ("FOREIGN KEY", "legacy", "Audit", "FK_Audit_Order"),
    ]
    work = pending(mig(M1, "ALTER TABLE [sales].[Order] ALTER COLUMN [OrderId] bigint NOT NULL;"))

    found = findings(work, database(stored=stored, blockers=rows))

    assert [blocker.split(" uses ")[0] for blocker in found.blockers] == [
        "FOREIGN KEY [legacy].[Audit].[FK_Audit_Order]",
        "STATISTICS [sales].[Order].[st_dba]",
    ]


@pytest.mark.parametrize(
    ("statement", "column"),
    [
        ("DROP TABLE [sales].[Order];", None),
        ("EXEC sys.sp_rename N'[sales].[Order]', N'Orders', N'OBJECT';", None),
        ("EXEC sys.sp_rename N'[sales].[Order].[Note]', N'Remark', N'COLUMN';", "Note"),
        ("ALTER TABLE [sales].[Order] ALTER COLUMN [Note] nvarchar(60) NULL;", "Note"),
        ("ALTER TABLE [sales].[Order] DROP COLUMN [Note];", "Note"),
    ],
)
def test_every_drop_rename_and_retype_is_scanned_on_the_table_or_the_column(
    statement: str, column: str | None
):
    db = database(blockers=[("STATISTICS", "sales", "Order", "st_dba")])

    found = findings(pending(mig(M1, statement)), db)

    assert len(found.blockers) == 1
    (scan,) = db.sent("azsqlcd:table_blockers")
    assert ("N'ColumnId'" in scan) is (column is not None)
    assert "OBJECT_ID(N'[sales].[Order]')" in scan


def test_a_change_that_drops_renames_and_retypes_nothing_is_not_scanned():
    work = pending(
        mig(
            M1,
            ADD_TO_ORDER,
            "CREATE NONCLUSTERED INDEX [IX_new] ON [sales].[Order] ([Note]);",
            "EXEC sys.sp_rename N'[sales].[Order].[IX_Order_Status]', N'IX_s', N'INDEX';",
            "EXEC sys.sp_rename N'[sales].[CK_Order_Status]', N'CK_s', N'OBJECT';",
        )
    )
    renamed = dataclasses.replace(
        ORDER,
        constraints=(
            *ORDER.constraints[:2],
            Check("CK_s", Expression.from_sql("(1=1)")),
            ORDER.constraints[3],
        ),
    )
    db = database(blockers=[("STATISTICS", "sales", "Order", "st_dba")])

    found = hooks(Model([SALES, renamed, CUSTOMER])).blockers_and_dependants(db, work, config())

    assert found == TableFindings()
    assert db.sent("azsqlcd:table_blockers") == []


def test_a_schema_bound_module_blocks_unless_it_is_managed_and_the_migration_unbinds_it():
    bound = [("MODULE", "rpt", "vMargin", "V")]
    work = pending(DROP_NOTE)
    stored = recorded(modules={VIEW: "V"})

    unmanaged = findings(work, database(blockers=bound))
    no_unbind = findings(work, database(stored=stored, blockers=bound))
    unbound = findings(dataclasses.replace(work, unbinds=(VIEW,)), database(stored=stored, blockers=bound))

    assert unmanaged.blockers == (f"{VIEW} is schema-bound to [sales].[Order].[Note] and is not managed",)
    assert len(no_unbind.blockers) == 1 and "-- azsqlcd:unbind [rpt].[vMargin]" in no_unbind.blockers[0]
    assert unbound.blockers == ()


def test_an_unmanaged_dependant_blocks_unless_the_ack_list_names_it():
    work = pending(DROP_NOTE)

    def blockers(ack: list[str]) -> tuple[str, ...]:
        db = database(blockers=[RECORDED_INDEX], dependants=[("rpt", "vMargin", "V", 0)])
        return findings(work, db, config(ack=ack)).blockers

    (blocker,) = blockers([])
    assert VIEW in blocker and '"[rpt].[vMargin] -> [sales].[Order].[Note]"' in blocker
    assert blockers(["[rpt].[vMargin] -> [sales].[Order].[Note]"]) == ()
    assert blockers(["[RPT].[vmargin]->[sales].[order]"]) == ()  # the whole table; case and spaces are free
    assert len(blockers(["[rpt].[vMargin] -> [sales].[Order].[Status]"])) == 1  # another column
    assert len(blockers(["[rpt].[vOther] -> [sales].[Order].[Note]"])) == 1  # another module
    assert len(blockers(["[rpt].[vMargin] -> [sales].[Customer]"])) == 1  # another table


def test_a_managed_dependant_does_not_block_and_one_that_is_broken_already_is_reported():
    work = pending(DROP_NOTE)
    stored = recorded(modules={VIEW: "V", "PROCEDURE:[rpt].[usp_Old]": "P", "VIEW:[rpt].[vNew]": "V"})
    dependants = [("rpt", "vMargin", "V", 0), ("rpt", "usp_Old", "P", 0), ("rpt", "vNew", "V", 0)]
    db = database(stored=stored, blockers=[RECORDED_INDEX], dependants=dependants, broken=("usp_Old", "vNew"))
    changed = ModuleChange("VIEW:[rpt].[vNew]", "schema/views/rpt.vNew.sql", "0" * 64, "alter")

    found = findings(dataclasses.replace(work, module_changes=(changed,)), db)

    assert found == TableFindings((), ("PROCEDURE:[rpt].[usp_Old]",))  # vNew is deployed again: checked after
    assert not any("vNew" in batch for batch in db.sent("azsqlcd:broken_references"))


def test_the_refresh_set_is_the_managed_unchanged_unbound_views_and_table_functions_that_bind():
    modules = {
        "VIEW:[rpt].[v_plain]": "V",
        "FUNCTION:[rpt].[tvf_inline]": "IF",
        "FUNCTION:[rpt].[tvf_multi]": "TF",
        "FUNCTION:[rpt].[fn_scalar]": "FN",
        "PROCEDURE:[rpt].[usp]": "P",
        "TRIGGER:[sales].[tr]": "TR",
        "VIEW:[rpt].[v_bound]": "V",
        "VIEW:[rpt].[v_changed]": "V",
        "VIEW:[rpt].[v_dropped]": "V",
        "VIEW:[rpt].[v_broken]": "V",
    }
    dependants = [
        (*names.parse_object_key(key)[1:], code, int(key.endswith("[v_bound]")))
        for key, code in modules.items()
    ]
    dependants.append(("rpt", "v_unmanaged", "V", 0))
    changed = ModuleChange("VIEW:[rpt].[v_changed]", "schema/views/rpt.v_changed.sql", "0" * 64, "alter")
    work = pending(mig(M1, ADD_TO_ORDER), module_changes=(changed,), drops=("VIEW:[rpt].[v_dropped]",))
    db = database(stored=recorded(modules=modules), dependants=dependants, broken=("v_broken",))

    refresh = hooks().refresh_set(db, work)

    assert refresh == ["FUNCTION:[rpt].[tvf_inline]", "FUNCTION:[rpt].[tvf_multi]", "VIEW:[rpt].[v_plain]"]
    (asked,) = db.sent("azsqlcd:dependants_of")
    assert "OBJECT_ID(N'[sales].[Order]')" in asked  # the dependants of the table that the release alters


def test_the_dependants_to_check_are_the_managed_modules_on_a_table_that_the_release_changes():
    # OM-02: after DROP TABLE or sp_rename the old name resolves to nothing, so the set is read before
    modules = {
        "PROCEDURE:[rpt].[usp_reader]": "P",
        "FUNCTION:[rpt].[fn_scalar]": "FN",
        "TRIGGER:[sales].[tr]": "TR",
        "VIEW:[rpt].[v_bound]": "V",
        "VIEW:[rpt].[v_changed]": "V",
        "VIEW:[rpt].[v_dropped]": "V",
    }
    dependants = [
        (*names.parse_object_key(key)[1:], code, int(key.endswith("[v_bound]")))
        for key, code in modules.items()
    ]
    dependants.append(("rpt", "v_unmanaged", "V", 0))
    changed = ModuleChange("VIEW:[rpt].[v_changed]", "schema/views/rpt.v_changed.sql", "0" * 64, "alter")
    statements = {
        "DROP TABLE [sales].[Order];": Model([SALES, CUSTOMER]),
        "EXEC sys.sp_rename N'[sales].[Order]', N'Orders', N'OBJECT';": Model(
            [SALES, dataclasses.replace(ORDER, name="Orders"), CUSTOMER]
        ),
        "ALTER TABLE [sales].[Order] DROP COLUMN [Note];": MODEL,
        ADD_TO_ORDER: MODEL,
    }
    for statement, head in statements.items():
        migration = mig(M1, statement)
        work = pending(migration, module_changes=(changed,), drops=("VIEW:[rpt].[v_dropped]",))
        db = database(stored=recorded(modules=modules), dependants=dependants)

        found = hooks(head, migration).dependants_to_check(db, work)

        # any kind, schema-bound or not, changed by the release or not; never one that the release drops
        assert found == [
            "FUNCTION:[rpt].[fn_scalar]",
            "PROCEDURE:[rpt].[usp_reader]",
            "TRIGGER:[sales].[tr]",
            "VIEW:[rpt].[v_bound]",
            "VIEW:[rpt].[v_changed]",
        ], statement
        (asked,) = db.sent("azsqlcd:dependants_of")
        assert "OBJECT_ID(N'[sales].[Order]')" in asked, statement  # the name before the change
        assert db.sent("azsqlcd:broken_references") == []


def test_the_findings_of_each_dependant_are_read_before_the_change_and_decide_nothing():
    # RO-2, measured on Azure SQL Database: a sound procedure with a #temp table has findings, and
    # the engine gives error 2020 for some sound procedures. The runner fails a dependant only for
    # a finding that is not in this result.
    temp, sound, unbound = "PROCEDURE:[rpt].[usp_Temp]", "VIEW:[rpt].[vSound]", "PROCEDURE:[rpt].[usp_Audit]"
    db = FakeSession()
    with_temp_table = [
        ("sales", "Order", None, 5, 0, 0, 0, "U "),  # a real table in a statement with a #temp table
        (None, "cc", None, None, 0, 0, 0, None),  # UPDATE cc ... FROM #changes AS cc
        ("seq", "OrderID", None, 9, 0, 0, 0, "SO"),  # no columns: never a finding
    ]
    db.respond(lambda batch: "azsqlcd:broken_references" in batch and "usp_Temp" in batch, [with_temp_table])
    db.respond(
        lambda batch: "azsqlcd:broken_references" in batch and "vSound" in batch,
        [[("sales", "Order", None, 5, 0, 1, 0, "U ")]],
    )
    not_bound = sql_error(
        'The dependencies reported for entity "rpt.usp_Audit" might not include references to all '
        "columns. This is either because the entity references an object that does not exist or "
        "because of an error in one or more statements in the entity."
    )
    db.fail_on(lambda batch: "azsqlcd:broken_references" in batch and "usp_Audit" in batch, not_bound)

    found = hooks().dependant_findings(db, [temp, sound, unbound, temp])

    assert found == {
        temp: ["COLUMNS_NOT_FOUND [sales].[Order]", "UNRESOLVED [cc]"],  # sorted, as the runner compares
        sound: [],  # every key has an entry: an empty list says "read, and sound"
        unbound: [catalog.REFERENCES_NOT_BOUND],
    }
    assert len(db.batches) == 3 and len(db.sent("azsqlcd:broken_references")) == 3  # one read for each key
    assert hooks().dependant_findings(FakeSession(), []) == {}


def test_a_release_that_changes_no_table_has_no_dependant_to_check():
    db = database(dependants=[("rpt", "vMargin", "V", 0)])

    assert hooks().dependants_to_check(db, pending(mig(M1, NEW_SEQUENCE))) == []
    assert db.sent("azsqlcd:dependants_of") == []


def test_the_index_of_an_engine_named_key_of_the_model_is_not_called_unrecorded():
    # OM-06: the capture holds the name of the file; the scan gives the name that the engine made
    def table(column: str) -> Table:
        columns = (Column(column, TypeRef("int"), False), Column("N", TypeRef("int"), True))
        return Table("sales", "P", columns, (PrimaryKey("PK_P", True, (KeyColumn(column),)),))

    before, after = Model([SALES, table("Id")]), Model([SALES, table("PId")])
    rename = mig(M1, "EXEC sys.sp_rename N'[sales].[P].[Id]', N'PId', N'COLUMN';")
    engine_name = "PK__P__3214EC07A1B2C3D4"
    live = rows_from_model(before)
    only(live, "indexes", name="PK_P").update(name=engine_name, constraint_is_system_named=1)
    db = database(live, recorded(before), blockers=[("INDEX", "sales", "P", engine_name)])

    found = hooks(after, rename).blockers_and_dependants(db, pending(rename), config())

    (blocker,) = found.blockers
    assert "Drop it by hand" not in blocker and "not recorded" not in blocker
    assert f"EXEC sys.sp_rename N'[sales].[{engine_name}]', N'PK_P', N'OBJECT';" in blocker
    # after the rename by the DBA the release goes on
    db = database(before, recorded(before), blockers=[("INDEX", "sales", "P", "PK_P")])
    assert hooks(after, rename).blockers_and_dependants(db, pending(rename), config()).blockers == ()
    # an index that nobody recorded is still told as such
    db = database(before, recorded(before), blockers=[("INDEX", "sales", "P", "IX_dba")])
    (blocker,) = hooks(after, rename).blockers_and_dependants(db, pending(rename), config()).blockers
    assert "is not recorded" in blocker


# ------------------------------------------------------------------ A20
def collisions(work: Work, present: list[int], stored: State | None = None) -> tuple[list[str], FakeSession]:
    db = database(present=present)
    return hooks().sub_object_collisions(db, work, stored or recorded()), db


def test_a_pending_create_index_whose_name_exists_live_and_is_not_recorded_is_a_collision():
    work = pending(mig(M1, "CREATE NONCLUSTERED INDEX [IX_dba] ON [sales].[Order] ([Note]);"))

    found, db = collisions(work, [0])

    assert found == ["INDEX [sales].[Order].[IX_dba]"]
    (probe,) = db.sent("azsqlcd:names_present")
    assert "INDEXPROPERTY(OBJECT_ID(N'[sales].[Order]'), N'IX_dba', N'IndexID')" in probe
    assert collisions(work, [])[0] == []  # the name is free


def test_a_recorded_index_that_a_migration_drops_and_creates_again_is_not_a_collision():
    work = pending(
        mig(
            M1,
            "DROP INDEX [IX_Order_Status] ON [sales].[Order];",
            "CREATE NONCLUSTERED INDEX [ix_order_status] ON [sales].[Order] ([Status], [Note]);",
        )
    )

    assert collisions(work, [0])[0] == []


def test_a_new_constraint_collides_with_any_unrecorded_object_of_its_schema():
    work = pending(
        mig(
            M1,
            "ALTER TABLE [sales].[Customer] ADD CONSTRAINT [CK_new] CHECK ([CustomerId] > 0);",
            "ALTER TABLE [sales].[Customer] ADD CONSTRAINT [CK_Order_Status] CHECK ([CustomerId] > 1);",
            "ALTER TABLE [sales].[Customer] ADD CONSTRAINT [UQ_new] UNIQUE NONCLUSTERED ([CustomerId]);",
            "ALTER TABLE [sales].[Customer] ADD [Tier] int NOT NULL CONSTRAINT [DF_new] DEFAULT (0);",
        )
    )

    found, db = collisions(work, [0, 1, 2, 3, 4])

    # CK_Order_Status is a recorded constraint of another table: the proof of the release knows it
    assert found == [
        "INDEX [sales].[Customer].[UQ_new]",
        "OBJECT [sales].[CK_new]",
        "OBJECT [sales].[DF_new]",
        "OBJECT [sales].[UQ_new]",
    ]
    (probe,) = db.sent("azsqlcd:names_present")
    assert probe.count("OBJECT_ID(N'[sales].[") == 5 and probe.count("INDEXPROPERTY(") == 1


def test_a_new_object_whose_name_exists_and_is_not_recorded_is_a_collision():
    work = pending(
        mig(
            M1,
            "CREATE SCHEMA [audit];",
            "CREATE TYPE [sales].[Code] FROM varchar(10) NOT NULL;",
            "CREATE TABLE [sales].[Customer] ([a] int NOT NULL, "
            "CONSTRAINT [PK_c2] PRIMARY KEY CLUSTERED ([a]));",
            "CREATE TABLE [sales].[New] ([a] int NOT NULL CONSTRAINT [DF_n] DEFAULT (0));",
            "CREATE SYNONYM [sales].[Old] FOR [sales].[Order];",
        )
    )

    found, _ = collisions(work, list(range(7)))

    # [sales].[Customer] is a managed table: recorded
    assert found == [
        "OBJECT [sales].[DF_n]",
        "OBJECT [sales].[New]",
        "OBJECT [sales].[Old]",
        "OBJECT [sales].[PK_c2]",
        "SCHEMA [audit]",
        "TYPE [sales].[Code]",
    ]


def test_a_release_that_creates_nothing_asks_for_no_name():
    found, db = collisions(pending(DROP_NOTE), [0])

    assert found == [] and db.sent("azsqlcd:names_present") == []


# ------------------------------------------------------------------ the hooks inside a plan
def test_the_hooks_have_the_methods_that_plan_and_runner_call():
    wanted = {name for name in vars(plan.TableHooks) | vars(runner.TableHooks) if not name.startswith("_")}

    known = {
        "touched_table_objects",
        "table_drift",
        "blockers_and_dependants",
        "sub_object_collisions",
        "refresh_set",
        "unrecorded_objects",
        "read_back",
    }
    # dependants_to_check (OM-02) and dependant_findings (RO-2): the hooks have them; the protocol of
    # the plan gets them with the sweep
    sweep = {"dependants_to_check", "dependant_findings"}
    assert known <= wanted <= known | sweep
    assert all(callable(getattr(Hooks, name)) for name in wanted | sweep)


VIEW_TEXT = "CREATE OR ALTER VIEW [rpt].[vMargin] AS\nSELECT 1 AS [x];\n"
VIEW_ROW = (VIEW, "rpt", "vMargin", "V", VIEW_TEXT, True, True, False, None, 1, None, None, None, None)
WITHOUT_NOTE = Model(
    [
        SALES,
        dataclasses.replace(
            with_columns(ORDER, drop=("Note",)),
            constraints=tuple(c for c in ORDER.constraints if c.name != "UQ_Order_Note"),
            indexes=(),
        ),
        CUSTOMER,
    ]
)
DROP_NOTE_AND_WHAT_USES_IT = mig(
    M1,
    "ALTER TABLE [sales].[Order] DROP CONSTRAINT [UQ_Order_Note];",
    "DROP INDEX [IX_Order_Status] ON [sales].[Order];",
    "ALTER TABLE [sales].[Order] DROP COLUMN [Note];",
)


def planned(
    live: Model | CatalogRows = MODEL, migration: tuple[str, str] = DROP_NOTE_AND_WHAT_USES_IT, **facts: Any
):
    """compute_plan with the hooks, on a baselined database that has one managed view on the Order table."""
    b = bundle(WITHOUT_NOTE, migration)
    b.files["schema/views/rpt.vMargin.sql"] = VIEW_TEXT.encode()
    probe = FakeSession()
    probe.respond("azsqlcd:capture_modules", [[VIEW_ROW]])
    view_capture = catalog.capture_modules(probe, [VIEW])[VIEW]
    stored = recorded()
    stored.objects[VIEW] = ObjectRow(
        "managed", modules.checksum(VIEW_TEXT.encode()), 1, view_capture, state.capture_sha256(view_capture)
    )
    db = database(live, stored, dependants=[("rpt", "vMargin", "V", 0)], **facts)
    db.respond("azsqlcd:fence_facts", [[(5, "READ_WRITE", "sales", "SQL_Latin1_General_CP1_CI_AS", 0)]])
    db.respond("azsqlcd:read_state.steps", [[(1, 1, "baseline", None, None, "ok", None)]])
    db.respond("azsqlcd:capture_modules", [[VIEW_ROW]])
    user_objects = [("rpt", "vMargin", "V"), ("sales", "Order", "U"), ("sales", "Customer", "U")]
    db.respond("azsqlcd:list_user_objects", [user_objects])
    db.respond("azsqlcd:service_objective", [[("S3",)]])
    db.respond("azsqlcd:table_facts", [[(ORDER_KEY, 10, 3)]])
    cfg = config()
    result = plan.compute_plan(
        b, cfg, "dev", "sales-dev", db, tool_version="0.1.0", tool_digest="d" * 64, table_hooks=Hooks(b, cfg)
    )
    return result, stored


def test_a_plan_with_the_hooks_holds_the_touched_table_its_refresh_and_its_read_back():
    result, stored = planned(blockers=[RECORDED_INDEX, ("INDEX", "sales", "Order", "UQ_Order_Note")])

    assert result.outcome == "work"
    assert plan.Touched(ORDER_KEY, stored.objects[ORDER_KEY].catalog_sha256) in result.touched
    (unit,) = result.units
    assert [step.kind for step in unit.steps] == ["batch", "batch", "batch", "refresh", "readback"]
    assert unit.steps[3].object_key == VIEW
    assert unit.steps[4].keys == (ORDER_KEY,)
    assert result.table_facts == (plan.TableFact(ORDER_KEY, 10, 3),)
    assert result.drift == () and result.pre_broken == ()


def test_a_plan_with_the_hooks_refuses_a_blocker_a_drifted_table_and_a_taken_name():
    drifted = rows_from_model(MODEL)
    only(drifted, "columns", name="Status")["is_nullable"] = 1
    create = mig(M1, "CREATE NONCLUSTERED INDEX [IX_dba] ON [sales].[Order] ([Status]);")

    blocked = refusal(planned, blockers=[("STATISTICS", "sales", "Order", "st_dba")])
    drift = refusal(planned, drifted)
    taken = refusal(planned, migration=create, present=[0])

    assert (blocked.exit_code, blocked.reason_code) == (Exit.REFUSED, "TABLE_BLOCKER")
    assert "STATISTICS [sales].[Order].[st_dba]" in blocked.message
    assert (drift.reason_code, drift.detail["objects"][0]["object"]) == ("DRIFT_TOUCHED", ORDER_KEY)
    assert (taken.reason_code, taken.detail["objects"]) == (
        "NAME_COLLISION",
        ["INDEX [sales].[Order].[IX_dba]"],
    )


# ------------------------------------------------------------------ export
RICH = loader.model("every_step_in_the_fixed_order", "head")


def export(live: Model | CatalogRows, cfg: Config | None = None) -> tables.TableExport:
    return export_tables(database(live), cfg or config())


def test_export_output_parses_back_to_the_catalog_model_and_passes_the_token_round_trip():
    result = export(RICH)

    catalog_model = read_model(session_for(rows_from_model(RICH))).model
    assert result.unmanaged == ()
    assert len(result.files) == len(RICH)
    parsed: list[ModelObject] = []
    for path, data in result.files.items():
        text = data.decode("utf-8")
        parsed += parse_object_file(text, path)
        assert emit.token_roundtrip_differences(text, path) == []
    assert Model(parsed) == catalog_model
    assert Model(parsed).diff_paths(catalog_model) == []


def test_export_writes_a_temporal_table_as_a_file_that_reads_back_as_the_catalog_table():
    result = export(Model([SALES, price(retention=(6, "MONTHS"), hidden=True), QUOTE]))

    assert result.unmanaged == ()
    assert sorted(result.files) == [
        "schema/schemas/sales.sql",
        "schema/tables/sales.Price.sql",
        "schema/tables/sales.Quote.sql",
    ]
    text = result.files["schema/tables/sales.Price.sql"].decode()
    assert "[ValidFrom] datetime2(7) GENERATED ALWAYS AS ROW START HIDDEN NOT NULL" in text
    assert "PERIOD FOR SYSTEM_TIME ([ValidFrom], [ValidTo])" in text
    assert "HISTORY_TABLE = [sales].[Price_History], HISTORY_RETENTION_PERIOD = 6 MONTHS" in text
    (parsed,) = parse_object_file(text, "schema/tables/sales.Price.sql")
    assert parsed == price(retention=(6, "MONTHS"), hidden=True)
    assert result.snapshot["captures"][PRICE_KEY]["temporal"]["retention"] == [6, "MONTHS"]


def test_a_history_table_is_owned_not_exported_and_not_quarantined():
    result = export(TEMPORAL)

    assert result.history_tables == ((PRICE_HISTORY_KEY, PRICE_KEY),)
    line = f"- `{PRICE_HISTORY_KEY}`: history table of `{PRICE_KEY}`, owned by the engine, not exported"
    assert line in result.report_md.splitlines()
    assert all(item.object_key != PRICE_HISTORY_KEY for item in result.unmanaged)
    assert not any("Price_History" in path for path in result.files)
    assert PRICE_HISTORY_KEY not in result.snapshot["captures"]
    assert json.dumps(PRICE_HISTORY_KEY) not in result.report_md  # not a line for [unmanaged] objects
    assert "Files written: 3. Left unmanaged: 0." in result.report_md


def test_a_database_without_temporal_tables_has_no_history_part_in_the_report():
    result = export(MODEL)

    assert result.history_tables == () and "History tables" not in result.report_md


def test_a_foreign_key_to_a_temporal_table_no_longer_quarantines_the_referencing_table():
    result = export(TEMPORAL)

    assert "schema/tables/sales.Quote.sql" in result.files
    assert [item for item in result.unmanaged if item.code == DEPENDS_ON_UNMANAGED] == []
    assert "REFERENCES [sales].[Price]" in result.files["schema/tables/sales.Quote.sql"].decode()


def test_the_history_table_of_a_temporal_table_that_is_not_exported_is_still_not_unmanaged():
    rows = price_rows()
    only(rows, "tabulars", name="Price")["is_memory_optimized"] = 1

    result = export(rows)

    assert [(item.object_key, item.code) for item in result.unmanaged] == [
        (PRICE_KEY, "UNSUPPORTED"),
        (QUOTE.key, DEPENDS_ON_UNMANAGED),  # for a reason that is not "temporal"
    ]
    assert result.history_tables == ((PRICE_HISTORY_KEY, PRICE_KEY),)


def test_a_temporal_table_whose_history_schema_is_not_exported_stays_unmanaged():
    # CREATE TABLE ... HISTORY_TABLE = [audit].[Price_History] cannot run without the schema audit
    table = price(history=("audit", "Price_History"))
    rows = rows_from_model(Model([SALES, table]))
    rows["schemas"].append({"name": "audit", "owner_name": None, "module_count": 0})  # owner not readable

    result = export(rows)

    assert [(item.object_key, item.code) for item in result.unmanaged] == [
        ("SCHEMA:[audit]", "UNSUPPORTED"),
        (PRICE_KEY, DEPENDS_ON_UNMANAGED),
    ]
    assert "SCHEMA:[audit]" in result.unmanaged[1].reason
    rows["schemas"][-1]["owner_name"] = "dbo"
    assert sorted(export(rows).files) == [
        "schema/schemas/audit.sql",
        "schema/schemas/sales.sql",
        "schema/tables/sales.Price.sql",
    ]


def test_export_only_reads():
    db = database(RICH)

    export_tables(db, config())

    for batch in db.batches:
        found = lex.significant(lex.tokenize(batch))
        assert found[0].text == "SELECT" and not {t.text.upper() for t in found if t.kind == "word"} & WRITES


def test_dbo_gets_no_schema_file():
    in_dbo = Table("dbo", "Settings", (Column("k", TypeRef("int"), False),))

    result = export(Model([SALES, CUSTOMER, in_dbo]))

    assert sorted(result.files) == [
        "schema/schemas/sales.sql",
        "schema/tables/dbo.Settings.sql",
        "schema/tables/sales.Customer.sql",
    ]
    assert not any("dbo" in path for path in result.files if path.startswith("schema/schemas/"))


def test_a_schema_that_holds_only_modules_gets_a_schema_file():
    rows = rows_from_model(MODEL)
    rows["schemas"].append({"name": "rpt", "owner_name": "dbo", "module_count": 4})

    assert export(rows).files["schema/schemas/rpt.sql"] == b"CREATE SCHEMA [rpt];\n"


def test_a_constraint_that_the_engine_named_is_exported_under_its_fixed_name_with_a_rename_script():
    rows = rows_from_model(MODEL)
    only(rows, "checks", name="CK_Order_Status").update(name="CK__Order__Status__7F2B", is_system_named=1)

    result = export(rows)

    text = result.files["schema/tables/sales.Order.sql"].decode()
    assert "CONSTRAINT [CK_Order_1] CHECK" in text and "7F2B" not in text
    assert result.rename_constraints_sql == (
        "EXEC sys.sp_rename N'[sales].[CK__Order__Status__7F2B]', N'CK_Order_1', N'OBJECT';\nGO\n"
    )
    assert "[CK__Order__Status__7F2B] -> [CK_Order_1]" in result.report_md
    assert "7F2B" not in json.dumps(result.snapshot)


def test_two_table_class_objects_with_one_file_path_are_not_both_exported_and_none_is_lost_silently():
    # OM-09: [dbo].[a.b] and [dbo.a].[b] both give schema/tables/dbo.a.b.sql
    def plain(schema: str, name: str) -> Table:
        return Table(schema, name, (Column("id", TypeRef("int"), False),))

    first, second = plain("dbo", "a.b"), plain("dbo.a", "b")

    result = export(Model([Schema("dbo.a"), first, second]))

    assert sorted(result.files) == ["schema/schemas/dbo.a.sql", "schema/tables/dbo.a.b.sql"]
    (lost,) = result.unmanaged
    assert lost.code == catalog_tables.UNSUPPORTED and "file name" in lost.reason
    assert {lost.object_key} < {first.key, second.key}
    (kept,) = {first.key, second.key} - {lost.object_key}
    assert (
        kept in lost.reason
        and names.quote(names.parse_object_key(kept)[2]) in result.files["schema/tables/dbo.a.b.sql"].decode()
    )
    # the snapshot holds what has a file, and the report names the other key as unmanaged
    assert lost.object_key not in result.snapshot["captures"] and kept in result.snapshot["captures"]
    assert f"| `{lost.object_key}` | UNSUPPORTED |" in result.report_md


def test_export_quarantines_a_table_whose_fixed_constraint_name_is_the_name_of_another_object():
    # OM-10: the names of a schema are one namespace; a module is not in the table model
    table = Table(
        "dbo", "t", (Column("id", TypeRef("int"), False),), (PrimaryKey("PKX", True, (KeyColumn("id"),)),)
    )
    engine_name = "PK__t__3213E83F00000000"
    rows = rows_from_model(Model([table]))
    only(rows, "indexes", name="PKX").update(name=engine_name, constraint_is_system_named=1)
    objects = [("dbo", "t", "U"), ("dbo", engine_name, "PK")]
    free, taken = database(rows), database(rows)
    free.respond("azsqlcd:list_user_objects", [objects])
    taken.respond("azsqlcd:list_user_objects", [[*objects, ("DBO", "pk_T", "V")]])

    assert "CONSTRAINT [PK_t]" in export_tables(free, config()).files["schema/tables/dbo.t.sql"].decode()
    result = export_tables(taken, config())

    assert result.files == {} and result.rename_constraints_sql == ""
    (item,) = result.unmanaged
    assert (item.object_key, item.code) == (table.key, catalog_tables.UNSUPPORTED)
    assert "[PK_t]" in item.reason


def test_an_object_outside_the_model_gets_no_file_and_takes_what_needs_it_with_it():
    line = Table(
        "sales",
        "Line",
        (Column("OrderId", TypeRef("int"), False),),
        (ForeignKey("FK_Line_Order", ("OrderId",), "sales", "Order", ("OrderId",)),),
    )
    note = Table(
        "sales",
        "LineNote",
        (Column("OrderId", TypeRef("int"), False),),
        (ForeignKey("FK_LineNote_Line", ("OrderId",), "sales", "Line", ("OrderId",)),),
    )
    rows = rows_from_model(Model([SALES, ORDER, CUSTOMER, line, note]))
    only(rows, "tabulars", name="Order")["ledger_type"] = 2

    result = export(rows)

    assert sorted(result.files) == ["schema/schemas/sales.sql", "schema/tables/sales.Customer.sql"]
    assert [(u.object_key, u.code) for u in result.unmanaged] == [
        (line.key, DEPENDS_ON_UNMANAGED),
        (note.key, DEPENDS_ON_UNMANAGED),
        (ORDER_KEY, "UNSUPPORTED"),
    ]
    assert ORDER_KEY in result.unmanaged[0].reason and line.key in result.unmanaged[1].reason
    assert sorted(result.snapshot["captures"]) == ["SCHEMA:[sales]", CUSTOMER_KEY]
    for key in (line.key, note.key, ORDER_KEY):
        assert f"| `{key}` |" in result.report_md  # onboard.export writes the one list for azsqlcd.toml


def test_a_table_on_an_alias_type_that_is_not_exported_stays_unmanaged():
    code = AliasType("sales", "Code", TypeRef("varchar", length=10), False)
    uses = Table("sales", "Uses", (Column("c", TypeRef("Code", "sales"), False),))
    sequence = Sequence("sales", "No", TypeRef("Code", "sales"), 1, 1, 1, 9, False, False)

    result = export(Model([SALES, code, uses, sequence]), config(unmanaged=[code.key]))

    assert sorted(result.files) == ["schema/schemas/sales.sql"]
    assert [(u.object_key, u.code) for u in result.unmanaged] == [
        (sequence.key, DEPENDS_ON_UNMANAGED),
        (uses.key, DEPENDS_ON_UNMANAGED),
    ]


def test_an_object_that_azsqlcd_toml_lists_as_unmanaged_is_skipped_and_stays_in_the_list():
    result = export(
        Model([SALES, CUSTOMER]), config(unmanaged=[CUSTOMER_KEY.lower().replace("table", "TABLE")])
    )

    assert sorted(result.files) == ["schema/schemas/sales.sql"]
    assert result.unmanaged == ()
    assert (
        "- `TABLE:[sales].[customer]`" in result.report_md
    )  # named as left out, in the spelling of the file
    assert result.snapshot["captures"].keys() == {"SCHEMA:[sales]"}


def test_a_table_type_whose_key_the_engine_named_is_exported_with_an_unnamed_key_and_no_rename():
    # measured on Azure SQL Database: the key constraint of a table type has is_system_named = 1.
    # A key inside CREATE TYPE has no name, so there is nothing to rename and nothing to quarantine.
    website = Schema("Website")
    id_list = TableType(
        "Website",
        "OrderIDList",
        (Column("OrderID", TypeRef("int"), False),),
        (PrimaryKey(None, True, (KeyColumn("OrderID"),)),),
    )
    rows = rows_from_model(Model([website, id_list]))
    key_row = only(rows, "indexes", is_primary_key=1)
    key_row.update(name="PK__OrderIDL__C3905BAF7B5B524B", constraint_is_system_named=1)

    result = export(rows)

    assert result.unmanaged == () and result.rename_constraints_sql == ""
    assert result.files["schema/types/Website.OrderIDList.sql"].decode() == (
        "CREATE TYPE [Website].[OrderIDList] AS TABLE (\n"
        "    [OrderID] int NOT NULL,\n"
        "    PRIMARY KEY CLUSTERED ([OrderID])\n"
        ");\n"
    )
    # the capture holds the fixed name, never the name that the engine made
    assert "constraint [PK_OrderIDList]" in result.snapshot["captures"][id_list.key]
    assert "C3905BAF" not in json.dumps(result.snapshot) and "C3905BAF" not in result.report_md
    # the same catalog reads back as the model of the file: no difference, no engine-named constraint
    release_ = bundle(Model([website, id_list]))
    table_hooks = Hooks(release_, config())
    db = database(rows, recorded(Model([website, id_list])))
    assert set(table_hooks.read_back(db, release_, [id_list.key], None)) == {id_list.key}
    assert table_hooks.engine_named(db, [id_list.key]) == ()


def test_the_table_part_of_the_export_report_holds_no_list_for_azsqlcd_toml():
    # RO-5: export.md had two [unmanaged] blocks, and the one of the table part was the shorter one
    rows = rows_from_model(Model([SALES, ORDER, CUSTOMER]))
    only(rows, "tabulars", name="Order")["temporal_type"] = 2

    report = export(rows, config(unmanaged=[CUSTOMER_KEY])).report_md

    assert "```" not in report and "[unmanaged]\n" not in report
    assert f"| `{ORDER_KEY}` | UNSUPPORTED |" in report and f"- `{CUSTOMER_KEY}`" in report


def test_an_object_whose_file_would_not_read_back_is_quarantined_with_the_code_roundtrip():
    rows = rows_from_model(MODEL)
    only(rows, "columns", name="Note")["collation_name"] = "not a collation"

    result = export(rows)

    assert [(u.object_key, u.code) for u in result.unmanaged] == [(ORDER_KEY, ROUNDTRIP)]
    assert "schema/tables/sales.Order.sql" not in result.files
    assert "schema/tables/sales.Customer.sql" in result.files


def test_the_rename_script_of_an_export_holds_no_constraint_of_a_table_that_is_not_exported():
    rows = rows_from_model(MODEL)
    only(rows, "columns", name="Note")["collation_name"] = "not a collation"
    only(rows, "defaults", name="DF_Order_Status").update(name="DF__1", is_system_named=1)
    only(rows, "indexes", name="PK_Customer").update(name="PK__1", constraint_is_system_named=1)

    result = export(rows)

    assert result.rename_constraints_sql == (
        "EXEC sys.sp_rename N'[sales].[PK__1]', N'PK_Customer', N'OBJECT';\nGO\n"
    )
    assert "DF__1" not in result.report_md


def test_an_object_in_a_schema_that_is_not_exported_stays_unmanaged():
    result = export(Model([SALES, CUSTOMER]), config(unmanaged=["SCHEMA:[sales]"]))

    assert result.files == {}
    assert result.unmanaged == (
        Unsupported(CUSTOMER_KEY, DEPENDS_ON_UNMANAGED, "it needs SCHEMA:[sales], which is not managed"),
    )


def test_catalog_text_that_a_file_would_say_differently_is_quarantined_not_exported():
    rows = rows_from_model(MODEL)
    # as a file this text is another column: ([Status]) with the option PERSISTED
    only(rows, "columns", name="Total")["definition"] = "([Status]) PERSISTED"

    result = export(rows)

    (item,) = result.unmanaged
    assert (item.object_key, item.code) == (ORDER_KEY, ROUNDTRIP)
    assert "columns[total].computed" in item.reason and "PERSISTED" not in item.reason
    assert "schema/tables/sales.Order.sql" not in result.files


def test_an_object_whose_name_cannot_be_a_file_name_is_quarantined():
    odd = Table("dbo", "a/b", (Column("a", TypeRef("int"), True),))

    result = export(Model([odd]))

    assert result.files == {}
    assert result.unmanaged == (Unsupported(odd.key, "UNSUPPORTED", "its name cannot be a file name"),)


def test_the_snapshot_is_json_with_the_captures_and_the_column_order_of_what_was_exported():
    result = export(MODEL)

    assert json.loads(json.dumps(result.snapshot)) == result.snapshot
    assert result.snapshot == {
        "format": 1,
        "captures": captures_of(MODEL),
        "column_order": {
            ORDER_KEY: ["OrderId", "CustomerId", "Status", "Note", "Total"],
            CUSTOMER_KEY: ["CustomerId"],
        },
    }


def test_the_report_names_the_state_that_a_file_cannot_say():
    rows = rows_from_model(MODEL)
    only(rows, "checks", name="CK_Order_Status").update(is_disabled=1, is_not_trusted=1)

    report = export(rows).report_md

    assert "- `TABLE:[sales].[Order]` constraint [CK_Order_Status]: is_disabled, is_not_trusted" in report
    assert "Files written: 3. Left unmanaged: 0." in report


# ------------------------------------------------------------------ baseline compare
def compare(
    live: Model | CatalogRows, reference: Model = MODEL, managed: list[str] | None = None
) -> tables.TableBaseline:
    snapshot = json.loads(json.dumps(export(reference).snapshot))  # as read from snapshot.json
    return compare_with_snapshot(database(live), snapshot, managed or list(reference))


def test_a_database_that_holds_the_reference_is_equal_object_by_object():
    result = compare(MODEL)

    assert result.items == tuple(BaselineItem(key, EQUAL) for key in MODEL)
    assert result.captures == captures_of(MODEL)
    assert result.rename_constraints_sql == ""


def test_column_order_differences_are_a_warning_in_the_baseline_compare_not_a_difference():
    rows = rows_from_model(MODEL)
    ids = {row["column_id"] for row in rows["columns"] if row["name"] in ("Note", "Status")}
    swap = dict(zip(sorted(ids), sorted(ids, reverse=True), strict=True))
    for query, column in (("columns", "column_id"), ("defaults", "parent_column_id")):
        for row in rows[query]:
            if (
                row.get("object_id", row.get("parent_object_id"))
                == only(rows, "tabulars", name="Order")["object_id"]
            ):
                row[column] = swap.get(row[column], row[column])

    result = compare(rows)

    (item,) = [item for item in result.items if item.object_key == ORDER_KEY]
    assert (item.state, item.properties) == (EQUAL, ())
    assert item.warnings == ("the columns are in another order here",)


def test_a_difference_names_the_capture_property_and_expression_text_counts():
    rows = rows_from_model(MODEL)
    only(rows, "columns", name="Note")["max_length"] = 200
    only(rows, "checks", name="CK_Order_Status")["definition"] = "([Status]<(10))"

    result = compare(rows)

    (item,) = [item for item in result.items if item.state != EQUAL]
    assert item == BaselineItem(ORDER_KEY, DIFFERS, ("column [Note]", "constraint [CK_Order_Status]"))


def test_an_object_of_one_side_only_is_only_here_or_missing_here():
    extra = Table("sales", "Scratch", (Column("a", TypeRef("int"), True),))
    rows = rows_from_model(Model([SALES, CUSTOMER, extra]))
    rows["tabulars"].append(
        only(rows, "tabulars", name="Scratch") | {"object_id": 77, "name": "Old", "ledger_type": 2}
    )

    result = compare(rows)

    assert result.items == (
        BaselineItem("SCHEMA:[sales]", EQUAL),
        BaselineItem(CUSTOMER_KEY, EQUAL),
        BaselineItem("TABLE:[sales].[Old]", ONLY_HERE, ("unsupported",)),
        BaselineItem(ORDER_KEY, MISSING_HERE),
        BaselineItem(extra.key, ONLY_HERE),
    )
    assert sorted(result.captures) == ["SCHEMA:[sales]", CUSTOMER_KEY]


def test_a_history_table_is_never_only_here_in_the_baseline_compare():
    result = compare(TEMPORAL, TEMPORAL)

    assert result.items == tuple(BaselineItem(key, EQUAL) for key in TEMPORAL)
    assert all(item.object_key != PRICE_HISTORY_KEY for item in result.items)
    assert PRICE_HISTORY_KEY not in result.captures


@pytest.mark.parametrize(
    ("live", "properties"),
    [
        (price(retention=(1, "YEARS")), ("temporal",)),
        (price(history=("sales", "Price_Old")), ("temporal",)),
        (price(hidden=True), ("column [ValidFrom]", "column [ValidTo]")),
    ],
)
def test_the_baseline_compare_sees_the_temporal_fields(live: Table, properties: tuple[str, ...]):
    result = compare(Model([SALES, live, QUOTE]), TEMPORAL)

    (item,) = [item for item in result.items if item.state != EQUAL]
    assert item == BaselineItem(PRICE_KEY, DIFFERS, properties)


def test_the_baseline_compare_sees_a_table_that_is_not_versioned_here():
    off = compare(versioning_off(price_rows()), TEMPORAL)
    plain = compare(versioning_off(price_rows(), drop_period=True), TEMPORAL)

    assert BaselineItem(PRICE_KEY, DIFFERS, ("unsupported",)) in off.items
    assert (
        BaselineItem(PRICE_KEY, DIFFERS, ("column [ValidFrom]", "column [ValidTo]", "temporal"))
        in plain.items
    )
    # the table that was the history table is a table like any other now: this database only
    assert BaselineItem(PRICE_HISTORY_KEY, ONLY_HERE) in off.items


def test_an_engine_named_constraint_of_equal_shape_is_equal_and_feeds_the_rename_script():
    human = dataclasses.replace(
        ORDER,
        constraints=(
            *ORDER.constraints[:2],
            Check("CK_OrderStatusRange", ORDER.constraints[2].expression),
            ORDER.constraints[3],
        ),  # type: ignore[union-attr]
    )
    reference = Model([SALES, human, CUSTOMER])
    rows = rows_from_model(MODEL)
    only(rows, "checks", name="CK_Order_Status").update(name="CK__1", is_system_named=1)
    only(rows, "defaults", name="DF_Order_Status").update(name="DF__1", is_system_named=1)

    result = compare(rows, reference)

    assert [item.state for item in result.items] == [EQUAL, EQUAL, EQUAL]
    assert all(item.warnings == () for item in result.items)
    # to the name of the reference, which here is not the fixed name CK_Order_1
    assert result.rename_constraints_sql == (
        "EXEC sys.sp_rename N'[sales].[CK__1]', N'CK_OrderStatusRange', N'OBJECT';\nGO\n"
        "EXEC sys.sp_rename N'[sales].[DF__1]', N'DF_Order_Status', N'OBJECT';\nGO\n"
    )


def test_an_engine_named_constraint_with_another_expression_is_a_difference():
    rows = rows_from_model(MODEL)
    only(rows, "checks", name="CK_Order_Status").update(name="CK__1", is_system_named=1, definition="(1=1)")

    result = compare(rows)

    (item,) = [item for item in result.items if item.object_key == ORDER_KEY]
    assert (item.state, item.properties) == (DIFFERS, ("constraint [CK_Order_Status]",))
    assert item.warnings == ("constraint [CK_Order_1] exists only here: an unmanaged sub-object",)
    assert result.rename_constraints_sql == ""


def test_a_sub_object_that_only_this_database_has_is_a_warning():
    result = compare(Model([SALES, with_parts(ORDER, indexes=(IX_DBA,)), CUSTOMER]))

    (item,) = [item for item in result.items if item.object_key == ORDER_KEY]
    assert (item.state, item.warnings) == (
        EQUAL,
        ("index [IX_dba] exists only here: an unmanaged sub-object",),
    )


def test_a_managed_object_outside_the_model_here_or_outside_the_snapshot_differs():
    rows = rows_from_model(MODEL)
    only(rows, "tabulars", name="Order")["is_memory_optimized"] = 1

    outside = compare(rows)
    stale = compare(MODEL, Model([SALES, CUSTOMER]), list(MODEL))

    assert BaselineItem(ORDER_KEY, DIFFERS, ("unsupported",)) in outside.items
    assert BaselineItem(ORDER_KEY, DIFFERS, ("snapshot",)) in stale.items


def test_an_object_of_the_snapshot_that_is_no_longer_managed_is_not_compared():
    result = compare(Model([SALES, CUSTOMER]), MODEL, ["SCHEMA:[sales]", CUSTOMER_KEY])

    assert [item.object_key for item in result.items] == ["SCHEMA:[sales]", CUSTOMER_KEY]


@pytest.mark.parametrize(
    "snapshot",
    [
        {},
        {"format": 2, "captures": {}, "column_order": {}},
        {"format": 1, "captures": []},
        {"format": 1, "captures": {}},
    ],
)
def test_a_dict_that_export_did_not_make_is_not_a_snapshot(snapshot: dict[str, Any]):
    db = database()

    error = refusal(compare_with_snapshot, db, snapshot, [])

    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "SNAPSHOT_INVALID")
    assert db.batches == []


def test_the_rename_script_of_a_compare_holds_only_constraints_of_managed_tables():
    rows = rows_from_model(MODEL)
    only(rows, "indexes", name="PK_Customer").update(name="PK__1", constraint_is_system_named=1)
    only(rows, "indexes", name="PK_Order").update(name="PK__2", constraint_is_system_named=1)

    result = compare(rows, MODEL, ["SCHEMA:[sales]", ORDER_KEY])

    assert (
        result.rename_constraints_sql
        == "EXEC sys.sp_rename N'[sales].[PK__2]', N'PK_Order', N'OBJECT';\nGO\n"
    )
    assert (
        catalog_tables.rename_constraints_sql([SystemNamed(ORDER_KEY, "PRIMARY KEY", "PK__2", "PK_Order")])
        == result.rename_constraints_sql
    )


# ------------------------------------------------------------------ keys without case; the missing baseline
def test_table_drift_reads_the_recorded_row_of_a_key_that_is_spelled_in_another_letter_case():
    as_written = "TABLE:[SALES].[order]"
    drifted = rows_from_model(MODEL)
    only(drifted, "columns", name="Status")["is_nullable"] = 1

    same = hooks().table_drift(database(MODEL), recorded(), [as_written])
    differs = hooks().table_drift(database(drifted), recorded(), [as_written])

    assert same == {}
    assert [d.property for d in differs[as_written]] == ["column [Status]"]


def a_plan_without_a_baseline_of(missing: str, migration: tuple[str, str]) -> Any:
    """compute_plan with the hooks on a database whose state lacks the row of one object of the model."""
    b = bundle(MODEL, migration)
    stored = recorded()
    del stored.objects[missing]
    db = database(MODEL, stored)
    db.respond("azsqlcd:fence_facts", [[(5, "READ_WRITE", "sales", "SQL_Latin1_General_CP1_CI_AS", 0)]])
    db.respond("azsqlcd:read_state.steps", [[(1, 1, "baseline", None, None, "ok", None)]])
    db.respond("azsqlcd:list_user_objects", [[("sales", "Order", "U"), ("sales", "Customer", "U")]])
    db.respond("azsqlcd:service_objective", [[("S3",)]])
    cfg = config()
    return db, lambda: plan.compute_plan(
        b, cfg, "dev", "sales-dev", db, tool_version="0.1.0", tool_digest="d" * 64, table_hooks=Hooks(b, cfg)
    )


def test_a_plan_refuses_a_table_of_the_model_that_has_no_row_and_that_no_pending_migration_creates():
    db, compute = a_plan_without_a_baseline_of(CUSTOMER_KEY, mig(M1, ADD_TO_ORDER))

    error = refusal(compute)

    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "BASELINE_REQUIRED")
    assert error.detail == {"objects": [CUSTOMER_KEY]} and "azsqlcd baseline" in error.message
    assert db.sent("azsqlcd:read_tabulars") == []  # before the drift step: the catalog was not compared


def test_a_table_that_a_pending_migration_creates_needs_no_baseline():
    create = mig(M1, emit.emit_object_file(CUSTOMER).strip())
    db, compute = a_plan_without_a_baseline_of(CUSTOMER_KEY, create)
    db.respond("azsqlcd:list_user_objects", [[("sales", "Order", "U")]])

    result = compute()

    assert result.outcome == "work" and CUSTOMER_KEY in [touched.key for touched in result.touched]


# ------------------------------------------------------------------ dynamic data masking
def masked_model(function: str | None = "default()") -> Model:
    columns = tuple(dataclasses.replace(c, masked=function) if c.name == "Note" else c for c in ORDER.columns)
    return Model([SALES, dataclasses.replace(ORDER, columns=columns), CUSTOMER])


def test_a_masked_column_reads_back_and_its_capture_holds_the_function():
    captures = read_back(masked_model(), masked_model())

    assert captures == captures_of(masked_model())
    assert captures[ORDER_KEY]["column [Note]"]["masking_function"] == "default()"


@pytest.mark.parametrize(
    ("live", "declared"),
    [(None, "default()"), ("default()", None), ("email()", "default()")],
    ids=["mask missing in the database", "mask only in the database", "another function"],
)
def test_read_back_fails_when_the_mask_of_the_database_is_not_the_mask_of_the_file(
    live: str | None, declared: str | None
):
    error = mismatch(masked_model(live), masked_model(declared))

    assert [(p["object"], p["properties"]) for p in error.detail["objects"]] == [
        (ORDER_KEY, ["columns[note].masked"])
    ]


def test_drift_sees_a_mask_that_was_removed_or_changed_outside_the_tool():
    for live in (None, "email()"):
        drift = hooks(masked_model()).table_drift(
            database(masked_model(live)), recorded(masked_model()), [ORDER_KEY]
        )

        assert [d.property for d in drift[ORDER_KEY]] == ["column [Note]"]
    same = hooks(masked_model()).table_drift(database(masked_model()), recorded(masked_model()), [ORDER_KEY])
    assert same == {}


def test_the_baseline_compare_sees_the_mask():
    assert compare(masked_model(), masked_model()).items == tuple(
        BaselineItem(key, EQUAL) for key in masked_model()
    )
    for live in (None, "email()"):
        result = compare(masked_model(live), masked_model())

        (item,) = [item for item in result.items if item.state != EQUAL]
        assert item == BaselineItem(ORDER_KEY, DIFFERS, ("column [Note]",))


def test_export_writes_the_mask_into_the_table_file():
    exported = export(masked_model('partial(1, "XXXX", 0)'))

    text = exported.files["schema/tables/sales.Order.sql"].decode("utf-8")
    assert "[Note] nvarchar(50) MASKED WITH (FUNCTION = 'partial(1, \"XXXX\", 0)') NULL" in text
    assert exported.unmanaged == ()
