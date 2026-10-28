"""azsqlcd align: the script that makes a database that differs equal to the files, for a DBA to run.

Every call goes through cli.main against support.fake_database.FakeDatabase, as the closed loops do.
The test plays the DBA: it runs the batches of align.sql out of band. The fake runs table DDL,
module text and sp_rename. It has no constraint state and no engine-made names (its catalog rows
come from the model), so a test sets those flags in the rows that the reader gets, and applies the
effect of ALTER TABLE ... CHECK CONSTRAINT, ALTER INDEX ... REBUILD and sp_rename by taking the
flags away again. Then `baseline` must pass.
"""

import dataclasses
import time
from pathlib import Path
from typing import Any

import pytest

import support.fake_database as fake_database
from azsqlcd import align as align_module
from azsqlcd.catalog_tables import CatalogRead
from azsqlcd.model import Check, Column, Expression, KeyColumn, Model, PrimaryKey, Schema, Table, TypeRef
from support.fake_database import FakeDatabase
from unit.test_catalog_tables import price, rows_from_model
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

    counts = COUNTS

    def _q_table_facts(self, toks):
        found = super()._q_table_facts(toks)
        return [(key, self.counts[key.partition(":")[2]], 0) for key, _, _ in found]


class LargeCustomer(Counted):
    """The table that a foreign key references holds more rows than the table of the foreign key."""

    counts = {"[sales].[Customer]": 500, "[sales].[Order]": 20}


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
    assert state.splitlines()[1:] == [  # every rebuild first: see the test of the rebuild order
        "-- [sales].[Order]: 500 rows",
        "ALTER INDEX [IX_Order_Status] ON [sales].[Order] REBUILD;",
        "GO",
        "-- [sales].[Customer]: 20 rows",
        "ALTER TABLE [sales].[Customer] WITH CHECK CHECK CONSTRAINT [CK_Customer_Id];",
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


def test_an_engine_named_constraint_of_a_table_that_has_no_file_keeps_its_name(project, monkeypatch):
    release_1(project)
    db = project.database()
    loose = Table(
        "sales",
        "Loose",
        (Column("Id", TypeRef("int"), False),),
        (PrimaryKey("PK_Loose", True, (KeyColumn("Id"),)),),
    )
    db.model = Model([SALES, CUSTOMER, ORDER, loose])

    def flagged(model):
        rows = rows_from_model(model)
        for row in rows["indexes"]:
            if row["name"] == "PK_Loose":
                row["name"], row["constraint_is_system_named"] = "PK__Loose__3214EC07", 1
        return rows

    monkeypatch.setattr(fake_database, "rows_from_model", flagged)
    _, script, report = align(project, db)

    # no file gives the table a name for its key: the tool must not make one up
    assert "-- 1. Constraint names" not in script and "sp_rename" not in script
    assert "[Loose]" not in script
    assert report.count("TABLE:[sales].[Loose]") == 1
    assert "TABLE:[sales].[Loose]" in report[report.index("## Only in the database") :]


def test_every_rebuild_comes_before_every_constraint_check(project, monkeypatch):
    built = release_1(project)
    setup = project.tool("setup-sql", *TARGET, "--root", str(project.repo))
    db = LargeCustomer("sales", setup_sql=setup.out)
    db.model = Model([SALES, CUSTOMER, ORDER])
    db.out_of_band(*(text for _, text in (FUNCTION, VIEW, PROCEDURE)))

    def flagged(model):
        rows = rows_from_model(model)
        for row in rows["indexes"]:
            if row["name"] in ("PK_Customer", "PK_Order", "IX_Order_Status"):
                row["is_disabled"] = 1  # ALTER INDEX ... DISABLE of the keys of both tables
        for row in rows["foreign_keys"]:
            row["is_disabled"] = row["is_not_trusted"] = 1  # the engine disables the foreign key too
        return rows

    monkeypatch.setattr(fake_database, "rows_from_model", flagged)
    _, script, _ = align(project, db)

    # the referenced key is in the larger table: a foreign key cannot be enabled while the unique
    # index of its key is disabled, and no index can be rebuilt while the clustered index is disabled
    state = script[script.index("-- 4. Constraint state") :]
    assert state.splitlines()[1:] == [
        "-- [sales].[Order]: 20 rows",
        "ALTER INDEX [PK_Order] ON [sales].[Order] REBUILD;",
        "GO",
        "-- [sales].[Order]: 20 rows",
        "ALTER INDEX [IX_Order_Status] ON [sales].[Order] REBUILD;",
        "GO",
        "-- [sales].[Customer]: 500 rows",
        "ALTER INDEX [PK_Customer] ON [sales].[Customer] REBUILD;",
        "GO",
        "-- [sales].[Order]: 20 rows",
        "ALTER TABLE [sales].[Order] WITH CHECK CHECK CONSTRAINT [FK_Order_Customer];",
        "GO",
    ]
    monkeypatch.setattr(fake_database, "rows_from_model", rows_from_model)
    assert baseline(project, built, db).code == 0


@pytest.mark.parametrize("reverse", [False, True])
def test_the_report_does_not_follow_the_order_of_the_catalog_rows(project, monkeypatch, reverse):
    def replicated(table: Table, name: str, sql: str) -> Table:
        check = Check(name, Expression.from_sql(sql), not_for_replication=True)
        return dataclasses.replace(table, constraints=(*table.constraints, check))

    customer = replicated(CUSTOMER, "CK_Customer_N", "([CustomerId]>(0))")
    order = replicated(ORDER, "CK_Order_N", "([Status]<(9))")
    project.write_objects(SALES, customer, order)
    project.release("not for replication", "replicated")
    db = project.database()
    db.model = Model([SALES, customer, order])

    def flagged(model):
        rows = rows_from_model(model)
        for row in rows["checks"]:
            row["is_disabled"] = row["is_not_trusted"] = 1
        if reverse:
            rows["tabulars"].reverse()
        return rows

    monkeypatch.setattr(fake_database, "rows_from_model", flagged)
    _, _, report = align(project, db)

    hand = report[report.index("## Hand-write") : report.index("## Only in the database")]
    assert hand.index("TABLE:[sales].[Customer]") < hand.index("TABLE:[sales].[Order]")


# ------------------------------------------------------------------ align._tables and the files alone
def tables_of(live: list, files: list, **read: Any) -> align_module.AlignResult:
    found = align_module._tables(CatalogRead(Model(live), {}, (), (), **read), Model(files))
    return align_module._result("dev", "sales-dev", None, found, (), [], [])


def test_a_change_that_has_only_hand_write_items_is_not_nothing_to_align():
    temporal = price()
    plain = dataclasses.replace(temporal, temporal=None, columns=temporal.columns[:2])

    found = tables_of([SALES, plain], [SALES, temporal])

    assert [item.note.split(":")[0] for item in found.hand_write] == ["TEMPORAL_CHANGE"]
    assert found.sql == "-- no statement: see Hand-write in align.md\n"
    assert "Nothing to align." not in found.md


def test_a_mask_that_only_the_database_has_is_dropped_only_after_review():
    masked = dataclasses.replace(
        CUSTOMER, columns=(CUSTOMER.columns[0], dataclasses.replace(CUSTOMER.columns[1], masked="default()"))
    )

    found = tables_of([SALES, masked], [SALES, CUSTOMER])

    # every reader of the column sees the real values after it: a reviewer must accept that
    assert (
        "-- REVIEW: unmasks column [Name] of [sales].[Customer]\n"
        "-- ALTER TABLE [sales].[Customer] ALTER COLUMN [Name] DROP MASKED;\nGO\n"
    ) in found.sql
    review = found.md[found.md.index("## To review") : found.md.index("## Modules")]
    assert "TABLE:[sales].[Customer]" in review and "unmasks column [Name]" in review


def test_many_objects_only_in_the_database_take_linear_time():
    many = [Table("sales", f"T{n:05}", (Column("Id", TypeRef("int"), False),)) for n in range(4000)]

    started = time.monotonic()
    found = tables_of([SALES, *many], [SALES])
    elapsed = time.monotonic() - started

    assert len(found.only_here) == 4000 and found.sql == "-- nothing to align\n"
    assert elapsed < 3, elapsed  # 5.5 s when each object rebuilt the whole model
    assert "| TABLE | [sales] | 4000 |" in found.md


def test_a_file_for_the_name_of_an_external_or_a_history_table_is_hand_written():
    ext = Table("sales", "Ext", (Column("Id", TypeRef("int"), False),))
    history = Table("sales", "Price_History", (Column("Id", TypeRef("int"), False),))

    found = tables_of(
        [SALES],
        [SALES, ext, history],
        history_tables=(("TABLE:[sales].[Price_History]", "TABLE:[sales].[Price]"),),
        external_tables=("TABLE:[sales].[Ext]",),
    )

    assert "CREATE TABLE" not in found.sql
    assert {(item.object_key, item.note) for item in found.hand_write} == {
        ("TABLE:[sales].[Ext]", "an external table holds this name"),
        ("TABLE:[sales].[Price_History]", "the history table of TABLE:[sales].[Price] holds this name"),
    }
