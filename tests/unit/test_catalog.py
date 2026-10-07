"""Read-only catalog queries. FakeSession gives the rows; the tests pin what the tool makes of them.

No test here runs T-SQL. Where the batch text carries the intent (which names are asked for, that
nothing is written), the tests read it with the lexer of the tool.
"""

import json
import re
from pathlib import Path

import pytest

from azsqlcd import catalog, lex, names, state
from azsqlcd.catalog import Dependant, Difference, ExportFact, FenceFacts, TableFacts, UserObject
from azsqlcd.errors import Exit, ToolError
from azsqlcd.sqlerrors import SqlError, sql_error
from support.fake_session import FakeSession

PROC = names.object_key("PROCEDURE", "sales", "usp_x")
VIEW = names.object_key("VIEW", "sales", "vw_Open")
TABLE = names.object_key("TABLE", "sales", "Order")
HOSTILE = "x'; DROP TABLE [azsqlcd].[run]; --]"
WRITES = frozenset("INSERT UPDATE DELETE MERGE EXEC EXECUTE CREATE ALTER DROP INTO".split())
ERROR_2020 = (
    'The dependencies reported for entity "sales.vw_Open" might not include references to all columns. '
    "This is either because the entity references an object that does not exist or because of an error "
    "in one or more statements in the entity."
)

# What mssql-python returned from Azure SQL Database (live spike X3, 2026-10-07).
LIVE = Path(__file__).resolve().parents[1] / "fixtures" / "live"
LIVE_ERRORS = json.loads((LIVE / "error_texts.json").read_text(encoding="utf-8"))
LIVE_READS = json.loads((LIVE / "catalog_rows.json").read_text(encoding="utf-8"))[
    "referenced_entities_after_column_drop"
]


def tokens(sql: str) -> list[lex.Tok]:
    return lex.significant(lex.tokenize(sql))


def words(sql: str) -> list[str]:
    return [t.text.upper() for t in tokens(sql) if t.kind == "word"]


def strings(sql: str) -> list[str]:
    return [t.value for t in tokens(sql) if t.kind in ("string", "nstring")]


def shape(sql: str) -> list[str]:
    return ["'?'" if t.kind in ("string", "nstring") else t.text for t in tokens(sql)]


def module_row(
    key: str | None = None,
    schema: str = "sales",
    name: str = "usp_x",
    type_code: str = "P",
    definition: str | None = "CREATE PROCEDURE sales.usp_x AS SELECT 1;",
    ansi_nulls: object = True,
    quoted_identifier: object = True,
    schema_bound: object = False,
    execute_as: int | None = None,
    can_view: int | None = 1,
    parent_schema: str | None = None,
    parent_name: str | None = None,
    is_disabled: object = None,
    events: str | None = None,
) -> tuple:
    """One row of the capture_modules result set, in its column order."""
    return (
        key,
        schema,
        name,
        type_code,
        definition,
        ansi_nulls,
        quoted_identifier,
        schema_bound,
        execute_as,
        can_view,
        parent_schema,
        parent_name,
        is_disabled,
        events,
    )


def modules(*rows: tuple) -> FakeSession:
    db = FakeSession()
    db.respond("azsqlcd:capture_modules", [list(rows)])
    return db


# ------------------------------------------------------------------ fence and lock
def test_fence_facts_are_reported_as_the_engine_gives_them():
    db = FakeSession()
    db.respond("azsqlcd:fence_facts", [[(5, "READ_WRITE", "sales", "SQL_Latin1_General_CP1_CI_AS", 0)]])
    assert catalog.fence_facts(db) == FenceFacts(
        engine_edition=5,
        updateability="READ_WRITE",
        db_name="sales",
        collation_name="SQL_Latin1_General_CP1_CI_AS",
        case_sensitive=False,
    )
    other = FakeSession()  # a read-only replica of a managed instance with a case-sensitive catalog
    other.respond("azsqlcd:fence_facts", [[(8, "READ_ONLY", "Sales", "Latin1_General_100_CS_AS", 1)]])
    assert catalog.fence_facts(other) == FenceFacts(8, "READ_ONLY", "Sales", "Latin1_General_100_CS_AS", True)
    (batch,) = db.batches
    assert batch.count("CAST(") == 3  # sql_variant never reaches the driver
    # case sensitivity is measured on the catalog, not read from the name of a collation
    assert "FROM sys.schemas WHERE [name] = N'SYS'" in batch
    # TQ-06: a catalog that finds [sys] under the name SYS compares without case: the answer is 0
    assert batch.endswith(
        "CASE WHEN EXISTS (SELECT 1 FROM sys.schemas WHERE [name] = N'SYS') THEN 0 ELSE 1 END;"
    )


@pytest.mark.parametrize(
    "answer", [[], [[]], [[(5, None, "sales", "X_CI_AS", 0)]], [[(5, "READ_WRITE", "s", "c", None)]]]
)
def test_a_fence_fact_that_the_engine_does_not_give_is_never_filled_in(answer):
    db = FakeSession()
    db.respond("azsqlcd:fence_facts", answer)
    with pytest.raises((RuntimeError, TypeError)):
        catalog.fence_facts(db)


def test_plan_asks_whether_the_deploy_lock_is_free_and_never_takes_it():
    db = FakeSession()
    assert catalog.applock_test(db) is True  # nobody holds it: a row with status running is stale
    db.applock_result = -1  # another session holds the lock
    assert catalog.applock_test(db) is False
    for batch in db.batches:
        assert "SELECT APPLOCK_TEST(N'public', N'azsqlcd:deploy', N'Exclusive', N'Session');" in batch
    assert db.sent("sp_getapplock") == [] and db.applock_held is False


def test_an_applock_answer_that_is_not_zero_or_one_is_not_read_as_free():
    for answer in (None, 2, -1):
        db = FakeSession()
        db.respond("APPLOCK_TEST", [[(answer,)]])
        with pytest.raises(TypeError):
            catalog.applock_test(db)


# ------------------------------------------------------------------ module captures
def test_capture_format_1_of_a_function_holds_exactly_these_properties():
    db = modules(
        module_row(
            name="fn_Tax",
            type_code="IF",
            definition="CREATE FUNCTION sales.fn_Tax()\r\nRETURNS TABLE\rAS\nRETURN SELECT 1 AS a;\r\n",
            ansi_nulls=1,
            quoted_identifier=0,
            schema_bound=True,
            execute_as=-2,
        )
    )
    assert catalog.capture_modules(db, None) == {
        "FUNCTION:[sales].[fn_Tax]": {
            "kind": "FUNCTION",
            "type": "IF",  # the sub-kind: FN scalar, IF inline, TF multi-statement
            "definition": "CREATE FUNCTION sales.fn_Tax()\nRETURNS TABLE\nAS\nRETURN SELECT 1 AS a;\n",
            "is_encrypted": False,
            "uses_ansi_nulls": True,
            "uses_quoted_identifier": False,
            "is_schema_bound": True,
            "execute_as_principal_id": -2,
        }
    }


@pytest.mark.parametrize(
    ("type_code", "kind"),
    [("V ", "VIEW"), ("P ", "PROCEDURE"), ("FN", "FUNCTION"), ("IF", "FUNCTION"), ("TF", "FUNCTION")],
)
def test_the_kind_comes_from_the_object_type_of_the_catalog(type_code, kind):
    captures = catalog.capture_modules(modules(module_row(name="m", type_code=type_code)), None)
    (capture,) = captures.values()
    assert list(captures) == [names.object_key(kind, "sales", "m")]
    assert (capture["kind"], capture["type"]) == (kind, type_code.strip())
    assert not {"parent", "is_disabled", "events"} & set(capture)  # trigger properties


def test_a_trigger_capture_also_holds_parent_disabled_and_first_last_order():
    db = modules(
        module_row(
            name="tr_Order_Audit",
            type_code="TR",
            parent_schema="sales",
            parent_name="Order",
            is_disabled=1,
            events="INSERT:0:0;UPDATE:1:0;DELETE:0:1",
        )
    )
    capture = catalog.capture_modules(db, None)["TRIGGER:[sales].[tr_Order_Audit]"]
    assert capture["parent"] == "[sales].[Order]"
    assert capture["is_disabled"] is True
    assert capture["events"] == {
        "INSERT": {"is_first": False, "is_last": False},
        "UPDATE": {"is_first": True, "is_last": False},
        "DELETE": {"is_first": False, "is_last": True},
    }
    assert "sys.trigger_events" in db.batches[0]


def test_a_null_definition_without_view_definition_stops_the_run_and_with_it_the_module_is_encrypted():
    secret = names.object_key("PROCEDURE", "sales", "usp_secret")
    granted = modules(module_row(name="usp_secret", definition=None, can_view=1), module_row())
    captures = catalog.capture_modules(granted, None)
    assert (captures[secret]["is_encrypted"], captures[secret]["definition"]) == (True, None)
    assert captures[PROC]["is_encrypted"] is False
    assert "N'OBJECT', N'VIEW DEFINITION')" in granted.batches[0]

    for can_view in (0, None):  # NULL: the engine could not answer for this object
        denied = modules(module_row(), module_row(name="usp_secret", definition=None, can_view=can_view))
        with pytest.raises(ToolError) as e:
            catalog.capture_modules(denied, None)
        assert (e.value.exit_code, e.value.reason_code) == (Exit.REFUSED, "NO_VIEW_DEFINITION")
        assert e.value.detail == {"objects": [secret]}


def test_a_flag_that_the_engine_does_not_give_is_not_recorded_as_false():
    for column in ("ansi_nulls", "quoted_identifier", "schema_bound"):
        with pytest.raises(TypeError):
            catalog.capture_modules(modules(module_row(**{column: None})), None)
    with pytest.raises(TypeError):
        catalog.capture_modules(modules(module_row(type_code="TR", parent_schema="s", parent_name="t")), None)
    with pytest.raises(ValueError):  # a trigger without a parent row
        catalog.capture_modules(modules(module_row(type_code="TR", is_disabled=0)), None)


def test_without_keys_every_user_module_outside_the_state_schema_is_read():
    db = modules(module_row(schema="my schema", name="weird]name", type_code="V"))
    assert list(catalog.capture_modules(db, None)) == [names.object_key("VIEW", "my schema", "weird]name")]
    (batch,) = db.batches
    assert "o.[is_ms_shipped] = 0 AND s.[name] <> N'azsqlcd'" in batch
    assert "OBJECT_ID(" not in batch


def test_a_capture_comes_back_under_the_key_that_was_asked_for():
    asked = "PROCEDURE:[Sales].[USP_X]"  # the file spells the name in another case than the catalog
    db = modules(module_row(key=asked, schema="sales", name="usp_x"))
    captures = catalog.capture_modules(db, [asked, VIEW])
    assert list(captures) == [asked]  # the engine matched the name; a key without an object is absent
    (batch,) = db.batches
    assert f"(N'{asked}', OBJECT_ID(N'[Sales].[USP_X]'))" in batch  # the engine resolves the name
    assert f"(N'{VIEW}', OBJECT_ID(N'[sales].[vw_Open]'))" in batch
    assert "is_ms_shipped" not in batch  # an object that was asked for by name is read wherever it is


def test_no_keys_means_no_batch_and_many_keys_are_read_in_chunks():
    db = FakeSession()
    assert catalog.capture_modules(db, []) == {}
    assert catalog.dependants_of(db, []) == []
    assert catalog.table_facts(db, set()) == {}
    assert db.batches == []
    keys = [names.object_key("VIEW", "s", f"v{i}") for i in range(2 * catalog.KEY_CHUNK + 1)]
    catalog.capture_modules(db, keys + keys[:10])  # a key that is given twice is asked once
    assert [batch.count("OBJECT_ID(") for batch in db.batches] == [catalog.KEY_CHUNK, catalog.KEY_CHUNK, 1]


# ------------------------------------------------------------------ objects
def test_user_objects_are_listed_with_kind_and_type_and_unmanaged_types_have_no_kind():
    db = FakeSession()
    db.respond(
        "azsqlcd:list_user_objects",
        [
            [
                ("dbo", "Legacy", "SN"),
                ("dbo", "ext_Rates", "ET"),
                ("sales", "Order", "U "),
                ("sales", "OrderNo", "SO"),
                ("sales", "PK_Order", "PK"),
                ("sales", "Price_History", "U/HISTORY"),
                ("sales", "fn_Total", "TF"),
                ("sales", "vw_Open", "V "),
            ]
        ],
    )
    assert catalog.list_user_objects(db) == [
        UserObject("SYNONYM", "dbo", "Legacy", "SN"),
        UserObject(None, "dbo", "ext_Rates", "ET"),
        UserObject("TABLE", "sales", "Order", "U"),
        UserObject("SEQUENCE", "sales", "OrderNo", "SO"),
        UserObject(None, "sales", "PK_Order", "PK"),  # a constraint takes a name of the schema too
        # the history table of a temporal table: the engine owns it, so it has no kind and plan and
        # drift do not list it as unmanaged (live run 2026-10-07); its name is taken all the same
        UserObject(None, "sales", "Price_History", catalog.HISTORY_TABLE_TYPE),
        UserObject("FUNCTION", "sales", "fn_Total", "TF"),
        UserObject("VIEW", "sales", "vw_Open", "V"),
    ]
    assert "WHERE o.[is_ms_shipped] = 0 AND s.[name] <> N'azsqlcd'" in db.batches[0]
    assert "t.[temporal_type] = 1) THEN N'U/HISTORY' ELSE RTRIM(o.[type]) END" in db.batches[0]


def test_an_object_exists_only_with_its_name_and_its_kind():
    db = FakeSession()
    db.respond("azsqlcd:object_exists", lambda batch: [[(1 if "usp_x" in batch else 0,)]])
    assert catalog.object_exists(db, PROC) is True
    assert catalog.object_exists(db, VIEW) is False
    catalog.object_exists(db, names.object_key("FUNCTION", "sales", "fn_Tax"))
    proc, view, function = db.batches
    assert "o.[object_id] = OBJECT_ID(N'[sales].[usp_x]') AND o.[type] IN (N'P');" in proc
    assert "AND o.[type] IN (N'V');" in view
    assert "AND o.[type] IN (N'FN', N'IF', N'TF');" in function


def test_absent_is_an_answer_of_the_engine_and_never_a_guess():
    # A18: an object that is absent is marked dropped and no DROP is sent. So no answer is not absent.
    with pytest.raises(RuntimeError):
        catalog.object_exists(FakeSession(), PROC)
    db = FakeSession()
    db.respond("azsqlcd:object_exists", [[(None,)]])
    with pytest.raises(TypeError):
        catalog.object_exists(db, PROC)
    for not_in_sys_objects in ("SCHEMA:[sales]", "TYPE:[sales].[OrderLine_tt]"):
        with pytest.raises(ValueError):
            catalog.object_exists(db, not_in_sys_objects)


# ------------------------------------------------------------------ indexed views
def test_has_index_is_true_only_when_the_catalog_holds_an_index_of_the_object():
    # OM-01: the runner asks this before an unbind or an ALTER of a view
    db = FakeSession()
    db.respond("azsqlcd:has_index", lambda batch: [[(1 if "vw_Open" in batch else 0,)]])

    assert catalog.has_index(db, VIEW) is True
    assert catalog.has_index(db, names.object_key("VIEW", "sales", "vw_Plain")) is False
    indexed, _ = db.batches
    assert (
        "FROM sys.indexes AS i WHERE i.[object_id] = OBJECT_ID(N'[sales].[vw_Open]') AND i.[index_id] > 0"
        in indexed
    )


def test_no_answer_about_the_indexes_of_an_object_is_not_read_as_no_index():
    # ALTER VIEW drops every index of the view: a guess here would lose them
    with pytest.raises(RuntimeError):
        catalog.has_index(FakeSession(), VIEW)
    db = FakeSession()
    db.respond("azsqlcd:has_index", [[(None,)]])
    with pytest.raises(TypeError):
        catalog.has_index(db, VIEW)
    with pytest.raises(ValueError):
        catalog.has_index(db, "SCHEMA:[sales]")


def test_the_indexed_views_of_a_database_are_listed_by_key():
    db = FakeSession()
    db.respond("azsqlcd:indexed_views", [[("sales", "vTotals"), ("rpt", "vMargin")]])

    assert catalog.indexed_views(db) == ["VIEW:[rpt].[vMargin]", "VIEW:[sales].[vTotals]"]
    (batch,) = db.batches
    assert "FROM sys.views AS v" in batch and "v.[is_ms_shipped] = 0" in batch
    assert "FROM sys.indexes AS i WHERE i.[object_id] = v.[object_id] AND i.[index_id] > 0" in batch
    assert catalog.indexed_views(FakeSession()) == []


# ------------------------------------------------------------------ dependants
def test_dependants_are_the_modules_that_reference_the_tables():
    db = FakeSession()
    db.respond(
        "azsqlcd:dependants_of",
        [[("sales", "vw_Open", "V ", 0), ("sales", "fn_Total", "IF", 1), ("sales", "vw_Open", "V ", 0)]],
    )
    assert catalog.dependants_of(db, [TABLE]) == [
        Dependant("FUNCTION:[sales].[fn_Total]", "FUNCTION", True),  # schema-bound: needs unbind, not refresh
        Dependant(VIEW, "VIEW", False),
    ]
    (batch,) = db.batches
    assert "FROM sys.sql_expression_dependencies AS d" in batch
    assert "d.[referenced_id] IN (OBJECT_ID(N'[sales].[Order]'))" in batch
    # TQ-06: the id of a type can equal the id of a table; only a reference to an object counts
    assert "WHERE d.[referenced_class] = 1 AND d.[referenced_id] IN (" in batch


def test_error_2020_from_the_referenced_entities_view_marks_the_dependant_broken():
    db = FakeSession()
    db.fail_on("azsqlcd:broken_references", sql_error(ERROR_2020))
    assert catalog.broken_references(db, VIEW) == ["ERROR_2020"] == [catalog.REFERENCES_NOT_BOUND]
    assert "FROM sys.dm_sql_referenced_entities(N'[sales].[vw_Open]', N'OBJECT') AS r " in db.batches[0]


# label of a captured error text -> the finding. The engine raises 207 (a column is gone) or 208 (a
# table is gone) and then 2020; the driver returns the first record only, with no number.
LIVE_NOT_BOUND = {
    "207 referenced entities after a column drop: view that uses the column": "ERROR_207",
    "207 referenced entities after a column drop: procedure that uses the column": "ERROR_207",
    "207 referenced entities after a column drop: inline function that uses the column": "ERROR_207",
    "208 referenced entities of a view whose table was dropped": "ERROR_208",
    "2020 referenced entities of a procedure that names a missing table": "ERROR_2020",
}


@pytest.mark.parametrize("label", sorted(LIVE_NOT_BOUND))
def test_the_first_error_that_the_engine_raises_for_a_broken_dependant_is_a_finding(label):
    # T3: a column drop broke a view, a procedure and an inline function, and the read raised
    # "Invalid column name" where the tool waited for the text of 2020. An error that is raised
    # again here ends the run as a failed batch, and the plan stops on an error that it did not plan.
    db = FakeSession()
    db.fail_on("azsqlcd:broken_references", sql_error(LIVE_ERRORS[label]["text"]))

    found = catalog.broken_references(db, VIEW)

    assert found == [LIVE_NOT_BOUND[label]]
    assert set(found) <= catalog.NOT_BOUND_FINDINGS
    assert len(db.batches) == 1  # the error is the answer: nothing is read again


def test_the_findings_of_a_module_that_does_not_bind_are_named_for_the_callers():
    assert catalog.NOT_BOUND_NUMBERS == {2020, 207, 208}
    assert catalog.NOT_BOUND_FINDINGS == {"ERROR_2020", "ERROR_207", "ERROR_208"}
    assert catalog.REFERENCES_NOT_BOUND == "ERROR_2020" and catalog.REFERENCES_NOT_BOUND in (
        catalog.NOT_BOUND_FINDINGS
    )


@pytest.mark.parametrize("case", ["view that does not use the column", "view with SELECT *"])
def test_a_dependant_that_does_not_use_the_dropped_column_stays_sound(case):
    # the rows of the live read; the spike read 7 columns, the type of the referenced table ('U ',
    # padded, as the driver gives char(2)) is the 8th column of the query now
    (result_set,) = LIVE_READS[case]
    rows = [(*row, "U ") for row in result_set]
    assert [len(row) for row in rows] == [8, 8] and rows[0][4:7] == (False, True, False)
    db = FakeSession()
    db.respond("azsqlcd:broken_references", [rows])
    assert catalog.broken_references(db, VIEW) == []


@pytest.mark.parametrize(
    "text",
    [
        "Lock request time out period exceeded.",
        "[Microsoft][SQL Server]Incorrect syntax near the keyword 'FROM'.",
        "[Microsoft][SQL Server]Conversion failed when converting the varchar value 'abc' to data type int.",
        "[Microsoft][SQL Server]Something that this tool has never seen.",
    ],
)
def test_another_error_of_the_referenced_entities_view_is_not_read_as_broken(text):
    db = FakeSession()
    db.fail_on("azsqlcd:broken_references", sql_error(text))
    with pytest.raises(SqlError) as e:
        catalog.broken_references(db, VIEW)
    assert e.value.number not in catalog.NOT_BOUND_NUMBERS


def test_a_lost_session_during_the_read_is_not_read_as_broken():
    # the class decides the exit code of the run; a number in the text does not make it a finding
    lost = sql_error("[Microsoft]TCP Provider: Error code 0x20", sqlstate="08S01")
    db = FakeSession()
    db.fail_on("azsqlcd:broken_references", lost)
    with pytest.raises(SqlError) as e:
        catalog.broken_references(db, VIEW)
    assert e.value is lost


@pytest.mark.parametrize(
    ("row", "broken"),
    [
        # schema, entity, column, referenced_id, is_caller_dependent, is_all_columns_found, is_incomplete,
        # sys.objects.type of the referenced object (char(2); NULL for a type and for an unresolved name)
        (("sales", "Order", None, 101, 0, 1, 0, "U "), []),
        (("sales", "Order", "Status", 101, False, True, False, "U "), []),
        (("sales", "Order", None, 101, 0, 0, 0, "U "), ["COLUMNS_NOT_FOUND [sales].[Order]"]),
        (("sales", "Order", None, 101, 0, 0, 0, "U"), ["COLUMNS_NOT_FOUND [sales].[Order]"]),
        (("sales", "vw_Other", None, 102, 0, 0, 0, "V "), ["COLUMNS_NOT_FOUND [sales].[vw_Other]"]),
        (("sales", "fn_Rows", None, 103, 0, 0, 0, "IF"), ["COLUMNS_NOT_FOUND [sales].[fn_Rows]"]),
        (("sales", "fn_Rows", None, 103, 0, 0, 0, "TF"), ["COLUMNS_NOT_FOUND [sales].[fn_Rows]"]),
        (("sales", "Order", "Stat", 101, 0, 1, 1, "U "), ["INCOMPLETE [sales].[Order].[Stat]"]),
        (("sales", "Gone", None, None, 0, 1, 0, None), ["UNRESOLVED [sales].[Gone]"]),
        ((None, "Gone", None, None, 0, 0, 0, None), ["UNRESOLVED [Gone]"]),
        # a procedure that is called with a one-part name is bound when the caller runs: not broken
        ((None, "usp_Other", None, None, 1, 1, 0, None), []),
        # measured on Azure SQL Database (RO-1): the engine gives is_all_columns_found = 0 for every
        # reference to an object that has no columns. A sound module is not broken by it.
        (("sales", "usp_Other", None, 104, 0, 0, 0, "P "), []),  # EXEC of a procedure
        (("sales", "fn_Price", None, 105, 0, 0, 0, "FN"), []),  # a scalar function
        (("seq", "OrderID", None, 106, 0, 0, 0, "SO"), []),  # NEXT VALUE FOR a sequence
        (("sales", "OrderList", None, 257, 0, 0, 0, None), []),  # a table type parameter: class TYPE
    ],
)
def test_a_reference_is_broken_by_the_rules_of_a12_and_by_no_other(row, broken):
    db = FakeSession()
    db.respond("azsqlcd:broken_references", [[("sales", "Order", None, 101, 0, 1, 0, "U "), row, row]])
    assert catalog.broken_references(db, VIEW) == broken


def test_the_type_of_a_referenced_object_is_read_for_objects_only():
    db = FakeSession()
    db.respond("azsqlcd:broken_references", [[]])
    catalog.broken_references(db, VIEW)
    # sys.objects.type is char(2): the padding is cut in the batch, and again in Python
    assert "r.[is_incomplete], RTRIM(ro.[type]) FROM" in db.batches[0]
    # the id of a type (class 6) can equal the id of an object: only class 1 is joined to sys.objects
    assert db.batches[0].endswith(
        "LEFT JOIN sys.objects AS ro ON ro.[object_id] = r.[referenced_id] AND r.[referenced_class] = 1;"
    )


def test_a_row_of_the_referenced_entities_view_without_the_object_type_is_a_defect():
    # a row of the old shape (7 columns) would read every reference as an object without columns
    db = FakeSession()
    db.respond("azsqlcd:broken_references", [[("sales", "Order", None, 101, 0, 0, 0)]])
    with pytest.raises(ValueError):
        catalog.broken_references(db, VIEW)


def test_only_a_module_has_references_to_sweep():
    for key in (TABLE, "SCHEMA:[sales]"):
        with pytest.raises(ValueError):
            catalog.broken_references(FakeSession(), key)


# ------------------------------------------------------------------ approver facts
def test_table_facts_are_rows_and_reserved_pages_of_the_tables_that_exist():
    db = FakeSession()
    db.respond("azsqlcd:table_facts", [[(TABLE, 1200, 96)]])
    gone = names.object_key("TABLE", "sales", "Gone")
    assert catalog.table_facts(db, [TABLE, gone]) == {TABLE: TableFacts(rows=1200, reserved_pages=96)}
    (batch,) = db.batches
    assert "FROM sys.dm_db_partition_stats AS p GROUP BY p.[object_id]" in batch  # summed for each object
    assert "p.[index_id] IN (0, 1)" in batch  # rows of the heap or the clustered index, not of every index


def test_the_service_objective_is_read_as_text():
    db = FakeSession()
    db.respond("azsqlcd:service_objective", [[("GP_S_Gen5_2",)]])
    assert catalog.service_objective(db) == "GP_S_Gen5_2"
    assert "CAST(DATABASEPROPERTYEX(DB_NAME(), N'ServiceObjective') AS nvarchar(128))" in db.batches[0]
    unknown = FakeSession()
    unknown.respond("azsqlcd:service_objective", [[(None,)]])
    with pytest.raises(RuntimeError):
        catalog.service_objective(unknown)


# ------------------------------------------------------------------ export facts
def test_export_facts_are_what_the_module_view_alone_does_not_tell():
    db = FakeSession()
    db.respond(
        "azsqlcd:export_facts",
        [
            [
                ("SIGNED", "sales", "usp_Signed", "P "),
                ("CLR", "sales", "fn_Clr", "FS"),
                ("DATABASE_TRIGGER", None, "tr_ddl", "TR"),
                ("INDEXED_VIEW", "sales", "vTotals", "V "),
            ]
        ],
    )
    db.respond("azsqlcd:numbered_procedures", [[("NUMBERED", "sales", "usp_Numbered", "P")]])
    assert catalog.export_facts(db) == [
        ExportFact(catalog.SIGNED, "sales", "usp_Signed", "P"),
        ExportFact(catalog.CLR, "sales", "fn_Clr", "FS"),
        ExportFact(catalog.DATABASE_TRIGGER, None, "tr_ddl", "TR"),  # it belongs to no schema
        ExportFact(catalog.INDEXED_VIEW, "sales", "vTotals", "V"),
        ExportFact(catalog.NUMBERED, "sales", "usp_Numbered", "P"),
    ]
    batch, numbered = db.batches
    # one read of: signatures of objects, CLR modules, database DDL triggers, views with an index
    assert "FROM sys.crypt_properties AS c" in batch and "WHERE c.[class] = 1" in batch
    assert "AND o.[type] IN (N'PC', N'FS', N'FT', N'AF', N'TA')" in batch
    assert "FROM sys.triggers AS t WHERE t.[parent_class] = 0 AND t.[is_ms_shipped] = 0" in batch
    assert "o.[is_ms_shipped] = 0 AND s.[name] <> N'azsqlcd'" in batch  # the state schema is not exported
    assert strings(batch)[:1] == ["SIGNED"]
    assert {"CLR", "DATABASE_TRIGGER", "INDEXED_VIEW"} <= set(strings(batch))
    assert set(catalog.CLR_TYPES.values()) <= set(names.MODULE_KINDS)
    # the numbered procedures have a batch of their own: the view is not documented for every edition
    assert "sys.numbered_procedures" not in batch
    assert "FROM sys.numbered_procedures AS n" in numbered and strings(numbered) == ["NUMBERED"]


def test_a_view_with_an_index_is_an_export_fact():
    # OM-01: ALTER VIEW drops every index of the view, so export must know the views that have one
    db = FakeSession()
    db.respond("azsqlcd:export_facts", [[("INDEXED_VIEW", "sales", "vTotals", "V")]])

    assert catalog.export_facts(db) == [ExportFact(catalog.INDEXED_VIEW, "sales", "vTotals", "V")]
    batch = db.batches[0]
    assert "o.[type] = N'V'" in batch
    assert (
        "EXISTS (SELECT 1 FROM sys.indexes AS i WHERE i.[object_id] = o.[object_id] AND i.[index_id] > 0)"
        in batch
    )


def test_export_facts_are_read_when_sys_numbered_procedures_does_not_exist():
    # SQL-3: Microsoft Learn does not list the view for Azure SQL Database. Error 208 on it means
    # "no numbered procedure"; the other facts are still read
    db = FakeSession()
    db.fail_on(
        "sys.numbered_procedures", sql_error("Invalid object name 'sys.numbered_procedures'.", number=208)
    )
    db.respond("azsqlcd:export_facts", [[("SIGNED", "sales", "usp_Signed", "P")]])

    assert catalog.export_facts(db) == [ExportFact(catalog.SIGNED, "sales", "usp_Signed", "P")]


def test_another_error_of_the_numbered_procedures_read_is_not_read_as_none():
    db = FakeSession()
    db.fail_on("sys.numbered_procedures", sql_error("Lock request time out period exceeded.", number=1222))
    with pytest.raises(SqlError):
        catalog.export_facts(db)


def test_a_database_with_no_such_object_has_no_export_fact():
    assert catalog.export_facts(FakeSession()) == []


@pytest.mark.parametrize(
    "row",
    [
        ("ENCRYPTED", "sales", "usp_x", "P"),  # a kind of row that the query does not make
        ("SIGNED", None, "usp_x", "P"),  # an object without its schema
        ("DATABASE_TRIGGER", "sales", "tr_ddl", "TR"),  # a database trigger has none
        ("CLR", "sales", None, "FS"),
        ("NUMBERED", "sales", "usp_x", "P"),  # the numbered procedures have a batch of their own
    ],
)
def test_an_export_fact_that_the_query_cannot_give_is_a_defect_not_a_fact(row):
    db = FakeSession()
    db.respond("azsqlcd:export_facts", [[row]])
    with pytest.raises(RuntimeError):
        catalog.export_facts(db)


# ------------------------------------------------------------------ what every query has in common
def every_query(key_schema: str, key_name: str) -> list[str]:
    """The batches of every catalog function, asked about objects with this name."""
    db = FakeSession()
    db.respond("azsqlcd:fence_facts", [[(5, "READ_WRITE", "sales", "c", 0)]])
    db.respond("azsqlcd:object_exists", [[(0,)]])
    db.respond("azsqlcd:service_objective", [[("S3",)]])
    db.respond("azsqlcd:has_index", [[(0,)]])
    module = names.object_key("PROCEDURE", key_schema, key_name)
    table = names.object_key("TABLE", key_schema, key_name)
    catalog.fence_facts(db)
    catalog.applock_test(db)
    catalog.service_objective(db)
    catalog.capture_modules(db, None)
    catalog.capture_modules(db, [module])
    catalog.list_user_objects(db)
    catalog.object_exists(db, module)
    catalog.dependants_of(db, [table])
    catalog.broken_references(db, module)
    catalog.table_facts(db, [table])
    catalog.export_facts(db)
    catalog.has_index(db, names.object_key("VIEW", key_schema, key_name))
    catalog.indexed_views(db)
    return db.batches


FUNCTIONS = (
    "fence_facts",
    "applock_test",
    "service_objective",
    "capture_modules",
    "capture_modules",
    "list_user_objects",
    "object_exists",
    "dependants_of",
    "broken_references",
    "table_facts",
    "export_facts",
    "numbered_procedures",
    "has_index",
    "indexed_views",
)


def test_every_type_code_that_a_batch_returns_is_trimmed_in_the_batch():
    # sys.objects.type and sys.triggers.type are char(2): the driver gives 'U ' and 'V ' (measured on
    # Azure SQL Database). A code in a select list is always RTRIM(...); a code in a predicate is
    # compared by the engine, which ignores the padding.
    code = re.compile(r"(RTRIM\()?\b\w+\.\[type\]( IN \(| = N')?")
    returned = 0
    for batch in every_query("sales", "x"):
        for found in code.finditer(batch):
            trimmed, compared = found.group(1), found.group(2)
            assert trimmed or compared, batch
            returned += bool(trimmed)
    assert returned == 10  # every select list that holds a type code, counted so that none is missed


@pytest.mark.parametrize(("padded", "code"), [("U ", "U"), ("V ", "V"), ("P ", "P"), ("TR", "TR")])
def test_a_padded_type_code_and_a_trimmed_one_are_one_code_in_every_result(padded, code):
    # also when a driver or an engine version does not apply RTRIM: Python cuts the padding again
    def results(type_code: str) -> list[object]:
        db = FakeSession()
        db.respond("azsqlcd:list_user_objects", [[("sales", "x", type_code)]])
        db.respond("azsqlcd:export_facts", [[("SIGNED", "sales", "x", type_code)]])
        db.respond("azsqlcd:numbered_procedures", [[("NUMBERED", "sales", "x", type_code)]])
        found: list[object] = [catalog.list_user_objects(db), catalog.export_facts(db)]
        if code in catalog.MODULE_TYPES:
            parent = ("sales", "Order") if code == "TR" else (None, None)
            row = module_row(
                type_code=type_code, parent_schema=parent[0], parent_name=parent[1], is_disabled=0
            )
            db.respond("azsqlcd:capture_modules", [[row]])
            db.respond("azsqlcd:dependants_of", [[("sales", "x", type_code, 0)]])
            found += [catalog.capture_modules(db, None), catalog.dependants_of(db, [TABLE])]
        return found

    assert results(padded) == results(code)
    assert results(padded)[0] == [UserObject(catalog.OBJECT_TYPES.get(code), "sales", "x", code)]


def test_every_catalog_batch_is_one_tagged_select_that_writes_nothing():
    sent = every_query("sales", "x")
    assert len(sent) == len(FUNCTIONS)
    for function, batch in zip(FUNCTIONS, sent, strict=True):
        assert batch.startswith(f"/* azsqlcd:{function} */ SELECT "), function  # the handle of a test rule
        ends = [index for index, t in enumerate(tokens(batch)) if t.kind == "op" and t.text == ";"]
        assert ends == [len(tokens(batch)) - 1], function  # one statement
        assert not WRITES & set(words(batch)), function


def test_a_quote_or_bracket_in_a_name_cannot_break_out_of_the_batch_text():
    plain = every_query("sales", "x")
    hostile = every_query("sa]les'", HOSTILE)
    asked = names.qualified("sa]les'", HOSTILE)
    for function, plain_batch, hostile_batch in zip(FUNCTIONS, plain, hostile, strict=True):
        assert shape(hostile_batch) == shape(plain_batch), function  # the name moved no token
        assert "DROP" not in words(hostile_batch), function
    with_names = [batch for batch in hostile if "OBJECT_ID(" in batch or "referenced_entities(" in batch]
    assert len(with_names) == 6
    for batch in with_names:
        assert asked in strings(batch)  # the engine reads the whole name back from the literal


# ------------------------------------------------------------------ comparison rule
def test_a_field_that_only_the_live_capture_has_is_not_a_difference():
    stored = {"kind": "VIEW", "definition": "CREATE VIEW v AS SELECT 1 AS a"}
    live = {**stored, "is_encrypted": False, "captured_since_format_2": {"x": 1}}
    assert catalog.project_capture(live, stored) == stored
    assert catalog.capture_differences(stored, live) == []


def test_a_field_of_the_stored_capture_that_changed_or_is_gone_is_a_difference():
    stored = {"kind": "TRIGGER", "is_disabled": False, "parent": "[sales].[Order]", "uses_ansi_nulls": True}
    live = {"kind": "TRIGGER", "is_disabled": True, "uses_ansi_nulls": True}
    assert catalog.capture_differences(stored, live) == [
        Difference(
            "is_disabled",
            state.capture_sha256({"is_disabled": False}),
            state.capture_sha256({"is_disabled": True}),
        ),
        Difference("parent", state.capture_sha256({"parent": "[sales].[Order]"}), None),
    ]


def test_differences_carry_hashes_never_definition_text():
    stored = {"kind": "PROCEDURE", "definition": "CREATE PROCEDURE p AS SELECT 'password one';"}
    live = {"kind": "PROCEDURE", "definition": "CREATE PROCEDURE p AS SELECT 'password two';"}
    (difference,) = catalog.capture_differences(stored, live)
    assert difference.property == "definition"
    assert difference.stored_sha256 != difference.live_sha256
    for value in (difference.stored_sha256, difference.live_sha256):
        assert len(value) == 64 and set(value) <= set("0123456789abcdef")
    assert "password" not in repr(difference) and "SELECT" not in repr(difference)


def test_a_capture_is_compared_as_stored_json_not_by_key_order():
    stored = {
        "events": {"UPDATE": {"is_last": False, "is_first": True}},
        "definition": "x\n",
        "kind": "TRIGGER",
    }
    live = {
        "kind": "TRIGGER",
        "definition": "x\n",
        "events": {"UPDATE": {"is_first": True, "is_last": False}},
    }
    assert catalog.capture_differences(stored, live) == []
    # a value is compared whole: an event that the trigger gained is a difference of "events"
    live["events"]["DELETE"] = {"is_first": False, "is_last": False}
    assert [d.property for d in catalog.capture_differences(stored, live)] == ["events"]
    # true and 1 are not the same JSON, so a capture that a row gave back is compared as it was stored
    assert catalog.capture_differences({"is_disabled": True}, {"is_disabled": 1}) != []
