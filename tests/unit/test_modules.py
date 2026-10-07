"""Module files: a checksum that never changes meaning, header rewrites that never touch a body,
and a deploy order that follows the dependencies.

Fixtures: tests/fixtures/modules is laid out like a database repository (schema/<kind dir>/...).
"""

import json
from pathlib import Path

import pytest

from azsqlcd import lex
from azsqlcd.errors import Exit, ToolError
from azsqlcd.modules import (
    ModuleFile,
    build_edges,
    checksum,
    deploy_order,
    drop_order,
    execute_as,
    normal_form,
    read_module,
    rewrite_for_export,
    rewrite_for_unbind,
    scan_references,
    stored_checksum,
    stored_text,
)
from azsqlcd.names import KIND_DIRS, path_for
from fixtures.modules.live import LIVE, UNBOUND, Live

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "modules"
LIVE_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "live"

# path -> (key, schema_bound, execute_as). Every fixture file has a line here.
FIXTURE_FACTS = {
    "schema/functions/dbo.fn_Clamp.sql": ("FUNCTION:[dbo].[fn_Clamp]", True, None),
    "schema/functions/dbo.fn_NoAs.sql": ("FUNCTION:[dbo].[fn_NoAs]", True, None),
    "schema/functions/dbo.fn_Numbers.sql": ("FUNCTION:[dbo].[fn_Numbers]", False, None),
    "schema/functions/sales.fn_CustomerTotal.sql": ("FUNCTION:[sales].[fn_CustomerTotal]", True, "OWNER"),
    "schema/functions/sales.fn_Label.sql": ("FUNCTION:[sales].[fn_Label]", False, "CALLER"),
    "schema/functions/sales.fn_OrderLines.sql": ("FUNCTION:[sales].[fn_OrderLines]", True, None),
    "schema/functions/sales.fn_SplitIds.sql": ("FUNCTION:[sales].[fn_SplitIds]", True, None),
    "schema/functions/sales.fn_Tax.sql": ("FUNCTION:[sales].[fn_Tax]", True, None),
    "schema/procedures/dbo.usp_CastDefault.sql": ("PROCEDURE:[dbo].[usp_CastDefault]", False, None),
    "schema/procedures/dbo.usp_ParamAs.sql": ("PROCEDURE:[dbo].[usp_ParamAs]", False, "'report_reader'"),
    "schema/procedures/rpt.usp_Words.sql": ("PROCEDURE:[rpt].[usp_Words]", False, None),
    "schema/procedures/sales.usp_Export.sql": ("PROCEDURE:[sales].[usp_Export]", False, "OWNER"),
    "schema/procedures/sales.usp_Ping.sql": ("PROCEDURE:[sales].[usp_Ping]", False, None),
    "schema/procedures/sales.usp_PlaceOrder.sql": ("PROCEDURE:[sales].[usp_PlaceOrder]", False, None),
    "schema/procedures/sales.usp_Pong.sql": ("PROCEDURE:[sales].[usp_Pong]", False, None),
    "schema/procedures/sales.usp_Recurse.sql": ("PROCEDURE:[sales].[usp_Recurse]", False, None),
    "schema/procedures/sales.usp_Report.sql": ("PROCEDURE:[sales].[usp_Report]", False, None),
    "schema/triggers/sales.tr_Order_Audit.sql": ("TRIGGER:[sales].[tr_Order_Audit]", False, None),
    "schema/triggers/sales.tr_Order_Guard.sql": ("TRIGGER:[sales].[tr_Order_Guard]", False, "OWNER"),
    "schema/views/rpt.vw_AliasTrap.sql": ("VIEW:[rpt].[vw_AliasTrap]", False, None),
    "schema/views/rpt.vw_Margin.sql": ("VIEW:[rpt].[vw_Margin]", True, None),
    "schema/views/sales.vw_ActiveCustomers.sql": ("VIEW:[sales].[vw_ActiveCustomers]", False, None),
    "schema/views/sales.vw_Odd]Name.sql": ("VIEW:[sales].[vw_Odd]]Name]", True, None),
    "schema/views/sales.vw_OpenOrders.sql": ("VIEW:[sales].[vw_OpenOrders]", False, None),
    "schema/views/sales.vw_OrderTotals.sql": ("VIEW:[sales].[vw_OrderTotals]", True, None),
    "schema/views/sales.vw_Quoted.sql": ("VIEW:[sales].[vw_Quoted]", False, None),
}
PATHS = sorted(FIXTURE_FACTS)

# path -> the text before the body after an unbind. One line for every schema-bound fixture.
UNBOUND_HEADERS = {
    "schema/functions/dbo.fn_Clamp.sql": (
        "ALTER function [dbo].[fn_Clamp] "
        "(@v AS decimal(19, 4), @lo AS decimal(19, 4) = 0, @hi AS decimal(19, 4) = 100)\n"
        "returns decimal(19, 4)\nwith returns null on null input\n"
    ),
    "schema/functions/dbo.fn_NoAs.sql": "ALTER FUNCTION [dbo].[fn_NoAs] (@x int)\nRETURNS int\n\n",
    "schema/functions/sales.fn_CustomerTotal.sql": (
        "ALTER FUNCTION [sales].[fn_CustomerTotal] (@CustomerId int)\nRETURNS decimal(19, 4)\n"
        "WITH RETURNS NULL ON NULL INPUT, EXECUTE AS OWNER\n"
    ),
    "schema/functions/sales.fn_OrderLines.sql": (
        "ALTER FUNCTION [sales].[fn_OrderLines] (@OrderId int)\nRETURNS TABLE "
    ),
    "schema/functions/sales.fn_SplitIds.sql": (
        "ALTER FUNCTION [sales].[fn_SplitIds] (@List nvarchar(max), @Sep nchar(1) = N',')\n"
        "RETURNS @t TABLE ([Id] int NOT NULL PRIMARY KEY WITH (IGNORE_DUP_KEY = ON), [Pos] int NOT NULL)\n\n"
    ),
    "schema/functions/sales.fn_Tax.sql": (
        "ALTER FUNCTION [sales].[fn_Tax] (@Amount decimal(19, 4), @Rate decimal(5, 4) = 0.2000)\n"
        "RETURNS decimal(19, 4)\n\n"
    ),
    "schema/views/rpt.vw_Margin.sql": "ALTER VIEW [rpt].[vw_Margin]\nWITH VIEW_METADATA\n",
    "schema/views/sales.vw_Odd]Name.sql": "ALTER VIEW [sales].[vw_Odd]]Name] ",
    "schema/views/sales.vw_OrderTotals.sql": (
        "ALTER VIEW [sales].[vw_OrderTotals] ([OrderId], [LineCount], [Total])\nWITH VIEW_METADATA\n"
    ),
}

TABLE_CLASS_KEYS = [
    "SCHEMA:[sales]",
    "TABLE:[sales].[Order]",
    "TABLE:[sales].[OrderLine]",
    "TABLE:[sales].[Customer]",
    "TABLE:[dbo].[AuditLog]",
    "TYPE:[sales].[OrderLine_tt]",
    "SEQUENCE:[sales].[OrderNo]",
]

# key -> keys of the modules it needs. A fixture that is not listed needs no module.
FIXTURE_EDGES = {
    "FUNCTION:[sales].[fn_CustomerTotal]": {"VIEW:[sales].[vw_OrderTotals]"},
    "PROCEDURE:[dbo].[usp_ParamAs]": {"PROCEDURE:[dbo].[usp_CastDefault]"},
    "PROCEDURE:[rpt].[usp_Words]": {"VIEW:[rpt].[vw_Margin]"},
    "PROCEDURE:[sales].[usp_Export]": {"VIEW:[sales].[vw_OpenOrders]", "VIEW:[sales].[vw_OrderTotals]"},
    "PROCEDURE:[sales].[usp_Ping]": {"PROCEDURE:[sales].[usp_Pong]"},
    "PROCEDURE:[sales].[usp_PlaceOrder]": {"FUNCTION:[dbo].[fn_Numbers]"},
    "PROCEDURE:[sales].[usp_Pong]": {"PROCEDURE:[sales].[usp_Ping]"},
    "PROCEDURE:[sales].[usp_Report]": {
        "FUNCTION:[sales].[fn_CustomerTotal]",
        "FUNCTION:[sales].[fn_Label]",
        "FUNCTION:[sales].[fn_OrderLines]",
        "FUNCTION:[sales].[fn_SplitIds]",
        "PROCEDURE:[sales].[usp_Export]",
    },
    "TRIGGER:[sales].[tr_Order_Audit]": {"PROCEDURE:[sales].[usp_Ping]"},
    "TRIGGER:[sales].[tr_Order_Guard]": {"TRIGGER:[sales].[tr_Order_Audit]"},
    "VIEW:[rpt].[vw_AliasTrap]": {"VIEW:[sales].[vw_OrderTotals]"},
    "VIEW:[rpt].[vw_Margin]": {"FUNCTION:[dbo].[fn_Clamp]", "VIEW:[sales].[vw_OrderTotals]"},
    "VIEW:[sales].[vw_OpenOrders]": {"FUNCTION:[sales].[fn_Tax]"},
    "VIEW:[sales].[vw_Quoted]": {"VIEW:[sales].[vw_ActiveCustomers]", "VIEW:[sales].[vw_OpenOrders]"},
}

# Worked out by hand from FIXTURE_EDGES: functions, views, procedures, triggers, then key, and a
# module after everything it needs. fn_CustomerTotal waits for a view; vw_Quoted waits for two.
FIXTURE_ORDER = [
    "FUNCTION:[dbo].[fn_Clamp]",
    "FUNCTION:[dbo].[fn_NoAs]",
    "FUNCTION:[dbo].[fn_Numbers]",
    "FUNCTION:[sales].[fn_Label]",
    "FUNCTION:[sales].[fn_OrderLines]",
    "FUNCTION:[sales].[fn_SplitIds]",
    "FUNCTION:[sales].[fn_Tax]",
    "VIEW:[sales].[vw_ActiveCustomers]",
    "VIEW:[sales].[vw_Odd]]Name]",
    "VIEW:[sales].[vw_OpenOrders]",
    "VIEW:[sales].[vw_OrderTotals]",
    "FUNCTION:[sales].[fn_CustomerTotal]",
    "VIEW:[rpt].[vw_AliasTrap]",
    "VIEW:[rpt].[vw_Margin]",
    "VIEW:[sales].[vw_Quoted]",
    "PROCEDURE:[dbo].[usp_CastDefault]",
    "PROCEDURE:[dbo].[usp_ParamAs]",
    "PROCEDURE:[rpt].[usp_Words]",
    "PROCEDURE:[sales].[usp_Export]",
    "PROCEDURE:[sales].[usp_Ping]",
    "PROCEDURE:[sales].[usp_PlaceOrder]",
    "PROCEDURE:[sales].[usp_Pong]",
    "PROCEDURE:[sales].[usp_Recurse]",
    "PROCEDURE:[sales].[usp_Report]",
    "TRIGGER:[sales].[tr_Order_Audit]",
    "TRIGGER:[sales].[tr_Order_Guard]",
]
PING_PONG = ["PROCEDURE:[sales].[usp_Ping]", "PROCEDURE:[sales].[usp_Pong]"]
CYCLE_WARNING = "dependency cycle of procedures and triggers, broken in key order: "


def load(path: str) -> ModuleFile:
    return read_module(path, (FIXTURES / path).read_bytes())


def load_all() -> list[ModuleFile]:
    return [load(path) for path in PATHS]


def mod(rel: str, sql: str) -> ModuleFile:
    return read_module(f"schema/{rel}.sql", sql.encode("utf-8"))


def stub(kind: str, name: str) -> ModuleFile:
    tail = {
        "VIEW": "AS SELECT 1 AS x",
        "PROCEDURE": "AS SELECT 1",
        "FUNCTION": "() RETURNS int AS BEGIN RETURN 1 END",
        "TRIGGER": "ON a.tbl AFTER INSERT AS SELECT 1",
    }[kind]
    return mod(f"{KIND_DIRS[kind]}/a.{name}", f"CREATE OR ALTER {kind} a.{name} {tail}")


def refusal(code: str, fn, *args) -> ToolError:
    with pytest.raises(ToolError) as caught:
        fn(*args)
    assert (caught.value.exit_code, caught.value.reason_code) == (Exit.REFUSED, code)
    return caught.value


# ------------------------------------------------------------------ normal form and checksum
def test_golden_checksums_pin_the_normal_form_forever():
    # The sha256 values come from shasum, not from this code. A change of the normal form would
    # redeploy every module of every database, so these values never change.
    raw = (
        b"\xef\xbb\xbfCREATE OR ALTER VIEW [a].[v]  \r\nAS\t\r\n\r\n  SELECT N'\xc3\xa9' AS [x] \r"
        b"FROM [a].[t];\x0b\x0c \n\n \t\n"
    )
    assert (
        normal_form(raw) == b"CREATE OR ALTER VIEW [a].[v]\nAS\n\n  SELECT N'\xc3\xa9' AS [x]\nFROM [a].[t];"
    )
    assert checksum(raw) == "d9c4af4dda243b55f1b2089837c8be58734a740a0f710b1cda036eafd80df2a7"
    assert checksum(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert checksum(b"a\n") == "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb"
    tax, margin = load("schema/functions/sales.fn_Tax.sql"), load("schema/views/rpt.vw_Margin.sql")
    assert tax.checksum == "bd3102be5fea9e2799b9e1703427d58f5d571110e73d050a1d4b938f60ee8d7c"
    assert margin.checksum == "5b07ecd585929747f7de92edd07d06857a1bdbdf07df58f8fb2a0bb636734f0e"


@pytest.mark.parametrize("path", PATHS)
def test_crlf_and_lf_versions_of_one_file_have_one_checksum(path):
    lf = (FIXTURES / path).read_bytes()
    assert b"\r" not in lf and not lf.startswith(b"\xef\xbb\xbf")
    variants = [
        lf.replace(b"\n", b"\r\n"),
        lf.replace(b"\n", b"\r"),
        b"\xef\xbb\xbf" + lf,
        lf.replace(b"\n", b" \t\n") + b"\r\n\n  \n",
        lf.rstrip(b"\n"),
    ]
    assert {checksum(v) for v in variants} == {checksum(lf)}


@pytest.mark.parametrize(
    "other",
    [
        b" SELECT 1\nFROM t",  # indent
        b"SELECT  1\nFROM t",  # space inside a line
        b"SELECT 1\n\nFROM t",  # empty line inside
        b"\nSELECT 1\nFROM t",  # empty line at the start
        b"select 1\nFROM t",  # case
        b"SELECT 1\xc2\xa0\nFROM t",  # a no-break space at a line end is content
        b"SELECT 1\n\xef\xbb\xbfFROM t",  # a BOM that is not the first character is content
    ],
)
def test_anything_other_than_line_ends_and_trailing_blanks_changes_the_checksum(other):
    assert checksum(other) != checksum(b"SELECT 1\nFROM t")


def test_bytes_that_are_not_utf8_are_refused_not_replaced():
    assert refusal("MODULE_INVALID", normal_form, b"SELECT '\xff'").detail == {"offset": 8}
    err = refusal(
        "MODULE_INVALID", read_module, "schema/views/a.v.sql", b"CREATE OR ALTER VIEW a.v AS\r\nSELECT '\xff'"
    )
    assert err.detail == {"path": "schema/views/a.v.sql", "line": 2}


# ------------------------------------------------------------------ reading a module file
def test_the_fixture_table_lists_every_fixture_file():
    on_disk = sorted(p.relative_to(FIXTURES).as_posix() for p in FIXTURES.rglob("*.sql"))
    assert on_disk == PATHS
    assert len(PATHS) >= 20
    assert sorted(UNBOUND_HEADERS) == [path for path in PATHS if FIXTURE_FACTS[path][1]]


@pytest.mark.parametrize("path", PATHS)
def test_an_awkward_header_gives_the_key_the_binding_and_the_execute_as_option(path):
    m = load(path)
    assert (m.key, m.schema_bound, execute_as(m.text)) == FIXTURE_FACTS[path]
    assert m.kind == m.key.split(":")[0]
    assert m.path == path
    assert m.checksum == checksum((FIXTURES / path).read_bytes())


def test_the_text_that_is_sent_is_the_file_text_without_the_bom():
    m = read_module(
        "schema/views/a.v.sql", b"\xef\xbb\xbfCREATE OR ALTER VIEW a.v AS\r\nSELECT 1 AS x  \r\n\r\n"
    )
    assert m.text == "CREATE OR ALTER VIEW a.v AS\r\nSELECT 1 AS x  \r\n\r\n"
    assert m.checksum == checksum(b"CREATE OR ALTER VIEW a.v AS\nSELECT 1 AS x")


@pytest.mark.parametrize(
    ("sql", "line"),
    [
        ("CREATE OR ALTER VIEW a.v AS SELECT 1 AS x\nGO\n", 2),
        (
            "CREATE OR ALTER VIEW a.v AS SELECT 1 AS x\n\n  go -- end\nCREATE OR ALTER VIEW a.w AS SELECT 2",
            3,
        ),
        ("GO\nCREATE OR ALTER VIEW a.v AS SELECT 1 AS x\n", 1),
        ("CREATE OR ALTER VIEW a.v AS SELECT 1 AS x\nGO 2", 2),
    ],
)
def test_a_go_separator_in_a_module_file_is_refused(sql, line):
    # The file bytes are the batch. A GO that reached the engine would be a syntax error or, in a
    # procedure, a call of a procedure named GO.
    err = refusal("MODULE_INVALID", read_module, "schema/views/a.v.sql", sql.encode())
    assert err.detail == {"path": "schema/views/a.v.sql", "line": line}
    assert "GO separator" in err.message


def test_go_inside_a_string_or_a_comment_does_not_split_a_module():
    for path in ("schema/views/rpt.vw_Margin.sql", "schema/procedures/rpt.usp_Words.sql"):
        data = (FIXTURES / path).read_bytes()
        assert b"\nGO\n" in data
        assert load(path).text == data.decode()


@pytest.mark.parametrize(
    ("path", "sql", "says"),
    [
        (
            "schema/views/a.v.sql",
            "CREATE VIEW a.v AS SELECT 1 AS x",
            "starts with CREATE OR ALTER, found CREATE",
        ),
        (
            "schema/views/a.v.sql",
            "ALTER VIEW a.v AS SELECT 1 AS x",
            "starts with CREATE OR ALTER, found ALTER",
        ),
        (
            "schema/views/a.v.sql",
            "SET NOCOUNT ON;\nCREATE OR ALTER VIEW a.v AS SELECT 1 AS x",
            "expected CREATE",
        ),
        (
            "schema/views/a.v.sql",
            "CREATE OR ALTER PROCEDURE a.v AS SELECT 1",
            "PROCEDURE in a file of the views",
        ),
        (
            "schema/procedures/a.p.sql",
            "CREATE OR ALTER FUNCTION a.p () RETURNS int AS BEGIN RETURN 1 END",
            "FUNCTION",
        ),
        ("schema/views/a.v.sql", "CREATE OR ALTER VIEW v AS SELECT 1 AS x", "two-part name is required"),
        (
            "schema/views/a.v.sql",
            "CREATE OR ALTER VIEW a.w AS SELECT 1 AS x",
            "[a].[w] is not the file name 'a.v'",
        ),
        (
            "schema/views/a.v.sql",
            "CREATE OR ALTER VIEW A.V AS SELECT 1 AS x",
            "[A].[V] is not the file name 'a.v'",
        ),
        ("schema/views/a.v.sql", "CREATE OR ALTER VIEW db.a.v AS SELECT 1 AS x", "3-part"),
        ("schema/views/a.v.sql", "CREATE OR ALTER VIEW a.v SELECT 1", "body"),
        # only a function may leave AS out
        ("schema/procedures/a.p.sql", "CREATE OR ALTER PROC a.p BEGIN SELECT 1 END", "body"),
        ("schema/views/a.v.sql", "CREATE OR ALTER VIEW a.v AS SELECT 'x", "unterminated string"),
        ("schema/views/a.v.sql", " \n\n", "empty module file"),
        ("schema/views/a.v.sql", "-- only a comment\n", "empty module batch"),
        (
            "schema/tables/a.v.sql",
            "CREATE OR ALTER VIEW a.v AS SELECT 1 AS x",
            "TABLE file is not a module file",
        ),
        ("views/a.v.sql", "CREATE OR ALTER VIEW a.v AS SELECT 1 AS x", "not an object file path"),
    ],
)
def test_a_file_that_breaks_a_module_file_rule_is_refused(path, sql, says):
    err = refusal("MODULE_INVALID", read_module, path, sql.encode())
    assert err.detail["path"] == path
    assert err.message.startswith(path + ": ") and says in err.message


def test_a_refusal_names_the_file_and_the_line():
    err = refusal(
        "MODULE_INVALID", read_module, "schema/views/a.v.sql", b"-- note\n\nCREATE VIEW a.v AS SELECT 1"
    )
    assert err.detail == {"path": "schema/views/a.v.sql", "line": 3}
    assert err.message.startswith("schema/views/a.v.sql: line 3: ")


def test_after_and_ignore_dep_lines_are_read_only_as_real_directives():
    m = mod(
        "procedures/a.p",
        "-- azsqlcd:after [a].[first]]one]\n"
        "-- azsqlcd:ignore-dep b.other\n"
        "CREATE OR ALTER PROC a.p AS\n"
        '-- azsqlcd:after "a".[late]\n'
        "SELECT '\n-- azsqlcd:after [a].[in_string]\n';\n"
        "/*\n-- azsqlcd:after [a].[in_comment]\n*/\n"
        "  -- azsqlcd:after [a].[indented]\n",
    )
    assert m.after == (("a", "first]one"), ("a", "late"))
    assert m.ignore_dep == (("b", "other"),)


@pytest.mark.parametrize("args", ["first_one", "[a].[b] [a].[c]", "[db].[a].[b]", "", "'a.b'", "[a].[b"])
def test_a_directive_that_is_not_one_two_part_name_is_refused(args):
    sql = f"CREATE OR ALTER PROC a.p AS\nSELECT 1;\n-- azsqlcd:ignore-dep {args}\n"
    err = refusal("MODULE_INVALID", read_module, "schema/procedures/a.p.sql", sql.encode())
    assert err.detail == {"path": "schema/procedures/a.p.sql", "line": 3}


# ------------------------------------------------------------------ header analysis
@pytest.mark.parametrize(
    ("rel", "sql", "bound"),
    [
        ("views/a.v", "CREATE OR ALTER VIEW a.v WITH SCHEMABINDING AS SELECT 1 AS x", True),
        ("views/a.v", "create or alter view a.v with view_metadata , schemabinding as select 1 as x", True),
        # a CTE named SCHEMABINDING is in the body
        (
            "views/a.v",
            "CREATE OR ALTER VIEW a.v AS WITH SCHEMABINDING AS (SELECT 1 AS x) SELECT x FROM SCHEMABINDING",
            False,
        ),
        (
            "views/a.v",
            "CREATE OR ALTER VIEW a.v /* WITH SCHEMABINDING */ AS SELECT 'WITH SCHEMABINDING' AS x",
            False,
        ),
        ("views/a.v", "CREATE OR ALTER VIEW a.v (x, SCHEMABINDING) WITH VIEW_METADATA AS SELECT 1, 2", False),
        ("views/a.v", "CREATE OR ALTER VIEW a.v WITH [SCHEMABINDING] AS SELECT 1 AS x", False),
        # an alias type named schemabinding, and a comma that is not in a WITH list
        (
            "functions/a.f",
            "CREATE OR ALTER FUNCTION a.f (@a int) RETURNS schemabinding AS BEGIN RETURN @a END",
            False,
        ),
        ("triggers/a.t", "CREATE OR ALTER TRIGGER a.t ON a.x AFTER INSERT, SCHEMABINDING AS SELECT 1", False),
        (
            "functions/a.f",
            "CREATE OR ALTER FUNCTION a.f (@p varchar(30) = 'WITH SCHEMABINDING') RETURNS @t TABLE "
            "(c int PRIMARY KEY WITH (IGNORE_DUP_KEY = ON), SCHEMABINDING int) AS BEGIN RETURN END",
            False,
        ),
    ],
)
def test_schemabinding_counts_only_as_an_option_of_the_header_with_list(rel, sql, bound):
    assert mod(rel, sql).schema_bound is bound


def test_the_as_of_execute_as_and_of_a_parameter_type_does_not_end_the_header():
    # Were the header cut at one of these AS words, the options after it would be read as body.
    function = (
        "CREATE OR ALTER FUNCTION a.f (@a AS int) RETURNS int WITH EXECUTE AS OWNER, SCHEMABINDING "
        "AS BEGIN RETURN 1 END"
    )
    assert mod("functions/a.f", function).schema_bound
    procedure = "CREATE OR ALTER PROC a.p @a AS int, @b AS dbo.tt READONLY WITH EXEC AS SELF AS SELECT 1 AS x"
    assert execute_as(procedure) == "SELF"


def test_execute_as_in_the_body_is_not_the_header_option():
    assert execute_as("CREATE OR ALTER PROC a.p AS EXECUTE AS USER = 'x'; SELECT 1; REVERT;") is None
    refusal("MODULE_INVALID", execute_as, "SELECT 1")


def test_a_function_without_as_starts_its_body_at_begin_or_return():
    scalar = "CREATE FUNCTION a.f () RETURNS int WITH SCHEMABINDING BEGIN RETURN (SELECT 1 AS x) END"
    assert (
        rewrite_for_unbind(scalar, "a", "f")
        == "ALTER FUNCTION [a].[f] () RETURNS int BEGIN RETURN (SELECT 1 AS x) END"
    )
    inline = (
        "CREATE OR ALTER FUNCTION a.g () RETURNS TABLE RETURN WITH SCHEMABINDING AS (SELECT 1 AS x) "
        "SELECT x FROM SCHEMABINDING"
    )
    assert not mod("functions/a.g", inline).schema_bound


# ------------------------------------------------------------------ export rewrite
@pytest.mark.parametrize(
    ("stored", "expected", "edits"),
    [
        (
            "CREATE VIEW [a].[v] AS SELECT 1 AS x",
            "CREATE OR ALTER VIEW [a].[v] AS SELECT 1 AS x",
            ["verb: CREATE -> CREATE OR ALTER"],
        ),
        (
            "-- c\nalter  view [a].[v] AS SELECT 1 AS x",
            "-- c\nCREATE OR ALTER view [a].[v] AS SELECT 1 AS x",
            ["verb: ALTER -> CREATE OR ALTER"],
        ),
        (
            # what the engine stores after CREATE OR ALTER VIEW (live spike L7): OR ALTER is blanked out
            "CREATE   VIEW [a].[v] AS SELECT 1 AS x",
            "CREATE OR ALTER VIEW [a].[v] AS SELECT 1 AS x",
            ["verb: CREATE -> CREATE OR ALTER"],
        ),
        ("create or alter view a.v AS SELECT 1 AS x", "create or alter view a.v AS SELECT 1 AS x", []),
    ],
)
def test_export_makes_the_verb_create_or_alter_and_lists_the_edit(stored, expected, edits):
    assert rewrite_for_export(stored, "a", "v") == (expected, edits)


def test_a_stale_name_left_by_sp_rename_is_replaced_by_the_catalog_name():
    stored = "/* v1 */ CREATE PROCEDURE [sales].[usp_Old] @a int AS EXEC [sales].[usp_Old] @a;"
    text, edits = rewrite_for_export(stored, "sales", "usp New]x")
    # the old name inside the body stays: the body is not ours to edit
    assert (
        text == "/* v1 */ CREATE OR ALTER PROCEDURE [sales].[usp New]]x] @a int AS EXEC [sales].[usp_Old] @a;"
    )
    assert edits == ["verb: CREATE -> CREATE OR ALTER", "name: [sales].[usp_Old] -> [sales].[usp New]]x]"]


@pytest.mark.parametrize(
    ("written", "edited"), [("usp_x", True), ("DBO.usp_x", True), ('"dbo".usp_x', False)]
)
def test_the_exported_name_is_two_part_and_spelled_as_the_catalog_spells_it(written, edited):
    text, edits = rewrite_for_export(f"CREATE OR ALTER PROC {written} AS SELECT 1", "dbo", "usp_x")
    assert edits == ([f"name: {written} -> [dbo].[usp_x]"] if edited else [])
    assert read_module("schema/procedures/dbo.usp_x.sql", text.encode()).key == "PROCEDURE:[dbo].[usp_x]"


@pytest.mark.parametrize("path", PATHS)
def test_the_body_bytes_are_never_changed_by_an_export_rewrite(path):
    m = load(path)
    h = lex.module_header(m.text)
    before_verb, between, after_name = (
        m.text[: h.verb_span[0]],
        m.text[h.verb_span[1] : h.name_span[0]],
        m.text[h.name_span[1] :],
    )
    stored = (
        before_verb + "create" + between + "old_name" + after_name
    )  # what a catalog holds after sp_rename
    text, edits = rewrite_for_export(stored, m.schema, m.name)
    # the white space between the verb and the kind word is one space in the export
    assert text == before_verb + "CREATE OR ALTER " + between.lstrip() + m.key.split(":", 1)[1] + after_name
    assert between.lstrip().split()[0].upper() in ("VIEW", "PROCEDURE", "PROC", "FUNCTION", "TRIGGER")
    assert len(edits) == 2
    assert read_module(path, text.encode()).key == m.key
    assert rewrite_for_export(text, m.schema, m.name) == (text, [])


def test_a_live_definition_after_the_export_rewrite_has_the_checksum_of_its_file():
    # A15: baseline records the file checksum only when this holds.
    m = load("schema/functions/sales.fn_Tax.sql")
    stored = m.text.replace("CREATE OR ALTER", "CREATE", 1).replace("\n", "\r\n")
    text, _ = rewrite_for_export(stored, m.schema, m.name)
    assert checksum(text.encode()) == m.checksum


# ------------------------------------------------------------------ definitions as a real catalog holds them
def lf(text: str) -> str:
    """Line ends as catalog.capture_modules gives them."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


@pytest.mark.parametrize("live", LIVE, ids=lambda live: live.name)
def test_a_definition_as_the_engine_stores_it_becomes_its_file_by_the_verb_edit_alone(live: Live):
    # leading white space before CREATE, CRLF with bare LF, 'CREATE   VIEW' after CREATE OR ALTER
    assert live.definition != live.definition.lstrip() or "CREATE   " in live.definition
    text, edits = rewrite_for_export(live.definition, live.schema, live.name)

    assert edits == ["verb: CREATE -> CREATE OR ALTER"]
    assert lf(text) == live.file_text  # white space before CREATE stays as it is
    # the verb and the white space up to the kind word become 'CREATE OR ALTER '; no other character moved
    gap = "CREATE   " if "CREATE   " in live.definition else "CREATE "
    assert text == live.definition.replace(gap, "CREATE OR ALTER ", 1)
    assert "CREATE OR ALTER  " not in text
    assert rewrite_for_export(lf(text), live.schema, live.name) == (live.file_text, [])
    assert rewrite_for_export(text, live.schema, live.name) == (text, [])  # a second export changes nothing


@pytest.mark.parametrize("live", LIVE, ids=lambda live: live.name)
def test_the_file_of_a_stored_definition_is_a_module_file_with_the_checksum_of_the_live_text(live: Live):
    path = path_for(live.kind, live.schema, live.name)

    module = read_module(path, live.file_text.encode())

    assert module.key == f"{live.kind}:[{live.schema}].[{live.name}]"
    assert (module.schema_bound, module.text) == (live.schema_bound, live.file_text)
    # A15: the live text after the header rewrite, with its CRLF and bare LF, equals the file
    rewritten, _ = rewrite_for_export(live.definition, live.schema, live.name)
    assert checksum(rewritten.encode()) == module.checksum == checksum(live.file_text.encode())
    assert read_module(path, rewritten.encode()).checksum == module.checksum
    # the white space before CREATE is content of the normal form: without it the text is another text
    if live.file_text[0].isspace():
        assert checksum(live.file_text.lstrip().encode()) != module.checksum


@pytest.mark.parametrize("live", [live for live in LIVE if live.schema_bound], ids=lambda live: live.name)
def test_unbind_of_a_stored_definition_keeps_the_white_space_before_the_verb(live: Live):
    unbound = rewrite_for_unbind(live.definition, live.schema, live.name)

    assert "CREATE   " in live.definition  # stored after CREATE OR ALTER: the gap is not content
    assert unbound == UNBOUND[live.name]
    assert f"ALTER {live.kind} [" in unbound  # one space after the verb
    assert lex.module_header(unbound).verb == "ALTER"
    as_file, _ = rewrite_for_export(lf(unbound), live.schema, live.name)
    assert not read_module(path_for(live.kind, live.schema, live.name), as_file.encode()).schema_bound


def test_every_schema_bound_live_fixture_has_its_unbound_text():
    assert sorted(UNBOUND) == sorted(live.name for live in LIVE if live.schema_bound)
    assert {live.type_code.strip() for live in LIVE} == {"P", "V", "FN"}


# ------------------------------------------------------------------ module texts of the live spike
# (sent, definition): the batch that was sent and what sys.sql_modules.definition held after it, on
# Azure SQL Database (live spike L7 and its probe, 2026-10-07). 43 pairs over 19 header shapes.
STORED = [
    (f"L7 {row['case']}: {row['step']}", row["sent"], row["definition"])
    for row in json.loads((LIVE_FIXTURES / "catalog_rows.json").read_text(encoding="utf-8"))["sql_modules"]
] + [
    (f"probe {row['case']}", row["sent"], row["definition"])
    for row in json.loads((LIVE_FIXTURES / "sql_modules_probe.json").read_text(encoding="utf-8"))
]
STORED_IDS = [label for label, _, _ in STORED]
KIND_WORDS = ("VIEW", "PROCEDURE", "PROC", "FUNCTION", "TRIGGER")


def header_parts(text: str) -> tuple[str, str, str]:
    """(text before the verb, text from the verb up to the kind word, text from the kind word on)."""
    start, end = lex.module_header(text).verb_span
    kind = next(t for t in lex.significant(lex.tokenize(text)) if t.pos >= end)
    assert kind.text.upper() in KIND_WORDS
    return text[:start], text[start : kind.pos], text[kind.pos :]


def catalog_name(definition: str) -> tuple[str, str]:
    header = lex.module_header(definition)
    assert header.schema is not None
    return header.schema, header.name


def test_the_live_fixture_holds_every_stored_form_of_the_spike():
    assert len(STORED) == 43 and len(set(STORED_IDS)) == 43
    assert all(definition is not None for _, _, definition in STORED)
    # the forms of the verb as the engine stored them: the gap that OR ALTER leaves, and CREATE for ALTER
    assert {header_parts(definition)[1] for _, _, definition in STORED} == {
        "CREATE   ",  # CREATE OR ALTER <kind>; also alter   procedure: the engine writes CREATE in upper case
        "CREATE ",  # ALTER <kind>, and plain CREATE <kind>
        "create         ",  # create   or   alter   procedure
        "CREATE /* c1 */   ",
        "CREATE /* c1 */  /* c2 */  -- c3\n ",
        "CREATE  /* c */  -- x\n ",
        "CREATE\n\t\r\n",
        "CREATE\r\n",
        "Create   ",
    }


@pytest.mark.parametrize(("sent", "definition"), [pair[1:] for pair in STORED], ids=STORED_IDS)
def test_stored_text_is_what_the_engine_keeps_of_a_module_batch(sent, definition):
    # T1: the read-back of the runner compares the catalog with this text. A wrong byte here fails
    # every deploy, so the pairs are the ones that the live spike captured.
    assert stored_text(sent) == definition
    assert stored_checksum(sent) == checksum(definition.encode())
    assert stored_text(definition) == definition  # the engine stores CREATE: nothing more to take away


# what stands between the verb and the kind word in a stored text -> the same place in the export
COMMENT_GAPS = {
    "CREATE /* c1 */   ": "CREATE OR ALTER /* c1 */ ",
    "CREATE /* c1 */  /* c2 */  -- c3\n ": "CREATE OR ALTER /* c1 */ /* c2 */ -- c3\n",
    "CREATE  /* c */  -- x\n ": "CREATE OR ALTER /* c */ -- x\n",
}


@pytest.mark.parametrize("definition", [pair[2] for pair in STORED], ids=STORED_IDS)
def test_every_stored_form_is_exported_with_one_space_between_the_verb_and_the_kind_word(definition):
    # T2: 'CREATE   VIEW' was exported as 'CREATE OR ALTER   VIEW', which is not the file that was
    # deployed, so a baseline could not record the module as equal (A15).
    schema, name = catalog_name(definition)
    before, verb, rest = header_parts(definition)

    text, edits = rewrite_for_export(definition, schema, name)

    assert edits == ["verb: CREATE -> CREATE OR ALTER"]
    assert text == before + COMMENT_GAPS.get(verb, "CREATE OR ALTER ") + rest
    # the bytes before the verb and from the kind word to the end are the stored bytes
    assert text.startswith(before) and text.endswith(rest)
    if verb not in COMMENT_GAPS:
        assert text[len(before) :].split(" [")[0] in {f"CREATE OR ALTER {rest.split()[0]}"}
    assert rewrite_for_export(text, schema, name) == (text, [])  # an export of the export changes nothing
    assert lex.module_header(text).verb == "CREATE OR ALTER"


def _file_of(label: str, sent: str) -> str:
    """The file of a spike module: the text of its CREATE OR ALTER step (the ALTER step sent another verb)."""
    case = label.rsplit(": ", 1)[0] if label.startswith("L7 ") else label.removesuffix(" / ALTER")
    return next(
        text
        for other, text, _ in STORED
        if other.startswith(case) and lex.module_header(text).verb == "CREATE OR ALTER"
    )


def _written_as_export_writes_it(file_text: str) -> bool:
    return header_parts(file_text)[1] == "CREATE OR ALTER "


ROUND_TRIP = [(label, sent, definition) for label, sent, definition in STORED if "plain CREATE" not in label]


@pytest.mark.parametrize(("label", "sent", "definition"), ROUND_TRIP, ids=[row[0] for row in ROUND_TRIP])
def test_the_export_of_a_deployed_module_is_its_file(label, sent, definition):
    # A15: after a deploy (CREATE OR ALTER) and after an ALTER of the same text, the export is the
    # file byte for byte, when the file writes its verb as export does: 'CREATE OR ALTER <kind>'.
    file_text = _file_of(label, sent)
    assert lex.module_header(file_text).verb == "CREATE OR ALTER"
    schema, name = catalog_name(definition)

    exported, _ = rewrite_for_export(definition, schema, name)

    if _written_as_export_writes_it(file_text):
        assert exported == file_text
        assert checksum(exported.encode()) == checksum(file_text.encode())
    else:
        # lower case, comments or line breaks inside the verb of the file: the engine keeps nothing
        # of OR ALTER, so the export has the verb as export writes it and differs in the verb only
        assert exported != file_text
        assert header_parts(exported)[2] == header_parts(file_text)[2]


def test_most_spike_modules_write_the_verb_as_export_does():
    files = {_file_of(label, sent) for label, sent, _ in ROUND_TRIP}
    assert sum(_written_as_export_writes_it(text) for text in files) == 10 and len(files) == 18


def _verb_as_export_writes_it(path: str) -> bool:
    return _written_as_export_writes_it(load(path).text)


# A file whose verb is not written this way (lower case, a line break after it) is exported with
# another verb text; its body is exported byte for byte.
EXPORT_SHAPED = [path for path in PATHS if _verb_as_export_writes_it(path)]


@pytest.mark.parametrize("path", EXPORT_SHAPED)
def test_a_file_that_was_deployed_is_exported_as_the_same_file(path):
    # A15: deploy stores stored_text(file); export of that definition must be the file again, or
    # baseline of a database that the tool deployed reports every module as different.
    m = load(path)
    stored = stored_text(m.text)
    assert stored != m.text and "CREATE   " in stored
    exported, _ = rewrite_for_export(stored, m.schema, m.name)
    assert exported == m.text and checksum(exported.encode()) == m.checksum


def test_most_fixture_files_have_the_verb_as_export_writes_it():
    assert len(EXPORT_SHAPED) > len(PATHS) // 2


def test_stored_text_of_text_that_is_not_a_module_is_refused_by_the_lexer():
    with pytest.raises(lex.LexError):
        stored_text("CREATE TABLE a.t (c int)")


# the verb of a stored schema-bound view, as the spike saw the forms -> the verb of the unbind
STORED_VERBS = [
    ("CREATE   VIEW", "ALTER VIEW"),
    ("CREATE VIEW", "ALTER VIEW"),
    ("create         view", "ALTER view"),
    ("Create   View", "ALTER View"),
    ("CREATE\n\t\r\nVIEW", "ALTER VIEW"),
    ("CREATE\r\nVIEW", "ALTER VIEW"),
    ("CREATE /* c1 */   VIEW", "ALTER /* c1 */ VIEW"),
    ("CREATE  /* c */  -- x\n VIEW", "ALTER /* c */ -- x\nVIEW"),
    ("CREATE /* c1 */  /* c2 */  -- c3\n VIEW", "ALTER /* c1 */ /* c2 */ -- c3\nVIEW"),
    ("\n\n  CREATE   VIEW", "\n\n  ALTER VIEW"),
    ("-- above\r\n/* block */\r\nCREATE   VIEW", "-- above\r\n/* block */\r\nALTER VIEW"),
]


@pytest.mark.parametrize(("stored_verb", "unbind_verb"), STORED_VERBS)
def test_unbind_of_every_stored_form_has_one_space_between_the_verb_and_the_kind_word(
    stored_verb, unbind_verb
):
    rest = "\r\nAS\r\nSELECT o.[a]   \nFROM [a].[t] AS o;  \r\n\n"
    stored = f"{stored_verb} [a].[v]\r\nWITH SCHEMABINDING{rest}"
    assert rewrite_for_unbind(stored, "a", "v") == f"{unbind_verb} [a].[v]\r\n{rest}"
    export_verb = unbind_verb.replace("ALTER", "CREATE OR ALTER", 1)
    assert rewrite_for_export(stored, "a", "v")[0] == f"{export_verb} [a].[v]\r\nWITH SCHEMABINDING{rest}"


def test_text_that_is_not_a_module_cannot_be_exported():
    refusal("MODULE_INVALID", rewrite_for_export, "CREATE TABLE a.t (c int)", "a", "t")


@pytest.mark.parametrize(
    ("code", "rewrite"), [("MODULE_INVALID", rewrite_for_export), ("UNBIND_HEADER", rewrite_for_unbind)]
)
def test_a_refusal_about_a_live_definition_never_quotes_its_text(code, rewrite):
    # A25. The lexer names the token it did not expect; for a catalog definition only the line is kept.
    err = refusal(code, rewrite, "\nhunter2 CREATE VIEW a.v WITH SCHEMABINDING AS SELECT 1 AS x", "a", "v")
    assert err.message == "definition of [a].[v]: the module header cannot be read (line 2)"
    assert err.detail == {"line": 2}


# ------------------------------------------------------------------ unbind rewrite
@pytest.mark.parametrize("path", sorted(UNBOUND_HEADERS))
def test_the_body_bytes_are_never_changed_by_an_unbind_rewrite(path):
    m = load(path)
    header = UNBOUND_HEADERS[path]
    unbound = rewrite_for_unbind(m.text, m.schema, m.name)
    assert unbound.startswith(header)
    body = unbound[len(header) :]
    assert body.split()[0].upper() in ("AS", "BEGIN")  # the expected header ends where the body starts
    assert m.text.endswith(body)


def test_removing_the_only_with_option_removes_with():
    unbound = rewrite_for_unbind("CREATE VIEW a.v WITH SCHEMABINDING AS SELECT 1 AS x", "a", "v")
    assert unbound == "ALTER VIEW [a].[v] AS SELECT 1 AS x"


@pytest.mark.parametrize(
    ("options", "left"),
    [
        ("SCHEMABINDING, VIEW_METADATA", "VIEW_METADATA"),
        ("VIEW_METADATA, SCHEMABINDING", "VIEW_METADATA"),
        ("ENCRYPTION, SCHEMABINDING, VIEW_METADATA", "ENCRYPTION, VIEW_METADATA"),
        ("ENCRYPTION ,schemabinding ,VIEW_METADATA", "ENCRYPTION ,VIEW_METADATA"),
        ("SCHEMABINDING -- bound\n , VIEW_METADATA", "VIEW_METADATA"),
        ("EXECUTE AS OWNER, SCHEMABINDING", "EXECUTE AS OWNER"),
    ],
)
def test_removing_one_of_several_with_options_fixes_the_commas(options, left):
    unbound = rewrite_for_unbind(f"CREATE VIEW a.v WITH {options} AS SELECT 1 AS x", "a", "v")
    assert unbound == f"ALTER VIEW [a].[v] WITH {left} AS SELECT 1 AS x"


def test_schemabinding_inside_the_body_or_a_string_is_not_touched():
    body = (
        "AS /* WITH SCHEMABINDING */ SELECT 'WITH SCHEMABINDING, x' AS s, [SCHEMABINDING] AS b "
        "FROM a.t -- , SCHEMABINDING\nWITH CHECK OPTION"
    )
    assert (
        rewrite_for_unbind("CREATE VIEW a.v (s, b) WITH SCHEMABINDING " + body, "a", "v")
        == "ALTER VIEW [a].[v] (s, b) " + body
    )
    # with no option in the header, nothing in the body is taken for it
    refusal("UNBIND_HEADER", rewrite_for_unbind, "CREATE VIEW a.v (s, b) " + body, "a", "v")
    function = (
        "CREATE FUNCTION a.f (@p varchar(40) = 'WITH SCHEMABINDING,') RETURNS int WITH SCHEMABINDING "
        "AS BEGIN RETURN 1 END"
    )
    assert rewrite_for_unbind(function, "a", "f") == (
        "ALTER FUNCTION [a].[f] (@p varchar(40) = 'WITH SCHEMABINDING,') RETURNS int AS BEGIN RETURN 1 END"
    )


@pytest.mark.parametrize(
    "head", ["CREATE VIEW sales.vw_Old", "create  or\nalter VIEW [sales].[vw_Old]", "ALTER VIEW vw_Old"]
)
def test_unbind_sends_alter_with_the_catalog_name_not_the_stale_name(head):
    # A15: after sp_rename the stored text still holds the old name; an ALTER of it would hit
    # another object or none.
    unbound = rewrite_for_unbind(f"{head} WITH SCHEMABINDING AS SELECT 1 AS x", "sales", "vw_New")
    assert unbound == "ALTER VIEW [sales].[vw_New] AS SELECT 1 AS x"


@pytest.mark.parametrize(
    "definition",
    [
        "CREATE VIEW a.v AS SELECT 1 AS x",  # not schema-bound
        "CREATE VIEW a.v WITH VIEW_METADATA AS SELECT 1 AS SCHEMABINDING",
        "CREATE VIEW a.v WITH SCHEMABINDING SELECT 1",  # no body start
        "CREATE VIEW a.v WITH SCHEMABINDING AS SELECT 'x",  # cannot be lexed
        "CREATE TABLE a.v (c int)",
        "",
    ],
)
def test_unbind_refuses_when_the_header_or_the_option_cannot_be_located(definition):
    refusal("UNBIND_HEADER", rewrite_for_unbind, definition, "a", "v")


def test_an_unbound_definition_is_no_longer_schema_bound():
    m = load("schema/views/sales.vw_OrderTotals.sql")
    unbound = rewrite_for_unbind(m.text, m.schema, m.name)
    as_file, _ = rewrite_for_export(unbound, m.schema, m.name)
    assert not read_module(m.path, as_file.encode()).schema_bound
    refusal("UNBIND_HEADER", rewrite_for_unbind, unbound, m.schema, m.name)


# ------------------------------------------------------------------ references and edges
def test_strings_and_comments_are_not_scanned():
    m = mod(
        "procedures/a.p",
        "CREATE OR ALTER PROC a.p AS\n-- EXEC a.q\n/* SELECT * FROM a.v */\n"
        "EXEC sys.sp_executesql N'SELECT * FROM [a].[v]; EXEC a.q';\nEXEC a.r;",
    )
    assert scan_references(m, ["PROCEDURE:[a].[q]", "VIEW:[a].[v]", "PROCEDURE:[a].[r]"]) == {
        "PROCEDURE:[a].[r]"
    }


@pytest.mark.parametrize(
    "written",
    [
        "[sales].[vw x]",
        "sales.[vw x]",
        '"sales"."vw x"',
        '[SALES]."VW X"',
        "sales . [Vw X]",
        "sales./* c */[vw x]",
    ],
)
def test_a_two_part_name_matches_in_any_quoting_and_any_case(written):
    m = mod("procedures/a.p", f"CREATE OR ALTER PROC a.p AS SELECT * FROM {written}")
    assert scan_references(m, ["VIEW:[sales].[vw x]", "VIEW:[dbo].[vw x]"]) == {"VIEW:[sales].[vw x]"}


def test_a_one_part_name_matches_only_schema_dbo():
    # A17. The schema of the module that holds the name plays no part.
    m = mod(
        "procedures/a.p", "CREATE OR ALTER PROC a.p AS SELECT * FROM fn_Numbers(3); EXEC usp_x; EXEC [USP_Y];"
    )
    known = [
        "FUNCTION:[dbo].[fn_Numbers]",
        "FUNCTION:[a].[fn_Numbers]",
        "PROCEDURE:[a].[usp_x]",
        "PROCEDURE:[dbo].[usp_y]",
    ]
    assert scan_references(m, known) == {"FUNCTION:[dbo].[fn_Numbers]", "PROCEDURE:[dbo].[usp_y]"}


def test_a_column_reference_names_its_table_and_a_member_names_no_object():
    m = mod(
        "procedures/a.p",
        "CREATE OR ALTER PROC a.p @doc xml AS "
        "SELECT [sales].[Order].[Status], @doc.fn_Numbers, o.fn_Numbers FROM sales.[Order] AS o",
    )
    known = ["TABLE:[sales].[Order]", "TABLE:[Order].[Status]", "FUNCTION:[dbo].[fn_Numbers]"]
    assert scan_references(m, known) == {"TABLE:[sales].[Order]"}


def test_a_scan_reports_table_class_objects_too():
    m = load("schema/procedures/sales.usp_PlaceOrder.sql")
    known = TABLE_CLASS_KEYS + [key for key, _, _ in FIXTURE_FACTS.values()]
    assert scan_references(m, known) == {
        "TYPE:[sales].[OrderLine_tt]",
        "SEQUENCE:[sales].[OrderNo]",
        "TABLE:[sales].[Order]",
        "TABLE:[sales].[OrderLine]",
        "FUNCTION:[dbo].[fn_Numbers]",
    }


def test_edges_of_the_fixture_set_hold_modules_only():
    mods = load_all()
    assert build_edges(mods, TABLE_CLASS_KEYS) == {m.key: FIXTURE_EDGES.get(m.key, set()) for m in mods}


def test_an_alias_named_like_a_module_in_another_schema_is_not_an_edge():
    # rpt.vw_AliasTrap uses the alias fn_Tax and the column alias [vw_OpenOrders]. Both name
    # modules of schema sales, and a one-part name is looked up in dbo only.
    edges = build_edges(load_all(), TABLE_CLASS_KEYS)
    assert edges["VIEW:[rpt].[vw_AliasTrap]"] == {"VIEW:[sales].[vw_OrderTotals]"}


def test_after_adds_an_edge_and_ignore_dep_removes_one():
    first = mod("procedures/a.first", "CREATE OR ALTER PROC a.first AS SELECT 1")
    second = mod(
        "procedures/a.second",
        "-- azsqlcd:after [a].[FIRST]\n-- azsqlcd:after [a].[not_in_the_set]\n"
        "CREATE OR ALTER PROC a.second AS SELECT 1",
    )
    third = mod(
        "procedures/a.third",
        "-- azsqlcd:ignore-dep a.first\nCREATE OR ALTER PROC a.third AS EXEC a.first; EXEC a.second;",
    )
    assert build_edges([first, second, third], []) == {
        first.key: set(),
        second.key: {first.key},
        third.key: {second.key},
    }


def test_a_self_reference_is_not_a_cycle():
    m = load("schema/procedures/sales.usp_Recurse.sql")
    assert m.text.count("[sales].[usp_Recurse]") == 2
    assert scan_references(m, [m.key]) == set()
    assert deploy_order([m], build_edges([m], [])) == ([m.key], [])
    assert deploy_order([m], {m.key: {m.key}}) == ([m.key], [])


# ------------------------------------------------------------------ order
def test_the_fixture_set_deploys_in_dependency_order_whatever_the_input_order():
    mods = load_all()
    edges = build_edges(mods, TABLE_CLASS_KEYS)
    order, warnings = deploy_order(reversed(mods), edges)
    assert order == FIXTURE_ORDER
    assert warnings == [CYCLE_WARNING + ", ".join(PING_PONG)]
    place = {key: n for n, key in enumerate(order)}
    for key, needs in edges.items():
        for needed in needs:
            if not (key in PING_PONG and needed in PING_PONG):
                assert place[needed] < place[key], f"{key} before {needed}"


def test_ties_are_broken_by_kind_then_key():
    mods = [stub("TRIGGER", "t1"), stub("PROCEDURE", "p2"), stub("PROCEDURE", "p1"), stub("VIEW", "v2")]
    mods += [stub("FUNCTION", "f9"), stub("VIEW", "v1")]
    assert deploy_order(mods, {}) == (
        [
            "FUNCTION:[a].[f9]",
            "VIEW:[a].[v1]",
            "VIEW:[a].[v2]",
            "PROCEDURE:[a].[p1]",
            "PROCEDURE:[a].[p2]",
            "TRIGGER:[a].[t1]",
        ],
        [],
    )


def test_a_dependency_wins_over_the_kind_order():
    view = stub("VIEW", "v")
    needs_view = mod(
        "functions/a.f", "CREATE OR ALTER FUNCTION a.f () RETURNS int AS BEGIN RETURN (SELECT x FROM a.v) END"
    )
    free = stub("FUNCTION", "g")
    mods = [needs_view, view, free]
    assert deploy_order(mods, build_edges(mods, []))[0] == [free.key, view.key, needs_view.key]


def test_mutual_recursion_between_procedures_orders_by_name_with_a_warning():
    view = stub("VIEW", "v")
    pa = mod("procedures/a.pa", "CREATE OR ALTER PROC a.pa AS SELECT x FROM a.v; EXEC a.pb;")
    pb = mod("procedures/a.pb", "CREATE OR ALTER PROC a.pb AS EXEC a.pa; DISABLE TRIGGER a.tr ON a.tbl;")
    pc = mod("procedures/a.pc", "CREATE OR ALTER PROC a.pc AS EXEC a.pa;")
    tr = mod("triggers/a.tr", "CREATE OR ALTER TRIGGER a.tr ON a.tbl AFTER INSERT AS EXEC a.pb;")
    mods = [tr, pc, pb, pa, view]
    order, warnings = deploy_order(mods, build_edges(mods, []))
    # pa, pb and tr are one cycle. The view they need still comes first; pc, which needs pa, after pa.
    assert order == [view.key, pa.key, pb.key, pc.key, tr.key]
    assert warnings == [CYCLE_WARNING + f"{pa.key}, {pb.key}, {tr.key}"]


def test_a_cycle_through_a_view_is_an_error():
    view = mod("views/a.v", "CREATE OR ALTER VIEW a.v AS SELECT a.f() AS x")
    function = mod(
        "functions/a.f", "CREATE OR ALTER FUNCTION a.f () RETURNS int AS BEGIN RETURN (SELECT x FROM a.v) END"
    )
    mods = [stub("PROCEDURE", "p"), view, function]
    err = refusal("ORD004", deploy_order, mods, build_edges(mods, []))
    assert err.detail == {"cycles": [[function.key, view.key, function.key]]}
    assert f"{function.key} -> {view.key} -> {function.key}" in err.message


def test_a_view_inside_a_cycle_of_procedures_is_still_an_error():
    p1, p2, view = stub("PROCEDURE", "p1"), stub("PROCEDURE", "p2"), stub("VIEW", "v")
    edges = {p1.key: {p2.key}, p2.key: {p1.key, view.key}, view.key: {p1.key}}
    err = refusal("ORD004", deploy_order, [p1, p2, view], edges)
    assert err.detail == {"cycles": [[view.key, p1.key, p2.key, view.key]]}


def test_ignore_dep_takes_a_false_edge_out_of_a_view_cycle():
    # The column alias Total reads as the view dbo.Total: a false edge that closes a cycle.
    total = mod(
        "views/dbo.Total", "CREATE OR ALTER VIEW dbo.Total AS SELECT SUM(Total) AS Total FROM dbo.vw_Lines"
    )
    sql = "CREATE OR ALTER VIEW dbo.vw_Lines AS SELECT Qty * Price AS Total FROM dbo.Lines"
    lines = mod("views/dbo.vw_Lines", sql)
    refusal("ORD004", deploy_order, [total, lines], build_edges([total, lines], []))
    fixed = mod("views/dbo.vw_Lines", "-- azsqlcd:ignore-dep [dbo].[Total]\n" + sql)
    assert deploy_order([total, fixed], build_edges([total, fixed], [])) == ([lines.key, total.key], [])


def test_edges_to_modules_outside_the_given_set_are_ignored():
    p = stub("PROCEDURE", "p")
    assert deploy_order([p], {p.key: {"VIEW:[a].[deployed_before]"}, "VIEW:[a].[other]": {p.key}}) == (
        [p.key],
        [],
    )


def test_drop_order_puts_dependants_first():
    keys = ["VIEW:[a].[v]", "FUNCTION:[a].[f]", "PROCEDURE:[a].[p]", "TRIGGER:[a].[t]", "FUNCTION:[a].[g]"]
    edges = {"VIEW:[a].[v]": {"FUNCTION:[a].[f]"}, "FUNCTION:[a].[g]": {"VIEW:[a].[v]"}}
    assert drop_order(keys, edges) == [
        "TRIGGER:[a].[t]",
        "PROCEDURE:[a].[p]",
        "FUNCTION:[a].[g]",
        "VIEW:[a].[v]",
        "FUNCTION:[a].[f]",
    ]


def test_a_long_dependency_chain_is_ordered_without_a_recursion_limit():
    keys = [f"PROCEDURE:[a].[p{n:05d}]" for n in range(5000)]
    edges = {keys[n]: {keys[n + 1]} for n in range(4999)}  # p00000 needs p00001 needs p00002 ...
    assert drop_order(reversed(keys), edges) == keys
