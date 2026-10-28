"""azsqlcd align: the script that makes a database that differs equal to the files, for a DBA to run.

Every call goes through cli.main against support.fake_database.FakeDatabase, as the closed loops do.
The test plays the DBA: it runs the batches of align.sql out of band. The fake runs table DDL,
module text and sp_rename. It has no constraint state and no engine-made names (its catalog rows
come from the model), so a test sets those flags in the rows that the reader gets, and applies the
effect of ALTER TABLE ... CHECK CONSTRAINT, ALTER INDEX ... REBUILD and sp_rename by taking the
flags away again. Then `baseline` must pass.
"""

import dataclasses
from pathlib import Path

import pytest

import support.fake_database as fake_database
from azsqlcd.model import Check, Column, Expression, Model, Schema, Table, TypeRef
from support.fake_database import FakeDatabase
from unit.test_catalog_tables import rows_from_model
from unit.test_closed_loop import (
    CUSTOMER,
    FUNCTION,
    ORDER,
    PROCEDURE,
    SALES,
    TARGET,
    VIEW,
    Built,
    Outcome,
    Project,
    release_1,
)

SCRATCH = Table("sales", "Scratch", (Column("Id", TypeRef("int"), False),))
OLD_VIEW = VIEW[1].replace("[OrderId], [Reference]", "[OrderId]")


@pytest.fixture
def project(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> Project:
    monkeypatch.chdir(tmp_path)  # a baseline that writes leaves its reports in the working directory
    return Project(tmp_path, capsys)


def align(project: Project, db: FakeDatabase, *extra: str) -> tuple[Outcome, str, str]:
    """One call of `azsqlcd align`; (outcome, align.sql, align.md)."""
    out = project.out("align")
    done = project.tool("align", "--root", str(project.repo), *TARGET, "--out", str(out), *extra, db=db)
    assert done.code == 0, done.err
    reports = out / "onboarding" / "dev"
    return done, (reports / "align.sql").read_text(), (reports / "align.md").read_text()


def run_as_dba(db: FakeDatabase, script: str) -> None:
    """A DBA runs align.sql batch by batch, and takes each statement under a REVIEW line after review."""
    for batch in script.split("\nGO\n"):
        lines: list[str] = []
        reviewed = False
        for line in batch.splitlines():
            if line.startswith("-- REVIEW:"):
                reviewed = True
            elif reviewed and line.startswith("-- "):
                lines.append(line.removeprefix("-- "))
            elif not line.startswith("--"):
                lines.append(line)
        text = "\n".join(lines).strip()
        if text:
            db.out_of_band(text)


def baseline(project: Project, built: Built, db: FakeDatabase) -> Outcome:
    out = project.out("report")
    return project.tool("baseline", *built.where, "--confirm-database", "sales", "--out", str(out), db=db)


def test_align_writes_what_makes_the_database_equal_to_the_files_and_then_baseline_passes(project):
    built = release_1(project)
    db = project.database()
    wide = dataclasses.replace(CUSTOMER, columns=(*CUSTOMER.columns, Column("Legacy", TypeRef("int"), True)))
    reference = Column("Reference", TypeRef("nvarchar", length=100), True)
    drifted = dataclasses.replace(
        ORDER, columns=(*ORDER.columns[:3], reference), indexes=()
    )  # a wider column, no index
    db.model = Model([SALES, wide, drifted, SCRATCH])
    db.out_of_band(FUNCTION[1], OLD_VIEW)  # the procedure is missing, the view is older
    before = (len(db.batches), list(db.ddl), db.rows("run"), db.rows("object"))

    done, script, report = align(project, db)

    # read-only: no DDL, no state row, and every batch of the call is a read or a session option
    assert (list(db.ddl), db.rows("run"), db.rows("object")) == before[1:]
    sent = db.batches[before[0] :]
    assert sent and all(batch.lstrip().startswith(("SET ", "/* azsqlcd:", "SELECT")) for batch in sent)
    assert "DROP TABLE" not in script and "[Scratch]" not in script  # only in the database: never dropped
    assert "CREATE NONCLUSTERED INDEX [IX_Order_Status] ON [sales].[Order] ([Status]);\nGO\n" in script
    assert (
        "-- REVIEW: loses data: drops column [Legacy] of [sales].[Customer]\n"
        "-- ALTER TABLE [sales].[Customer] DROP COLUMN [Legacy];\nGO\n"
    ) in script
    assert "-- REVIEW: loses data: column [Reference] of [sales].[Order] gets a type" in script
    assert FUNCTION[1] not in script  # equal to its file
    assert script.index(VIEW[1]) < script.index(PROCEDURE[1])  # the order of a deploy
    assert script.index("CREATE NONCLUSTERED INDEX") < script.index(VIEW[1])  # tables first
    assert "## Only in the database\n" in report and "TABLE:[sales].[Scratch]" in report
    assert "| Table-class statements | 3 |" in report and "| To review: loses data | 2 |" in report
    assert "| Modules | 2 |" in report
    assert "azsqlcd baseline" in report and "maintenance window" in report
    assert "statement(s), 2 module(s)" in done.out

    early = baseline(project, built, db)
    assert early.code == 22  # refused: the view differs and the tables do not read back as the files

    run_as_dba(db, script)

    recorded = baseline(project, built, db)
    assert recorded.code == 0, recorded.err


def test_align_of_a_database_that_equals_the_files_writes_nothing_to_align(project):
    built = release_1(project)
    db = project.database()
    assert project.deploy(built, db).code == 0

    _, script, report = align(project, db)

    assert script == "-- nothing to align\n"
    assert "Nothing to align." in report


CHECKED_CUSTOMER = dataclasses.replace(
    CUSTOMER,
    constraints=(*CUSTOMER.constraints, Check("CK_Customer_Id", Expression.from_sql("([CustomerId]>(0))"))),
)
CHECKED_ORDER = dataclasses.replace(
    ORDER, constraints=(*ORDER.constraints, Check("CK_Order_Status", Expression.from_sql("([Status]<(9))")))
)
COUNTS = {"[sales].[Customer]": 20, "[sales].[Order]": 500}


class Counted(FakeDatabase):
    """A database whose tables hold rows: catalog.table_facts gives COUNTS."""

    def _q_table_facts(self, toks):
        found = super()._q_table_facts(toks)
        return [(key, COUNTS[key.partition(":")[2]], 0) for key, _, _ in found]


def test_constraint_state_and_engine_names_give_their_statements_smallest_table_first(project, monkeypatch):
    project.write_objects(SALES, CHECKED_CUSTOMER, CHECKED_ORDER)
    built = project.release("checked tables", "create_checked")
    script_of_setup = project.tool("setup-sql", *TARGET, "--root", str(project.repo))
    db = Counted("sales", setup_sql=script_of_setup.out)
    db.model = Model([SALES, CHECKED_CUSTOMER, CHECKED_ORDER])

    def flagged(model):
        rows = rows_from_model(model)
        for row in rows["checks"]:
            row["is_disabled"] = row["is_not_trusted"] = 1  # ALTER TABLE ... NOCHECK CONSTRAINT
        for row in rows["foreign_keys"]:
            row["is_not_trusted"] = 1  # enabled again WITH NOCHECK
        for row in rows["indexes"]:
            if row["name"] == "IX_Order_Status":
                row["is_disabled"] = 1
            if row["name"] == "PK_Customer":
                row["name"], row["constraint_is_system_named"] = "PK__Customer__3214EC07", 1
        return rows

    monkeypatch.setattr(fake_database, "rows_from_model", flagged)
    early = baseline(project, built, db)
    assert early.code == 22 and "CONSTRAINT_NAMES: " in early.err

    _, script, report = align(project, db)

    assert script.index("-- 1. Constraint names") < script.index("-- 4. Constraint state")
    assert "-- 2. Table-class objects" not in script and "-- 3. Modules" not in script
    assert (
        "EXEC sys.sp_rename N'[sales].[PK__Customer__3214EC07]', N'PK_Customer', N'OBJECT';\nGO\n" in script
    )
    state = script[script.index("-- 4. Constraint state") :]
    assert state.splitlines()[1:] == [
        "-- [sales].[Customer]: 20 rows",
        "ALTER TABLE [sales].[Customer] WITH CHECK CHECK CONSTRAINT [CK_Customer_Id];",
        "GO",
        "-- [sales].[Order]: 500 rows",
        "ALTER INDEX [IX_Order_Status] ON [sales].[Order] REBUILD;",
        "GO",
        "-- [sales].[Order]: 500 rows",
        "ALTER TABLE [sales].[Order] WITH CHECK CHECK CONSTRAINT [CK_Order_Status];",
        "GO",
        "-- [sales].[Order]: 500 rows",
        "ALTER TABLE [sales].[Order] WITH CHECK CHECK CONSTRAINT [FK_Order_Customer];",
        "GO",
    ]
    assert "| Constraint names | 1 |" in report and "| Constraint state | 4 |" in report

    # the fake runs none of these statements: their effect is the catalog without the flags
    monkeypatch.setattr(fake_database, "rows_from_model", rows_from_model)
    recorded = baseline(project, built, db)
    assert recorded.code == 0, recorded.err


def test_a_change_that_gen_refuses_is_hand_written_and_the_other_objects_still_get_statements(project):
    release_1(project)
    db = project.database()
    narrow = dataclasses.replace(
        CUSTOMER,
        columns=(
            CUSTOMER.columns[0],
            dataclasses.replace(CUSTOMER.columns[1], type=TypeRef("nvarchar", length=50)),
        ),
    )
    other = dataclasses.replace(ORDER.columns[3], collation="Latin1_General_100_BIN2")
    collated = dataclasses.replace(ORDER, columns=(*ORDER.columns[:3], other), indexes=())
    db.model = Model([SALES, narrow, collated])

    _, script, report = align(project, db)

    assert "ALTER TABLE [sales].[Customer] ALTER COLUMN [Name] nvarchar(100) NOT NULL;\nGO\n" in script
    # no statement for an object that gen refuses: also not the index that it lacks
    assert "ALTER TABLE [sales].[Order]" not in script and "[IX_Order_Status]" not in script
    hand = report[report.index("## Hand-write") :]
    assert "TABLE:[sales].[Order]" in hand and "COLLATION_CHANGE" in hand


def test_align_of_one_schema_reads_and_writes_that_schema_only(project):
    release_1(project)
    project.write_objects(Schema("bal"), Table("bal", "Balance", (Column("Id", TypeRef("int"), False),)))
    db = project.database()
    db.model = Model([SALES, CUSTOMER])  # [sales].[Order] and the whole schema bal are missing

    _, script, report = align(project, db, "--schema", "bal")

    assert "CREATE SCHEMA [bal]" in script and "CREATE TABLE [bal].[Balance]" in script
    assert "[sales]" not in script
