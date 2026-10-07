"""Lint: each rule has a small repository that breaks it and one that comes close and passes.

Fixtures: tests/fixtures/lint/<case>/ is a database repository; a case for a rule that needs the
base revision holds base/ and head/ instead (no base/ = an empty base). expected.txt lists the
findings as 'CODE path:line'. A line '# near-miss: CODE, CODE' names the rules that the case comes
close to and does not break. A case without azsqlcd.toml gets _default/azsqlcd.toml, so that a case
holds only the files of its rule. The sha256 values in migrations.sum are real (chain.file_sha256).
"""

from pathlib import Path

import pytest

from azsqlcd.chain import file_sha256
from azsqlcd.errors import Exit, ToolError
from azsqlcd.lint import (
    ALLOW_CODES,
    CODES,
    DEFERRED_ALLOW_CODES,
    Finding,
    lint_change,
    lint_repo,
    secret_findings,
    secret_in_text,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "lint"
DEFAULT_CONFIG = (FIXTURES / "_default" / "azsqlcd.toml").read_bytes()
CASES = sorted(p.name for p in FIXTURES.iterdir() if p.is_dir() and not p.name.startswith("_"))
MIGRATION = "migrations/0001__change.sql"

FN_TAX = b"""CREATE OR ALTER FUNCTION [sales].[fn_Tax] (@Total decimal(18, 2))
RETURNS decimal(18, 2)
AS
BEGIN
    RETURN @Total * 0.2;
END
"""
FN_TAX_NEW_RATE = FN_TAX.replace(b"0.2", b"0.25")
FN_TAX_PATH = "schema/functions/sales.fn_Tax.sql"
BACKFILL = "UPDATE [sales].[Order] SET [Tax] = [sales].[fn_Tax]([Total]) WHERE [Tax] IS NULL;"
VW_OPEN = b"CREATE OR ALTER VIEW [sales].[vw_Open]\nAS\nSELECT 1 AS [One];\n"
USP_OLD = b"CREATE OR ALTER PROCEDURE [sales].[usp_Old]\nAS\nSELECT 1 AS [One];\n"
TOMBSTONE_OLD = b'[[drop]]\nobject = "PROCEDURE:[sales].[usp_Old]"\nreason = "unused since r40"\n'


# ------------------------------------------------------------------ helpers
def tree(folder: Path) -> dict[str, bytes]:
    """The files of a folder as lint takes them. A folder that does not exist is an empty revision."""
    if not folder.is_dir():
        return {}
    return {
        p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()
    }


def with_config(files: dict[str, bytes]) -> dict[str, bytes]:
    return {"azsqlcd.toml": DEFAULT_CONFIG, **files}


def findings_of(case: str) -> list[Finding]:
    """What `verify` reports for a case: the rules of one revision, then the rules of the change."""
    folder = FIXTURES / case
    if (folder / "head").is_dir():
        head = with_config(tree(folder / "head"))
        return lint_repo(head) + lint_change(tree(folder / "base"), head)
    files = tree(folder)
    del files["expected.txt"]
    return lint_repo(with_config(files))


def expected_of(case: str) -> tuple[list[str], set[str]]:
    """(expected 'CODE path:line' lines, codes that the case is a near miss for)."""
    lines, near = [], set()
    for line in (FIXTURES / case / "expected.txt").read_text().splitlines():
        if line.startswith("# near-miss:"):
            near |= {code.strip() for code in line.split(":", 1)[1].split(",")}
        elif line.strip() and not line.startswith("#"):
            lines.append(line)
    return lines, near


def shown(findings: list[Finding]) -> list[str]:
    return sorted(f"{f.code} {f.path}:{f.line}" for f in findings)


def repo(*bodies: str, mode: str = "tx", files: dict[str, bytes] | None = None) -> dict[str, bytes]:
    """A repository with one migration per body (0001__change, 0002__change, ...) and its chain."""
    out = with_config(files or {})
    lines = ["azsqlcd-sum 1"]
    for n, body in enumerate(bodies, start=1):
        stem = f"{n:04d}__change"
        header = mode if mode == "tx" else f"{mode} expected-minutes: 30"
        data = f"-- azsqlcd:migration {stem}\n-- azsqlcd:mode {header}\n{body}\n".encode()
        out[f"migrations/{stem}.sql"] = data
        lines.append(f"{stem}.sql sha256:{file_sha256(data)} {mode}")
    out["migrations/migrations.sum"] = ("\n".join(lines) + "\n").encode()
    return out


# ------------------------------------------------------------------ the fixture table
@pytest.mark.parametrize("case", CASES)
def test_a_fixture_repository_gives_exactly_its_expected_findings(case):
    expected, _ = expected_of(case)
    assert shown(findings_of(case)) == sorted(expected)


def test_every_code_has_a_case_that_breaks_the_rule_and_a_case_that_comes_close():
    broken: set[str] = set()
    close: set[str] = set()
    for case in CASES:
        expected, near = expected_of(case)
        raised = {line.split(" ", 1)[0] for line in expected}
        assert not raised & near, f"{case} names a near miss that it raises"
        broken |= raised
        close |= near
    assert broken == set(CODES)
    assert close == set(CODES)
    assert len(CASES) >= 40


def test_the_severity_of_a_finding_is_the_severity_of_its_code():
    """Residue (A19), DROP INDEX and DROP CONSTRAINT (h), NTX004, EXA001, a procedure cycle (A17) and
    a directive that names no module file (DIR002) are warnings; so is a module file that is renamed
    by letter case only (MODULE_CASE, N2-F3), and a NOT NULL column with no DEFAULT (NNL001, E2E-9:
    the statement is right for an empty table). Everything else stops the pull request."""
    warnings = {code for code, severity in CODES.items() if severity == "warning"}
    assert warnings == {
        "DROP_INDEX",
        "DROP_CONSTRAINT",
        "NTX004",
        "NNL001",
        "EXA001",
        "CYCLE_BROKEN",
        "REN001",
        "DRP002",
        "DRP003",
        "DIR002",
        "MODULE_CASE",
    }
    assert set(CODES.values()) == {"error", "warning"}
    for case in CASES:
        for finding in findings_of(case):
            assert finding.severity == CODES[finding.code], case


def test_findings_come_back_sorted_by_path_line_and_code():
    found = lint_repo(
        repo(
            "DROP TABLE [sales].[B];\nGO\nDROP TABLE [sales].[A];",
            "-- azsqlcd:data\nCOMMIT; DELETE FROM [sales].[A];",
        )
    )
    assert [(f.path, f.line, f.code) for f in found] == [
        ("migrations/0001__change.sql", 3, "DROP_TABLE"),
        ("migrations/0001__change.sql", 5, "DROP_TABLE"),
        ("migrations/0002__change.sql", 4, "DATA_NO_WHERE"),
        ("migrations/0002__change.sql", 4, "FORBIDDEN_TOKEN"),
    ]


# ------------------------------------------------------------------ allow lines
def test_an_allow_line_for_another_object_does_not_allow_this_statement():
    drop = "DROP TABLE [sales].[Old];"
    other_table = repo("-- azsqlcd:allow DROP_TABLE [sales].[Older] reason: replaced in r12\n" + drop)
    other_code = repo("-- azsqlcd:allow DROP_COLUMN [sales].[Old] reason: replaced in r12\n" + drop)
    other_schema = repo("-- azsqlcd:allow DROP_TABLE [dbo].[Old] reason: replaced in r12\n" + drop)
    for files in (other_table, other_code, other_schema):
        assert shown(lint_repo(files)) == [f"ALLOW_UNUSED {MIGRATION}:3", f"DROP_TABLE {MIGRATION}:4"]
    same = repo("-- azsqlcd:allow DROP_TABLE [sales].[Old] reason: replaced in r12\n" + drop)
    assert lint_repo(same) == []


def test_an_allow_line_of_another_batch_does_not_allow_this_statement():
    files = repo(
        "-- azsqlcd:allow DROP_TABLE [sales].[Old] reason: replaced in r12\n"
        "CREATE TABLE [sales].[New] ([Id] int NOT NULL);\nGO\nDROP TABLE [sales].[Old];"
    )
    assert shown(lint_repo(files)) == [f"ALLOW_UNUSED {MIGRATION}:3", f"DROP_TABLE {MIGRATION}:6"]


def test_an_allow_line_names_its_object_in_any_quoting_and_case():
    """The catalog collation is case-insensitive: [sales].[Old], sales.OLD and "Sales".old are one table."""
    for written in ("sales.OLD", '"Sales".old', "[SALES].[Old]"):
        files = repo(
            f"-- azsqlcd:allow DROP_TABLE {written} reason: replaced in r12\nDROP TABLE [sales].[Old];"
        )
        assert lint_repo(files) == []


@pytest.mark.parametrize("reason", ["TODO", "todo", "TODO: ask the DBA team", "Todo."])
def test_a_reason_of_todo_is_not_a_reason(reason):
    files = repo(f"-- azsqlcd:allow DROP_TABLE [sales].[Old] reason: {reason}\nDROP TABLE [sales].[Old];")
    found = lint_repo(files)
    assert shown(found) == [f"ALLOW_REASON {MIGRATION}:3"]
    assert found[0].severity == "error"


def test_a_reason_that_only_starts_like_todo_is_a_reason():
    files = repo(
        "-- azsqlcd:allow DROP_TABLE [sales].[Old] reason: todos moved to the planner\n"
        "DROP TABLE [sales].[Old];"
    )
    assert lint_repo(files) == []


def test_an_allow_line_that_allows_nothing_is_an_error():
    files = repo(
        "-- azsqlcd:data\n-- azsqlcd:allow DATA_NO_WHERE [sales].[Order] reason: every row changes\n"
        "UPDATE [sales].[Order] SET [Status] = 1 WHERE [Status] IS NULL;"
    )
    found = lint_repo(files)
    assert [(f.code, f.severity, f.line) for f in found] == [("ALLOW_UNUSED", "error", 4)]


def test_an_allow_line_with_a_code_outside_the_closed_list_allows_nothing():
    files = repo(
        "-- azsqlcd:allow DROP_EVERYTHING [sales].[Old] reason: replaced in r12\nDROP TABLE [sales].[Old];"
    )
    found = lint_repo(files)
    assert shown(found) == [f"ALLOW_UNUSED {MIGRATION}:3", f"DROP_TABLE {MIGRATION}:4"]
    assert "not an allow code" in found[0].message


def test_an_allow_code_that_the_lexer_cannot_match_is_left_to_the_rule_that_can():
    """SET_NOT_NULL and REPLACEMENT_EDGE belong to the model proof, OLD_MODULE to the base revision.
    Lint of one revision cannot call them unused. It still wants a reason."""
    assert DEFERRED_ALLOW_CODES == {"SET_NOT_NULL", "OLD_MODULE", "REPLACEMENT_EDGE"}
    assert DEFERRED_ALLOW_CODES < ALLOW_CODES
    for code in sorted(DEFERRED_ALLOW_CODES):
        body = (
            f"-- azsqlcd:allow {code} [sales].[Order] reason: %s\n"
            "CREATE TABLE [sales].[New] ([Id] int NOT NULL);"
        )
        assert lint_repo(repo(body % "checked by hand")) == []
        assert shown(lint_repo(repo(body % "TODO"))) == [f"ALLOW_REASON {MIGRATION}:3"]


def test_a_long_lock_without_allow_is_lck001_and_names_the_nontx_way():
    (finding,) = lint_repo(repo("CREATE INDEX [IX_Order_Status] ON [sales].[Order] ([Status]);"))
    assert (finding.code, finding.severity, finding.line) == ("LCK001", "error", 3)
    assert "-- azsqlcd:allow LONG_LOCK [sales].[Order] reason:" in finding.message
    assert "nontx migration with ONLINE = ON" in finding.message


def test_a_finding_for_a_missing_allow_line_shows_the_line_to_write():
    (finding,) = lint_repo(repo("ALTER TABLE sales.[Order] DROP COLUMN Stat;"))
    assert "-- azsqlcd:allow DROP_COLUMN [sales].[Order].[Stat] reason:" in finding.message


# ------------------------------------------------------------------ forbidden tokens
def test_a_forbidden_word_inside_a_string_or_comment_is_not_a_finding():
    quiet = (
        "-- azsqlcd:data\n-- COMMIT, then GRANT and DROP TABLE x\n"
        "UPDATE [sales].[Order]\n"
        "SET [Note] = N'ROLLBACK; RETURN; USE other; EXEC (x); TRUNCATE TABLE y' /* RAISERROR GOTO */\n"
        "WHERE [Note] = 'BEGIN TRANSACTION' AND [commit] = 1;"
    )
    assert lint_repo(repo(quiet)) == []
    loud = "-- azsqlcd:data\nUPDATE [sales].[Order] SET [Note] = N'x' WHERE [Note] = 'y';\nCOMMIT;"
    assert shown(lint_repo(repo(loud))) == [f"FORBIDDEN_TOKEN {MIGRATION}:5"]


def test_a_module_file_may_hold_what_a_migration_batch_may_not():
    """RETURN, COMMIT and EXECUTE AS are part of a procedure body; the forbidden tokens are rules
    for migration batches."""
    procedure = (
        b"CREATE OR ALTER PROCEDURE [sales].[usp_Close] @Id int\nAS\nBEGIN\n    BEGIN TRANSACTION;\n"
        b"    DELETE FROM [sales].[Open];\n    COMMIT;\n    RETURN 0;\nEND\n"
    )
    assert lint_repo(with_config({"schema/procedures/sales.usp_Close.sql": procedure})) == []


# ------------------------------------------------------------------ files of one revision
def test_a_repository_without_azsqlcd_toml_is_a_finding():
    found = lint_repo({})
    assert [(f.code, f.path, f.severity) for f in found] == [("CONFIG_INVALID", "azsqlcd.toml", "error")]


def test_a_crlf_checkout_of_a_migration_has_the_sha256_of_its_chain_line():
    files = repo("ALTER TABLE [sales].[Order] ADD [Note] nvarchar(200) NULL;")
    files[MIGRATION] = b"\xef\xbb\xbf" + files[MIGRATION].replace(b"\n", b"\r\n")
    assert lint_repo(files) == []
    files[MIGRATION] = files[MIGRATION].replace(b"200", b"400")
    assert shown(lint_repo(files)) == ["CHN004 migrations/migrations.sum:2"]


def test_each_table_class_file_goes_to_the_check_of_the_caller():
    table = b"CREATE TABLE [sales].[Order] ([OrderId] int NOT NULL);\n"
    files = with_config(
        {
            "schema/tables/sales.Order.sql": b"\xef\xbb\xbf" + table,
            "schema/schemas/sales.sql": b"CREATE SCHEMA [sales];\n",
            "schema/sequences/sales.OrderNo.sql": b"CREATE SEQUENCE [sales].[OrderNo] START WITH '1;\n",
            "schema/views/sales.vw_Open.sql": VW_OPEN,
        }
    )
    seen: list[tuple[str, str]] = []

    def check(path: str, text: str) -> list[Finding]:
        seen.append((path, text))
        return [Finding("NF001", "error", path, 7, "explicit NULL or NOT NULL")] if "tables" in path else []

    found = lint_repo(files, table_file_check=check)
    # the file text without the BOM; never a module file, never a file that cannot be lexed
    assert seen == [
        ("schema/schemas/sales.sql", "CREATE SCHEMA [sales];\n"),
        ("schema/tables/sales.Order.sql", table.decode()),
    ]
    assert shown(found) == [
        "FILE_INVALID schema/sequences/sales.OrderNo.sql:1",
        "NF001 schema/tables/sales.Order.sql:7",
    ]


def test_a_file_that_is_not_utf8_is_a_finding_for_that_file():
    files = repo("SELECT 1;", files={"schema/tables/sales.Order.sql": b"CREATE TABLE \xff"})
    files[MIGRATION] = b"-- azsqlcd:migration 0001__change\n\xff"
    files["schema/_tombstones.toml"] = b"\xff"
    assert shown(lint_repo(files)) == [
        "CHN004 migrations/migrations.sum:2",
        f"FILE_INVALID {MIGRATION}:1",
        "FILE_INVALID schema/_tombstones.toml:1",
        "FILE_INVALID schema/tables/sales.Order.sql:1",
    ]


def test_a_secret_literal_is_found_in_sql_as_tokens_and_in_any_other_file_as_text():
    files = with_config(
        {
            "onboarding/prod/align.sql": b"-- step 1\nCREATE USER [x] WITH PASSWORD = N'p@ss';\n",
            "onboarding/prod/notes.md": b"line\nuse secret = 'abc' for the feed\n",
            "onboarding/prod/broken.sql": b"SELECT 'open;\nALTER LOGIN x WITH PASSWORD = 0x01AF HASHED;\n",
        }
    )
    assert shown(lint_repo(files)) == [
        "SECRET_LITERAL onboarding/prod/align.sql:2",
        "SECRET_LITERAL onboarding/prod/broken.sql:2",
        "SECRET_LITERAL onboarding/prod/notes.md:2",
    ]


def test_a_password_variable_or_column_is_not_a_secret_literal():
    files = with_config(
        {
            "onboarding/prod/align.sql": b"DECLARE @password nvarchar(10) = N'x';\n"
            b"UPDATE [s].[u] SET [password] = N'x', [secret_hash] = 0x00 WHERE password = @password;\n"
            b"-- PASSWORD = 'in a comment'\nSELECT 'SECRET = ''in a string''';\n",
            "onboarding/prod/notes.md": b"The password = the one in the vault. @secret = 'x' is not.\n",
        }
    )
    assert lint_repo(files) == []


def test_a_module_with_execute_as_another_principal_is_a_warning_at_the_header_line():
    module = (
        b"-- export\nCREATE OR ALTER PROCEDURE [sales].[usp_Export]\n"
        b"WITH RECOMPILE, EXECUTE AS 'export_reader'\nAS\nSELECT 1 AS [One];\n"
    )
    (finding,) = lint_repo(with_config({"schema/procedures/sales.usp_Export.sql": module}))
    assert (finding.code, finding.severity, finding.line) == ("EXA001", "warning", 3)


def test_a_cycle_through_a_view_names_the_path_of_the_cycle():
    files = with_config(
        {
            "schema/views/sales.vw_A.sql": b"CREATE OR ALTER VIEW [sales].[vw_A]\nAS\n"
            b"SELECT * FROM [sales].[fn_B]();\n",
            "schema/functions/sales.fn_B.sql": b"CREATE OR ALTER FUNCTION [sales].[fn_B] ()\n"
            b"RETURNS TABLE\nAS\nRETURN (SELECT * FROM [sales].[vw_A]);\n",
        }
    )
    (finding,) = lint_repo(files)
    assert (finding.code, finding.severity) == ("ORD004", "error")
    assert "FUNCTION:[sales].[fn_B] -> VIEW:[sales].[vw_A] -> FUNCTION:[sales].[fn_B]" in finding.message
    assert finding.path == "schema/functions/sales.fn_B.sql"


# ------------------------------------------------------------------ rules that need the base revision
def test_a_new_nontx_migration_cannot_share_a_pull_request_with_a_module_change():
    add_note = "ALTER TABLE [sales].[Order] ADD [Note] nvarchar(200) NULL;"
    build = (
        "CREATE INDEX [IX_Order_Status] ON [sales].[Order] ([Status]) "
        "WITH (ONLINE = ON (WAIT_AT_LOW_PRIORITY (MAX_DURATION = 5 MINUTES, ABORT_AFTER_WAIT = SELF)));"
    )

    def head(module: bytes) -> dict[str, bytes]:
        files = repo(add_note, files={FN_TAX_PATH: module})
        data = (
            f"-- azsqlcd:migration 0002__ix\n-- azsqlcd:mode nontx expected-minutes: 30\n{build}\n".encode()
        )
        files["migrations/0002__ix.sql"] = data
        files["migrations/migrations.sum"] += f"0002__ix.sql sha256:{file_sha256(data)} nontx\n".encode()
        return files

    base = repo(add_note, files={FN_TAX_PATH: FN_TAX})
    shared = head(FN_TAX_NEW_RATE)
    assert lint_repo(shared) == []
    found = lint_change(base, shared)
    assert [(f.code, f.severity, f.path) for f in found] == [("NTX003", "error", "migrations/0002__ix.sql")]
    # alone: the same module text, and a change that the normal form removes, are no module change
    assert lint_change(base, head(FN_TAX)) == []
    assert lint_change(base, head(FN_TAX.replace(b"\n", b"  \r\n") + b"\n\n")) == []
    # a new module file is a module change too
    added = head(FN_TAX)
    added["schema/views/sales.vw_Open.sql"] = VW_OPEN
    assert shown(lint_change(base, added)) == ["NTX003 migrations/0002__ix.sql:1"]


def test_a_new_nontx_migration_cannot_share_a_pull_request_with_a_module_drop():
    """A8: a tombstone is a change of the release, with or without the removal of a file."""
    tombstone = {"schema/_tombstones.toml": TOMBSTONE_OLD}
    old_file = {"schema/procedures/sales.usp_Old.sql": USP_OLD}
    head = repo(
        "CREATE INDEX [IX_Order_Status] ON [sales].[Order] ([Status]);", mode="nontx", files=tombstone
    )
    nontx = ["NTX003 migrations/0001__change.sql:1"]
    # the pull request adds the tombstone; the file never was in the repository
    assert shown(lint_change(with_config({}), head)) == nontx
    # the pull request removes the file and adds the tombstone
    assert shown(lint_change(with_config(old_file), head)) == nontx
    # the pull request removes the file; the tombstone was merged before
    assert shown(lint_change(with_config(old_file | tombstone), head)) == nontx
    # the tombstone was merged before and no file goes: the nontx migration is alone
    assert lint_change(with_config(tombstone), head) == []


def test_a_data_batch_that_calls_a_changed_function_needs_deploy_module_or_an_explicit_allow():
    base = with_config({FN_TAX_PATH: FN_TAX})

    def head(above: str = "", module: bytes = FN_TAX_NEW_RATE) -> dict[str, bytes]:
        return repo(f"-- azsqlcd:data\n{above}{BACKFILL}", files={FN_TAX_PATH: module})

    found = lint_change(base, head())
    assert [(f.code, f.severity, f.path, f.line) for f in found] == [("DAT001", "error", MIGRATION, 4)]
    assert "-- azsqlcd:deploy-module [sales].[fn_Tax]" in found[0].message
    assert lint_change(base, head("-- azsqlcd:deploy-module [sales].[fn_Tax]\n")) == []
    assert (
        lint_change(base, head("-- azsqlcd:allow OLD_MODULE sales.FN_TAX reason: old rate for these rows\n"))
        == []
    )
    # a line for another module covers nothing
    assert shown(lint_change(base, head("-- azsqlcd:deploy-module [sales].[fn_Round]\n"))) == [
        f"DAT001 {MIGRATION}:5"
    ]
    other = "-- azsqlcd:allow OLD_MODULE [dbo].[fn_Tax] reason: old rate for these rows\n"
    assert shown(lint_change(base, head(other))) == [f"DAT001 {MIGRATION}:5"]
    # the function is not changed: nothing to ask
    assert lint_change(base, head(module=FN_TAX)) == []
    # the function is new in this pull request: the batch would find no function at all
    assert shown(lint_change(with_config({}), head(module=FN_TAX))) == [f"DAT001 {MIGRATION}:4"]


def test_a_one_part_name_in_a_data_batch_is_a_module_of_schema_dbo_only():
    """A17, the rule of the module order: fn_Tax is [dbo].[fn_Tax], never [sales].[fn_Tax]."""
    one_part = "-- azsqlcd:data\nUPDATE [sales].[Order] SET [Tax] = fn_Tax([Total]) WHERE [Tax] IS NULL;"
    member = "-- azsqlcd:data\nUPDATE [sales].[Order] SET [Tax] = @doc.fn_Tax('/r') WHERE o.fn_Tax = 1;"
    for schema in ("sales", "dbo"):
        path = f"schema/functions/{schema}.fn_Tax.sql"
        old = FN_TAX.replace(b"[sales]", f"[{schema}]".encode())
        new = FN_TAX_NEW_RATE.replace(b"[sales]", f"[{schema}]".encode())
        found = lint_change(with_config({path: old}), repo(one_part, files={path: new}))
        assert shown(found) == ([f"DAT001 {MIGRATION}:4"] if schema == "dbo" else [])
        # a method of a variable and a column of an alias are not the module
        assert lint_change(with_config({path: old}), repo(member, files={path: new})) == []


def test_a_deploy_module_line_of_an_earlier_batch_covers_the_later_batches_of_the_file():
    base = with_config({FN_TAX_PATH: FN_TAX})
    first = "-- azsqlcd:deploy-module [sales].[fn_Tax]\nALTER TABLE [sales].[Order] ADD [Tax] money NULL;"
    covered = repo(f"{first}\nGO\n-- azsqlcd:data\n{BACKFILL}", files={FN_TAX_PATH: FN_TAX_NEW_RATE})
    assert lint_change(base, covered) == []
    later = repo(
        f"-- azsqlcd:data\n{BACKFILL}\nGO\n{first}",
        files={FN_TAX_PATH: FN_TAX_NEW_RATE},
    )
    assert shown(lint_change(base, later)) == [f"DAT001 {MIGRATION}:4"]


def test_a_merged_migration_is_not_asked_for_deploy_module():
    """A merged migration cannot change. Only the migrations that the pull request adds are checked."""
    merged = repo(f"-- azsqlcd:data\n{BACKFILL}", files={FN_TAX_PATH: FN_TAX})
    head = {**merged, FN_TAX_PATH: FN_TAX_NEW_RATE}
    assert lint_change(merged, head) == []


def test_a_model_batch_is_not_scanned_for_module_names():
    """A10 covers data and raw batches; the function of a constraint is the work of deploy-module in gen."""
    base = with_config({FN_TAX_PATH: FN_TAX})
    head = repo(
        "ALTER TABLE [sales].[Order] ADD [Tax] AS ([sales].[fn_Tax]([Total]));",
        files={FN_TAX_PATH: FN_TAX_NEW_RATE},
    )
    assert lint_change(base, head) == []


def test_a_tombstone_counts_only_with_the_exact_key_of_the_removed_file():
    """The planner finds the managed row by the key text, so another case is another key."""
    path = "schema/procedures/sales.usp_Old.sql"
    base = with_config({path: b"CREATE OR ALTER PROCEDURE [sales].[usp_Old]\nAS\nSELECT 1 AS [One];\n"})

    def head(key: str) -> dict[str, bytes]:
        return with_config(
            {"schema/_tombstones.toml": f'[[drop]]\nobject = "{key}"\nreason = "unused"\n'.encode()}
        )

    assert lint_change(base, head("PROCEDURE:[sales].[usp_Old]")) == []
    for key in ("PROCEDURE:[sales].[USP_OLD]", "PROCEDURE:[dbo].[usp_Old]", "VIEW:[sales].[usp_Old]"):
        assert shown(lint_change(base, head(key))) == [f"TMB001 {path}:1"]


def test_a_kind_change_is_an_error_even_with_a_tombstone_for_the_old_kind():
    """Modules are created before the drops run, so the new kind would meet the old object."""
    view = b"CREATE OR ALTER VIEW [rpt].[Margin]\nAS\nSELECT 1 AS [One];\n"
    function = b"CREATE OR ALTER FUNCTION [rpt].[margin] ()\nRETURNS TABLE\nAS\nRETURN (SELECT 1 AS [One]);\n"
    tombstone = b'[[drop]]\nobject = "VIEW:[rpt].[Margin]"\nreason = "now a function"\n'
    base = with_config({"schema/views/rpt.Margin.sql": view})
    head = with_config({"schema/functions/rpt.margin.sql": function, "schema/_tombstones.toml": tombstone})
    found = lint_change(base, head)
    assert [(f.code, f.severity, f.path) for f in found] == [
        ("KND001", "error", "schema/functions/rpt.margin.sql")
    ]
    del head["schema/_tombstones.toml"]
    assert shown(lint_change(base, head)) == [
        "KND001 schema/functions/rpt.margin.sql:1",
        "TMB001 schema/views/rpt.Margin.sql:1",
    ]


def test_an_edited_chain_line_is_chn001_at_the_line_of_the_head_file():
    base = repo(
        "ALTER TABLE [sales].[Order] ADD [A] int NULL;", "ALTER TABLE [sales].[Order] ADD [B] int NULL;"
    )
    head = repo(
        "ALTER TABLE [sales].[Order] ADD [A] int NULL;", "ALTER TABLE [sales].[Order] ADD [B] bigint NULL;"
    )
    found = lint_change(base, head)
    assert [(f.code, f.severity, f.path, f.line) for f in found] == [
        ("CHN001", "error", "migrations/migrations.sum", 3)
    ]
    assert lint_change(base, base) == []


def test_residue_of_an_old_name_is_a_warning_not_an_error():
    """A19: the dependant check inside the transaction (A12) is the gate; lint only points."""
    view = b"CREATE OR ALTER VIEW [sales].[vw_Open]\nAS\nSELECT o.[Stat]\nFROM sales.[order] AS o;\n"
    other = (
        b"CREATE OR ALTER VIEW [sales].[vw_Customer]\nAS\nSELECT c.[Stat]\nFROM [sales].[Customer] AS c;\n"
    )
    base = with_config({"schema/views/sales.vw_Open.sql": view, "schema/views/sales.vw_Customer.sql": other})
    rename = (
        "-- azsqlcd:allow RENAME [sales].[Order].[Stat] reason: API name\n"
        "EXEC sys.sp_rename N'[sales].[Order].[Stat]', N'Status', N'COLUMN';"
    )
    head = repo(rename, files={k: v for k, v in base.items() if k.startswith("schema/")})
    found = lint_change(base, head)
    # only the module that names the table; the column of another table is not the renamed column
    assert [(f.code, f.severity, f.path, f.line) for f in found] == [
        ("REN001", "warning", "schema/views/sales.vw_Open.sql", 3)
    ]


def test_a_base_revision_that_cannot_be_read_stops_the_change_rules():
    head = repo("ALTER TABLE [sales].[Order] ADD [A] int NULL;")
    with pytest.raises(ToolError) as chain_error:
        lint_change({"migrations/migrations.sum": b"not a chain\n"}, head)
    assert (chain_error.value.exit_code, chain_error.value.reason_code) == (Exit.REFUSED, "CHAIN_INVALID")
    with pytest.raises(ToolError) as tombstone_error:
        lint_change({"schema/_tombstones.toml": b"[[drop]]\n"}, head)
    assert tombstone_error.value.reason_code == "TOMBSTONE_INVALID"


def test_a_head_file_that_cannot_be_read_is_reported_once_by_the_rules_of_one_revision():
    base = with_config({FN_TAX_PATH: FN_TAX})
    head = with_config(
        {
            FN_TAX_PATH: b"CREATE FUNCTION [sales].[fn_Tax] () RETURNS int AS BEGIN RETURN 1; END\n",
            "migrations/migrations.sum": b"not a chain\n",
            "schema/_tombstones.toml": b"[[drop]]\n",
        }
    )
    assert shown(lint_repo(head)) == [
        "CHAIN_INVALID migrations/migrations.sum:1",
        f"MODULE_INVALID {FN_TAX_PATH}:1",
        "TOMBSTONE_INVALID schema/_tombstones.toml:1",
    ]
    assert lint_change(base, head) == []


# ------------------------------------------------------------------ rules that plan and deploy refuse late
def test_lint_and_build_see_a_tombstone_for_a_module_that_still_has_its_file():
    """`lint` and `build` have one revision only. The plan refuses such a release (TOMBSTONE_CONFLICT)."""
    path = "schema/procedures/sales.usp_Old.sql"
    files = with_config({path: USP_OLD, "schema/_tombstones.toml": b"# drops\n\n" + TOMBSTONE_OLD})
    (finding,) = lint_repo(files)
    assert (finding.code, finding.severity, finding.path, finding.line) == (
        "TMB002",
        "error",
        "schema/_tombstones.toml",
        4,
    )
    assert path in finding.message
    # `verify` reports it once: the rules of a change do not repeat a rule of one revision
    assert lint_change(with_config({path: USP_OLD}), files) == []
    del files[path]
    assert lint_repo(files) == []


def test_set_parseonly_and_set_noexec_in_a_module_file_are_refused_at_the_pull_request():
    def procedure(body: str) -> dict[str, bytes]:
        text = f"CREATE OR ALTER PROCEDURE [sales].[usp_Check]\nAS\nBEGIN\n{body}\nEND\n"
        return with_config({"schema/procedures/sales.usp_Check.sql": text.encode()})

    path = "schema/procedures/sales.usp_Check.sql"
    for body in ("SET PARSEONLY ON;", "set parseonly off", "SET NOEXEC ON;", "SET\n  NOEXEC OFF;"):
        assert shown(lint_repo(procedure(body))) == [f"FORBIDDEN_TOKEN {path}:4"], body
    # the planner refuses every text with the unquoted word, so lint does too
    assert shown(lint_repo(procedure("SELECT parseonly FROM [sales].[t];"))) == [f"FORBIDDEN_TOKEN {path}:4"]
    for body in (
        "SELECT [PARSEONLY], [NOEXEC] FROM [sales].[t]; -- SET PARSEONLY ON",
        "UPDATE [sales].[t] SET noexec = 1;",  # a column, not the setting
        "SET NOCOUNT ON; PRINT N'SET NOEXEC ON';",
    ):
        assert lint_repo(procedure(body)) == [], body


def test_two_module_files_for_one_object_name_are_a_finding_whatever_the_case_or_the_kind():
    """One schema is one namespace for every kind, and the catalog compares names without case."""
    view = b"CREATE OR ALTER VIEW [sales].[%s]\nAS\nSELECT 1 AS [One];\n"
    lower, upper = "schema/views/sales.vw_open.sql", "schema/views/sales.VW_OPEN.sql"
    same_directory = with_config({lower: view % b"vw_open", upper: view % b"VW_OPEN"})
    (finding,) = lint_repo(same_directory)
    assert (finding.code, finding.severity, finding.path, finding.line) == (
        "MODULE_DUPLICATE",
        "error",
        lower,
        1,
    )
    assert upper in finding.message  # the finding is at the second file by path and names the first
    function = (
        b"CREATE OR ALTER FUNCTION [sales].[vw_open] ()\nRETURNS TABLE\nAS\nRETURN (SELECT 1 AS [One]);\n"
    )
    two_kinds = with_config({lower: view % b"vw_open", "schema/functions/sales.vw_open.sql": function})
    assert shown(lint_repo(two_kinds)) == [f"MODULE_DUPLICATE {lower}:1"]
    three = two_kinds | {upper: view % b"VW_OPEN"}
    assert shown(lint_repo(three)) == [f"MODULE_DUPLICATE {upper}:1", f"MODULE_DUPLICATE {lower}:1"]
    in_rpt = view.replace(b"[sales]", b"[rpt]") % b"vw_open"
    other_schema = with_config({lower: view % b"vw_open", "schema/views/rpt.vw_open.sql": in_rpt})
    assert lint_repo(other_schema) == []


def test_a_directive_that_a_module_file_does_not_read_is_an_error_at_its_line():
    """read_module reads `after` and `ignore-dep`. Anything else is silently not applied: a typing
    mistake in the directive that was meant to cut a cycle."""

    def view(*lines: str) -> dict[str, bytes]:
        text = "\n".join(lines) + "\nCREATE OR ALTER VIEW [sales].[vw_A]\nAS\nSELECT 1 AS [One];\n"
        return with_config(
            {"schema/views/sales.vw_A.sql": text.encode(), "schema/views/sales.vw_Open.sql": VW_OPEN}
        )

    path = "schema/views/sales.vw_A.sql"
    assert lint_repo(view("-- azsqlcd:after [sales].[vw_Open]", "-- azsqlcd:ignore-dep sales.vw_open")) == []
    for wrong in ("ignore_dep", "ignoredep", "After", "allow", "deploy-module", "unbind", "data", ""):
        found = lint_repo(view("-- a note", f"-- azsqlcd:{wrong} [sales].[vw_Open]"))
        assert [(f.code, f.severity, f.path, f.line) for f in found] == [("DIR001", "error", path, 2)], wrong
    # a directive is read in column 0 only; behind other text it is a comment that does nothing
    assert shown(lint_repo(view("  -- azsqlcd:after [sales].[vw_Open]"))) == [f"DIR001 {path}:1"]
    assert lint_repo(view("-- see azsqlcd:after in the docs", "/* -- azsqlcd:afterwards */")) == []


def test_an_after_or_ignore_dep_that_names_no_module_file_is_a_warning():
    """modules.build_edges ignores such a target, because a deploy set can be a part of the files."""

    def procedure(*lines: str) -> dict[str, bytes]:
        text = "\n".join(lines) + "\nCREATE OR ALTER PROCEDURE [sales].[usp_A]\nAS\nSELECT 1 AS [One];\n"
        files = {
            "schema/procedures/sales.usp_A.sql": text.encode(),
            "schema/views/sales.vw_Open.sql": VW_OPEN,
        }
        return with_config(files)

    path = "schema/procedures/sales.usp_A.sql"
    found = lint_repo(
        procedure(
            "-- azsqlcd:after [sales].[vw_Open]",
            "-- azsqlcd:after [sales].[vw_Closed]",
            "-- azsqlcd:ignore-dep [sales].[VW_OPEN]",
            "-- azsqlcd:ignore-dep [dbo].[vw_Open]",
        )
    )
    assert [(f.code, f.severity, f.path, f.line) for f in found] == [
        ("DIR002", "warning", path, 2),
        ("DIR002", "warning", path, 4),
    ]
    assert "[sales].[vw_Closed]" in found[0].message and "[dbo].[vw_Open]" in found[1].message
    # a module may name itself: the file exists
    assert lint_repo(procedure("-- azsqlcd:ignore-dep [sales].[usp_A]")) == []


def test_a_nontx_migration_needs_expected_minutes_for_the_job_timeout():
    build = (
        "CREATE INDEX [IX_Order_Status] ON [sales].[Order] ([Status]) "
        "WITH (ONLINE = ON (WAIT_AT_LOW_PRIORITY (MAX_DURATION = 5 MINUTES, ABORT_AFTER_WAIT = SELF)));"
    )

    def nontx(mode_line: str) -> dict[str, bytes]:
        data = f"-- azsqlcd:migration 0001__change\n-- azsqlcd:mode {mode_line}\n{build}\n".encode()
        chain_line = f"azsqlcd-sum 1\n0001__change.sql sha256:{file_sha256(data)} nontx\n".encode()
        return with_config({MIGRATION: data, "migrations/migrations.sum": chain_line})

    (finding,) = lint_repo(nontx("nontx"))
    assert (finding.code, finding.severity, finding.path) == ("NTX005", "error", MIGRATION)
    assert "expected-minutes" in finding.message
    assert lint_repo(nontx("nontx expected-minutes: 45")) == []
    # a transactional migration has no build that a job timeout could end
    assert lint_repo(repo("ALTER TABLE [sales].[Order] ADD [Note] nvarchar(200) NULL;")) == []


def test_the_secret_rule_is_one_public_rule_for_files_and_for_live_text():
    sql = b"CREATE OR ALTER PROCEDURE [s].[p]\nAS\nEXEC (N'CREATE LOGIN x WITH PASSWORD = ''p''');\n"
    # as tokens: the words stand inside a string, so the file rule has no finding
    assert secret_findings("schema/procedures/s.p.sql", sql) == []
    # as text: the same words are found, for text that nobody reviewed
    assert secret_in_text(sql.decode()) is True
    assert secret_in_text("the password = the one in the vault; @secret = @other") is False
    (finding,) = secret_findings(
        "onboarding/prod/align.sql", b"-- x\nALTER LOGIN [x] WITH PASSWORD = N'p';\n"
    )
    assert (finding.code, finding.severity, finding.line) == ("SECRET_LITERAL", "error", 2)


@pytest.mark.parametrize(
    "text",
    [
        "DECLARE @password varchar(9) = 'hunter2'",
        "DECLARE @Pwd AS nvarchar(20) = N'hunter2';",
        "EXEC sp_addlinkedsrvlogin @rmtsrvname = N'x', @rmtpassword = 'hunter2'",
        "SET @secret = 'x'",
        "UPDATE [u] SET [password] = 'hunter2' WHERE [id] = 1",
        "UPDATE [u] SET \"ApiSecret\" = N'hunter2'",
        "CREATE LOGIN x WITH PASSWORD = ('hunter2')",
        "SELECT @conn = 'Server=x;Uid=u;Pwd=hunter2;'",
        "SELECT 'Server=x;User Id=u;Password=hunter2'",
        "SELECT ENCRYPTBYPASSPHRASE('hunter2', @clear)",
        "SELECT DecryptByPassPhrase ( N'hunter2', [c]) FROM [t]",
        "SET @passphrase = 0xDEADBEEF",
        "IF @client_secret='x' RETURN",
    ],
)
def test_the_rule_for_live_text_finds_a_credential_in_a_variable_a_quoted_name_or_a_call(text):
    """OM-07. Text of a live module was never reviewed; a variable or a named argument with a
    literal is the usual place of a hard-coded credential. The rule for repository files, which
    reads tokens, stays as it is. This test replaces the old assertion that @secret = 'x' is not
    found in live text."""
    assert secret_in_text(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "DECLARE @password varchar(9) = @other",
        "UPDATE [u] SET [password] = HASHBYTES('SHA2_256', @p)",
        "SELECT [PasswordHash], [secret_id] FROM [u] WHERE [a] = 'x'",
        "EXEC [dbo].[usp_SetPassword] @user = 'x', @password = @p",
        "SELECT ENCRYPTBYPASSPHRASE(@passphrase, @clear)",
        "-- the password is in the vault",
        "IF @password IS NULL SET @x = 'y'",
    ],
)
def test_the_rule_for_live_text_does_not_find_a_name_without_a_literal(text):
    assert secret_in_text(text) is False


# ------------------------------------------------------------------ review findings
@pytest.mark.parametrize("char", ["\x00", "\x01", "\x1a", "\x0c", "\x7f", "\u0085", "\u2028", "\u2029"])
def test_migration_with_nul_in_a_comment_is_file_invalid(char):
    """LB-08. If the driver ends the text at NUL, or the engine ends the comment at a character
    that the lexer keeps in it, the text that runs is not the text that was classified."""
    line_comment = repo(f"-- azsqlcd:data\nDELETE FROM [dbo].[T] --{char}\nWHERE [id] = 1;")
    block_comment = repo(f"-- azsqlcd:data\nDELETE FROM [dbo].[T] /*{char}*/ WHERE [id] = 1;")
    literal = repo(f"-- azsqlcd:data\nUPDATE [dbo].[T] SET [a] = N'{char}'\nWHERE [id] = 1;")
    for files in (line_comment, block_comment, literal):
        (finding,) = lint_repo(files)
        assert (finding.code, finding.severity, finding.path, finding.line) == (
            "FILE_INVALID",
            "error",
            MIGRATION,
            4,
        )
        assert f"U+{ord(char):04X}" in finding.message
    assert lint_repo(repo("-- azsqlcd:data\nDELETE FROM [dbo].[T] --\ta tab\nWHERE [id] = 1;")) == []


@pytest.mark.parametrize("char", ["\x00", "\x1a", "\u0085", "\u2028"])
def test_module_with_control_character_is_refused(char):
    path = "schema/procedures/sales.usp_A.sql"
    text = f"CREATE OR ALTER PROCEDURE [sales].[usp_A]\nAS\n-- note{char}\nSELECT 1 AS [One];\n"
    assert shown(lint_repo(with_config({path: text.encode()}))) == [f"FILE_INVALID {path}:3"]
    table = "schema/tables/sales.Order.sql"
    ddl = f"CREATE TABLE [sales].[Order] (\n    [OrderId] int NOT NULL -- key{char}\n);\n"
    assert shown(lint_repo(with_config({table: ddl.encode()}))) == [f"FILE_INVALID {table}:2"]


def test_a_number_with_a_keyword_directly_after_it_cannot_be_linted_as_one_name():
    """LB-04. The engine reads 1eDELETE as the float 1e and the keyword DELETE."""
    (finding,) = lint_repo(repo("-- azsqlcd:data\nSELECT 1eDELETE FROM [dbo].[T];"))
    assert (finding.code, finding.path, finding.line) == ("MIGRATION_INVALID", MIGRATION, 4)
    assert "space" in finding.message


def test_a_data_batch_that_sets_implicit_transactions_is_refused():
    """SQL-5, RS-9. After the COMMIT of the tool, its own UPDATE of the run row would open a
    transaction that nothing commits."""
    update = "UPDATE [sales].[Order] SET [c] = 0 WHERE [c] IS NULL;"
    body = "-- azsqlcd:data\nSET IMPLICIT_TRANSACTIONS ON;\n" + update
    assert shown(lint_repo(repo(body))) == [f"FORBIDDEN_TOKEN {MIGRATION}:4"]
    assert shown(lint_repo(repo(body.replace("IMPLICIT_TRANSACTIONS", "ANSI_DEFAULTS")))) == [
        f"FORBIDDEN_TOKEN {MIGRATION}:4"
    ]
    assert lint_repo(repo(body.replace("IMPLICIT_TRANSACTIONS", "NOCOUNT"))) == []


def test_module_set_option_list_with_noexec_is_refused():
    """LB-05, SQL-5. In a module, SET IMPLICIT_TRANSACTIONS OFF and SET XACT_ABORT ON are usual and
    safe; NOEXEC in a list, and an option that opens a transaction, are not."""
    path = "schema/procedures/sales.usp_A.sql"

    def procedure(statement: str) -> dict[str, bytes]:
        text = f"CREATE OR ALTER PROCEDURE [sales].[usp_A]\nAS\n{statement};\nSELECT 1 AS [One];\n"
        return with_config({path: text.encode()})

    for statement in (
        "SET NOEXEC ON",
        "SET NOCOUNT, NOEXEC ON",
        "SET NOEXEC, NOCOUNT OFF",
        "SET IMPLICIT_TRANSACTIONS ON",
        "SET NOCOUNT, IMPLICIT_TRANSACTIONS ON",
        "SET ANSI_DEFAULTS ON",
    ):
        assert shown(lint_repo(procedure(statement))) == [f"FORBIDDEN_TOKEN {path}:3"], statement
    for statement in (
        "SET NOCOUNT ON",
        "SET XACT_ABORT ON",
        "SET NOCOUNT, XACT_ABORT ON",
        "SET IMPLICIT_TRANSACTIONS OFF",
        "SET ANSI_DEFAULTS OFF",
        "DECLARE @noexec int; SET @noexec = 1",
        "UPDATE [sales].[Order] SET implicit_transactions = 1 WHERE [a] = 1",
    ):
        assert lint_repo(procedure(statement)) == [], statement


def test_a_procedure_call_without_exec_passes_only_with_its_allow_line():
    """LB-03."""
    call = "-- azsqlcd:data\n{allow}[dbo].[usp_purge_all] @confirm = 1;"
    assert shown(lint_repo(repo(call.format(allow="")))) == [f"EXEC_PROC {MIGRATION}:4"]
    allow = "-- azsqlcd:allow EXEC_PROC [dbo].[usp_purge_all] reason: agreed for r41\n"
    assert lint_repo(repo(call.format(allow=allow))) == []


def test_tombstone_must_name_the_key_of_the_removed_module_not_only_its_path():
    """LB-13. [a.b].[c] and [a].[b.c] have one file path, schema/procedures/a.b.c.sql."""
    path = "schema/procedures/a.b.c.sql"
    base = with_config({path: b"CREATE OR ALTER PROCEDURE [a.b].[c]\nAS\nSELECT 1 AS [One];\n"})

    def head(key: str) -> dict[str, bytes]:
        return with_config(
            {"schema/_tombstones.toml": f'[[drop]]\nobject = "{key}"\nreason = "unused"\n'.encode()}
        )

    assert shown(lint_change(base, head("PROCEDURE:[a].[b.c]"))) == [f"TMB001 {path}:1"]
    assert lint_change(base, head("PROCEDURE:[a.b].[c]")) == []
    # a base file that cannot be read has no key; the path decides, as before
    broken = with_config({path: b"CREATE OR ALTER PROCEDURE [a.b].[c] '\n"})
    assert lint_change(broken, head("PROCEDURE:[a].[b.c]")) == []
    assert shown(lint_change(broken, with_config({}))) == [f"TMB001 {path}:1"]


USP_OLD_UPPER_PATH = "schema/procedures/SALES.USP_OLD.sql"
USP_OLD_UPPER = USP_OLD.replace(b"[sales].[usp_Old]", b"[SALES].[USP_OLD]")


def test_a_module_file_renamed_by_letter_case_only_is_one_object_and_needs_no_tombstone():
    """N2-F3. The database compares names without case, and so does the plan: with the tombstone
    that TMB001 asked for, every deploy of the release was refused (TOMBSTONE_CONFLICT)."""
    old = "schema/procedures/sales.usp_Old.sql"
    found = lint_change(with_config({old: USP_OLD}), with_config({USP_OLD_UPPER_PATH: USP_OLD_UPPER}))
    assert [(f.code, f.severity, f.path, f.line) for f in found] == [
        ("MODULE_CASE", "warning", USP_OLD_UPPER_PATH, 1)
    ]
    message = found[0].message
    assert old in message and "no tombstone" in message and "catalog does not change" in message
    # the file text can keep the old name: the key of the file is its text, the path is the same object
    found = lint_change(with_config({old: USP_OLD}), with_config({USP_OLD_UPPER_PATH: b"CREATE VIEW x '"}))
    assert shown(found) == [f"MODULE_CASE {USP_OLD_UPPER_PATH}:1"]  # lint_repo reports MODULE_INVALID
    # a base file that cannot be read has no key: the path decides
    broken = with_config({old: b"CREATE OR ALTER PROCEDURE [sales].[usp_Old] '\n"})
    assert shown(lint_change(broken, with_config({USP_OLD_UPPER_PATH: USP_OLD_UPPER}))) == [
        f"MODULE_CASE {USP_OLD_UPPER_PATH}:1"
    ]


def test_a_rename_to_another_name_or_kind_still_needs_the_tombstone():
    old = "schema/procedures/sales.usp_Old.sql"
    other = {"schema/procedures/sales.usp_Older.sql": USP_OLD.replace(b"usp_Old", b"usp_Older")}
    assert shown(lint_change(with_config({old: USP_OLD}), with_config(other))) == [f"TMB001 {old}:1"]
    # a file that stays is no rename: the removed file of the same key without case is a second file
    both = {old: USP_OLD, USP_OLD_UPPER_PATH: USP_OLD_UPPER}
    assert shown(lint_change(with_config(both), with_config({old: USP_OLD}))) == [
        f"TMB001 {USP_OLD_UPPER_PATH}:1"
    ]


def test_a_tombstone_and_a_file_whose_keys_differ_in_letter_case_only_are_tmb002():
    """The plan refuses that release with TOMBSTONE_CONFLICT, so the pull request check must fail."""
    head = with_config({USP_OLD_UPPER_PATH: USP_OLD_UPPER, "schema/_tombstones.toml": TOMBSTONE_OLD})
    found = [f for f in lint_repo(head) if f.code == "TMB002"]
    assert [(f.path, f.line) for f in found] == [("schema/_tombstones.toml", 2)]
    assert USP_OLD_UPPER_PATH in found[0].message
    # the pull request that renames by case and adds the tombstone: no TMB001, and TMB002 stops it
    change = lint_change(with_config({"schema/procedures/sales.usp_Old.sql": USP_OLD}), head)
    assert "TMB001" not in {f.code for f in change}


def test_lint_reads_the_head_revision_once_when_the_caller_gives_it():
    """N5-04: verify gives one parse_revision result to both rule sets; the findings are the same."""
    from azsqlcd.lint import parse_revision

    base = with_config({FN_TAX_PATH: FN_TAX})
    head = repo(f"-- azsqlcd:data\n{BACKFILL}", files={FN_TAX_PATH: FN_TAX_NEW_RATE})
    parsed = parse_revision(head)
    assert lint_repo(head, parsed=parsed) == lint_repo(head)
    assert lint_change(base, head, parsed=parsed) == lint_change(base, head)
    assert "DAT001" in {f.code for f in lint_change(base, head, parsed=parsed)}


# ------------------------------------------------------------------ the data switch (owner decision)
SUM = "migrations/migrations.sum"
OFF_CONFIG = DEFAULT_CONFIG.replace(b"data_batches = true\n", b"")
OFF_WRITTEN = DEFAULT_CONFIG.replace(b"data_batches = true", b"data_batches = false")
DATA_OFF = (
    "data batches are switched off; this version manages structural changes; set data_batches = true "
    "in azsqlcd.toml to use them"
)


def test_the_fixtures_and_the_helpers_of_the_data_rules_run_with_data_batches_on():
    """Else every data-rule test would pass or fail on DATA000 and say nothing about its rule."""
    assert b"\ndata_batches = true\n" in DEFAULT_CONFIG
    assert b"data_batches" not in OFF_CONFIG and b"\ndata_batches = false\n" in OFF_WRITTEN


@pytest.mark.parametrize("config", [OFF_CONFIG, OFF_WRITTEN], ids=["key absent", "false"])
def test_a_data_batch_is_data000_once_at_its_first_line_when_data_batches_are_off(config):
    body = (
        "ALTER TABLE [sales].[Order] ADD [Status] int NULL;\nGO\n"
        "-- azsqlcd:data\n-- azsqlcd:allow TRUNCATE [sales].[Old] reason: nothing reads it\n"
        "COMMIT;\nDELETE FROM [sales].[Order];\nDROP TABLE [sales].[Old];\nGO\n"
        "-- azsqlcd:data\nUPDATE [sales].[Order] SET [Status] = 1 WHERE [Status] IS NULL;"
    )
    on = repo(body)
    assert shown(lint_repo(on)) == [
        f"ALLOW_UNUSED {MIGRATION}:6",
        f"DATA_DDL {MIGRATION}:9",
        f"DATA_NO_WHERE {MIGRATION}:8",
        f"FORBIDDEN_TOKEN {MIGRATION}:7",
    ]
    found = lint_repo({**on, "azsqlcd.toml": config})
    # one finding per data batch, also for the batch that breaks no data rule; no other data rule
    assert [(f.code, f.severity, f.path, f.line, f.message) for f in found] == [
        ("DATA000", "error", MIGRATION, 5, DATA_OFF),
        ("DATA000", "error", MIGRATION, 11, DATA_OFF),
    ]


def test_model_and_raw_batches_are_not_touched_by_the_data_switch():
    body = (
        "DROP TABLE [sales].[Old];\nGO\n"
        "-- azsqlcd:raw TABLE:[audit].[Log] reason: outside the model\nALTER TABLE [audit].[Log] REBUILD;"
    )
    expected = [f"DROP_TABLE {MIGRATION}:3", f"RAW {MIGRATION}:6"]
    assert shown(lint_repo(repo(body))) == expected
    assert shown(lint_repo({**repo(body), "azsqlcd.toml": OFF_CONFIG})) == expected


def test_a_config_that_does_not_load_keeps_its_finding_and_counts_as_data_batches_off():
    files = repo("-- azsqlcd:data\nUPDATE [sales].[Order] SET [Status] = 1 WHERE [Status] IS NULL;")
    assert lint_repo(files) == []
    files["azsqlcd.toml"] = DEFAULT_CONFIG + b"\n[extra]\nx = 1\n"
    assert shown(lint_repo(files)) == ["CONFIG_INVALID azsqlcd.toml:1", f"DATA000 {MIGRATION}:3"]
    files["azsqlcd.toml"] = b"\xff"
    assert shown(lint_repo(files)) == ["CONFIG_INVALID azsqlcd.toml:1", f"DATA000 {MIGRATION}:3"]
    del files["azsqlcd.toml"]
    assert shown(lint_repo(files)) == ["CONFIG_INVALID azsqlcd.toml:1", f"DATA000 {MIGRATION}:3"]


def test_the_change_rule_of_a_data_batch_is_not_reported_when_data_batches_are_off():
    """DAT001 is a data rule. A raw batch keeps it."""
    base = with_config({FN_TAX_PATH: FN_TAX})
    head = repo(f"-- azsqlcd:data\n{BACKFILL}", files={FN_TAX_PATH: FN_TAX_NEW_RATE})
    assert shown(lint_change(base, head)) == [f"DAT001 {MIGRATION}:4"]
    head["azsqlcd.toml"] = OFF_CONFIG
    assert lint_change(base, head) == []
    assert shown(lint_repo(head)) == [f"DATA000 {MIGRATION}:3"]
    raw = repo(
        "-- azsqlcd:raw TABLE:[sales].[Order] reason: backfill outside the model\n"
        f"-- azsqlcd:allow RAW TABLE:[sales].[Order] reason: agreed\n{BACKFILL}",
        files={FN_TAX_PATH: FN_TAX_NEW_RATE},
    )
    raw["azsqlcd.toml"] = OFF_CONFIG
    assert shown(lint_change(base, raw)) == [f"DAT001 {MIGRATION}:5"]


# ------------------------------------------------------------------ second wave of review findings
def withdraw(files: dict[str, bytes], stem: str, replacement: str | None = None) -> dict[str, bytes]:
    """The repository with the chain line of the migration marked withdrawn; with a replacement
    body, also a new migration whose chain line says replaces=<that file>."""
    out = dict(files)
    lines = out[SUM].decode().splitlines()
    lines = [line + " withdrawn" if line.startswith(f"{stem}.sql ") else line for line in lines]
    if replacement is not None:
        new = f"{len(lines):04d}__change"
        data = f"-- azsqlcd:migration {new}\n-- azsqlcd:mode tx\n{replacement}\n".encode()
        out[f"migrations/{new}.sql"] = data
        lines.append(f"{new}.sql sha256:{file_sha256(data)} tx replaces={stem}.sql")
    out[SUM] = ("\n".join(lines) + "\n").encode()
    return out


def test_a_merged_migration_that_is_withdrawn_needs_its_replacement_in_the_same_pull_request():
    """LB-07, table_model = false. A database that did not run the withdrawn migration skips it for
    good, and no model proof compares the chain with the tables."""
    add_a, add_b = (f"ALTER TABLE [sales].[Order] ADD [{c}] int NULL;" for c in "AB")
    base = repo(add_a, add_b)
    alone = withdraw(base, "0002__change")
    assert lint_repo(alone) == []  # one revision cannot know that the line was merged
    (finding,) = lint_change(base, alone)
    assert (finding.code, finding.severity, finding.path, finding.line) == ("CHN006", "error", SUM, 3)
    assert "replaces=0002__change.sql" in finding.message
    # with its replacement: the near miss
    replaced = withdraw(base, "0002__change", "ALTER TABLE [sales].[Order] ADD [B] bigint NULL;")
    assert lint_repo(replaced) == [] and lint_change(base, replaced) == []
    # a new migration that replaces nothing, or another line, is no replacement
    unrelated = withdraw(base, "0002__change")
    extra = b"-- azsqlcd:migration 0003__change\n-- azsqlcd:mode tx\nDROP SYNONYM [dbo].[x];\n"
    unrelated["migrations/0003__change.sql"] = extra
    unrelated[SUM] += f"0003__change.sql sha256:{file_sha256(extra)} tx\n".encode()
    assert shown(lint_change(base, unrelated)) == [f"CHN006 {SUM}:3"]
    both = withdraw(withdraw(base, "0001__change"), "0002__change", add_b)
    assert shown(lint_change(base, both)) == [f"CHN006 {SUM}:2"]
    # the line was withdrawn in an earlier pull request: nothing to ask now
    assert lint_change(alone, alone) == []
    # with table_model = true the model proof of `verify` holds the rule
    with_model = {
        **alone,
        "azsqlcd.toml": DEFAULT_CONFIG.replace(b"table_model = false", b"table_model = true"),
    }
    assert lint_change(base, with_model) == []


def test_a_tombstone_beside_the_file_of_another_object_with_the_same_path_is_no_conflict():
    """TMB002 is decided by the object key, as the plan decides TOMBSTONE_CONFLICT: [a.b].[c] and
    [a].[b.c] have one file path. (Deferred in the first wave: the path decided.)"""
    path = "schema/procedures/a.b.c.sql"
    module = b"CREATE OR ALTER PROCEDURE [a.b].[c]\nAS\nSELECT 1 AS [One];\n"

    def files(key: str, data: bytes = module) -> dict[str, bytes]:
        tombstone = f'[[drop]]\nobject = "{key}"\nreason = "unused"\n'.encode()
        return with_config({path: data, "schema/_tombstones.toml": tombstone})

    assert lint_repo(files("PROCEDURE:[a].[b.c]")) == []
    assert shown(lint_repo(files("PROCEDURE:[a.b].[c]"))) == ["TMB002 schema/_tombstones.toml:2"]
    # a file that cannot be read has no key; the path decides, and the file has its own finding
    broken = files("PROCEDURE:[a].[b.c]", b"CREATE OR ALTER PROCEDURE [a.b].[c] '\n")
    assert shown(lint_repo(broken)) == [f"MODULE_INVALID {path}:1", "TMB002 schema/_tombstones.toml:2"]


def test_a_hex_password_in_sql_that_lexes_is_a_secret_literal():
    """TQ-10: the hex form was tested only in a file that cannot be lexed (the text rule)."""
    sql = b"-- step 1\nALTER LOGIN [x] WITH PASSWORD = 0x01AF HASHED;\n"
    (finding,) = secret_findings("onboarding/prod/align.sql", sql)
    assert (finding.code, finding.severity, finding.line) == ("SECRET_LITERAL", "error", 2)
    files = repo(
        "-- azsqlcd:raw TABLE:[audit].[Log] reason: outside the model\n"
        "-- azsqlcd:allow RAW TABLE:[audit].[Log] reason: agreed\n"
        "ALTER LOGIN [x] WITH\n    secret = 0X01af;"
    )
    assert shown(lint_repo(files)) == [f"SECRET_LITERAL {MIGRATION}:6"]
    # a number that is not hex, a quoted name, and a variable are no literal of a secret
    quiet = b"SELECT 1 WHERE password = 1 AND [password] = 0x01 AND @secret = 0x01;\n"
    assert secret_findings("onboarding/prod/align.sql", quiet) == []


def test_a_digit_like_character_is_a_finding_with_its_line_and_never_a_crash():
    """PP-5. str.isdigit is true for a superscript two and the number rule does not match it. The
    lexer gave an AssertionError, which no caller catches. (Closed in the first wave; this is the
    path through lint for a table file, a module and a migration.)"""
    files = repo(
        "-- azsqlcd:data\nUPDATE [s].[t] SET [a] = 5\u00b2\nWHERE [b] = 1;",
        files={
            "schema/tables/sales.Order.sql": "CREATE TABLE [sales].[Order] (\n    [OrderId] int NOT NULL,\n"
            "    [Area] AS ([OrderId] * 10\u00b2)\n);\n".encode(),
            "schema/views/sales.vw_A.sql": "CREATE OR ALTER VIEW [sales].[vw_A]\nAS\n"
            "SELECT \u2460 AS [x];\n".encode(),
        },
    )
    assert shown(lint_repo(files)) == [
        "FILE_INVALID schema/tables/sales.Order.sql:3",
        f"MIGRATION_INVALID {MIGRATION}:4",
        "MODULE_INVALID schema/views/sales.vw_A.sql:3",
    ]


# ------------------------------------------------------------------ review 2 and the live runs
def test_an_allow_line_with_a_wrong_prefix_is_named_as_a_plain_comment():
    """LFP-06. The author sees an allow line above the statement and 'has no allow line'."""
    drop = "ALTER TABLE [dbo].[T] DROP COLUMN [a];"
    for prefix in ("--azsqlcd:", "--  azsqlcd:", "--\tazsqlcd:", "-- AZSQLCD:", "-- azsqlcd :"):
        found = lint_repo(repo(f"{prefix}allow DROP_COLUMN [dbo].[T].[a] reason: gone\n{drop}"))
        assert shown(found) == [f"ALLOW_FORMAT {MIGRATION}:3", f"DROP_COLUMN {MIGRATION}:4"], prefix
        message = found[0].message
        assert "plain comment" in message and "'-- azsqlcd:'" in message
        assert found[0].severity == "error"
    right = "-- azsqlcd:allow DROP_COLUMN [dbo].[T].[a] reason: gone\n"
    assert lint_repo(repo(right + drop)) == []
    # a comment that only names the tool, and the text inside a block comment or a string
    for comment in ("-- see azsqlcd:allow in the runbook", "/* --azsqlcd:allow x */", "-- azsqlcd"):
        assert lint_repo(repo(f"{comment}\n{right}{drop}")) == [], comment


def test_a_wrong_prefix_is_a_finding_in_a_data_batch_that_is_switched_off_too():
    files = repo("--azsqlcd:data\nDELETE FROM [s].[t] WHERE [a] = 1;")
    assert "ALLOW_FORMAT" in [f.code for f in lint_repo(files)]


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE u SET Password = '' WHERE 1 = 0;",
        "UPDATE u SET Password = N'', Secret = '' WHERE 1 = 0;",
        "SELECT 1 WHERE Secret = 'none';",
        "SELECT 1 FROM u WHERE a = 1 AND Password = 'x' OR Secret = N'y';",
        "SELECT 1 FROM u WHERE (a = 1 OR (Password = 'x'));",
        "SELECT 1 FROM u JOIN v ON v.Password = 'x' AND u.Id = v.Id;",
        "IF EXISTS (SELECT 1 FROM u WHERE a IN ('p', 'q') AND Password = 'x') SELECT 1;",
        "SELECT CASE WHEN Password = 'x' THEN 1 ELSE 0 END FROM u;",
        "DELETE FROM u WHERE Id = 1 AND NOT Secret = 'none';",
    ],
)
def test_an_empty_literal_and_a_comparison_are_not_secret_literals(sql):
    """LFP-08. A column named Password that is compared, or set to '', holds no secret of the file."""
    module = f"CREATE OR ALTER PROCEDURE [dbo].[p]\nAS\n{sql}\n".encode()
    assert secret_findings("schema/procedures/dbo.p.sql", module) == []


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE u SET Password = 'hunter2' WHERE Id = 1;",
        "UPDATE u SET a = 1, Secret = N'hunter2';",
        "CREATE USER [x] WITH PASSWORD = N'p@ss';",
        "CREATE USER [x] WITH DEFAULT_SCHEMA = dbo, PASSWORD = 'p';",
        "ALTER LOGIN x WITH PASSWORD = 0x01AF HASHED;",
        "CREATE DATABASE SCOPED CREDENTIAL c WITH IDENTITY = 'a', SECRET = 'b';",
        # no ';' is needed between statements: the WHERE of the statement before decides nothing
        "SELECT 1 FROM u WHERE a = 1 OPEN SYMMETRIC KEY k DECRYPTION BY PASSWORD = 'x';",
        "SELECT 1 FROM u WHERE a = 1 ALTER LOGIN x WITH PASSWORD = 'p';",
        "SELECT 1 FROM u WHERE a = 1; CREATE USER y WITH PASSWORD = 'p';",
        "SELECT 1 FROM u WHERE a = 1 UPDATE u SET Password = 'p';",
        "SELECT 1 FROM u JOIN v ON u.a = v.a CREATE USER y WITH PASSWORD = 'p';",
        "BACKUP CERTIFICATE c TO FILE = 'f' WITH PRIVATE KEY (FILE = 'k', ENCRYPTION BY PASSWORD = 'x');",
        "SELECT 1 FROM u WHERE a = f(PASSWORD = 'x');",
        "SELECT Password = 'x' FROM u;",
    ],
)
def test_a_literal_that_is_given_to_a_password_is_still_a_secret_literal(sql):
    module = f"CREATE OR ALTER PROCEDURE [dbo].[p]\nAS\n{sql}\n".encode()
    assert shown(secret_findings("schema/procedures/dbo.p.sql", module)) == [
        "SECRET_LITERAL schema/procedures/dbo.p.sql:3"
    ]


def test_a_money_literal_with_another_currency_symbol_is_read_in_a_module_file():
    module = "CREATE OR ALTER PROCEDURE [dbo].[p]\nAS\nSELECT \u00a35 AS [a], \u20ac5 AS [b];\n".encode()
    assert lint_repo(with_config({"schema/procedures/dbo.p.sql": module})) == []


def test_the_chain_findings_name_the_command_that_repairs_the_chain():
    """CU-06. lint is the command that the author runs after an edit; gen --resum is the repair."""
    files = repo("SELECT 1;")
    files[MIGRATION] += b"-- edited\n"  # CHN004
    files["migrations/0002__by_hand.sql"] = (
        b"-- azsqlcd:migration 0002__by_hand\n-- azsqlcd:mode tx\nCREATE SCHEMA [x];\n"  # CHN002
    )
    found = {f.code: f.message for f in lint_repo(files) if f.code.startswith("CHN")}
    assert set(found) == {"CHN002", "CHN004"}
    for message in found.values():
        assert "azsqlcd gen --resum" in message
        assert "a merged migration never changes" in message
    # two equal numbers after a merge of main
    twice = repo("CREATE SCHEMA [a];", "CREATE SCHEMA [b];")
    sum_text = twice["migrations/migrations.sum"].decode().replace("0002__change", "0001__other")
    twice["migrations/migrations.sum"] = sum_text.encode()
    invalid = [f for f in lint_repo(twice) if f.code == "CHAIN_INVALID"]
    assert len(invalid) == 1 and "azsqlcd gen --resum" in invalid[0].message


def test_a_wrong_tombstone_is_reported_at_the_line_of_its_drop_table():
    """CU-08. The n-th [[drop]] was always reported at line 1."""
    text = (
        "# modules that a release drops\n\n"
        + TOMBSTONE_OLD.decode()
        + '\n[[drop]]\nobject = "sales.usp_CancelOrder"\nreason = "x"\n'
    )
    found = lint_repo(with_config({"schema/_tombstones.toml": text.encode()}))
    assert shown(found) == ["TOMBSTONE_INVALID schema/_tombstones.toml:7"]
    assert "PROCEDURE:[sales].[usp_x]" in found[0].message
    # a file that is not TOML: the line that the TOML reader names
    broken = b'[[drop]]\nobject = "PROCEDURE:[s].[p]"\nreason = \n'
    assert shown(lint_repo(with_config({"schema/_tombstones.toml": broken}))) == [
        "TOMBSTONE_INVALID schema/_tombstones.toml:3"
    ]


def test_tmb001_shows_the_entry_to_add():
    """CU-08. The key form KIND:[schema].[name] was only in the comment of the template."""
    base = with_config({"schema/procedures/sales.usp_Old.sql": USP_OLD})
    (found,) = lint_change(base, with_config({}))
    assert found.code == "TMB001"
    assert "[[drop]]" in found.message
    assert 'object = "PROCEDURE:[sales].[usp_Old]"' in found.message
    assert 'reason = "' in found.message
    # a base file that cannot be read has no key: the message still says what to write
    broken = with_config(
        {"schema/procedures/sales.usp_Old.sql": b"CREATE OR ALTER PROCEDURE [sales].[usp_Old] '\n"}
    )
    (found,) = lint_change(broken, with_config({}))
    assert "[[drop]]" in found.message and "PROCEDURE:[<schema>].[<name>]" in found.message


def test_nf000_for_a_clause_in_another_place_is_one_finding_that_says_the_order():
    """CU-09. The token diff gave two lines that contradict each other and never the order."""
    path = "schema/tables/sales.Address.sql"

    def check(checked: str, _text: str) -> list[Finding]:
        return [
            Finding(
                "NF000", "error", checked, 4, "line 4: the canonical form has NOT NULL; the file does not"
            ),
            Finding(
                "NF000", "error", checked, 4, "line 4: the file has NOT NULL; the canonical form does not"
            ),
            Finding(
                "NF000", "error", checked, 6, "line 6: the file has WITH CHECK; the canonical form does not"
            ),
            Finding(
                "NF000", "error", checked, 7, "line 7: the canonical form has ( 1 , 1 ); the file does not"
            ),
        ]

    found = lint_repo(
        with_config({path: b"CREATE TABLE [sales].[Address] ([a] int NOT NULL);\n"}), table_file_check=check
    )
    assert shown(found) == [f"NF000 {path}:4", f"NF000 {path}:6", f"NF000 {path}:7"]
    moved = found[0].message
    assert "NOT NULL is in another place than in the canonical form" in moved
    assert moved.count("NULL | NOT NULL") == 1  # the order, once
    assert "the file does not" not in moved and "the canonical form does not" not in moved
    assert found[1].message == "line 6: the file has WITH CHECK; the canonical form does not"
    assert found[2].message == "line 7: the canonical form has ( 1 , 1 ); the file does not"
