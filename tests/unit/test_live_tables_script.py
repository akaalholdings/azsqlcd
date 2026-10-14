"""The pure parts of scripts/live_tables.py: the repository of the table-model scenario.

No test here connects to a database. What must hold before the owner runs the script: the table
files that it writes are canonical, `gen` writes a migration for each structural release, and the
pull-request check has no error for it. A file that the parser refuses would stop the live run
after the database was reset.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from azsqlcd import chain, gen, lex, names, release, state
from azsqlcd.config import load_config
from azsqlcd.model import Partition, Schema, Table
from azsqlcd.session import AccessToken
from support.fake_database import FakeDatabase, FakeDbSession
from unit.test_live_scripts import DATABASE, SERVER, _load, live_acceptance

live_tables = _load("live_tables")


TOML = live_acceptance.config_toml(SERVER, DATABASE).replace("table_model = false", "table_model = true")


@pytest.fixture
def repo(tmp_path: Path):
    toml = TOML
    made = live_tables.Repo(tmp_path / "repository")
    made.write(release.CONFIG_PATH, toml)
    made.write(chain.SUM_PATH, chain.format_sum(chain.Chain(False, ())))
    made.write(chain.TOMBSTONES_PATH, "# no module is dropped yet\n")
    made.commit("r1: the repository")
    return made


def generated(repo, name: str) -> str:
    """gen, verify and commit, as release_of does without a database. Returns the migration text."""
    _, text = live_tables.write_migration(repo, name)
    assert live_tables.verify_errors(repo) == ()
    repo.commit(name)
    return text


def test_gen_writes_the_migration_of_each_structural_release_of_the_scenario(repo):
    t = live_tables
    repo.write_object("SCHEMA", None, t.SCHEMA, t.SCHEMA_FILE)
    repo.write_object("TABLE", t.SCHEMA, "Customer", t.CUSTOMER_FILE)
    repo.write_object("TABLE", t.SCHEMA, "Order", t.order_file())
    repo.write(t.VIEW_PATH, t.view_file("[OrderId], [Note]"))
    created = generated(repo, "create_tm")
    # the foreign key comes after both tables: the order of the files does not decide it
    assert created.index(f"CREATE TABLE {t.CUSTOMER}") < created.index("ADD CONSTRAINT [FK_Order_Customer]")
    assert created.index(f"CREATE TABLE {t.ORDER}") < created.index("ADD CONSTRAINT [FK_Order_Customer]")
    assert "CREATE NONCLUSTERED INDEX [IX_Order_Customer]" in created

    repo.write_object("TABLE", t.SCHEMA, "Order", t.order_file(new=True))
    added = generated(repo, "order_priority")
    assert "ADD [Priority] tinyint NOT NULL" in added and "WHERE ([Status] > (0))" in added
    assert "CREATE TABLE" not in added  # only what the first migration did not do

    repo.write_object("TABLE", t.SCHEMA, "Order", t.order_file(new=True, hotfix=True))
    assert "ADD [Region] char(2) NULL" in generated(repo, "order_region")


def test_a_table_file_of_the_scenario_is_written_in_its_canonical_form():
    t = live_tables
    path = f"schema/tables/{t.SCHEMA}.Order.sql"
    once = t.canonical(path, t.order_file(new=True, hotfix=True))
    assert t.canonical(path, once) == once
    assert gen.table_file_check(path, once) == []


# ------------------------------------------------------------------ the pass for partitioned tables
def test_gen_writes_the_migration_of_each_release_of_the_partitioned_pass(repo):
    t = live_tables
    on_year = "ON [PS_azsqlcd_tm_year]([OrderDate])"
    repo.write_object("SCHEMA", None, t.SCHEMA, t.SCHEMA_FILE)
    repo.write_object("TABLE", t.SCHEMA, "OrderLineStage", t.stage_file())
    repo.write_object("TABLE", t.SCHEMA, "OrderLine", t.line_file())
    repo.write_object("TABLE", t.SCHEMA, "FactOrderLine", t.FACT_FILE)
    created = generated(repo, "create_partitioned")
    # a heap, a clustered key and a columnstore index on the scheme; an aligned index; two on [PRIMARY]
    assert created.count(on_year) == 7 and created.count("ON [PRIMARY]") == 2
    assert f"CREATE CLUSTERED COLUMNSTORE INDEX [CCI_FactOrderLine] ON {t.FACT}" in created
    assert "PARTITION FUNCTION" not in created and "PARTITION SCHEME" not in created  # a DBA owns them

    repo.write_object("TABLE", t.SCHEMA, "OrderLineStage", t.stage_file(compressed=True))
    repo.write_object("TABLE", t.SCHEMA, "OrderLine", t.line_file(compressed=True))
    compressed = generated(repo, "compress_partitioned")
    assert f"ALTER TABLE {t.STAGE} REBUILD PARTITION = ALL WITH (DATA_COMPRESSION = PAGE" in compressed
    assert "DROP INDEX [IX_OrderLine_Product]" in compressed
    assert compressed.index("DROP INDEX") < compressed.index(
        "CREATE NONCLUSTERED INDEX [IX_OrderLine_Product]"
    )
    assert f"WITH (DATA_COMPRESSION = PAGE) {on_year};" in compressed

    repo.write_object("TABLE", t.SCHEMA, "OrderLineStage", t.stage_file(compressed=True, columnstore=True))
    repo.write_object("TABLE", t.SCHEMA, "OrderLine", t.line_file(compressed=True, implicit=True))
    repo.write_object("TABLE", t.SCHEMA, "OrderLineArchive", t.ARCHIVE_FILE)
    implicit = generated(repo, "implicit_column")
    assert "WITH (STATISTICS_INCREMENTAL = ON)" in implicit
    assert "CREATE NONCLUSTERED COLUMNSTORE INDEX [NCCI_OrderLineStage]" in implicit
    assert f"CREATE CLUSTERED INDEX [CIX_OrderLineArchive] ON {t.ARCHIVE} ([OrderLineId])" in implicit
    assert "CREATE TABLE [azsqlcd_tm].[OrderLine] " not in implicit  # only what is new


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("OrderLineStage", live_tables.stage_file()),
        ("OrderLineStage", live_tables.stage_file(compressed=True, columnstore=True)),
        ("OrderLine", live_tables.line_file()),
        ("OrderLine", live_tables.line_file(compressed=True, implicit=True)),
        ("FactOrderLine", live_tables.FACT_FILE),
        ("OrderLineArchive", live_tables.ARCHIVE_FILE),
    ],
)
def test_a_partitioned_table_file_of_the_scenario_is_written_in_its_canonical_form(name: str, text: str):
    t = live_tables
    path = f"schema/tables/{t.SCHEMA}.{name}.sql"
    once = t.canonical(path, text)
    assert t.canonical(path, once) == once
    assert gen.table_file_check(path, once) == []
    assert "ON [PS_azsqlcd_tm_year]([OrderDate])" in once


def test_the_script_makes_and_drops_a_partition_function_and_scheme_with_its_own_prefix():
    t = live_tables
    assert all(t.SCHEMA in name for name in (t.PF_YEAR, t.PS_YEAR, t.PS_ABSENT))
    function, scheme = t.CREATE_PARTITIONING
    assert function.startswith(f"CREATE PARTITION FUNCTION [{t.PF_YEAR}] (date) AS RANGE RIGHT FOR VALUES (")
    # Azure SQL Database has one filegroup
    assert scheme == f"CREATE PARTITION SCHEME [{t.PS_YEAR}] AS PARTITION [{t.PF_YEAR}] ALL TO ([PRIMARY]);"
    assert t.PS_ABSENT not in "".join(t.CREATE_PARTITIONING)  # the scheme that never exists
    drop_scheme, drop_function = t.drop_partitioning_sql()  # the scheme first: it holds the function
    assert drop_scheme.endswith(f"DROP PARTITION SCHEME [{t.PS_YEAR}];") and "IF EXISTS" in drop_scheme
    assert drop_function.endswith(f"DROP PARTITION FUNCTION [{t.PF_YEAR}];") and "IF EXISTS" in drop_function
    assert all(t.PF_YEAR in batch or t.PS_YEAR in batch for batch in t.SPLIT)


class Recorder:
    """A session that keeps what was sent and answers nothing."""

    def __init__(self) -> None:
        self.batches: list[str] = []

    def execute(self, batch: str) -> list:
        self.batches.append(batch)
        return []


def test_a_reset_drops_the_tables_before_the_scheme_and_the_scheme_before_the_function():
    session = Recorder()

    live_tables.reset(session)

    sent = "\n".join(session.batches)
    assert sent.index("azsqlcd:live_objects") < sent.index("DROP PARTITION SCHEME")
    assert sent.index("DROP PARTITION SCHEME") < sent.index("DROP PARTITION FUNCTION")
    assert sent.index("DROP PARTITION FUNCTION") < sent.index("DROP TABLE [azsqlcd].[object]")


def test_the_partitioned_pass_runs_first_and_the_plain_pass_leaves_its_state_as_before():
    t = live_tables
    assert [title.split(":")[0] for title, _ in t.PARTITION_SCENARIOS] == ["P1", "P2", "P3", "P4", "P5"]
    source = (Path(t.__file__)).read_text(encoding="utf-8")
    assert source.index('("partitioned", PARTITION_SCENARIOS)') < source.index('("plain", SCENARIOS)')


# ------------------------------------------------------------------ a rehearsal on the fake engine
class Tokens:
    def get(self) -> AccessToken:
        return AccessToken("the-access-token-of-the-test", int(time.time()) + 3600)


class Owner:
    """The owner session of a rehearsal. The fake database runs what the tool sends and the DDL
    that the owner writes by hand. The reads of the owner and the statements that the fake engine
    does not model (partition function and scheme, rows of a table) are answered here, from the
    model of the fake."""

    def __init__(self, db: FakeDatabase) -> None:
        self.db, self.inner, self.splits, self.closed = db, FakeDbSession(db, 99, recorded=False), 0, False

    def close(self) -> None:
        self.closed = True
        self.inner.close()

    def _table(self, batch: str) -> Table:
        literals = [tok.value for tok in lex.tokenize(batch) if tok.kind == "nstring"]
        literal = next(value for value in literals if value.startswith("["))  # OBJECT_ID(N'[s].[t]')
        schema, name = (tok.value for tok in lex.significant(lex.tokenize(literal)) if tok.kind == "bident")
        table = self.db.model[names.object_key("TABLE", schema, name)]
        assert isinstance(table, Table)
        return table

    def _data_spaces(self, table: Table) -> list[tuple[Any, ...]]:
        def space(name: str, on: object) -> tuple[Any, ...]:
            return (name, "PS", on.scheme) if isinstance(on, Partition) else (name, "FG", "PRIMARY")

        found = [] if table.clustered_item() is not None else [space("", table.on)]
        return found + [space(item.name or "", item.on) for _, item in table.placed_items()]

    def _compression(self, table: Table, batch: str) -> list[tuple[Any, ...]]:
        value = table.compression
        if "i.[index_id] = 0" not in batch:
            name = [tok.value for tok in lex.tokenize(batch) if tok.kind == "nstring"][-1]
            index = table.index(name)
            assert index is not None
            value = dict(index.options).get("DATA_COMPRESSION")
        code = {None: 0, "NONE": 0, "ROW": 1, "PAGE": 2}[value]
        return [(code,)] * (live_tables.PARTITIONS + self.splits)

    def execute(self, batch: str) -> list[list[tuple[Any, ...]]]:
        t, text = live_tables, " ".join(batch.split())
        if text.startswith("CREATE PARTITION SCHEME"):
            self.db.partition_schemes.add(t.PS_YEAR)
        elif text.startswith("ALTER PARTITION FUNCTION"):
            self.splits += 1
        elif text.startswith(
            ("CREATE PARTITION FUNCTION", "ALTER PARTITION SCHEME", "INSERT INTO [azsqlcd_tm]")
        ):
            pass
        elif text.startswith("SELECT SCHEMA_ID("):
            exists = any(isinstance(obj, Schema) and obj.name == t.SCHEMA for obj in self.db.model.values())
            return [[(1 if exists else None,)]]
        elif text == "SELECT COUNT(*) FROM [azsqlcd].[run];":
            return [[(len(self.db.rows("run")),)]]
        elif text.startswith("SELECT COUNT(*) FROM [azsqlcd_tm]"):
            return [[(3,)]]
        elif "FROM sys.indexes AS i JOIN sys.data_spaces" in text:
            return [self._data_spaces(self._table(batch))]
        elif "SELECT pt.[data_compression]" in text:
            return [self._compression(self._table(batch), batch)]
        elif "st.[is_incremental]" in text:
            return [[(1,)]]
        elif "JOIN sys.index_columns AS ic" in text and not text.startswith("/* azsqlcd:"):
            return [[]]
        else:
            return self.inner.execute(batch)
        return []


def test_the_partitioned_pass_passes_on_the_fake_engine_of_the_unit_tests(repo, tmp_path):
    # A rehearsal of the wiring: every call of the tool is real (gen, verify, build, plan, deploy,
    # read-back, drift), the engine is the fake. It does not say what Azure SQL Database answers.
    t = live_tables
    config = load_config(TOML)
    db = FakeDatabase(DATABASE, setup_sql=state.setup_sql(config, t.ENV, t.TARGET))
    checks: list[Any] = []
    scenario = t.Scenario(
        database=DATABASE,
        config=config,
        bundles=[],
        trees=[],
        connect=db.session,
        provider=Tokens(),
        inspector=Owner(db),
        tool_digest=release.tool_digest(),
        checks=checks,
    )

    t.run_scenarios(scenario, repo, tmp_path / "releases", t.PARTITION_SCENARIOS)

    assert [(check.result, check.name, check.detail) for check in checks if check.result != "pass"] == []
    assert len(checks) == 17 and db.unknown == []
    assert [check.seen for check in checks[1:3]] == ["22 PARTITION_SCHEME_MISSING"] * 2
    # the tool sent no statement for a partition function or scheme
    assert not any("PARTITION FUNCTION" in batch or "PARTITION SCHEME" in batch for batch in db.ddl)
