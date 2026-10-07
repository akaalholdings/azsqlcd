"""The pure parts of scripts/live_tables.py: the repository of the table-model scenario.

No test here connects to a database. What must hold before the owner runs the script: the table
files that it writes are canonical, `gen` writes a migration for each structural release, and the
pull-request check has no error for it. A file that the parser refuses would stop the live run
after the database was reset.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from azsqlcd import chain, gen, release
from unit.test_live_scripts import DATABASE, SERVER, _load, live_acceptance

live_tables = _load("live_tables")


@pytest.fixture
def repo(tmp_path: Path):
    toml = live_acceptance.config_toml(SERVER, DATABASE).replace("table_model = false", "table_model = true")
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
