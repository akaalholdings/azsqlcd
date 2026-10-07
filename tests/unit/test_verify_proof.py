"""`verify`: the model proof of a pull request, with no database.

The new migrations are replayed on the model of the base revision; the result must be the model
of the object files. Every test builds a temporary git repository: branch main is the base of the
pull request, the working tree is its head.
"""

from pathlib import Path

import pytest

from azsqlcd import chain, gen, release
from azsqlcd.errors import Exit, ToolError
from azsqlcd.lint import Finding
from fixtures.gen import repo as R
from fixtures.pairs import loader

ADD_NOTE = "ALTER TABLE [sales].[Order] ADD [Note] nvarchar(200) NULL;\nGO\n"
WITH_NOTE = R.order("[Note] nvarchar(200) NULL")
ACCEPTED = [case for case in loader.CASES if not case.startswith("refuse_")]


def start(tmp_path: Path, *columns: str, more: R.Files | None = None) -> Path:
    """A repository whose main holds [sales].[Order] with the given columns."""
    return R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order(*columns), **(more or {})})


def one(file: str, body: str, mode: str = "tx") -> R.Files:
    """One new migration and the chain that lists it."""
    return R.with_migrations({file: R.migration(file, body, mode)})


def check(repo: Path) -> list[Finding]:
    return gen.verify(repo, R.git(repo, "rev-parse", "main"))


def errors(findings: list[Finding]) -> list[tuple[str, str, int]]:
    return [(f.code, f.path, f.line) for f in findings if f.severity == "error"]


def the(findings: list[Finding], code: str) -> Finding:
    (found,) = [f for f in findings if f.code == code]
    return found


def give_reasons(repo: Path) -> None:
    """What the author does after gen: a reason on every allow line, then gen --resum."""
    for path in (repo / "migrations").glob("*.sql"):
        path.write_bytes(path.read_bytes().replace(b"reason: TODO", b"reason: reviewed with the DBA team"))
    gen.write_resum(repo, gen.resum(repo, "main"))


# ------------------------------------------------------------------ proof step 5: replay
def test_verify_fails_when_a_table_file_changes_with_no_migration_and_prints_the_statements(tmp_path: Path):
    repo = start(tmp_path)
    R.put(repo, {R.ORDER: R.order("[Note] nvarchar(200) NULL", "[Code] varchar(10) NOT NULL")})
    found = check(repo)
    assert errors(found) == [("PRF002", "migrations/migrations.sum", 1)]
    message = the(found, "PRF002").message
    # what gen would write: the statements, in order, with their directive lines
    assert "ALTER TABLE [sales].[Order] ADD [Note] nvarchar(200) NULL;\nGO\n" in message
    assert message.index("ADD [Note]") < message.index("ADD [Code] varchar(10) NOT NULL;")
    assert "azsqlcd gen --base" in message and "TABLE:[sales].[Order]" in message


def test_the_missing_migration_message_says_so_when_gen_refuses_the_change(tmp_path: Path):
    repo = start(tmp_path, "[A] int NULL", "[B] int NULL")
    R.put(repo, {R.ORDER: R.order("[B] int NULL", "[A] int NULL")})
    message = the(check(repo), "PRF002").message
    assert "hand-write the migration" in message and "ORD001" in message


def test_a_change_of_a_table_file_that_does_not_change_the_model_needs_no_migration(tmp_path: Path):
    repo = start(tmp_path, "[Note] nvarchar(200) NULL")
    R.put(repo, {R.ORDER: R.order("[note] nvarchar(200) NULL")})  # letter case of a name
    assert errors(check(repo)) == []


def test_a_generated_migration_fails_lint_until_a_reason_replaces_todo_and_then_passes(tmp_path: Path):
    repo = start(tmp_path, "[Stat] tinyint NULL")
    R.put(repo, {R.ORDER: R.order()})
    gen.write_result(repo, gen.generate(repo, "main", "drop_stat"))
    assert errors(check(repo)) == [("ALLOW_REASON", "migrations/0001__drop_stat.sql", 3)]
    give_reasons(repo)
    assert errors(check(repo)) == []


def test_a_hand_edited_migration_that_reaches_the_head_model_passes(tmp_path: Path):
    # gen wrote DROP COLUMN + ADD; the author replaces both with one sp_rename (design (b), human edit)
    repo = start(tmp_path, "[Stat] tinyint NULL")
    R.put(repo, {R.ORDER: R.order("[Status] tinyint NULL")})
    generated = gen.generate(repo, "main", "rename_stat")
    assert "DROP COLUMN [Stat]" in (generated.text or "")
    gen.write_result(repo, generated)
    edited = R.migration(
        "0001__rename_stat.sql",
        "-- azsqlcd:allow RENAME [sales].[Order].[Stat] reason: aligned with the API name\n"
        "EXEC sys.sp_rename N'[sales].[Order].[Stat]', N'Status', N'COLUMN';\nGO\n",
    )
    R.put(repo, {"migrations/0001__rename_stat.sql": edited})
    assert errors(check(repo)) == [("CHN004", "migrations/migrations.sum", 2)]  # the sha256 is stale
    gen.write_resum(repo, gen.resum(repo, "main"))
    assert errors(check(repo)) == []


def test_a_hand_edited_migration_that_does_not_reach_it_names_the_object_and_the_property(tmp_path: Path):
    repo = start(tmp_path)
    R.put(repo, {R.ORDER: WITH_NOTE, **one("0001__add_note.sql", ADD_NOTE.replace("(200)", "(100)"))})
    found = check(repo)
    assert errors(found) == [("PRF001", R.ORDER, 1)]
    message = the(found, "PRF001").message
    assert "TABLE:[sales].[Order]" in message and "columns[note].type.length" in message
    assert "100" not in message and "200" not in message  # object and property, never values


def test_an_object_that_the_migrations_leave_without_a_file_and_a_file_without_an_object_are_named(
    tmp_path: Path,
):
    repo = start(tmp_path)
    other = R.table("Other", "[a] int NOT NULL")
    create = "CREATE TABLE [sales].[Extra] (\n    [a] int NOT NULL\n);\nGO\n" + ADD_NOTE
    R.put(repo, {R.ORDER: WITH_NOTE, "schema/tables/sales.Other.sql": other, **one("0001__x.sql", create)})
    found = [f for f in check(repo) if f.code == "PRF001"]
    assert [(f.path, f.message) for f in found] == [
        (
            "schema/tables/sales.Extra.sql",
            "TABLE:[sales].[Extra]: the migrations leave the object and it has no object file",
        ),
        (
            "schema/tables/sales.Other.sql",
            "TABLE:[sales].[Other]: the object file exists and no migration creates the object",
        ),
    ]


def test_a_statement_that_the_model_does_not_accept_is_reported_at_its_line(tmp_path: Path):
    # the column exists at the base revision: the engine would refuse the statement too
    repo = start(tmp_path, "[Note] nvarchar(200) NULL")
    body = "ALTER TABLE [sales].[Order] ADD [More] int NULL;\nGO\n" + ADD_NOTE
    R.put(
        repo, {R.ORDER: R.order("[Note] nvarchar(200) NULL", "[More] int NULL"), **one("0001__x.sql", body)}
    )
    found = check(repo)
    assert errors(found) == [("PRF001", "migrations/0001__x.sql", 5)]
    assert "TABLE:[sales].[Order]" in the(found, "PRF001").message
    assert "[Note]" in the(found, "PRF001").message


def test_a_model_batch_outside_the_closed_grammar_is_reported_with_the_code_and_line_of_the_parser(
    tmp_path: Path,
):
    repo = start(tmp_path)
    body = "ALTER TABLE [sales].[Order] ADD [More] int NULL;\nGO\n" + ADD_NOTE.replace(" NULL;", ";")
    R.put(
        repo, {R.ORDER: R.order("[More] int NULL", "[Note] nvarchar(200) NULL"), **one("0001__x.sql", body)}
    )
    # NF001 at the statement; no PRF001 on top: nothing can be said about a model that was not replayed
    assert errors(check(repo)) == [("NF001", "migrations/0001__x.sql", 5)]


def test_a_data_batch_is_ignored_by_the_proof(tmp_path: Path):
    # The data batch names a table that no model holds. The proof does not model rows.
    repo = start(tmp_path)
    data = "-- azsqlcd:data\nUPDATE [legacy].[Elsewhere] SET [Note] = N'x' WHERE [Note] IS NULL;\nGO\n"
    R.put(repo, {R.ORDER: WITH_NOTE, **one("0001__add_note.sql", ADD_NOTE + data)})
    assert errors(check(repo)) == []
    # the same file without the model batch does not reach the head model: the data batch adds nothing
    R.put(repo, one("0001__add_note.sql", data))
    assert errors(check(repo)) == [("PRF001", R.ORDER, 1)]


def test_new_migrations_are_replayed_in_chain_order_one_on_the_result_of_the_other(tmp_path: Path):
    repo = start(tmp_path)
    index = (
        "-- azsqlcd:allow LONG_LOCK [sales].[Order] reason: small table\n"
        "CREATE NONCLUSTERED INDEX [IX_Order_Note] ON [sales].[Order] ([Note]);\nGO\n"
    )
    files = {
        "0001__add_note.sql": R.migration("0001__add_note.sql", ADD_NOTE),
        "0002__ix_note.sql": R.migration("0002__ix_note.sql", index),
    }
    head = WITH_NOTE + "GO\nCREATE NONCLUSTERED INDEX [IX_Order_Note] ON [sales].[Order] ([Note]);\n"
    R.put(repo, {R.ORDER: head, **R.with_migrations(files)})
    assert errors(check(repo)) == []


def test_the_working_tree_is_the_head_and_a_merged_migration_is_not_replayed_again(tmp_path: Path):
    # 0001 is merged (it is in the chain of main): only 0002 is new
    merged = {"0001__add_note.sql": R.migration("0001__add_note.sql", ADD_NOTE)}
    repo = start(tmp_path, "[Note] nvarchar(200) NULL", more=R.with_migrations(merged))
    more = {**merged, "0002__more.sql": R.migration("0002__more.sql", ADD_NOTE.replace("[Note]", "[More]"))}
    R.put(repo, {R.ORDER: R.order("[Note] nvarchar(200) NULL", "[More] nvarchar(200) NULL")})
    R.put(repo, R.with_migrations(more))
    assert errors(check(repo)) == []


# ------------------------------------------------------------------ allow lines that only the model knows
NOT_NULL_BODY = (
    ADD_NOTE
    + "-- azsqlcd:data\nUPDATE [sales].[Order] SET [Note] = N'' WHERE [Note] IS NULL;\nGO\n"
    + "-- azsqlcd:allow ALTER_COLUMN_LOSSY [sales].[Order].[Note] reason: the type does not change\n"
    + "{allow}"
    + "ALTER TABLE [sales].[Order] ALTER COLUMN [Note] nvarchar(200) NOT NULL;\nGO\n"
)
SET_NOT_NULL = "-- azsqlcd:allow SET_NOT_NULL {object} reason: the batch above filled every row\n"


def test_add_null_then_alter_column_not_null_needs_allow_set_not_null(tmp_path: Path):
    # Against the base revision the column does not exist. The proof knows the model before each
    # statement: the column allows NULL there, so NOT NULL can fail on the rows.
    repo = start(tmp_path)
    head = {R.ORDER: R.order("[Note] nvarchar(200) NOT NULL")}
    R.put(repo, {**head, **one("0001__note.sql", NOT_NULL_BODY.format(allow=""))})
    found = check(repo)
    assert errors(found) == [("PRF003", "migrations/0001__note.sql", 9)]
    assert "-- azsqlcd:allow SET_NOT_NULL [sales].[Order].[Note] reason:" in the(found, "PRF003").message

    allow = SET_NOT_NULL.format(object="sales.[order].NOTE")  # quoting and case do not matter
    R.put(repo, {**head, **one("0001__note.sql", NOT_NULL_BODY.format(allow=allow))})
    assert errors(check(repo)) == []


def test_an_alter_column_that_keeps_not_null_needs_no_set_not_null(tmp_path: Path):
    repo = start(tmp_path, "[Code] varchar(10) NOT NULL")
    body = (
        "-- azsqlcd:allow ALTER_COLUMN_LOSSY [sales].[Order].[Code] reason: wider\n"
        "ALTER TABLE [sales].[Order] ALTER COLUMN [Code] varchar(20) NOT NULL;\nGO\n"
    )
    R.put(repo, {R.ORDER: R.order("[Code] varchar(20) NOT NULL"), **one("0001__wider.sql", body)})
    assert errors(check(repo)) == []


@pytest.mark.parametrize(
    ("allow", "line"),
    [
        (SET_NOT_NULL.format(object="[sales].[Order].[Other]"), 9),  # another column
        ("-- azsqlcd:allow REPLACEMENT_EDGE 0001__gone.sql reason: copied from another file\n", 9),
    ],
    ids=["SET_NOT_NULL", "REPLACEMENT_EDGE"],
)
def test_an_allow_line_of_the_proof_that_matches_nothing_is_an_error(tmp_path: Path, allow: str, line: int):
    # lint never reports these two codes as unused, so the proof must
    repo = start(tmp_path)
    body = NOT_NULL_BODY.format(allow=SET_NOT_NULL.format(object="[sales].[Order].[Note]") + allow)
    R.put(repo, {R.ORDER: R.order("[Note] nvarchar(200) NOT NULL"), **one("0001__note.sql", body)})
    assert errors(check(repo)) == [("PRF003", "migrations/0001__note.sql", line + 1)]


def test_allow_set_not_null_in_a_data_batch_matches_nothing(tmp_path: Path):
    repo = start(tmp_path)
    data = (
        "-- azsqlcd:data\n"
        "-- azsqlcd:allow SET_NOT_NULL [sales].[Order].[Note] reason: filled\n"
        "UPDATE [sales].[Order] SET [Note] = N'' WHERE [Note] IS NULL;\nGO\n"
    )
    R.put(repo, {R.ORDER: WITH_NOTE, **one("0001__add_note.sql", ADD_NOTE + data)})
    assert errors(check(repo)) == [("PRF003", "migrations/0001__add_note.sql", 6)]


# ------------------------------------------------------------------ raw batches
def raw(key: str) -> str:
    return (
        f"-- azsqlcd:raw {key} reason: partitioned, outside the model\n"
        f"-- azsqlcd:allow RAW {key} reason: reviewed\n"
        "ALTER TABLE [audit].[Log] SWITCH PARTITION 1 TO [audit].[LogOld];\nGO\n"
    )


def test_a_raw_batch_for_an_object_that_is_not_unmanaged_fails_raw001(tmp_path: Path):
    # [sales].[Order] is managed: a raw batch on it would change what the model says, unseen
    repo = start(tmp_path)
    R.put(repo, one("0001__raw.sql", ADD_NOTE.replace("[Note]", "[x]") + raw("TABLE:[sales].[Order]")))
    R.put(repo, {R.ORDER: R.order("[x] nvarchar(200) NULL")})
    found = check(repo)
    assert errors(found) == [("RAW001", "migrations/0001__raw.sql", 5)]
    assert "TABLE:[sales].[Order]" in the(found, "RAW001").message


def test_a_raw_batch_for_an_object_of_the_unmanaged_list_passes_and_is_not_replayed(tmp_path: Path):
    repo = start(tmp_path)
    R.put(repo, one("0001__raw.sql", raw("TABLE:[AUDIT].[log]")))  # the catalog ignores case
    assert errors(check(repo)) == []


USP_TOUCH = "schema/procedures/sales.usp_Touch.sql"
RAW_FOR_LOG = (
    "-- azsqlcd:raw TABLE:[audit].[Log] reason: housekeeping\n"
    "-- azsqlcd:allow RAW TABLE:[audit].[Log] reason: reviewed\n"
)


@pytest.mark.parametrize(
    ("statement", "key"),
    [
        ("DROP TABLE [sales].[Order];", "TABLE:[sales].[Order]"),
        ("ALTER TABLE [sales].[Order] ALTER COLUMN [Note] nvarchar(10) NOT NULL;", "TABLE:[sales].[Order]"),
        ('ALTER TABLE SALES."order" ADD [x] int NULL;', "TABLE:[sales].[Order]"),  # other quoting and case
        ("EXEC [sales].[usp_Touch];", "PROCEDURE:[sales].[usp_Touch]"),
        (
            "DELETE [audit].[Log] WHERE [Id] IN (SELECT [OrderId] FROM sales.[Order]);",
            "TABLE:[sales].[Order]",
        ),
    ],
)
def test_a_raw_batch_that_names_a_managed_object_is_raw002_whatever_object_its_directive_names(
    tmp_path: Path, statement: str, key: str
):
    # PP-2: RAW001 reads the directive only. The batch text is not replayed and not read back, so
    # a managed table could change with no proof and under an allow line for another object.
    module = "CREATE OR ALTER PROCEDURE [sales].[usp_Touch]\nAS\nSELECT 1 AS [x];\n"
    repo = start(tmp_path, "[Note] nvarchar(200) NULL", more={USP_TOUCH: module})
    R.put(repo, one("0001__x.sql", RAW_FOR_LOG + statement + "\nGO\n"))
    found = check(repo)
    assert errors(found) == [("RAW002", "migrations/0001__x.sql", 5)]
    assert key in the(found, "RAW002").message and "TABLE:[audit].[Log]" in the(found, "RAW002").message


def test_a_raw_batch_names_a_managed_object_only_outside_strings_comments_and_its_own_directive(
    tmp_path: Path,
):
    repo = start(tmp_path)
    body = (
        RAW_FOR_LOG + "-- was [sales].[Order]\n"
        "UPDATE [audit].[Log] SET [Source] = N'[sales].[Order]' /* sales.Order */ WHERE [Order] = 1;\nGO\n"
    )
    # [Order] alone is a column here: a one-part name is an object of schema dbo only
    R.put(repo, one("0001__x.sql", body))
    assert errors(check(repo)) == []


def test_a_one_part_name_in_a_raw_batch_is_an_object_of_dbo(tmp_path: Path):
    stock = "schema/tables/dbo.Stock.sql"
    repo = start(tmp_path, more={stock: "CREATE TABLE [dbo].[Stock] (\n    [Id] int NOT NULL\n);\n"})
    R.put(repo, one("0001__x.sql", RAW_FOR_LOG + "TRUNCATE TABLE Stock;\nGO\n"))
    assert errors(check(repo)) == [("RAW002", "migrations/0001__x.sql", 5)]


# ------------------------------------------------------------------ table_model
def test_with_table_model_false_there_is_no_proof_and_one_warning_says_so(tmp_path: Path):
    repo = start(tmp_path, more={"azsqlcd.toml": R.TOML_NO_MODEL})
    # no migration, a file that is not in normal form: neither is checked without the model
    R.put(repo, {R.ORDER: R.order("[Note] nvarchar(200)")})
    found = check(repo)
    assert [(f.code, f.severity, f.path) for f in found] == [("PRF000", "warning", "azsqlcd.toml")]
    assert "table_model = false" in the(found, "PRF000").message


def test_the_pull_request_that_switches_the_model_on_brings_its_table_files_with_no_migration(tmp_path: Path):
    # design (c) 8: the files are recorded by baseline; there is no base model to prove them against
    repo = R.new_repo(tmp_path / "r", {"azsqlcd.toml": R.TOML_NO_MODEL})
    R.put(repo, {"azsqlcd.toml": R.TOML, **R.SALES, R.ORDER: WITH_NOTE})
    found = check(repo)
    assert errors(found) == []
    assert "sets table_model = true" in the(found, "PRF000").message

    # the files of the switch are still held to the normal form and must fit together
    R.put(repo, {R.ORDER: R.order("[Note] nvarchar(200)"), "schema/schemas/sales.sql": None})
    assert errors(check(repo)) == [("MODEL_INVALID", R.ORDER, 3), ("NF001", R.ORDER, 3)]


def test_the_pull_request_that_switches_the_model_on_adds_no_migration(tmp_path: Path):
    # nothing could prove its statements: the base revision has no model
    repo = R.new_repo(tmp_path / "r", {"azsqlcd.toml": R.TOML_NO_MODEL})
    R.put(
        repo, {"azsqlcd.toml": R.TOML, **R.SALES, R.ORDER: WITH_NOTE, **one("0001__add_note.sql", ADD_NOTE)}
    )
    assert errors(check(repo)) == [("PRF004", "migrations/0001__add_note.sql", 1)]


def test_a_pull_request_that_sets_table_model_to_false_fails_and_is_still_proven(tmp_path: Path):
    # The switch would take the proof, RAW001 and NF000 away for the very change that makes it.
    repo = start(tmp_path)
    backdoor = ADD_NOTE.replace("[Note]", "[Backdoor]") + raw("TABLE:[sales].[Order]")
    R.put(repo, {"azsqlcd.toml": R.TOML_NO_MODEL, **one("0001__x.sql", backdoor)})
    found = check(repo)
    assert errors(found) == [
        ("PRF005", "azsqlcd.toml", 1),
        ("RAW001", "migrations/0001__x.sql", 5),
        ("PRF001", R.ORDER, 1),  # the migration adds a column that the table file does not hold
    ]
    assert "from true to false" in the(found, "PRF005").message
    assert [f.code for f in found if f.code == "PRF000"] == []  # nothing says "no proof": there is one

    # the normal form of the table files is still checked, and a file that changes needs a migration
    R.put(repo, {"migrations/0001__x.sql": None, "migrations/migrations.sum": R.EMPTY_SUM})
    R.put(repo, {R.ORDER: R.order("[Note] nvarchar(200)")})
    assert errors(check(repo)) == [
        ("PRF005", "azsqlcd.toml", 1),
        ("MODEL_INVALID", R.ORDER, 3),
        ("NF001", R.ORDER, 3),
    ]
    R.put(repo, {R.ORDER: WITH_NOTE})
    assert errors(check(repo)) == [("PRF005", "azsqlcd.toml", 1), ("PRF002", "migrations/migrations.sum", 1)]

    # the switch alone, with nothing else in the pull request, is the error too
    R.put(repo, {R.ORDER: R.order()})
    assert errors(check(repo)) == [("PRF005", "azsqlcd.toml", 1)]


def test_a_repository_that_never_had_the_model_is_not_a_switch_to_false(tmp_path: Path):
    # no azsqlcd.toml at the base revision, and table_model = false in the first pull request
    repo = R.new_repo(tmp_path / "r", {})
    R.git(repo, "checkout", "-q", "main")
    R.git(repo, "rm", "-q", "azsqlcd.toml")
    R.commit(repo, "empty")
    R.git(repo, "checkout", "-q", "-B", "work")
    R.put(repo, {"azsqlcd.toml": R.TOML_NO_MODEL})
    assert [(f.code, f.severity) for f in check(repo)] == [("PRF000", "warning")]


def test_a_first_pull_request_of_a_repository_is_proven_from_the_empty_model(tmp_path: Path):
    # no azsqlcd.toml at the base revision is not "table_model = false": the chain creates everything
    repo = R.new_repo(tmp_path / "r", {})
    R.git(repo, "checkout", "-q", "main")
    R.git(repo, "rm", "-q", "azsqlcd.toml")
    R.commit(repo, "empty")
    R.git(repo, "checkout", "-q", "-B", "work")
    R.put(repo, {"azsqlcd.toml": R.TOML, **R.SALES, R.ORDER: R.order()})
    assert errors(check(repo)) == [("PRF002", "migrations/migrations.sum", 1)]
    gen.write_result(repo, gen.generate(repo, "main", "first"))
    assert errors(check(repo)) == []


# ------------------------------------------------------------------ gen, then verify
@pytest.mark.parametrize("case", ACCEPTED)
def test_gen_then_verify_passes_for_every_accepted_pair(tmp_path: Path, case: str):
    repo = R.new_repo(tmp_path / "r", dict(loader.object_files(case, "base")))
    R.put(repo, {path: None for path, _ in loader.object_files(case, "base")})
    R.put(repo, dict(loader.object_files(case, "head")))
    renames_file = loader.ROOT / case / "renames.txt"
    renames = renames_file.read_text(encoding="utf-8").splitlines() if renames_file.exists() else []
    result = gen.generate(repo, "main", "change", [r for r in renames if r])
    gen.write_result(repo, result)
    expected = (loader.ROOT / case / "expected.sql").read_text(encoding="utf-8")
    if result.text is None:
        assert expected == "" and errors(check(repo)) == []
        return
    # the statements are those of the recorded diff; gen adds only comment lines
    written = [line for line in result.text.splitlines() if not line.startswith("-- azsqlcd:")]
    assert written == expected.splitlines()
    # as written, the only errors are the reasons that the author owes
    assert {code for code, _, _ in errors(check(repo))} <= {"ALLOW_REASON"}
    assert len(errors(check(repo))) == result.text.count("reason: TODO")
    give_reasons(repo)
    assert errors(check(repo)) == []


TEAM = "schema/tables/sales.Team.sql"
TEAM_TEXT = (
    "CREATE TABLE [sales].[Team] (\n"
    "    [TeamId] int NOT NULL,\n"
    "    [ValidFrom] datetime2(7) GENERATED ALWAYS AS ROW START NOT NULL,\n"
    "    [ValidTo] datetime2(7) GENERATED ALWAYS AS ROW END NOT NULL,\n"
    "    CONSTRAINT [PK_Team] PRIMARY KEY CLUSTERED ([TeamId]),\n"
    "    PERIOD FOR SYSTEM_TIME ([ValidFrom], [ValidTo])\n"
    ")\n"
    "WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [sales].[Team_History]));\n"
)
TEAM_OFF = "ALTER TABLE [sales].[Team] SET (SYSTEM_VERSIONING = OFF);\nGO\n"
TEAM_DROP = (
    "-- azsqlcd:allow DROP_TABLE [sales].[Team] reason: replaced by hr.Team in r40\n"
    "DROP TABLE [sales].[Team];\nGO\n"
)
TEAM_ALLOW_OFF = "-- azsqlcd:allow TEMPORAL_OFF [sales].[Team] reason: the auditors keep sales.Team_History\n"


def test_a_hand_written_drop_of_a_temporal_table_needs_allow_temporal_off_at_the_statement(tmp_path: Path):
    repo = start(tmp_path, more={TEAM: TEAM_TEXT})
    R.put(repo, {TEAM: None, **one("0001__x.sql", TEAM_OFF + TEAM_DROP)})
    assert errors(check(repo)) == [("TEMPORAL_OFF", "migrations/0001__x.sql", 3)]
    R.put(repo, one("0001__x.sql", TEAM_ALLOW_OFF + TEAM_OFF + TEAM_DROP))
    assert errors(check(repo)) == []


def test_a_new_temporal_table_is_proven_from_its_create_statement(tmp_path: Path):
    repo = start(tmp_path)
    R.put(repo, {TEAM: TEAM_TEXT, **one("0001__x.sql", TEAM_TEXT + "GO\n")})
    assert errors(check(repo)) == []
    # the same statement with another history table is not the table of the file
    other = TEAM_TEXT.replace("[Team_History]", "[Team_Old]")
    R.put(repo, one("0001__x.sql", other + "GO\n"))
    assert [(code, path) for code, path, _ in errors(check(repo))] == [("PRF001", TEAM)]


def test_versioning_off_for_a_table_that_stays_has_no_proof(tmp_path: Path):
    # the table of the file is system-versioned; a history that ends with versioning off is not it
    repo = start(tmp_path, more={TEAM: TEAM_TEXT})
    R.put(repo, one("0001__x.sql", TEAM_ALLOW_OFF + TEAM_OFF))
    assert [(code, path) for code, path, _ in errors(check(repo))] == [("PRF001", TEAM)]


def test_removing_a_statement_from_a_generated_migration_fails_the_proof(tmp_path: Path):
    case = "every_step_in_the_fixed_order"
    repo = R.new_repo(tmp_path / "r", dict(loader.object_files(case, "base")))
    R.put(repo, {path: None for path, _ in loader.object_files(case, "base")})
    R.put(repo, dict(loader.object_files(case, "head")))
    result = gen.generate(repo, "main", "change", [str(r) for r in _rename_lines(case)])
    gen.write_result(repo, result)
    path = repo / "migrations" / (result.file or "")
    batches = path.read_text(encoding="utf-8").split("GO\n")
    path.write_text(
        "GO\n".join(batches[:-2] + batches[-1:]), encoding="utf-8", newline=""
    )  # DROP SCHEMA [old]
    give_reasons(repo)
    found = check(repo)
    assert errors(found) == [("PRF001", "schema/schemas/old.sql", 1)]
    assert the(found, "PRF001").message.startswith("SCHEMA:[old]: the migrations leave the object")


def _rename_lines(case: str) -> list[str]:
    return [
        line for line in (loader.ROOT / case / "renames.txt").read_text(encoding="utf-8").splitlines() if line
    ]


# ------------------------------------------------------------------ verify as a whole
def test_verify_reports_lint_the_change_rules_the_model_and_the_proof_together_sorted_with_no_repeat(
    tmp_path: Path,
):
    view = "CREATE OR ALTER VIEW [sales].[vw_Plain] AS SELECT [OrderId] FROM [sales].[Order];\n"
    repo = start(tmp_path, more={"schema/views/sales.vw_Plain.sql": view})
    fk = "CONSTRAINT [FK_Order_Gone] FOREIGN KEY ([OrderId]) REFERENCES [sales].[Gone] ([Id])"
    R.put(
        repo,
        {
            R.ORDER: R.table("Order", R.ORDER_ID, R.PK_ORDER + " ON [PRIMARY]", fk),  # NF000, MDL001, PRF002
            "schema/views/sales.vw_Plain.sql": None,  # TMB001: removed with no tombstone
            "schema/notes.txt": "x",  # UNKNOWN_PATH
        },
    )
    found = check(repo)
    assert errors(found) == [
        ("PRF002", "migrations/migrations.sum", 1),
        ("UNKNOWN_PATH", "schema/notes.txt", 1),
        ("MDL001", R.ORDER, 1),
        ("NF000", R.ORDER, 3),
        ("TMB001", "schema/views/sales.vw_Plain.sql", 1),
    ]
    assert len(found) == len(set(found))


def test_a_chain_that_does_not_parse_is_one_finding_and_not_an_exception(tmp_path: Path):
    repo = start(tmp_path)
    R.put(repo, {"migrations/migrations.sum": "azsqlcd-sum 1\nnot a line\n"})
    # lint reports it, the change rules skip it, the proof refuses with the same text: one finding
    assert errors(check(repo)) == [("CHAIN_INVALID", "migrations/migrations.sum", 2)]


def test_a_new_migration_file_that_is_missing_is_reported_and_stops_the_proof_only(tmp_path: Path):
    repo = start(tmp_path)
    files = one("0001__add_note.sql", ADD_NOTE)
    R.put(repo, {R.ORDER: WITH_NOTE, "migrations/migrations.sum": files["migrations/migrations.sum"]})
    assert errors(check(repo)) == [
        ("MIGRATION_INVALID", "migrations/0001__add_note.sql", 1),
        ("CHN003", "migrations/migrations.sum", 2),
    ]


def test_a_base_revision_that_git_cannot_read_is_a_finding_with_the_reason_of_git(tmp_path: Path):
    repo = start(tmp_path)
    found = gen.verify(repo, "0" * 40)
    assert [(f.code, f.severity, f.path) for f in found] == [("GIT_FAILED", "error", ".")]


def test_an_invalid_config_leaves_the_finding_to_lint_and_runs_no_proof(tmp_path: Path):
    repo = start(tmp_path)
    R.put(repo, {"azsqlcd.toml": "[project]\n", R.ORDER: WITH_NOTE})
    assert errors(check(repo)) == [("CONFIG_INVALID", "azsqlcd.toml", 1)]


def test_head_files_that_do_not_parse_give_the_parser_findings_and_one_finding_that_nothing_was_proven(
    tmp_path: Path,
):
    repo = start(tmp_path)
    R.put(repo, {R.ORDER: R.order("[Note] nvarchar(200)")})
    found = check(repo)
    assert errors(found) == [("MODEL_INVALID", R.ORDER, 3), ("NF001", R.ORDER, 3)]
    assert "there is no model" in the(found, "MODEL_INVALID").message


# ------------------------------------------------------------------ prove() on file maps
def as_bytes(files: R.Files) -> dict[str, bytes]:
    return {path: text.encode() for path, text in files.items() if isinstance(text, str)}


def test_prove_needs_no_repository_for_a_change_without_a_replacement():
    base = as_bytes({"azsqlcd.toml": R.TOML, **R.SALES, R.ORDER: R.order()})
    head = {**base, **as_bytes({R.ORDER: WITH_NOTE, **one("0001__add_note.sql", ADD_NOTE)})}
    assert gen.prove(base, head) == []
    assert [f.code for f in gen.prove(base, {**head, R.ORDER: base[R.ORDER]})] == ["PRF001"]


def test_prove_with_table_model_false_is_one_warning_and_no_proof():
    base = as_bytes({"azsqlcd.toml": R.TOML_NO_MODEL, **R.SALES, R.ORDER: R.order()})
    (finding,) = gen.prove(base, {**base, **as_bytes({R.ORDER: WITH_NOTE})})
    assert (finding.code, finding.severity) == ("PRF000", "warning")
    assert finding.message.startswith("table_model = false")


def test_prove_of_a_change_that_sets_table_model_to_false_is_an_error_and_the_proof():
    base = as_bytes({"azsqlcd.toml": R.TOML, **R.SALES, R.ORDER: R.order()})
    head = {**base, **as_bytes({"azsqlcd.toml": R.TOML_NO_MODEL, R.ORDER: WITH_NOTE})}
    assert [(f.code, f.severity) for f in gen.prove(base, head)] == [("PRF005", "error"), ("PRF002", "error")]


def test_a_base_config_that_cannot_be_read_counts_as_table_model_true_unless_it_says_false():
    # The tool that reads the pull request can be newer than the config of main. Only a base
    # config that says table_model = false in so many words lets the head say false.
    head = as_bytes({"azsqlcd.toml": R.TOML_NO_MODEL, **R.SALES, R.ORDER: R.order()})
    for text, codes in (
        (b"[project]\n", ["PRF005"]),
        (b"[project]\ntable_model = true\n", ["PRF005"]),
        (b"not toml [", ["PRF005"]),
        (b"\xff", ["PRF005"]),
        (b"[project]\ntable_model = false\n", ["PRF000"]),
    ):
        assert [f.code for f in gen.prove({**head, "azsqlcd.toml": text}, head)] == codes, text


def test_a_base_config_that_cannot_be_read_does_not_switch_the_proof_off():
    # Only a base revision that says table_model = false is the switch. Anything else is proven.
    head = as_bytes({"azsqlcd.toml": R.TOML, **R.SALES, R.ORDER: WITH_NOTE})
    base = {**head, "azsqlcd.toml": b"[project]\n", R.ORDER: R.order().encode()}
    assert [f.code for f in gen.prove(base, head)] == ["PRF002"]


def test_prove_refuses_when_the_head_config_cannot_be_read():
    with pytest.raises(ToolError) as caught:
        gen.prove({}, as_bytes({**R.SALES, R.ORDER: R.order()}))
    assert (caught.value.exit_code, caught.value.reason_code) == (Exit.REFUSED, "CONFIG_INVALID")


def test_every_finding_code_of_the_proof_is_listed_with_its_severity():
    assert gen.CODES["PRF000"] == "warning"
    assert {code for code, severity in gen.CODES.items() if severity == "error"} == {
        "NF000",
        "MDL001",
        "MODEL_INVALID",
        "PRF001",
        "PRF002",
        "PRF003",
        "PRF004",
        "PRF005",
        "PRF006",
        "PRF007",
        "RAW001",
        "RAW002",
        "WDR001",
        "WDR002",
        "WDR003",
        "WDR004",
        "WDR005",
        "TMB003",
    }


# ------------------------------------------------------------------ proof step 6: withdraw and replace (A31)
A, W, W2 = "0001__a.sql", "0002__w.sql", "0003__w2.sql"
INDEX = "CREATE NONCLUSTERED INDEX [IX_Order_Note] ON [sales].[Order] ([Note]);\n"
LONG_LOCK = "-- azsqlcd:allow LONG_LOCK [sales].[Order] reason: small table\n"
EDGE = "-- azsqlcd:allow REPLACEMENT_EDGE {id} reason: same index, built with the same statement\n"
MERGED = {
    A: R.migration(A, ADD_NOTE),
    W: R.migration(W, LONG_LOCK + INDEX + "GO\n"),
}
INDEXED = WITH_NOTE + "GO\n" + INDEX


def merged_withdrawn(tmp_path: Path) -> Path:
    """main: one merge commit brought in 0001__a (ADD [Note]) and then 0002__w (an index on [Note]).

    The side branch made them in two commits that are not consistent one by one, as work in
    progress is: first the two migration files, then the chain lines and the table file. Only the
    first-parent history of main shows the change as the databases got it. The working tree is a
    new branch on that main.
    """
    repo = start(tmp_path)
    R.git(repo, "checkout", "-q", "-b", "feature", "main")
    R.commit(repo, "work in progress", {f"migrations/{file}": text for file, text in MERGED.items()})
    R.commit(repo, "done", {R.ORDER: INDEXED, **R.with_migrations(MERGED)})
    R.git(repo, "checkout", "-q", "main")
    R.git(repo, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    R.git(repo, "checkout", "-q", "-B", "work", "main")
    return repo


def release_sha(repo: Path, file: str) -> str:
    return chain.file_sha256((repo / "migrations" / file).read_bytes())


def replace_w(repo: Path, body: str, more: R.Files | None = None) -> None:
    """The pull request: 0002__w is withdrawn, 0003__w2 replaces it."""
    files = {**MERGED, W2: R.migration(W2, body)}
    R.put(repo, {**R.with_migrations(files, {W: "withdrawn", W2: f"replaces={W}"}), **(more or {})})


def test_a_replacement_that_gives_the_model_of_the_withdrawn_migration_passes(tmp_path: Path):
    # The commit that added 0002__w on the first-parent history of main is the merge. Its parent
    # has no [Note] column: the model before 0002__w is that parent plus 0001__a, which the same
    # merge added before it (A31). Without that replay the index has no column to stand on.
    repo = merged_withdrawn(tmp_path)
    replace_w(repo, LONG_LOCK + EDGE.format(id=W) + INDEX + "GO\n")
    assert errors(check(repo)) == []
    # the id in the header spelling, without .sql, names the same migration
    replace_w(repo, LONG_LOCK + EDGE.format(id=W.removesuffix(".sql")) + INDEX + "GO\n")
    assert errors(check(repo)) == []


def test_the_withdraw_and_replace_steps_of_the_how_to_pass_with_hand_edits_and_resum(tmp_path: Path):
    # E2E-10, the steps as the developer does them: no tool writes the two chain words.
    repo = merged_withdrawn(tmp_path)
    chain_file = repo / "migrations/migrations.sum"
    lines = chain_file.read_text(encoding="utf-8").splitlines()
    # 1. the word 'withdrawn' at the end of the line of the merged migration; its file stays
    lines[2] += " withdrawn"
    # 2. the replacement, written by hand under any number, with allow REPLACEMENT_EDGE
    hand = "0009__w_again.sql"
    R.put(repo, {f"migrations/{hand}": R.migration(hand, LONG_LOCK + EDGE.format(id=W) + INDEX + "GO\n")})
    # 3. its chain line with replaces=; gen --resum writes the number and the sha256
    lines.append(f"{hand} sha256:{'0' * 64} tx replaces={W}")
    chain_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = gen.resum(repo, "main")
    gen.write_resum(repo, result)
    assert [(r.old, r.new) for r in result.renames] == [(hand, "0003__w_again.sql")]
    assert chain_file.read_text(encoding="utf-8").splitlines()[2:] == [
        f"{lines[2]}",
        f"0003__w_again.sql sha256:{release_sha(repo, '0003__w_again.sql')} tx replaces={W}",
    ]
    assert errors(check(repo)) == []


def test_a_replacement_that_reaches_another_model_fails_wdr002_with_object_and_property(tmp_path: Path):
    repo = merged_withdrawn(tmp_path)
    replace_w(repo, LONG_LOCK + EDGE.format(id=W) + INDEX.replace("([Note])", "([Note] DESC)") + "GO\n")
    found = check(repo)
    assert errors(found) == [("WDR002", f"migrations/{W2}", 1)]
    message = the(found, "WDR002").message
    assert "TABLE:[sales].[Order]" in message and "indexes[ix_order_note]" in message and W in message


def test_a_replacement_whose_statement_the_model_before_the_withdrawn_migration_refuses_fails_wdr002(
    tmp_path: Path,
):
    repo = merged_withdrawn(tmp_path)
    replace_w(repo, LONG_LOCK + EDGE.format(id=W) + INDEX.replace("([Note])", "([Gone])") + "GO\n")
    assert errors(check(repo)) == [("WDR002", f"migrations/{W2}", 5)]


def test_a_replacement_without_the_allow_line_fails_wdr003(tmp_path: Path):
    repo = merged_withdrawn(tmp_path)
    replace_w(repo, LONG_LOCK + INDEX + "GO\n")
    found = check(repo)
    assert errors(found) == [("WDR003", f"migrations/{W2}", 1)]
    assert f"-- azsqlcd:allow REPLACEMENT_EDGE {W} reason:" in the(found, "WDR003").message
    # an allow line for another migration does not count, and it is reported as matching nothing
    replace_w(repo, LONG_LOCK + EDGE.format(id=A) + INDEX + "GO\n")
    assert errors(check(repo)) == [("WDR003", f"migrations/{W2}", 1), ("PRF003", f"migrations/{W2}", 4)]


def test_a_replacement_in_a_pull_request_that_also_changes_a_table_file_fails_wdr001(tmp_path: Path):
    # The table file change has its own migration, so the replay proof alone would pass.
    repo = merged_withdrawn(tmp_path)
    more = "0004__more.sql"
    files = {
        **MERGED,
        W2: R.migration(W2, LONG_LOCK + EDGE.format(id=W) + INDEX + "GO\n"),
        more: R.migration(more, ADD_NOTE.replace("[Note]", "[More]")),
    }
    head = R.order("[Note] nvarchar(200) NULL", "[More] nvarchar(200) NULL") + "GO\n" + INDEX
    R.put(repo, {**R.with_migrations(files, {W: "withdrawn", W2: f"replaces={W}"}), R.ORDER: head})
    found = check(repo)
    assert errors(found) == [("WDR001", f"migrations/{W2}", 1)]
    assert R.ORDER in the(found, "WDR001").message


def test_a_replacement_needs_set_not_null_like_any_other_new_migration(tmp_path: Path):
    repo = merged_withdrawn(tmp_path)
    alter = (
        "-- azsqlcd:allow ALTER_COLUMN_LOSSY [sales].[Order].[Note] reason: same type\n"
        "ALTER TABLE [sales].[Order] ALTER COLUMN [Note] nvarchar(200) NOT NULL;\nGO\n"
    )
    replace_w(repo, alter + LONG_LOCK + EDGE.format(id=W) + INDEX + "GO\n")
    codes = [(code, line) for code, _, line in errors(check(repo))]
    assert codes == [("WDR002", 1), ("PRF003", 4)]  # another model, and NOT NULL without its allow line


def test_a_replacement_cannot_be_proven_without_the_history_of_main(tmp_path: Path):
    repo = merged_withdrawn(tmp_path)
    replace_w(repo, LONG_LOCK + EDGE.format(id=W) + INDEX + "GO\n")
    base, head = release.read_tree(repo, "main"), gen.read_working_tree(repo)
    assert [f.code for f in gen.prove(base, head, root=repo, main_ref="refs/heads/main")] == []
    # no repository given
    (finding,) = gen.prove(base, head)
    assert (finding.code, finding.severity, finding.path) == ("WDR002", "error", f"migrations/{W2}")
    assert "no repository was given" in finding.message  # never the directory that the process is in
    # a history that never added the withdrawn file on its first-parent chain (the side branch did)
    (finding,) = gen.prove(base, head, root=repo, main_ref=R.git(repo, "rev-parse", "main~1"))
    assert finding.code == "WDR002" and "no first-parent commit" in finding.message


# ------------------------------------------------------------------ withdrawn with no replacement
def withdraw(
    repo: Path, *files: str, merged: dict[str, str] | None = None, more: R.Files | None = None
) -> None:
    """The pull request: the word 'withdrawn' on the chain lines of the files, and nothing else."""
    chain_files = merged or MERGED
    R.put(repo, {**R.with_migrations(chain_files, dict.fromkeys(files, "withdrawn")), **(more or {})})


def test_a_merged_migration_that_is_withdrawn_with_no_replacement_fails_wdr004(tmp_path: Path):
    # A database that never ran 0002__w skips it for good; the table file still holds its index.
    repo = merged_withdrawn(tmp_path)
    withdraw(repo, W)
    found = check(repo)
    assert errors(found) == [("WDR004", "migrations/migrations.sum", 3)]
    message = the(found, "WDR004").message
    assert W in message and "TABLE:[sales].[Order]" in message and "indexes[ix_order_note]" in message
    assert f"replaces={W}" in message


def test_a_withdrawal_passes_when_the_table_files_go_back_to_the_model_without_the_migration(tmp_path: Path):
    repo = merged_withdrawn(tmp_path)
    withdraw(repo, W, more={R.ORDER: WITH_NOTE})
    assert errors(check(repo)) == []
    # the table file of the model before 0001__a is not the model without 0002__w
    withdraw(repo, W, more={R.ORDER: R.order()})
    assert errors(check(repo)) == [("WDR004", "migrations/migrations.sum", 3)]


def test_a_withdrawal_that_a_later_merged_migration_stands_on_is_refused(tmp_path: Path):
    # 0002__w builds its index on the column of 0001__a: without 0001__a it does not replay
    repo = merged_withdrawn(tmp_path)
    withdraw(repo, A, more={R.ORDER: R.order()})
    found = check(repo)
    assert errors(found) == [("WDR004", "migrations/migrations.sum", 2)]
    assert "cannot be proven" in the(found, "WDR004").message and W in the(found, "WDR004").message
    # both withdrawn, and the table file as it was before both: proven
    withdraw(repo, A, W, more={R.ORDER: R.order()})
    assert errors(check(repo)) == []


def test_new_migrations_of_the_pull_request_are_replayed_on_the_model_without_the_withdrawn_one(
    tmp_path: Path,
):
    repo = merged_withdrawn(tmp_path)
    more = "0003__more.sql"
    files = {**MERGED, more: R.migration(more, ADD_NOTE.replace("[Note]", "[More]"))}
    head = R.order("[Note] nvarchar(200) NULL", "[More] nvarchar(200) NULL")
    R.put(repo, {**R.with_migrations(files, {W: "withdrawn"}), R.ORDER: head})
    assert errors(check(repo)) == []
    R.put(repo, {R.ORDER: head + "GO\n" + INDEX})  # the index of the withdrawn migration stays in the file
    assert errors(check(repo)) == [("WDR004", "migrations/migrations.sum", 3)]


def test_gen_starts_from_the_model_without_a_migration_that_the_branch_withdraws(tmp_path: Path):
    # gen must not write the statements that undo the withdrawn migration: a database that never ran it
    # has nothing to undo, and the proof replays on the model without it
    repo = merged_withdrawn(tmp_path)
    withdraw(repo, W, more={R.ORDER: WITH_NOTE})
    assert gen.generate(repo, "main", "nothing").reason == gen.NO_CHANGE
    R.put(repo, {R.ORDER: R.order("[Note] nvarchar(200) NULL", "[More] nvarchar(200) NULL")})
    result = gen.generate(repo, "main", "more")
    assert result.file == "0003__more.sql" and result.text is not None
    assert "ADD [More] nvarchar(200) NULL;" in result.text and "IX_Order_Note" not in result.text
    gen.write_result(repo, result)
    assert errors(check(repo)) == []
    # the table file still holds the index of the withdrawn migration: gen refuses and says why
    R.put(repo, {"migrations/0003__more.sql": None})
    withdraw(repo, A, more={R.ORDER: INDEXED})
    with pytest.raises(ToolError) as caught:
        gen.generate(repo, "main", "x")
    assert caught.value.reason_code == "GEN_INVALID" and W in caught.value.message


def test_a_withdrawn_migration_with_no_model_batch_needs_no_replacement(tmp_path: Path):
    # data only: the model does not change with it, and the proof is about the model
    data = "0001__data.sql"
    text = R.migration(data, "-- azsqlcd:data\nUPDATE [sales].[Order] SET [OrderId] = 1 WHERE 1 = 0;\nGO\n")
    repo = start(tmp_path)
    R.git(repo, "checkout", "-q", "main")
    R.commit(repo, "data", R.with_migrations({data: text}))
    R.git(repo, "checkout", "-q", "-B", "work")
    withdraw(repo, data, merged={data: text})
    assert errors(check(repo)) == []


def test_a_withdrawal_cannot_be_proven_without_the_history_of_main(tmp_path: Path):
    repo = merged_withdrawn(tmp_path)
    withdraw(repo, W, more={R.ORDER: WITH_NOTE})
    base, head = release.read_tree(repo, "main"), gen.read_working_tree(repo)
    assert gen.prove(base, head, root=repo, main_ref="refs/heads/main") == []
    (finding,) = gen.prove(base, head)
    assert (finding.code, finding.severity, finding.path) == ("WDR004", "error", "migrations/migrations.sum")
    assert "no repository was given" in finding.message
    (finding,) = gen.prove(base, head, root=repo, main_ref=R.git(repo, "rev-parse", "main~1"))
    assert finding.code == "WDR004" and "no first-parent commit" in finding.message


def test_the_ref_of_main_in_the_proof_is_one_exact_ref_or_a_full_commit_id_and_never_a_tag(tmp_path: Path):
    # Same class as LB-01: git reads the short name origin/main as refs/tags/origin/main first, and
    # who can push a tag can make one. Here the tag is on a commit whose history never added 0002__w.
    repo = merged_withdrawn(tmp_path)
    withdraw(repo, W, more={R.ORDER: WITH_NOTE})
    base, head = release.read_tree(repo, "refs/heads/main"), gen.read_working_tree(repo)
    main, before = R.git(repo, "rev-parse", "refs/heads/main"), R.git(repo, "rev-parse", "main~1")
    R.git(repo, "update-ref", release.MAIN_REF, main)
    for tag in ("origin/main", "main"):
        R.git(repo, "tag", tag, before)
    assert gen.prove(base, head, root=repo) == []  # the default is release.MAIN_REF, read exactly
    assert gen.prove(base, head, root=repo, main_ref="refs/heads/main") == []
    assert gen.prove(base, head, root=repo, main_ref=main) == []
    for short in ("origin/main", "main", "heads/main", "refs/tags/main", "main~1", main[:12], "HEAD"):
        (finding,) = gen.prove(base, head, root=repo, main_ref=short)
        assert finding.code == "WDR004" and "MAIN_REF_INVALID" in finding.message, short


def test_verify_takes_its_base_as_a_full_commit_id_or_one_exact_ref_and_never_as_a_short_name(tmp_path: Path):
    repo = start(tmp_path)
    R.put(repo, {R.ORDER: WITH_NOTE})  # a table file changes and no migration is added
    R.git(repo, "tag", "main", R.commit(repo, "the change, with a tag that has the name of main"))
    wanted = [("PRF002", "migrations/migrations.sum", 1)]
    assert errors(gen.verify(repo, R.git(repo, "rev-parse", "refs/heads/main"))) == wanted
    assert errors(gen.verify(repo, "refs/heads/main")) == wanted
    # git reads the short name as the tag: the base would be the change itself, and nothing differs
    for short in ("main", "refs/tags/main", "heads/main", "HEAD~1"):
        found = gen.verify(repo, short)
        assert errors(found) == [("MAIN_REF_INVALID", ".", 1)], short
        assert release.MAIN_REF in the(found, "MAIN_REF_INVALID").message


def test_a_rename_that_plan_could_not_follow_is_a_finding_of_the_proof(tmp_path: Path):
    # PP-8: the replay goes step by step and passes; plan reads the statements with the head model
    # and must know every table that a rename changes. Here no statement and no model names it.
    two = {
        "schema/tables/sales.Two.sql": R.table("Two", "[Id] int NOT NULL CONSTRAINT [DF_Old] DEFAULT ((0))")
    }
    repo = start(tmp_path, more=two)
    body = (
        "-- azsqlcd:allow RENAME [sales].[DF_Old] reason: r\n"
        "EXEC sys.sp_rename N'[sales].[DF_Old]', N'DF_New', N'OBJECT';\nGO\n"
        "-- azsqlcd:allow DROP_TABLE [sales].[Two] reason: r\nDROP TABLE [sales].[Two];\nGO\n"
    )
    R.put(repo, {"schema/tables/sales.Two.sql": None, **one("0001__x.sql", body)})
    found = check(repo)
    assert errors(found) == [("PRF006", "migrations/0001__x.sql", 4)]
    assert "[sales].[DF_Old]" in the(found, "PRF006").message
    # the reviewer's case: the constraint is renamed and then dropped by name; the statement names the table
    repo = R.new_repo(
        tmp_path / "t1", {**R.SALES, R.ORDER: R.order("[Note] int NULL CONSTRAINT [DF_Old] DEFAULT ((0))")}
    )
    body = (
        body.split("-- azsqlcd:allow DROP_TABLE")[0]
        + "ALTER TABLE [sales].[Order] DROP CONSTRAINT [DF_New];\nGO\n"
    )
    R.put(repo, {R.ORDER: R.order("[Note] int NULL"), **one("0001__x.sql", body)})
    assert errors(check(repo)) == []


def test_a_replacement_is_added_by_the_pull_request_that_withdraws_the_migration(tmp_path: Path):
    # RS-2: 0002__w was withdrawn by an earlier pull request. A database that skipped it has taken
    # later migrations since; a replacement now would stand before them in the apply order.
    repo = merged_withdrawn(tmp_path)
    R.git(repo, "checkout", "-q", "main")
    R.commit(repo, "withdraw w", {**R.with_migrations(MERGED, {W: "withdrawn"}), R.ORDER: WITH_NOTE})
    R.git(repo, "checkout", "-q", "-B", "work")
    replace_w(repo, LONG_LOCK + EDGE.format(id=W) + INDEX + "GO\n")
    found = check(repo)
    assert ("WDR005", f"migrations/{W2}", 1) in errors(found)
    assert "the pull request that withdraws" in the(found, "WDR005").message


# ------------------------------------------------------------------ PRF007: a missing unbind line (live T3)
BOUND_VIEW: R.Files = {
    "schema/views/sales.vw_Orders.sql": (
        "CREATE OR ALTER VIEW [sales].[vw_Orders] WITH SCHEMABINDING AS\n"
        "SELECT [OrderId], [Note] FROM [sales].[Order];\n"
    )
}
BOUND_ON_VIEW: R.Files = {
    "schema/views/sales.vw_Totals.sql": (
        "CREATE OR ALTER VIEW [sales].[vw_Totals] WITH SCHEMABINDING AS\n"
        "SELECT COUNT_BIG(*) AS [n] FROM [sales].[vw_Orders];\n"
    )
}
LONGER_NOTE = (
    "-- azsqlcd:allow ALTER_COLUMN_LOSSY [sales].[Order].[Note] reason: wider\n"
    "ALTER TABLE [sales].[Order] ALTER COLUMN [Note] nvarchar(400) NULL;\nGO\n"
)
UNBIND_ORDERS = "-- azsqlcd:unbind [sales].[vw_Orders]\n"


def test_a_hand_written_alter_column_under_a_schema_bound_view_without_unbind_fails_prf007(tmp_path: Path):
    # live: gen refused the change (an index used the column), the hand-written migration passed
    # lint, verify and build, and plan refused the merged release with TABLE_BLOCKER. A merged
    # migration cannot change: the way out was withdraw-and-replace.
    repo = start(tmp_path, "[Note] nvarchar(200) NULL", more=BOUND_VIEW)
    R.put(repo, {R.ORDER: R.order("[Note] nvarchar(400) NULL"), **one("0001__x.sql", LONGER_NOTE)})

    found = check(repo)

    assert errors(found) == [("PRF007", "migrations/0001__x.sql", 4)]
    message = the(found, "PRF007").message
    assert f"'{UNBIND_ORDERS.strip()}'" in message and "TABLE_BLOCKER" in message
    assert "[sales].[Order]" in message and "[Note]" in message and "gen --resum" in message


def test_a_migration_with_the_unbind_line_in_any_spelling_passes(tmp_path: Path):
    repo = start(tmp_path, "[Note] nvarchar(200) NULL", more=BOUND_VIEW)
    body = "-- azsqlcd:unbind [SALES].[VW_Orders]\n" + LONGER_NOTE
    R.put(repo, {R.ORDER: R.order("[Note] nvarchar(400) NULL"), **one("0001__x.sql", body)})

    assert [f.code for f in check(repo)] == []


def test_the_schema_bound_module_on_top_of_the_blocking_one_needs_its_unbind_line_too(tmp_path: Path):
    # the engine refuses the ALTER of vw_Orders while vw_Totals is bound to it
    repo = start(tmp_path, "[Note] nvarchar(200) NULL", more={**BOUND_VIEW, **BOUND_ON_VIEW})
    files = {R.ORDER: R.order("[Note] nvarchar(400) NULL"), **one("0001__x.sql", UNBIND_ORDERS + LONGER_NOTE)}
    R.put(repo, files)

    found = check(repo)

    assert errors(found) == [("PRF007", "migrations/0001__x.sql", 5)]
    assert "'-- azsqlcd:unbind [sales].[vw_Totals]'" in the(found, "PRF007").message


def test_a_statement_that_no_schema_bound_module_can_block_has_no_such_finding(tmp_path: Path):
    # ADD of a column: the engine binds a module to what it names, never to what comes later
    repo = start(tmp_path, more=BOUND_VIEW)
    R.put(repo, {R.ORDER: WITH_NOTE, **one("0001__x.sql", ADD_NOTE)})

    assert [f.code for f in check(repo)] == []


def test_a_schema_bound_module_that_does_not_name_the_column_blocks_no_statement_for_it(tmp_path: Path):
    # A schema-bound module names every column that it is bound to ('*' is not allowed in it), so
    # this is an error and not a warning: the view is bound to [OrderId] and [Note], not to [Code].
    repo = start(tmp_path, "[Note] nvarchar(200) NULL", "[Code] varchar(10) NULL", more=BOUND_VIEW)
    body = LONGER_NOTE.replace("[Note] nvarchar(400)", "[Code] varchar(20)").replace(".[Note]", ".[Code]")
    head = R.order("[Note] nvarchar(200) NULL", "[Code] varchar(20) NULL")
    R.put(repo, {R.ORDER: head, **one("0001__x.sql", body)})

    assert [f.code for f in check(repo)] == []


@pytest.mark.parametrize(
    ("head", "body"),
    [
        (
            R.order(),
            "-- azsqlcd:allow DROP_COLUMN [sales].[Order].[Note] reason: unused\n"
            "ALTER TABLE [sales].[Order] DROP COLUMN [Note];\nGO\n",
        ),
        (
            R.order("[Remark] nvarchar(200) NULL"),
            "-- azsqlcd:allow RENAME [sales].[Order].[Note] reason: the API name\n"
            "EXEC sys.sp_rename N'[sales].[Order].[Note]', N'Remark', N'COLUMN';\nGO\n",
        ),
        (
            None,
            "-- azsqlcd:allow DROP_TABLE [sales].[Order] reason: moved to another database\n"
            "DROP TABLE [sales].[Order];\nGO\n",
        ),
    ],
    ids=["drop column", "rename column", "drop table"],
)
def test_drop_column_rename_and_drop_table_need_the_unbind_line_too(
    tmp_path: Path, head: str | None, body: str
):
    repo = start(tmp_path, "[Note] nvarchar(200) NULL", more=BOUND_VIEW)
    R.put(repo, {R.ORDER: head, **one("0001__x.sql", body)})
    assert ("PRF007", "migrations/0001__x.sql", 4) in errors(check(repo))
    R.put(repo, one("0001__x.sql", UNBIND_ORDERS + body))
    assert "PRF007" not in [f.code for f in check(repo)]


def test_what_gen_writes_for_a_blocked_table_has_no_missing_unbind_line(tmp_path: Path):
    repo = start(tmp_path, "[Note] nvarchar(200) NULL", more={**BOUND_VIEW, **BOUND_ON_VIEW})
    R.put(repo, {R.ORDER: R.order("[Note] nvarchar(400) NULL")})
    gen.write_result(repo, gen.generate(repo, "main", "longer_note"))
    give_reasons(repo)
    assert [f.code for f in check(repo)] == []


# ------------------------------------------------------------------ TMB003: a tombstoned function (CU-10)
FN_VALID = "schema/functions/sales.fn_IsValid.sql"
FN_VALID_TEXT = (
    "CREATE OR ALTER FUNCTION [sales].[fn_IsValid] (@sku varchar(20))\n"
    "RETURNS bit\nAS\nBEGIN\n    RETURN 1;\nEND;\n"
)
TOMBSTONES = "schema/_tombstones.toml"
DROP_FN_VALID = '[[drop]]\nobject = "FUNCTION:[sales].[fn_IsValid]"\nreason = "no caller is left"\n'
SKU = "[Sku] varchar(20) NOT NULL"
CK_SKU = "CONSTRAINT [CK_Order_Sku] CHECK ([sales].[fn_IsValid]([Sku]) = 1)"


@pytest.mark.parametrize(
    ("table", "line", "what"),
    [
        (R.table("Order", R.ORDER_ID, SKU, R.PK_ORDER, CK_SKU), 5, "CHECK [CK_Order_Sku]"),
        (
            R.order(SKU, "[Ok] bit NOT NULL CONSTRAINT [DF_Order_Ok] DEFAULT ([sales].[fn_IsValid]('x'))"),
            4,
            "DEFAULT [DF_Order_Ok]",
        ),
        (R.order(SKU, "[Ok] AS ([sales].[fn_IsValid]([Sku]))"), 4, "computed column [Ok]"),
    ],
    ids=["check", "default", "computed column"],
)
def test_a_tombstone_for_a_function_that_a_table_file_still_uses_fails_tmb003(
    tmp_path: Path, table: str, line: int, what: str
):
    # The engine refuses DROP FUNCTION while a CHECK, a DEFAULT or a computed column uses the
    # function: without this finding the first deploy of the merged release is the one that fails.
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: table, FN_VALID: FN_VALID_TEXT})
    R.put(repo, {FN_VALID: None, TOMBSTONES: DROP_FN_VALID})

    found = check(repo)

    assert errors(found) == [("TMB003", R.ORDER, line)]
    message = the(found, "TMB003").message
    assert (
        "FUNCTION:[sales].[fn_IsValid]" in message and what in message and "TABLE:[sales].[Order]" in message
    )


def test_a_tombstone_for_a_function_whose_last_use_goes_in_the_same_pull_request_passes(tmp_path: Path):
    # the migration drops the constraint, and the release drops the function after its migrations
    table = R.table("Order", R.ORDER_ID, SKU, R.PK_ORDER, CK_SKU)
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: table, FN_VALID: FN_VALID_TEXT})
    drop = "ALTER TABLE [sales].[Order] DROP CONSTRAINT [CK_Order_Sku];\nGO\n"
    R.put(
        repo, {FN_VALID: None, TOMBSTONES: DROP_FN_VALID, R.ORDER: R.order(SKU), **one("0001__x.sql", drop)}
    )

    assert errors(check(repo)) == []


def test_a_function_with_a_tombstone_from_an_earlier_pull_request_is_not_read_again(tmp_path: Path):
    # TMB003 is a rule of the pull request that adds the tombstone, as DRP002 is
    base = {**R.SALES, R.ORDER: R.order(SKU), TOMBSTONES: DROP_FN_VALID}
    repo = R.new_repo(tmp_path / "r", base)
    R.put(
        repo,
        {
            R.ORDER: R.order(SKU, "[More] int NULL"),
            **one("0001__x.sql", ADD_NOTE.replace("[Note] nvarchar(200)", "[More] int")),
        },
    )

    assert errors(check(repo)) == []
