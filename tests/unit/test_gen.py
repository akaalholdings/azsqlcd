"""`gen`: the model of a revision, the generated migration text and the chain line.

generate() and resum() run on temporary git repositories. The proof of the generated SQL is in
test_verify_proof.py.
"""

import dataclasses
from pathlib import Path

import pytest

from azsqlcd import chain, diff, emit, gen, lint, release, replay
from azsqlcd.errors import Exit, ToolError
from fixtures.gen import repo as R
from fixtures.pairs import loader

FN_TAX = "schema/functions/dbo.fn_Tax.sql"
VW_ORDERS = "schema/views/sales.vw_Orders.sql"
VW_TOTALS = "schema/views/sales.vw_Totals.sql"
VW_PLAIN = "schema/views/sales.vw_Plain.sql"


def files_of(text: dict[str, str]) -> dict[str, bytes]:
    return {path: content.encode("utf-8") for path, content in text.items()}


def refusal(run) -> ToolError:
    with pytest.raises(ToolError) as caught:
        run()
    assert caught.value.exit_code is Exit.REFUSED
    return caught.value


def tree(repo: Path) -> dict[str, bytes]:
    return {
        p.relative_to(repo).as_posix(): p.read_bytes()
        for p in repo.rglob("*")
        if p.is_file() and ".git/" not in p.as_posix()
    }


# ------------------------------------------------------------------ load_model
def test_the_model_holds_every_table_class_file_and_no_module():
    model = gen.load_model(
        files_of(
            {
                "azsqlcd.toml": R.TOML,
                "schema/schemas/sales.sql": "CREATE SCHEMA [sales];\n",
                "schema/types/sales.Code.sql": "CREATE TYPE [sales].[Code] FROM varchar(10) NOT NULL;\n",
                "schema/sequences/sales.OrderNo.sql": (
                    "CREATE SEQUENCE [sales].[OrderNo] AS int START WITH 1 INCREMENT BY 1 MINVALUE 1 "
                    "MAXVALUE 100 NO CYCLE NO CACHE;\n"
                ),
                R.ORDER: R.order(),
                "schema/synonyms/sales.Orders.sql": "CREATE SYNONYM [sales].[Orders] FOR [sales].[Order];\n",
                VW_PLAIN: "CREATE OR ALTER VIEW [sales].[vw_Plain] AS SELECT 1 AS [x];\n",
                "schema/_tombstones.toml": "",
                "migrations/migrations.sum": R.EMPTY_SUM,
            }
        )
    )
    assert list(model) == [
        "SCHEMA:[sales]",
        "SEQUENCE:[sales].[OrderNo]",
        "SYNONYM:[sales].[Orders]",
        "TABLE:[sales].[Order]",
        "TYPE:[sales].[Code]",
    ]


def test_a_table_file_with_a_byte_order_mark_and_windows_line_ends_gives_the_same_model():
    plain = gen.load_model(files_of({R.ORDER: R.order("[Note] nvarchar(200) NULL")}))
    windows = {R.ORDER: b"\xef\xbb\xbf" + R.order("[Note] nvarchar(200) NULL").replace("\n", "\r\n").encode()}
    assert gen.load_model(windows) == plain


def test_every_file_that_does_not_parse_is_reported_in_one_refusal():
    # The author sees all files to correct in one run, not one file for each run.
    error = refusal(
        lambda: gen.load_model(
            {
                R.ORDER: R.order("[Note] nvarchar(200)").encode(),  # NF001: no NULL / NOT NULL
                "schema/tables/sales.Bad.sql": b"CREATE TABEL [sales].[Bad] ([a] int NOT NULL);\n",
                "schema/tables/sales.Bytes.sql": b"\xff\xfe",
                "schema/tables/sales.Good.sql": R.table("Good", "[a] int NOT NULL").encode(),
            }
        )
    )
    assert error.reason_code == "MODEL_INVALID"
    found = {(e["path"], e["code"], e["line"]) for e in error.detail["errors"]}
    assert found == {
        ("schema/tables/sales.Bad.sql", "SYNTAX", 1),
        ("schema/tables/sales.Bytes.sql", "FILE_INVALID", 1),
        (R.ORDER, "NF001", 3),
    }
    assert (error.detail["path"], error.detail["line"]) == ("schema/tables/sales.Bad.sql", 1)
    assert "3 table-class file(s)" in error.message


def test_two_files_that_define_one_object_are_refused_and_both_are_named():
    # file names that differ in letter case only: the catalog holds one object
    other = "schema/tables/sales.order.sql"
    files = {R.ORDER: R.order().encode(), other: R.order().replace("[Order]", "[order]").encode()}
    error = refusal(lambda: gen.load_model(files))
    assert error.reason_code == "MODEL_INVALID"
    (item,) = error.detail["errors"]
    assert (item["path"], item["code"]) == (other, "MDL001")
    assert R.ORDER in str(item["message"])


# ------------------------------------------------------------------ table_file_check (NF000, A14)
def test_a_file_in_normal_form_has_no_finding():
    assert gen.table_file_check(R.ORDER, R.order("[Note] nvarchar(200) NULL")) == []


def test_a_file_that_drops_a_token_the_parser_reads_fails_nf000():
    # ASC is read by the parser and is not in the model: the file says more than the model holds.
    text = R.order("[Note] nvarchar(200) NULL").replace("([OrderId])", "([OrderId] ASC)")
    (finding,) = gen.table_file_check(R.ORDER, text)
    assert (finding.code, finding.severity, finding.path, finding.line) == ("NF000", "error", R.ORDER, 4)
    assert "ASC" in finding.message


def test_a_file_that_does_not_parse_keeps_the_code_and_line_of_the_parser():
    (finding,) = gen.table_file_check(R.ORDER, R.order("[Note] nvarchar(200)"))
    assert (finding.code, finding.severity, finding.path, finding.line) == ("NF001", "error", R.ORDER, 3)
    assert "NULL" in finding.message


def test_a_value_with_no_canonical_spelling_is_a_finding_and_not_a_crash():
    # lint_repo must never raise for the content of a file
    text = R.order("[Note] varchar(20) COLLATE [Latin1 General] NULL")
    (finding,) = gen.table_file_check(R.ORDER, text)
    assert (finding.code, finding.line) == ("NF000", 1)
    assert "collation" in finding.message


def test_lint_reports_nf000_only_when_it_is_given_the_table_file_check():
    text = R.order().replace("([OrderId])", "([OrderId] ASC)")
    files = files_of(
        {"azsqlcd.toml": R.TOML, "migrations/migrations.sum": R.EMPTY_SUM, **R.SALES, R.ORDER: text}
    )
    assert lint.lint_repo(files) == []
    assert [(f.code, f.path) for f in lint.lint_repo(files, table_file_check=gen.table_file_check)] == [
        ("NF000", R.ORDER)
    ]


# ------------------------------------------------------------------ validate_model (rung 3)
CUSTOMER = "schema/tables/sales.Customer.sql"
CUSTOMER_TEXT = R.table(
    "Customer", "[CustomerId] int NOT NULL", "CONSTRAINT [PK_Customer] PRIMARY KEY CLUSTERED ([CustomerId])"
)
FK = "CONSTRAINT [FK_Order_Customer] FOREIGN KEY ([CustomerId]) REFERENCES [sales].[Customer] ([CustomerId])"


def findings_of_model(text: dict[str, str]) -> list[tuple[str, str, str]]:
    found = gen.validate_model(gen.load_model(files_of(text)))
    assert all(f.severity == "error" and f.line == 1 for f in found)
    return [(f.code, f.path, f.message) for f in found]


def test_a_model_whose_files_fit_together_has_no_finding():
    order = R.table("Order", R.ORDER_ID, "[CustomerId] int NOT NULL", R.PK_ORDER, FK)
    assert findings_of_model({**R.SALES, CUSTOMER: CUSTOMER_TEXT, R.ORDER: order}) == []


def test_a_foreign_key_to_a_table_with_no_file_is_a_finding_on_the_file_of_its_table():
    order = R.table("Order", R.ORDER_ID, "[CustomerId] int NOT NULL", R.PK_ORDER, FK)
    ((code, path, message),) = findings_of_model({**R.SALES, R.ORDER: order})
    assert (code, path) == ("MDL001", R.ORDER)
    assert "TABLE:[sales].[Order]" in message and "[sales].[Customer]" in message


def test_a_foreign_key_whose_column_type_differs_from_its_target_is_a_finding():
    order = R.table("Order", R.ORDER_ID, "[CustomerId] bigint NOT NULL", R.PK_ORDER, FK)
    ((code, path, message),) = findings_of_model({**R.SALES, CUSTOMER: CUSTOMER_TEXT, R.ORDER: order})
    assert (code, path) == ("MDL001", R.ORDER)
    assert "type" in message


def test_an_index_on_a_column_that_the_table_does_not_have_is_a_finding():
    order = R.order() + "GO\nCREATE NONCLUSTERED INDEX [IX_Order_Gone] ON [sales].[Order] ([Gone]);\n"
    ((code, path, message),) = findings_of_model({**R.SALES, R.ORDER: order})
    assert (code, path) == ("MDL001", R.ORDER)
    assert "[Gone]" in message


def test_a_table_in_a_schema_that_has_no_file_is_a_finding_and_its_echoes_are_not_repeated():
    # The index of the table fails too (the table was not created): one finding for the object.
    order = R.order() + "GO\nCREATE NONCLUSTERED INDEX [IX_Order_Id] ON [sales].[Order] ([OrderId]);\n"
    ((code, path, message),) = findings_of_model({R.ORDER: order})
    assert (code, path) == ("MDL001", R.ORDER)
    assert "schema" in message


def test_a_schema_file_for_a_schema_that_every_database_has_is_a_finding():
    # CREATE SCHEMA [dbo] fails on every database, and the catalog never gives dbo as an object
    ((code, path, message),) = findings_of_model({"schema/schemas/dbo.sql": "CREATE SCHEMA [dbo];\n"})
    assert (code, path) == ("MDL001", "schema/schemas/dbo.sql")
    assert "exists in every database" in message


def test_two_constraints_of_one_name_in_a_schema_are_a_finding():
    other = R.table("Other", "[OrderId] int NOT NULL", R.PK_ORDER)
    ((code, path, message),) = findings_of_model(
        {**R.SALES, R.ORDER: R.order(), "schema/tables/sales.Other.sql": other}
    )
    assert (code, path) == ("MDL001", "schema/tables/sales.Other.sql")
    assert "[PK_Order]" in message


def test_a_model_with_a_value_that_has_no_sql_spelling_is_a_finding_and_not_a_crash():
    order = R.order("[Note] varchar(20) COLLATE [Latin1 General] NULL")
    ((code, path, message),) = findings_of_model({**R.SALES, R.ORDER: order})
    assert (code, path) == ("MDL001", "schema")
    assert "collation" in message


@pytest.mark.parametrize("case", loader.CASES)
def test_the_object_files_of_every_fixture_pair_fit_together(case: str):
    for side in ("base", "head"):
        assert gen.validate_model(loader.model(case, side)) == []


# ------------------------------------------------------------------ the working tree
def test_the_working_tree_is_read_like_a_revision_of_git(tmp_path: Path):
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order(), "onboarding/dev/note.md": "x\n"})
    R.put(repo, {"README.md": "not part of a release\n", "docs/a.sql": "SELECT 1;\n"})
    R.commit(repo, "more")
    files = gen.read_working_tree(repo)
    assert files == release.read_tree(repo, "HEAD")
    assert list(files) == [
        "azsqlcd.toml",
        "migrations/migrations.sum",
        "onboarding/dev/note.md",
        "schema/schemas/sales.sql",
        R.ORDER,
    ]


def test_a_symbolic_link_in_the_working_tree_is_refused(tmp_path: Path):
    # git would store the link, not the file: the check would read other bytes than the release holds
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order()})
    (repo / "schema/tables/sales.Link.sql").symlink_to(repo / R.ORDER)
    error = refusal(lambda: gen.read_working_tree(repo))
    assert error.reason_code == "TREE_INVALID"
    assert error.detail["path"] == "schema/tables/sales.Link.sql"


@pytest.mark.parametrize("name", ["schema", "migrations", "onboarding", "azsqlcd.toml"])
def test_a_root_of_the_working_tree_that_is_a_symbolic_link_is_refused(tmp_path: Path, name: str):
    # os.walk follows a root that is a link; git stores the link and a release holds no file under it
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order(), "onboarding/dev/note.md": "x\n"})
    (repo / name).rename(repo / "elsewhere")
    (repo / name).symlink_to("elsewhere")
    error = refusal(lambda: gen.read_working_tree(repo))
    assert (error.reason_code, error.detail["path"]) == ("TREE_INVALID", name)
    # verify gives the refusal as its one finding and reads nothing through the link
    assert [(f.code, f.severity, f.path) for f in gen.verify(repo, R.git(repo, "rev-parse", "main"))] == [
        ("TREE_INVALID", "error", name)
    ]


# ------------------------------------------------------------------ generate: what is written
def test_gen_writes_nothing_when_only_modules_changed(tmp_path: Path):
    view = "CREATE OR ALTER VIEW [sales].[vw_Plain] AS SELECT [OrderId] FROM [sales].[Order];\n"
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order(), VW_PLAIN: view})
    R.put(repo, {VW_PLAIN: view.replace("[OrderId]", "[OrderId] AS [Id]")})
    before = tree(repo)
    result = gen.generate(repo, "main", "change")
    assert result == gen.GenResult(gen.ONLY_MODULES)
    assert gen.write_result(repo, result) == []
    assert tree(repo) == before


def test_gen_says_no_change_when_the_files_give_the_same_model(tmp_path: Path):
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order("[Note] nvarchar(200) NULL")})
    R.put(repo, {R.ORDER: R.order("[note] NVARCHAR(200) NULL")})  # letter case only
    assert gen.generate(repo, "main", "change") == gen.GenResult(gen.NO_CHANGE)


def test_a_renamed_column_without_rename_becomes_drop_and_add_with_a_drop_column_allow_line_marked_todo(
    tmp_path: Path,
):
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order("[Stat] tinyint NULL")})
    R.put(repo, {R.ORDER: R.order("[Status] tinyint NULL")})
    result = gen.generate(repo, "main", "rename_stat")
    assert result.text == (
        "-- azsqlcd:migration 0001__rename_stat\n"
        "-- azsqlcd:mode tx\n"
        "-- azsqlcd:allow DROP_COLUMN [sales].[Order].[Stat] reason: TODO\n"
        "ALTER TABLE [sales].[Order] DROP COLUMN [Stat];\n"
        "GO\n"
        "ALTER TABLE [sales].[Order] ADD [Status] tinyint NULL;\n"
        "GO\n"
    )


def test_with_rename_the_migration_holds_one_sp_rename_under_allow_rename(tmp_path: Path):
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order("[Stat] tinyint NULL")})
    R.put(repo, {R.ORDER: R.order("[Status] tinyint NULL")})
    result = gen.generate(repo, "main", "rename_stat", ["column:[sales].[Order].[Stat]=[Status]"])
    assert result.text == (
        "-- azsqlcd:migration 0001__rename_stat\n"
        "-- azsqlcd:mode tx\n"
        "-- azsqlcd:allow RENAME [sales].[Order].[Stat] reason: TODO\n"
        "EXEC sys.sp_rename N'[sales].[Order].[Stat]', N'Status', N'COLUMN';\n"
        "GO\n"
    )


def test_alter_column_gets_the_allow_line_that_lint_asks_for_and_the_one_that_only_the_model_knows(
    tmp_path: Path,
):
    # lint asks ALTER_COLUMN_LOSSY for every ALTER COLUMN (the lexer cannot see the old type). NULL
    # to NOT NULL is known to the model only. The second column does not change its nullability.
    base = R.order("[Note] nvarchar(200) NULL", "[Code] varchar(10) NOT NULL")
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: base})
    R.put(repo, {R.ORDER: R.order("[Note] nvarchar(200) NOT NULL", "[Code] varchar(20) NOT NULL")})
    text = gen.generate(repo, "main", "tighten").text
    assert text is not None
    assert text.splitlines()[2:] == [
        "-- azsqlcd:allow ALTER_COLUMN_LOSSY [sales].[Order].[Note] reason: TODO",
        "-- azsqlcd:allow SET_NOT_NULL [sales].[Order].[Note] reason: TODO",
        "ALTER TABLE [sales].[Order] ALTER COLUMN [Note] nvarchar(200) NOT NULL;",
        "GO",
        "-- azsqlcd:allow ALTER_COLUMN_LOSSY [sales].[Order].[Code] reason: TODO",
        "ALTER TABLE [sales].[Order] ALTER COLUMN [Code] varchar(20) NOT NULL;",
        "GO",
    ]


def pair_repo(tmp_path: Path, case: str) -> Path:
    """A repository whose main holds the base side of a fixture pair and whose tree holds the head."""
    repo = R.new_repo(tmp_path / "r", dict(loader.object_files(case, "base")))
    R.put(repo, {path: None for path, _ in loader.object_files(case, "base")})
    R.put(repo, dict(loader.object_files(case, "head")))
    return repo


def test_the_drop_of_a_temporal_table_switches_versioning_off_under_allow_temporal_off_first(
    tmp_path: Path,
):
    # the engine refuses DROP TABLE while versioning is on; OFF ends the history, so it is declared
    text = gen.generate(pair_repo(tmp_path, "temporal_table_drop"), "main", "drop_staff").text
    assert text is not None
    assert text.splitlines()[2:] == [
        "-- azsqlcd:allow TEMPORAL_OFF [dbo].[Staff] reason: TODO",
        "ALTER TABLE [dbo].[Staff] SET (SYSTEM_VERSIONING = OFF);",
        "GO",
        "-- azsqlcd:allow DROP_TABLE [dbo].[Staff] reason: TODO",
        "DROP TABLE [dbo].[Staff];",
        "GO",
    ]


def test_a_new_temporal_table_and_a_change_of_one_need_no_temporal_allow_line(tmp_path: Path):
    for case in ("temporal_table_new", "temporal_table_add_column_check_and_index"):
        text = gen.generate(pair_repo(tmp_path / case, case), "main", "change").text
        assert text is not None and "SYSTEM_VERSIONING = OFF" not in text
        assert "TEMPORAL_OFF" not in text


def test_lint_reads_from_the_statement_every_allow_code_that_the_model_gives_for_it():
    """gen writes the allow lines that lint.py reads from the emitted statement, and verify asks
    lint.py. So a code of diff.classify that lint.py does not read (TEMPORAL_OFF, UNMASK, DROP_...)
    would be a destructive statement that nobody declares."""
    seen: set[str] = set()
    for case in (case for case in loader.CASES if not case.startswith("refuse_")):
        state = loader.model(case, "base")
        for op in diff.diff(state, loader.model(case, "head"), loader.renames(case)):
            statement = emit.emit_operation(op)
            batch = chain.MigrationBatch("model", statement, 1, ())
            read = {item.code for item in lint.classify_batch(batch, "tx").needs_allow}
            wanted = set(diff.classify(op, state)) - {"SET_NOT_NULL", "LONG_LOCK"}
            assert wanted <= read, f"{case}: {statement}"
            assert wanted <= lint.ALLOW_CODES
            seen |= wanted
            state = replay.apply(state, op)
    # the pairs do reach the codes that a statement alone decides
    assert {"DROP_TABLE", "DROP_COLUMN", "RENAME", "TEMPORAL_OFF", "ALTER_COLUMN_LOSSY"} <= seen


def test_gen_stops_when_the_model_gives_an_allow_code_that_lint_does_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # the guard behind the test above: a new destructive operation whose statement lint.py cannot
    # classify must not leave gen as a migration without its allow line
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order()})
    R.put(repo, {R.ORDER: R.order("[Note] nvarchar(200) NULL")})
    monkeypatch.setattr(diff, "classify", lambda op, base, **_: ["UNMASK"])
    error = refusal(lambda: gen.generate(repo, "main", "note"))
    assert error.reason_code == "GEN_INVALID"
    assert "UNMASK" in error.message and "ADD [Note]" in error.message


def test_an_index_on_a_table_of_the_base_revision_gets_long_lock_and_one_on_a_new_table_does_not(
    tmp_path: Path,
):
    index = "GO\nCREATE NONCLUSTERED INDEX [IX_{0}_Id] ON [sales].[{0}] ([OrderId]);\n"
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order()})
    new_table = R.table("Fresh", R.ORDER_ID) + index.format("Fresh")
    R.put(repo, {R.ORDER: R.order() + index.format("Order"), "schema/tables/sales.Fresh.sql": new_table})
    text = gen.generate(repo, "main", "indexes").text
    assert text is not None
    assert (
        "-- azsqlcd:allow LONG_LOCK [sales].[Order] reason: TODO\nCREATE NONCLUSTERED INDEX [IX_Order_Id]"
        in text
    )
    assert "GO\nCREATE NONCLUSTERED INDEX [IX_Fresh_Id]" in text
    assert text.count("azsqlcd:allow") == 1


# ------------------------------------------------------------------ generate: deploy-module, ORD002
def function(body: str) -> str:
    head = "CREATE OR ALTER FUNCTION [dbo].[fn_Tax] (@amount int)\nRETURNS int\nAS\nBEGIN\n"
    return f"{head}    RETURN {body};\nEND;\n"


CHECK_TAX = "CONSTRAINT [CK_Order_Tax] CHECK ([dbo].[fn_Tax]([Amount]) >= 0)"


def test_a_new_check_that_calls_a_function_file_gets_deploy_module_above_it(tmp_path: Path):
    base = {**R.SALES, R.ORDER: R.order("[Amount] int NOT NULL"), FN_TAX: function("@amount / 5")}
    repo = R.new_repo(tmp_path / "r", base)
    R.put(repo, {R.ORDER: R.table("Order", R.ORDER_ID, "[Amount] int NOT NULL", R.PK_ORDER, CHECK_TAX)})
    text = gen.generate(repo, "main", "tax_check").text
    assert text is not None
    assert text.splitlines()[2:] == [
        "-- azsqlcd:deploy-module [dbo].[fn_Tax]",
        "-- azsqlcd:allow LONG_LOCK [sales].[Order] reason: TODO",
        "ALTER TABLE [sales].[Order] ADD CONSTRAINT [CK_Order_Tax] CHECK ([dbo].[fn_Tax]([Amount]) >= 0);",
        "GO",
    ]


def test_a_new_default_that_calls_a_function_file_gets_deploy_module_above_it(tmp_path: Path):
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order(), FN_TAX: function("1")})
    with_default = "[Rate] int NOT NULL CONSTRAINT [DF_Order_Rate] DEFAULT ([dbo].[fn_Tax]((100)))"
    R.put(repo, {R.ORDER: R.order(with_default)})
    lines = (gen.generate(repo, "main", "rate").text or "").splitlines()
    assert lines[2:4] == [
        "-- azsqlcd:deploy-module [dbo].[fn_Tax]",
        "ALTER TABLE [sales].[Order] ADD [Rate] int NOT NULL CONSTRAINT [DF_Order_Rate] DEFAULT "
        "([dbo].[fn_Tax]((100)));",
    ]


def test_a_function_is_deployed_once_above_the_first_statement_that_names_it(tmp_path: Path):
    # computed column of a new table, DEFAULT of a new column, then a CHECK: three statements
    repo = R.new_repo(
        tmp_path / "r", {**R.SALES, R.ORDER: R.order("[Amount] int NOT NULL"), FN_TAX: function("1")}
    )
    line = R.table("Line", "[Amount] int NOT NULL", "[Tax] AS ([dbo].[fn_Tax]([Amount]))")
    with_default = "[Rate] int NOT NULL CONSTRAINT [DF_Order_Rate] DEFAULT ([dbo].[fn_Tax]((100)))"
    order = R.table("Order", R.ORDER_ID, "[Amount] int NOT NULL", with_default, R.PK_ORDER, CHECK_TAX)
    R.put(repo, {R.ORDER: order, "schema/tables/sales.Line.sql": line})
    lines = (gen.generate(repo, "main", "tax").text or "").splitlines()
    assert lines.count("-- azsqlcd:deploy-module [dbo].[fn_Tax]") == 1
    assert lines[2:4] == ["-- azsqlcd:deploy-module [dbo].[fn_Tax]", "CREATE TABLE [sales].[Line] ("]


def test_an_expression_that_names_no_function_file_gets_no_deploy_module(tmp_path: Path):
    repo = R.new_repo(
        tmp_path / "r", {**R.SALES, R.ORDER: R.order("[Amount] int NOT NULL"), FN_TAX: function("1")}
    )
    check = "CONSTRAINT [CK_Order_Amount] CHECK ([Amount] >= 0 AND 'dbo.fn_Tax' <> '')"
    R.put(repo, {R.ORDER: R.table("Order", R.ORDER_ID, "[Amount] int NOT NULL", R.PK_ORDER, check)})
    assert "deploy-module" not in (gen.generate(repo, "main", "amount").text or "")


def test_a_function_that_needs_an_object_created_later_in_the_same_migration_is_refused_ord002(
    tmp_path: Path,
):
    # deploy-module would create the function before CREATE TABLE [sales].[Rate] ran
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order("[Amount] int NOT NULL")})
    R.put(
        repo,
        {
            FN_TAX: function("(SELECT MAX([Percent]) FROM [sales].[Rate])"),
            "schema/tables/sales.Rate.sql": R.table("Rate", "[Percent] int NOT NULL"),
            "schema/tables/sales.Line.sql": R.table(
                "Line", "[Amount] int NOT NULL", "[Tax] AS ([dbo].[fn_Tax]([Amount]))"
            ),
        },
    )
    error = refusal(lambda: gen.generate(repo, "main", "tax"))
    assert error.reason_code == "GEN_REFUSED"
    (item,) = error.detail["refusals"]
    assert (item["code"], item["object"]) == ("ORD002", "FUNCTION:[dbo].[fn_Tax]")
    assert "TABLE:[sales].[Rate]" in item["message"] and "Split" in item["hint"]


def test_a_function_that_needs_the_table_whose_create_statement_names_it_is_refused_ord002(tmp_path: Path):
    # the function would be deployed above CREATE TABLE [sales].[Line], and it reads that table
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order()})
    line = R.table("Line", "[Amount] int NOT NULL", "[Tax] AS ([dbo].[fn_Tax]([Amount]))")
    R.put(
        repo,
        {FN_TAX: function("(SELECT COUNT(*) FROM [sales].[Line])"), "schema/tables/sales.Line.sql": line},
    )
    error = refusal(lambda: gen.generate(repo, "main", "tax"))
    assert [(item["code"], item["object"]) for item in error.detail["refusals"]] == [
        ("ORD002", "FUNCTION:[dbo].[fn_Tax]")
    ]


def test_a_function_that_needs_an_object_created_earlier_in_the_same_migration_is_not_refused(tmp_path: Path):
    # CREATE TABLE comes before ADD CONSTRAINT in the fixed order: [sales].[Rate] exists in time
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order("[Amount] int NOT NULL")})
    R.put(
        repo,
        {
            FN_TAX: function("(SELECT MAX([Percent]) FROM [sales].[Rate])"),
            "schema/tables/sales.Rate.sql": R.table("Rate", "[Percent] int NOT NULL"),
            R.ORDER: R.table("Order", R.ORDER_ID, "[Amount] int NOT NULL", R.PK_ORDER, CHECK_TAX),
        },
    )
    text = gen.generate(repo, "main", "tax").text or ""
    assert text.index("CREATE TABLE [sales].[Rate]") < text.index("-- azsqlcd:deploy-module [dbo].[fn_Tax]")


# ------------------------------------------------------------------ generate: unbind
def bound_views() -> R.Files:
    return {
        VW_ORDERS: (
            "CREATE OR ALTER VIEW [sales].[vw_Orders] WITH SCHEMABINDING AS\n"
            "SELECT [OrderId], [Note] FROM [sales].[Order];\n"
        ),
        VW_TOTALS: (
            "CREATE OR ALTER VIEW [sales].[vw_Totals] WITH SCHEMABINDING AS\n"
            "SELECT COUNT_BIG(*) AS [n] FROM [sales].[vw_Orders];\n"
        ),
        # not schema-bound: the engine does not block the table for it
        VW_PLAIN: "CREATE OR ALTER VIEW [sales].[vw_Plain] AS SELECT [OrderId] FROM [sales].[Order];\n",
        # schema-bound, on another table
        "schema/views/sales.vw_Other.sql": (
            "CREATE OR ALTER VIEW [sales].[vw_Other] WITH SCHEMABINDING AS SELECT [a] FROM [sales].[Other];\n"
        ),
        "schema/tables/sales.Other.sql": R.table("Other", "[a] int NOT NULL"),
    }


def test_a_schema_bound_view_on_an_altered_table_gets_an_unbind_line_dependants_first(tmp_path: Path):
    # vw_Totals names only vw_Orders, and it blocks the ALTER of vw_Orders: it is unbound first
    # (the order of the names would put it last).
    base = {**R.SALES, R.ORDER: R.order("[Note] nvarchar(200) NULL"), **bound_views()}
    repo = R.new_repo(tmp_path / "r", base)
    R.put(repo, {R.ORDER: R.order("[Note] nvarchar(400) NULL")})
    text = gen.generate(repo, "main", "longer_note").text
    assert text is not None
    assert text.splitlines() == [
        "-- azsqlcd:migration 0001__longer_note",
        "-- azsqlcd:mode tx",
        "-- azsqlcd:unbind [sales].[vw_Totals]",
        "-- azsqlcd:unbind [sales].[vw_Orders]",
        "-- azsqlcd:allow ALTER_COLUMN_LOSSY [sales].[Order].[Note] reason: TODO",
        "ALTER TABLE [sales].[Order] ALTER COLUMN [Note] nvarchar(400) NULL;",
        "GO",
    ]
    unbinds = [d for d in chain.parse_migration(text, "0001__longer_note.sql").batches[0].directives]
    assert [type(d).__name__ for d in unbinds] == ["Unbind", "Unbind", "Allow"]


@pytest.mark.parametrize(
    ("head", "renames"),
    [
        (R.order(), []),  # DROP COLUMN
        (R.order("[Remark] nvarchar(200) NULL"), ["column:[sales].[Order].[Note]=[Remark]"]),
        (None, []),  # DROP TABLE
    ],
    ids=["drop column", "rename column", "drop table"],
)
def test_drop_column_rename_and_drop_table_get_the_unbind_lines_too(tmp_path: Path, head, renames):
    base = {**R.SALES, R.ORDER: R.order("[Note] nvarchar(200) NULL"), **bound_views()}
    repo = R.new_repo(tmp_path / "r", base)
    R.put(repo, {R.ORDER: head})
    lines = (gen.generate(repo, "main", "change", renames).text or "").splitlines()
    assert lines[2:4] == [
        "-- azsqlcd:unbind [sales].[vw_Totals]",
        "-- azsqlcd:unbind [sales].[vw_Orders]",
    ]
    assert sum("azsqlcd:unbind" in line for line in lines) == 2


def test_a_renamed_table_is_looked_up_in_the_modules_under_its_name_at_the_base_revision(tmp_path: Path):
    # DROP COLUMN names [sales].[Sale], which no base module knows. The rename itself is blocked by the
    # modules that name [sales].[Order], and it is the first statement: the lines come from it.
    base = {**R.SALES, R.ORDER: R.order("[Note] nvarchar(200) NULL"), **bound_views()}
    repo = R.new_repo(tmp_path / "r", base)
    sale = R.order().replace("[sales].[Order]", "[sales].[Sale]")
    R.put(repo, {R.ORDER: None, "schema/tables/sales.Sale.sql": sale})
    lines = (gen.generate(repo, "main", "sale", ["table:[sales].[Order]=[Sale]"]).text or "").splitlines()
    assert lines[2:4] == [
        "-- azsqlcd:unbind [sales].[vw_Totals]",
        "-- azsqlcd:unbind [sales].[vw_Orders]",
    ]
    assert "ALTER TABLE [sales].[Sale] DROP COLUMN [Note];" in lines


def test_a_statement_that_a_schema_bound_module_does_not_block_gets_no_unbind_line(tmp_path: Path):
    base = {**R.SALES, R.ORDER: R.order("[Note] nvarchar(200) NULL"), **bound_views()}
    repo = R.new_repo(tmp_path / "r", base)
    R.put(repo, {R.ORDER: R.order("[Note] nvarchar(200) NULL", "[More] int NULL")})
    assert "azsqlcd:unbind" not in (gen.generate(repo, "main", "more").text or "")


# ------------------------------------------------------------------ generate: number, chain, purity
def test_the_new_migration_gets_the_next_number_and_its_chain_line_holds_the_sha256_of_its_text(
    tmp_path: Path,
):
    earlier = {
        "0001__first.sql": R.migration("0001__first.sql", "CREATE SCHEMA [sales];\nGO\n"),
        "0007__seventh.sql": R.migration("0007__seventh.sql", f"{R.order().rstrip()}\nGO\n"),
    }
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order(), **R.with_migrations(earlier)})
    R.put(repo, {R.ORDER: R.order("[Note] nvarchar(200) NULL")})
    result = gen.generate(repo, "main", "add_note")
    assert result.file == "0008__add_note.sql" and result.reason == ""
    # a file that has no chain line yet keeps its number too: no second file with one number
    R.put(repo, {"migrations/0011__draft.sql": "-- not in the chain\n"})
    assert gen.generate(repo, "main", "add_note").file == "0012__add_note.sql"
    (repo / "migrations/0011__draft.sql").unlink()
    assert result.text is not None and result.sum_text is not None
    assert result.sum_text.startswith(R.sum_text(earlier))  # merged lines stay byte-identical
    assert chain.parse_sum(result.sum_text).entries[-1] == chain.ChainEntry(
        "0008__add_note.sql", chain.file_sha256(result.text.encode()), "tx"
    )


def test_generate_writes_nothing_and_write_result_writes_the_migration_and_the_chain(tmp_path: Path):
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order()})
    R.put(repo, {R.ORDER: R.order("[Note] nvarchar(200) NULL")})
    before = tree(repo)
    result = gen.generate(repo, "main", "add_note")
    assert tree(repo) == before
    assert gen.write_result(repo, result) == ["migrations/0001__add_note.sql", "migrations/migrations.sum"]
    assert result.text is not None and result.sum_text is not None
    assert tree(repo) == {
        **before,
        "migrations/0001__add_note.sql": result.text.encode(),
        "migrations/migrations.sum": result.sum_text.encode(),
    }
    assert b"\r" not in result.text.encode()
    # a migration file is never replaced: the author may have edited it
    assert refusal(lambda: gen.write_result(repo, result)).reason_code == "GEN_INVALID"


def test_gen_compares_with_the_merge_base_and_not_with_the_tip_of_main(tmp_path: Path):
    # main moves on after the branch starts. The branch must not undo that change.
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order()})
    R.git(repo, "checkout", "-q", "main")
    R.commit(repo, "main moves", {"schema/tables/sales.Other.sql": R.table("Other", "[a] int NOT NULL")})
    R.git(repo, "checkout", "-q", "work")
    R.put(repo, {R.ORDER: R.order("[Note] nvarchar(200) NULL")})
    text = gen.generate(repo, "main", "add_note").text or ""
    assert "ADD [Note]" in text and "[Other]" not in text


def test_a_second_gen_on_one_branch_writes_only_what_the_first_migration_does_not_do(tmp_path: Path):
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order()})
    R.put(repo, {R.ORDER: R.order("[Note] nvarchar(200) NULL")})
    gen.write_result(repo, gen.generate(repo, "main", "add_note"))
    R.put(repo, {R.ORDER: R.order("[Note] nvarchar(200) NULL", "[More] int NULL")})
    second = gen.generate(repo, "main", "add_more")
    assert second.file == "0002__add_more.sql"
    assert (second.text or "").splitlines()[2:] == ["ALTER TABLE [sales].[Order] ADD [More] int NULL;", "GO"]
    gen.write_result(repo, second)
    assert gen.generate(repo, "main", "nothing") == gen.GenResult(gen.NO_CHANGE)


def test_gen_refuses_when_a_new_migration_of_the_branch_does_not_replay(tmp_path: Path):
    # The next migration would start from a model that nobody can know.
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order()})
    broken = {"0001__x.sql": R.migration("0001__x.sql", "ALTER TABLE [sales].[Gone] ADD [a] int NULL;\nGO\n")}
    R.put(repo, {**R.with_migrations(broken), R.ORDER: R.order("[Note] nvarchar(200) NULL")})
    error = refusal(lambda: gen.generate(repo, "main", "add_note"))
    assert error.reason_code == "GEN_INVALID"
    assert (error.detail["path"], error.detail["line"]) == ("migrations/0001__x.sql", 3)


# ------------------------------------------------------------------ generate: refusals
def test_a_change_that_the_generator_does_not_write_is_refused_with_code_object_and_hint(tmp_path: Path):
    base = R.order("[A] int NULL", "[B] int NULL")
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: base})
    R.put(repo, {R.ORDER: R.order("[B] int NULL", "[A] int NULL")})
    error = refusal(lambda: gen.generate(repo, "main", "reorder"))
    assert error.reason_code == "GEN_REFUSED"
    (item,) = error.detail["refusals"]
    assert (item["code"], item["object"]) == ("ORD001", "TABLE:[sales].[Order]")
    assert item["message"] and "table rebuild" in item["hint"]
    assert "ORD001 TABLE:[sales].[Order]" in error.message


def test_a_temporal_change_that_the_generator_does_not_write_is_refused_with_the_hint_of_its_reason(
    tmp_path: Path,
):
    # gen passes the hint of diff on unchanged. What this version cannot do at all:
    repo = pair_repo(tmp_path / "off", "refuse_temporal_versioning_off_for_a_table_that_stays")
    error = refusal(lambda: gen.generate(repo, "main", "change"))
    assert error.reason_code == "GEN_REFUSED"
    assert {item["code"] for item in error.detail["refusals"]} == {"TEMPORAL_CHANGE"}
    assert error.detail["refusals"][0]["hint"] == diff.TEMPORAL_HINTS["not supported"]
    assert "TEMPORAL_CHANGE TABLE:" in error.message
    # and what a migration written by hand can do, in the order of its statements:
    repo = pair_repo(tmp_path / "history", "refuse_temporal_history_table_change")
    hint = refusal(lambda: gen.generate(repo, "main", "change")).detail["refusals"][0]["hint"]
    assert hint == diff.TEMPORAL_HINTS["history table"]
    assert hint.index("SYSTEM_VERSIONING = OFF") < hint.index("SYSTEM_VERSIONING = ON")


@pytest.mark.parametrize("case", [case for case in loader.CASES if case.startswith("refuse_")])
def test_gen_refuses_every_refused_fixture_pair_with_the_codes_of_the_fixture(tmp_path: Path, case: str):
    repo = R.new_repo(tmp_path / "r", dict(loader.object_files(case, "base")))
    R.put(repo, {path: None for path, _ in loader.object_files(case, "base")})
    R.put(repo, dict(loader.object_files(case, "head")))
    renames_file = loader.ROOT / case / "renames.txt"
    renames = renames_file.read_text(encoding="utf-8").split("\n") if renames_file.exists() else []
    error = refusal(lambda: gen.generate(repo, "main", "change", [r for r in renames if r]))
    assert error.reason_code == "GEN_REFUSED"
    expected = (loader.ROOT / case / "expected_refusals.txt").read_text(encoding="utf-8").splitlines()
    assert [f"{item['code']} {item['object']}" for item in error.detail["refusals"]] == expected


def test_a_rename_value_that_cannot_be_read_is_refused_before_git_is_read(tmp_path: Path):
    error = refusal(lambda: gen.generate(tmp_path / "no repository", "main", "x", ["column:[a].[b]=[c]"]))
    assert error.reason_code == "RENAME_INVALID"
    assert "KIND:[schema].[table].[old]=[new]" in error.message


@pytest.mark.parametrize("name", ["", "add status", "add-status", "0001__x.sql", "x" * 181])
def test_a_name_that_cannot_be_part_of_a_migration_file_name_is_refused(tmp_path: Path, name: str):
    assert refusal(lambda: gen.generate(tmp_path, "main", name)).reason_code == "GEN_INVALID"


def test_a_base_revision_that_git_does_not_know_is_refused_with_the_reason_of_git(tmp_path: Path):
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order()})
    assert refusal(lambda: gen.generate(repo, "origin/main", "x")).reason_code == "GIT_FAILED"


def test_head_files_that_do_not_parse_stop_gen_with_every_error(tmp_path: Path):
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order()})
    R.put(repo, {R.ORDER: R.order("[Note] nvarchar(200)")})
    error = refusal(lambda: gen.generate(repo, "main", "x"))
    assert error.reason_code == "MODEL_INVALID"
    assert [e["code"] for e in error.detail["errors"]] == ["NF001"]


# ------------------------------------------------------------------ resum
ADD_NOTE = "ALTER TABLE [sales].[Order] ADD [Note] nvarchar(200) NULL;\nGO\n"
ADD_A = "ALTER TABLE [sales].[Other] ADD [b] int NULL;\nGO\n"
OTHER = "schema/tables/sales.Other.sql"


def collided(tmp_path: Path) -> tuple[Path, str, str]:
    """A branch and main that both added migration 0001. Returns (repo with main merged into the
    branch and migrations.sum in conflict, the text of main's migrations.sum, the branch's line)."""
    base = {**R.SALES, R.ORDER: R.order(), OTHER: R.table("Other", "[a] int NOT NULL")}
    repo = R.new_repo(tmp_path / "r", base)
    mine = {"0001__mine.sql": R.migration("0001__mine.sql", ADD_NOTE)}
    R.commit(repo, "mine", {**R.with_migrations(mine), R.ORDER: R.order("[Note] nvarchar(200) NULL")})
    R.git(repo, "checkout", "-q", "main")
    theirs = {"0001__theirs.sql": R.migration("0001__theirs.sql", ADD_A)}
    R.commit(
        repo,
        "theirs",
        {**R.with_migrations(theirs), OTHER: R.table("Other", "[a] int NOT NULL", "[b] int NULL")},
    )
    R.git(repo, "checkout", "-q", "work")
    with pytest.raises(Exception, match="returned non-zero exit status 1"):
        R.git(repo, "merge", "-q", "main")  # both sides added line 2 of migrations.sum
    return repo, R.sum_text(theirs), R.sum_text(mine).splitlines()[1] + "\n"


def test_resum_renumbers_a_migration_that_collides_after_a_merge_of_main(tmp_path: Path):
    repo, main_sum, my_line = collided(tmp_path)
    # The author keeps the lines of both sides (here in the wrong order) and concludes the merge.
    R.commit(
        repo,
        "merge main",
        {"migrations/migrations.sum": R.EMPTY_SUM + my_line + main_sum.splitlines()[1] + "\n"},
    )
    before = tree(repo)
    result = gen.resum(repo, "main")
    assert tree(repo) == before  # resum() writes nothing
    (renumbered,) = result.renames
    assert (renumbered.old, renumbered.new) == ("0001__mine.sql", "0002__mine.sql")
    assert renumbered.text == R.migration("0002__mine.sql", ADD_NOTE)
    # main's line is byte-identical and first; the branch's line follows with its new sha256
    assert result.sum_text == main_sum + (
        f"0002__mine.sql sha256:{chain.file_sha256(renumbered.text.encode())} tx\n"
    )

    assert gen.write_resum(repo, result) == ["migrations/0002__mine.sql", "migrations/migrations.sum"]
    after = tree(repo)
    assert "migrations/0001__mine.sql" not in after
    assert after["migrations/0002__mine.sql"] == renumbered.text.encode()
    assert after["migrations/0001__theirs.sql"] == before["migrations/0001__theirs.sql"]
    assert [f for f in gen.verify(repo, "refs/heads/main") if f.severity == "error"] == []
    assert gen.resum(repo, "main") == gen.ResumResult((), result.sum_text)  # a second run changes nothing


def test_resum_renumbers_a_migration_file_that_starts_with_a_bom(tmp_path: Path):
    # PP-7: chain.parse_migration reads a file with a byte order mark, so the file is valid until
    # main takes its number. The header line then starts with U+FEFF, not with the directive.
    repo, main_sum, _ = collided(tmp_path)
    bom = "\ufeff" + R.migration("0001__mine.sql", ADD_NOTE).replace("\n", "\r\n")
    R.commit(repo, "merge main", {"migrations/migrations.sum": main_sum, "migrations/0001__mine.sql": bom})
    result = gen.resum(repo, "main")
    (renumbered,) = result.renames
    assert (renumbered.old, renumbered.new) == ("0001__mine.sql", "0002__mine.sql")
    assert renumbered.text == bom.replace("0001__mine", "0002__mine")
    assert result.sum_text == main_sum + (
        f"0002__mine.sql sha256:{chain.file_sha256(renumbered.text.encode())} tx\n"
    )


def test_resum_works_while_the_merge_is_not_concluded_and_the_branch_line_was_lost(tmp_path: Path):
    # The easy way out of the conflict: take migrations.sum of main. The branch's file has no line then.
    repo, main_sum, _ = collided(tmp_path)
    R.put(repo, {"migrations/migrations.sum": main_sum})
    result = gen.resum(repo, "main")
    assert [(r.old, r.new) for r in result.renames] == [("0001__mine.sql", "0002__mine.sql")]
    assert result.sum_text.startswith(main_sum) and "0002__mine.sql sha256:" in result.sum_text


def test_resum_refuses_a_chain_file_that_still_holds_conflict_markers(tmp_path: Path):
    repo, _, _ = collided(tmp_path)
    error = refusal(lambda: gen.resum(repo, "main"))
    assert error.reason_code == "CHAIN_INVALID"
    assert "Resolve the merge first" in error.message


def test_resum_gives_a_hand_edited_migration_its_sha256_again_and_renames_nothing(tmp_path: Path):
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order()})
    R.put(repo, {R.ORDER: R.order("[Note] nvarchar(200) NULL")})
    generated = gen.generate(repo, "main", "add_note")
    gen.write_result(repo, generated)
    edited = (
        generated.text or ""
    ) + "-- azsqlcd:data\nUPDATE [sales].[Order] SET [Note] = N'' WHERE [Note] IS NULL;\nGO\n"
    R.put(repo, {"migrations/0001__add_note.sql": edited})
    result = gen.resum(repo, "main")
    assert result.renames == ()
    assert result.sum_text == R.sum_text({"0001__add_note.sql": edited})
    assert result.sum_text != generated.sum_text


def test_resum_keeps_a_withdrawal_and_its_replacement(tmp_path: Path):
    merged = {"0001__a.sql": R.migration("0001__a.sql", ADD_NOTE)}
    base = {**R.SALES, R.ORDER: R.order("[Note] nvarchar(200) NULL"), **R.with_migrations(merged)}
    repo = R.new_repo(tmp_path / "r", base)
    both = {**merged, "0005__a2.sql": R.migration("0005__a2.sql", ADD_NOTE)}
    extra = {"0001__a.sql": "withdrawn", "0005__a2.sql": "replaces=0001__a.sql"}
    R.put(repo, R.with_migrations(both, extra))
    result = gen.resum(repo, "main")
    assert [(r.old, r.new) for r in result.renames] == [("0005__a2.sql", "0002__a2.sql")]
    assert [(e.file, e.withdrawn, e.replaces) for e in chain.parse_sum(result.sum_text).entries] == [
        ("0001__a.sql", True, None),
        ("0002__a2.sql", False, "0001__a.sql"),
    ]


def test_resum_refuses_when_the_branch_does_not_hold_the_migrations_of_the_base(tmp_path: Path):
    # main was not merged: the new chain would list files that the branch does not have
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order()})
    R.git(repo, "checkout", "-q", "main")
    R.commit(
        repo, "theirs", R.with_migrations({"0001__theirs.sql": R.migration("0001__theirs.sql", ADD_NOTE)})
    )
    R.git(repo, "checkout", "-q", "work")
    error = refusal(lambda: gen.resum(repo, "main"))
    assert error.reason_code == "GEN_INVALID"
    assert "merge main" in error.message


# ------------------------------------------------------------------ generate: the loop with lint (LFP-03)
def test_gen_stops_when_lint_refuses_a_statement_that_gen_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # A statement that the parser and the emitter accept and that lint refuses must fail in gen:
    # written to a file, it fails lint, verify and build in the pull request, with a message about
    # the migration text that the author did not write.
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order()})
    R.put(repo, {R.ORDER: R.order("[Note] nvarchar(200) NULL")})
    real = lint.classify_batch

    def refusing(batch: chain.MigrationBatch, mode: str, created: tuple[str, ...] = (), *, path: str = ""):
        facts = real(batch, mode, created, path=path)
        error = lint.Finding("MODEL_STATEMENT", "error", path, 1, "& has no place in the statement before it")
        return dataclasses.replace(facts, findings=(*facts.findings, error))

    monkeypatch.setattr(lint, "classify_batch", refusing)
    error = refusal(lambda: gen.generate(repo, "main", "note"))
    assert error.reason_code == "GEN_INVALID"
    # the statement class and the finding code of lint, then what lint said and the statement
    assert "ALTER TABLE" in error.message and "MODEL_STATEMENT" in error.message
    assert "& has no place" in error.message and "ADD [Note] nvarchar(200) NULL;" in error.message
    assert (error.detail["statement_class"], error.detail["code"]) == ("ALTER TABLE", "MODEL_STATEMENT")
    assert not (repo / "migrations/0001__note.sql").exists()


def test_a_warning_of_lint_for_a_generated_statement_does_not_stop_gen(tmp_path: Path):
    # DROP INDEX is a warning of lint (DROP_INDEX): the author reads it in the pull request
    index = "GO\nCREATE NONCLUSTERED INDEX [IX_Order_Id] ON [sales].[Order] ([OrderId]);\n"
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order() + index})
    R.put(repo, {R.ORDER: R.order()})
    statement = "DROP INDEX [IX_Order_Id] ON [sales].[Order];"
    facts = lint.classify_batch(chain.MigrationBatch("model", statement, 1, ()), "tx")
    assert [f.severity for f in facts.findings] == ["warning"]
    assert statement in (gen.generate(repo, "main", "drop_index").text or "")


# ------------------------------------------------------------------ generate: an undeclared rename (CU-03)
def test_drop_and_add_of_one_column_with_one_type_comes_with_the_exact_rename_argument(tmp_path: Path):
    # gen infers no rename. DROP COLUMN + ADD loses the rows of the column, so the author must see
    # the other way before the reason of the allow line is written.
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order("[Stat] tinyint NULL")})
    R.put(repo, {R.ORDER: R.order("[Status] tinyint NULL")})
    result = gen.generate(repo, "main", "rename_stat")
    assert "DROP COLUMN [Stat]" in (result.text or "")
    (hint,) = result.hints
    assert '--rename "column:[sales].[Order].[Stat]=[Status]"' in hint
    assert "migrations/0001__rename_stat.sql" in hint and "gen --resum" in hint
    # the argument that the hint gives is the one that gen reads, and then there is nothing to say
    renamed = gen.generate(repo, "main", "rename_stat", ["column:[sales].[Order].[Stat]=[Status]"])
    assert "sp_rename" in (renamed.text or "") and renamed.hints == ()


@pytest.mark.parametrize(
    "head",
    [
        R.order("[Status] smallint NULL"),  # another type: not the same column under a new name
        R.order("[Status] tinyint NULL", "[More] tinyint NULL"),  # two new columns: which one?
        R.order(),  # a drop only
    ],
    ids=["another type", "two new columns", "drop only"],
)
def test_a_drop_and_add_that_cannot_be_one_rename_has_no_rename_hint(tmp_path: Path, head: str):
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order("[Stat] tinyint NULL")})
    R.put(repo, {R.ORDER: head})
    assert gen.generate(repo, "main", "change").hints == ()


def test_a_renamed_column_that_is_not_the_last_column_is_refused_with_the_rename_argument(tmp_path: Path):
    # ORD001 says "new column is not the last column": true, and not what the author did
    base = R.order("[Stat] tinyint NULL", "[Note] nvarchar(200) NULL")
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: base})
    R.put(repo, {R.ORDER: R.order("[Status] tinyint NULL", "[Note] nvarchar(200) NULL")})
    error = refusal(lambda: gen.generate(repo, "main", "rename_stat"))
    (item,) = error.detail["refusals"]
    assert (item["code"], item["object"]) == ("ORD001", "TABLE:[sales].[Order]")
    assert '--rename "column:[sales].[Order].[Stat]=[Status]"' in item["hint"]
    assert item["hint"].startswith(diff.REFUSALS["ORD001"])  # the hint of the code stays
    assert "--rename" in error.message  # the one line that every caller prints
    result = gen.generate(repo, "main", "rename_stat", ["column:[sales].[Order].[Stat]=[Status]"])
    assert "sp_rename" in (result.text or "")


def test_a_refused_change_with_no_column_that_can_be_a_rename_keeps_the_hint_of_its_code(tmp_path: Path):
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order("[A] int NULL", "[B] int NULL")})
    R.put(repo, {R.ORDER: R.order("[B] int NULL", "[A] int NULL")})
    error = refusal(lambda: gen.generate(repo, "main", "reorder"))
    assert [item["hint"] for item in error.detail["refusals"]] == [diff.REFUSALS["ORD001"]]
    assert "--rename" not in error.message


def test_a_table_file_that_was_renamed_comes_with_the_rename_argument_for_the_table(tmp_path: Path):
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order("[Note] nvarchar(200) NULL")})
    sale = R.order("[Note] nvarchar(200) NULL").replace("[sales].[Order]", "[sales].[Sale]")
    R.put(repo, {R.ORDER: None, "schema/tables/sales.Sale.sql": sale})
    result = gen.generate(repo, "main", "sale")
    assert "DROP TABLE [sales].[Order];" in (result.text or "")
    (hint,) = result.hints
    assert '--rename "table:[sales].[Order]=[Sale]"' in hint
    assert gen.generate(repo, "main", "sale", ["table:[sales].[Order]=[Sale]"]).hints == ()


# ------------------------------------------------------------------ generate: a schema with no file (CU-04)
LEAD = "schema/tables/crm.Lead.sql"
LEAD_TEXT = "CREATE TABLE [crm].[Lead] (\n    [LeadId] int NOT NULL\n);\n"


def test_a_table_in_a_schema_that_has_no_schema_file_is_refused_with_the_file_to_add(tmp_path: Path):
    # ORDER_BLOCKED said "hand-write the migration"; a hand-written CREATE SCHEMA fails verify
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order()})
    R.put(repo, {LEAD: LEAD_TEXT})
    error = refusal(lambda: gen.generate(repo, "main", "lead"))
    assert error.reason_code == "MODEL_INVALID"
    assert "schema/schemas/crm.sql" in error.message and "CREATE SCHEMA [crm];" in error.message
    assert "TABLE:[crm].[Lead]" in error.message and "and-write" not in error.message
    assert (error.detail["path"], error.detail["line"]) == (LEAD, 1)
    assert [(e["path"], e["code"]) for e in error.detail["errors"]] == [(LEAD, "MDL001")]
    # the file that the message names is the whole correction
    R.put(repo, {"schema/schemas/crm.sql": "CREATE SCHEMA [crm];\n"})
    text = gen.generate(repo, "main", "lead").text or ""
    assert text.index("CREATE SCHEMA [crm];") < text.index("CREATE TABLE [crm].[Lead]")


def test_the_schema_of_a_history_table_needs_its_file_too(tmp_path: Path):
    repo = pair_repo(tmp_path, "temporal_table_new")
    staff = repo / "schema/tables/dbo.Staff.sql"
    text = staff.read_text(encoding="utf-8")
    staff.write_text(text.replace("[dbo].[Staff_History]", "[archive].[Staff_History]"), encoding="utf-8")
    error = refusal(lambda: gen.generate(repo, "main", "staff"))
    assert error.reason_code == "MODEL_INVALID"
    assert "schema/schemas/archive.sql" in error.message and "history table" in error.message
    assert error.detail["path"] == "schema/tables/dbo.Staff.sql"


def test_the_model_check_names_the_schema_file_to_add(tmp_path: Path):
    # verify gives the same object as MDL001: the message is the same correction, once
    ((code, path, message),) = findings_of_model({LEAD: LEAD_TEXT})
    assert (code, path) == ("MDL001", LEAD)
    assert "schema/schemas/crm.sql" in message and "CREATE SCHEMA [crm];" in message


# ------------------------------------------------------------------ generate, resum: start over (CU-01)
def test_delete_the_file_of_a_new_migration_then_resum_then_gen_writes_it_again(tmp_path: Path):
    # The everyday loop: gen, see the mistake, correct the table file, gen again. A second gen would
    # write ALTER COLUMN on top of the first migration, so the author deletes the file of the first.
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order()})
    R.put(repo, {R.ORDER: R.order("[Phone] varchar(3) NULL")})
    assert gen.write_result(repo, gen.generate(repo, "main", "phone"))
    R.put(repo, {R.ORDER: R.order("[Phone] varchar(30) NULL"), "migrations/0001__phone.sql": None})

    # gen cannot start from a chain line with no file, and it names the command that drops the line
    error = refusal(lambda: gen.generate(repo, "main", "phone"))
    assert error.reason_code == "MIGRATION_INVALID"
    assert "migrations/0001__phone.sql" in error.message and "azsqlcd gen --resum" in error.message

    result = gen.resum(repo, "main")
    assert (result.dropped, result.renames, result.sum_text) == (("0001__phone.sql",), (), R.EMPTY_SUM)
    assert gen.write_resum(repo, result) == ["migrations/migrations.sum"]
    assert (repo / "migrations/migrations.sum").read_text(encoding="utf-8") == R.EMPTY_SUM

    again = gen.generate(repo, "main", "phone")
    assert again.file == "0001__phone.sql"
    assert (again.text or "").splitlines()[2:] == [
        "ALTER TABLE [sales].[Order] ADD [Phone] varchar(30) NULL;",
        "GO",
    ]
    gen.write_result(repo, again)
    assert [f for f in gen.verify(repo, "refs/heads/main") if f.severity == "error"] == []
    assert gen.resum(repo, "main").dropped == ()  # nothing is gone now


def test_resum_drops_only_the_new_line_whose_file_is_gone_and_renumbers_the_rest(tmp_path: Path):
    merged = {"0001__a.sql": R.migration("0001__a.sql", ADD_NOTE)}
    base = {**R.SALES, R.ORDER: R.order("[Note] nvarchar(200) NULL"), **R.with_migrations(merged)}
    repo = R.new_repo(tmp_path / "r", base)
    more = ADD_NOTE.replace("[Note]", "[More]")
    new = {
        "0002__gone.sql": R.migration("0002__gone.sql", ADD_A),
        "0003__more.sql": R.migration("0003__more.sql", more),
    }
    R.put(repo, R.with_migrations({**merged, **new}))
    (repo / "migrations/0002__gone.sql").unlink()  # the chain line stays
    result = gen.resum(repo, "main")
    assert result.dropped == ("0002__gone.sql",)
    assert [(r.old, r.new) for r in result.renames] == [("0003__more.sql", "0002__more.sql")]
    assert [entry.file for entry in chain.parse_sum(result.sum_text).entries] == [
        "0001__a.sql",
        "0002__more.sql",
    ]
    assert result.sum_text.startswith(R.sum_text(merged))  # the merged line stays byte-identical


def test_resum_never_drops_the_line_of_a_merged_migration_whose_file_is_gone(tmp_path: Path):
    # a merged migration ran on a database: its line and its file stay for good
    merged = {"0001__a.sql": R.migration("0001__a.sql", ADD_NOTE)}
    base = {**R.SALES, R.ORDER: R.order("[Note] nvarchar(200) NULL"), **R.with_migrations(merged)}
    repo = R.new_repo(tmp_path / "r", base)
    R.put(repo, {"migrations/0001__a.sql": None})
    error = refusal(lambda: gen.resum(repo, "main"))
    assert error.reason_code == "GEN_INVALID" and "migrations/0001__a.sql" in error.message


# ------------------------------------------------------------------ git: the revision is named
def test_a_revision_that_git_cannot_resolve_is_named_in_the_refusal(tmp_path: Path):
    # git says only "Needed a single revision"
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order()})
    error = refusal(lambda: gen.generate(repo, "origin/main", "x"))
    assert error.reason_code == "GIT_FAILED"
    assert "'origin/main'" in error.message and error.detail["revision"] == "origin/main"
    # verify: the ref of the base, read exactly
    (finding,) = [f for f in gen.verify(repo, "refs/remotes/origin/main") if f.code == "GIT_FAILED"]
    assert "'refs/remotes/origin/main'" in finding.message
