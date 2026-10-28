"""Transitive dependants (A35) through the command line on FakeDatabase, which keeps the references
of each module, also to views: a module on a module on a changed table, and the dependants of a
module that the release changes, are refreshed in dependency order and checked after the change.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from azsqlcd import names
from unit.test_closed_loop import (
    FUNCTION,
    ORDER_NO_REFERENCE,
    ORDER_WIDE,
    PROCEDURE,
    PROCEDURE_KEY,
    TOML,
    VIEW,
    VIEW_KEY,
    VIEW_NO_REFERENCE,
    Project,
    deployed,
    release_1,
)

TABLE = names.object_key("TABLE", "sales", "Order")


def view(name: str) -> str:
    return names.object_key("VIEW", "sales", name)


# ------------------------------------------------------------------ closed loop
@pytest.fixture
def project(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Project:
    return Project(tmp_path, capsys)


OPEN_REFS = (
    "schema/views/sales.vw_OpenRefs.sql",
    "CREATE OR ALTER VIEW [sales].[vw_OpenRefs] AS\nSELECT [Reference] FROM [sales].[vw_OpenOrders];\n",
)
OPEN_REFS_KEY = view("vw_OpenRefs")
REFRESH_REFS = "EXEC sys.sp_refreshsqlmodule @name = N'[sales].[vw_OpenRefs]';"
REFRESH_OPEN = "EXEC sys.sp_refreshsqlmodule @name = N'[sales].[vw_OpenOrders]';"


def with_open_refs(project):
    project.write(*OPEN_REFS)
    first = release_1(project)
    return first, deployed(project, first)


def test_a_view_on_a_view_on_a_changed_table_is_refreshed_after_it_and_shown_with_its_path(project):
    _, db = with_open_refs(project)
    project.write_objects(ORDER_WIDE)
    wide = project.release("a longer reference", "widen_reference")

    plan = project.tool("plan", *wide.where, "--out", str(project.out("plan")), db=db)

    assert plan.code == 0, plan.err
    assert f"dependant to check: {OPEN_REFS_KEY} (findings before the change: none)" in plan.out
    assert f"note: dependant through other modules: {OPEN_REFS_KEY} -> {VIEW_KEY} -> {TABLE}" in plan.out
    assert plan.out.index(f"refresh refresh:{VIEW_KEY}") < plan.out.index(f"refresh refresh:{OPEN_REFS_KEY}")
    sent = len(db.ddl)
    assert project.deploy(wide, db).code == 0
    assert db.ddl[sent:][-2:] == [REFRESH_OPEN, REFRESH_REFS]


def test_a_view_that_selects_a_dropped_column_through_a_changed_view_fails_the_deploy(project):
    """Before A35 the deploy ended ok and left vw_OpenRefs broken: it is no direct dependant of the
    table, and the release changed vw_OpenOrders, whose dependants nothing checked."""
    _, db = with_open_refs(project)
    before = (db.model, dict(db.modules), db.rows("step"), db.rows("object"))
    project.write_objects(ORDER_NO_REFERENCE)
    project.write(*VIEW_NO_REFERENCE)  # the view is fixed; the view on the view is not
    third = project.release("drop the reference", "drop_reference")

    failed = project.deploy(third, db)

    assert (failed.code, failed.reason) == (21, "DEPENDANT_BROKEN"), failed.err
    assert OPEN_REFS_KEY in failed.err
    assert project.report()["failed_step"] == f"refresh:{OPEN_REFS_KEY}"
    assert (db.model, db.modules, db.rows("step"), db.rows("object")) == before


def test_a_release_that_changes_only_a_view_refreshes_and_checks_the_views_on_it(project):
    _, db = with_open_refs(project)
    project.write(VIEW[0], VIEW[1].replace("WHERE [Status] = 0", "WHERE [Status] IN (0, 1)"))
    changed = project.release("open means 0 or 1")
    sent = len(db.ddl)

    assert project.deploy(changed, db).code == 0
    assert db.ddl[sent:] == [db.module(VIEW_KEY).sent, REFRESH_REFS]

    before = (dict(db.modules), db.rows("object"))
    project.write(*VIEW_NO_REFERENCE)  # vw_OpenRefs still selects [Reference] of the view
    breaking = project.release("the view loses the reference")
    failed = project.deploy(breaking, db)

    assert (failed.code, failed.reason) == (21, "DEPENDANT_BROKEN"), failed.err
    assert project.report()["failed_step"] == f"refresh:{OPEN_REFS_KEY}"
    assert (db.modules, db.rows("object")) == before


UNMANAGED_REFS = "CREATE VIEW [sales].[vw_Shadow] AS\nSELECT [Reference] FROM [sales].[vw_OpenOrders];\n"


def shadowed(project, ack: str = ""):
    """Release 1 deployed, an unmanaged view on the managed view, and a release that widens the
    column that both views select."""
    project.write("azsqlcd.toml", TOML.format(table_model="true") + ack)
    db = deployed(project, release_1(project))
    db.out_of_band(UNMANAGED_REFS)
    project.write_objects(ORDER_WIDE)
    return db, project.release("a longer reference", "widen_reference")


def test_an_unmanaged_view_on_a_managed_view_blocks_the_change_of_the_table_until_it_is_acknowledged(
    tmp_path, capsys
):
    project = Project(tmp_path / "a", capsys)
    db, wide = shadowed(project)

    plan = project.tool("plan", *wide.where, "--out", str(project.out("plan")), db=db)

    assert (plan.code, plan.reason) == (22, "TABLE_BLOCKER")
    assert (
        f"{view('vw_Shadow')} uses [sales].[Order] through {VIEW_KEY} and is not managed. If the change of "
        '[sales].[Order].[Reference] is safe for it, add "[sales].[vw_Shadow] -> [sales].[Order].[Reference]"'
    ) in plan.err

    project = Project(tmp_path / "b", capsys)
    ack = '\n[ack]\nunmanaged_dependants = ["[sales].[vw_Shadow] -> [sales].[Order].[Reference]"]\n'
    db, wide = shadowed(project, ack)
    sent = len(db.ddl)
    assert project.deploy(wide, db).code == 0
    assert not any("vw_Shadow" in batch for batch in db.ddl[sent:])  # an unmanaged view is not refreshed


BOUND_ON_BOUND = (
    "CREATE VIEW [sales].[vw_Bound] WITH SCHEMABINDING AS\n"
    "SELECT [OrderId], [Reference] FROM [sales].[Order];\n",
    "CREATE VIEW [sales].[vw_BoundTop] WITH SCHEMABINDING AS\nSELECT [Reference] FROM [sales].[vw_Bound];\n",
)


def test_a_schema_bound_blocker_shows_the_whole_schema_bound_chain(project):
    first = release_1(project)
    db = deployed(project, first)
    db.out_of_band(*BOUND_ON_BOUND)
    project.write_objects(ORDER_WIDE)
    wide = project.release("a longer reference", "widen_reference")

    plan = project.tool("plan", *wide.where, "--out", str(project.out("plan")), db=db)

    assert (plan.code, plan.reason) == (22, "TABLE_BLOCKER")
    assert (
        f"{view('vw_Bound')} is schema-bound to [sales].[Order].[Reference] and is not managed "
        f"(schema-bound chain: {view('vw_BoundTop')} -> {view('vw_Bound')})"
    ) in plan.err


TOMBSTONE = '[[drop]]\nobject = "{}"\nreason = "gone"\n'


def dropping_the_view(project):
    """Release 1 with vw_OpenRefs deployed, the procedure on vw_OpenOrders tombstoned, then a release
    that tombstones vw_OpenOrders too. vw_OpenRefs still selects from it."""
    _, db = with_open_refs(project)
    (project.repo / PROCEDURE[0]).unlink()
    project.write("schema/_tombstones.toml", TOMBSTONE.format(PROCEDURE_KEY))
    assert project.deploy(project.release("the procedure is gone"), db).code == 0
    (project.repo / VIEW[0]).unlink()
    project.write("schema/_tombstones.toml", TOMBSTONE.format(PROCEDURE_KEY) + TOMBSTONE.format(VIEW_KEY))
    return db


def test_a_release_that_drops_a_view_with_a_managed_view_on_it_fails_the_deploy(project):
    """Before the fix the drop of a module was no root of the search: the plan listed no dependant,
    and the deploy ended ok with vw_OpenRefs left broken."""
    db = dropping_the_view(project)
    before = (dict(db.modules), db.rows("object"))
    dropping = project.release("drop the view in the middle")

    plan = project.tool("plan", *dropping.where, "--out", str(project.out("plan")), db=db)

    assert plan.code == 0, plan.err
    assert f"dependant to check: {OPEN_REFS_KEY} (findings before the change: none)" in plan.out
    assert f"refresh refresh:{OPEN_REFS_KEY}" in plan.out
    failed = project.deploy(dropping, db)
    assert (failed.code, failed.reason) == (21, "DEPENDANT_BROKEN"), failed.err
    assert "ERROR_208" in failed.err
    assert project.report()["failed_step"] == f"refresh:{OPEN_REFS_KEY}"
    assert (db.modules, db.rows("object")) == before


def test_a_release_that_drops_a_view_and_changes_another_module_checks_the_views_on_the_dropped_one(project):
    db = dropping_the_view(project)
    project.write(FUNCTION[0], FUNCTION[1].replace("[Status] = 0", "[Status] = 1"))
    before = (dict(db.modules), db.rows("object"))

    failed = project.deploy(project.release("drop the view and change the function"), db)

    assert (failed.code, failed.reason) == (21, "DEPENDANT_BROKEN"), failed.err
    assert project.report()["failed_step"] == f"refresh:{OPEN_REFS_KEY}"
    assert (db.modules, db.rows("object")) == before


LAST_REF = (
    "schema/procedures/sales.usp_LastRef.sql",
    "CREATE OR ALTER PROCEDURE [sales].[usp_LastRef] AS\n"
    "SELECT MAX([Reference]) AS [r] FROM [sales].[vw_OpenOrders];\n",
)
MODULE_FIX = "Change this module (or tombstone it) in the same pull request as the module that it uses"


def test_a_release_with_no_migration_that_breaks_a_procedure_on_a_changed_view_names_the_module_fix(project):
    """The message spoke of a table change and told the operator to withdraw a migration that this
    release does not have."""
    project.write(*LAST_REF)
    db = deployed(project, release_1(project))
    before = (dict(db.modules), db.rows("object"))
    project.write(*VIEW_NO_REFERENCE)

    failed = project.deploy(project.release("the view loses the reference"), db)

    assert (failed.code, failed.reason) == (21, "DEPENDANT_BROKEN"), failed.err
    assert project.report()["failed_step"] == "dependants"
    assert "PROCEDURE:[sales].[usp_LastRef] does not bind after the change of a module it uses" in failed.err
    assert MODULE_FIX in failed.err and "withdraw the migration" not in failed.err
    assert (db.modules, db.rows("object")) == before


def test_a_release_with_no_migration_whose_view_refresh_fails_names_the_module_fix(project):
    _, db = with_open_refs(project)
    project.write(*VIEW_NO_REFERENCE)

    failed = project.deploy(project.release("the view loses the reference"), db)

    assert (failed.code, failed.reason) == (21, "DEPENDANT_BROKEN"), failed.err
    assert (
        f"{OPEN_REFS_KEY} does not bind after the change of a module it uses: its refresh failed"
        in failed.err
    )
    assert MODULE_FIX in failed.err and "withdraw the migration" not in failed.err
