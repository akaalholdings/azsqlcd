"""[env.<name>] not_managed: objects that one environment does not manage yet (not_managed.py).

In that environment the matching objects are outside the scope, as a pattern of [scope]
exclude_objects puts them, for that environment only. A pending migration that touches only such
objects is set aside there for good (reason not_managed); one that touches managed objects too is
refused (MIXED_NOT_MANAGED). The skip is a migration step with the status not_applied and the note
set_aside.skip_note(env): no column, status or constraint was added.
"""

import dataclasses

import pytest

from azsqlcd import chain, not_managed, set_aside
from azsqlcd.config import Scope, for_env, in_scope, load_config, out_of_scope
from azsqlcd.errors import Exit, ToolError
from azsqlcd.state import StepRow

VALID = """
[project]
name = "sales"
tenant_id = "11111111-2222-3333-4444-555555555555"
table_model = false
min_token_minutes = 20

[identities]
plan = "aaaaaaaa-0000-0000-0000-000000000001"
deploy = "aaaaaaaa-0000-0000-0000-000000000002"

[env.dev]
plan_identity = "plan"
deploy_identity = "deploy"
drift = "report"
lock_timeout_ms = 30000
applock_wait_s = 600
job_timeout_minutes = 120
targets = [{ id = "sales-dev", server = "sql-sales-dev.database.windows.net", database = "sales" }]

[env.prod]
plan_identity = "plan"
deploy_identity = "deploy"
drift = "block"
lock_timeout_ms = 10000
applock_wait_s = 600
job_timeout_minutes = 120
targets = [{ id = "sales-prod", server = "sql-sales-prod.database.windows.net", database = "sales" }]
"""


def with_not_managed(text: str, env: str = "dev", value: str = '["legacy.*"]') -> str:
    """VALID with a not_managed line in [env.<env>]."""
    head = f"[env.{env}]\n"
    return text.replace(head, f"{head}not_managed = {value}\n", 1)


# ------------------------------------------------------------------ config
def test_not_managed_is_read_per_environment_and_is_empty_without_the_key():
    config = load_config(with_not_managed(VALID, value='["legacy.*", "dbo.Old?"]'))
    assert config.env["dev"].not_managed == ("legacy.*", "dbo.Old?")
    assert config.env["prod"].not_managed == ()
    assert load_config(VALID).env["dev"].not_managed == ()


@pytest.mark.parametrize(
    ("value", "key"),
    [
        ('["legacy"]', "env.dev.not_managed[0]"),  # no dot: a pattern is schema.name
        ('["*"]', "env.dev.not_managed[0]"),
        ('["legacy.*", "LEGACY.*"]', "env.dev.not_managed[1]"),  # a duplicate, without letter case
        ('["azsqlcd.*"]', "env.dev.not_managed[0]"),  # the schema of the tool
        ('["[legacy].[Old]"]', "env.dev.not_managed[0]"),  # brackets
        ('["legacy. Old"]', "env.dev.not_managed[0]"),
        ('"legacy.*"', "env.dev.not_managed"),  # not a list
    ],
)
def test_a_not_managed_pattern_is_checked_as_a_pattern_of_exclude_objects(value, key):
    with pytest.raises(ToolError) as e:
        load_config(with_not_managed(VALID, value=value))
    assert (e.value.exit_code, e.value.reason_code) == (Exit.REFUSED, "CONFIG_INVALID")
    assert e.value.detail["key"] == key


def test_under_a_scope_a_pattern_whose_schema_the_scope_does_not_manage_is_refused():
    scoped = '\n[scope]\nschemas = ["sales"]\n'
    with pytest.raises(ToolError) as e:
        load_config(with_not_managed(VALID) + scoped)
    assert e.value.detail["key"] == "env.dev.not_managed[0]"
    assert load_config(with_not_managed(VALID, value='["sales.Old*"]') + scoped).scope == Scope(("sales",))


def test_for_env_is_the_config_itself_where_the_environment_has_no_not_managed():
    config = load_config(with_not_managed(VALID))
    assert for_env(config, "prod") is config
    plain = load_config(VALID)
    assert for_env(plain, "dev") is plain and for_env(plain, "nosuch") is plain


def test_in_the_environment_the_matching_objects_are_out_of_scope_with_their_own_reason():
    config = for_env(load_config(with_not_managed(VALID)), "dev")
    scope = config.scope
    assert scope == Scope(("*",), (), (), ("legacy.*",), "dev")
    assert out_of_scope(scope, "TABLE:[Legacy].[Old]") == "it matches 'legacy.*' of [env.dev] not_managed"
    assert in_scope(scope, "TABLE:[sales].[Order]") and in_scope(scope, "SCHEMA:[legacy]")
    # baseline --schema keeps the patterns of the environment
    assert not in_scope(scope.only("legacy"), "VIEW:[legacy].[vw_Old]")


def test_under_a_scope_the_patterns_join_those_of_exclude_objects():
    text = with_not_managed(VALID, value='["sales.Old*"]')
    text += '\n[scope]\nschemas = ["sales"]\nexclude_objects = ["sales.tmp_*"]\n'
    scope = for_env(load_config(text), "dev").scope
    assert scope == Scope(("sales",), (), ("sales.tmp_*",), ("sales.Old*",), "dev")
    excluded = "it matches 'sales.tmp_*' of [scope] exclude_objects"
    assert out_of_scope(scope, "TABLE:[sales].[tmp_x]") == excluded
    not_here = "it matches 'sales.Old*' of [env.dev] not_managed"
    assert out_of_scope(scope, "TABLE:[sales].[OldOrder]") == not_here


# ------------------------------------------------------------------ the record of a skip
def step(step_id: int, file: str | None, status: str, note: str | None, kind: str = "migration") -> StepRow:
    return StepRow(step_id, step_id, kind, file, None, status, note)


def test_a_skip_is_a_not_applied_step_with_the_note_of_the_environment():
    steps = (
        step(1, "0002__legacy.sql", "not_applied", set_aside.skip_note("test")),
        step(2, "0003__sales.sql", "ok", None),
        step(3, "0004__index.sql", "not_applied", "marked not applied by run 7", "nontx"),
    )
    found = set_aside.skipped(steps)
    assert list(found) == ["0002__legacy.sql"]
    assert found["0002__legacy.sql"].reason == set_aside.NOT_MANAGED
    assert found["0002__legacy.sql"].note == "not_managed: 0002__legacy.sql (skipped by run 1)"
    assert set_aside.skipped(steps[1:]) == {}


# ------------------------------------------------------------------ which migrations are skipped
def migration(file: str, *batches: str) -> tuple[chain.ChainEntry, chain.Migration]:
    header = f"-- azsqlcd:migration {file.removesuffix('.sql')}\n-- azsqlcd:mode tx\n"
    found = chain.parse_migration(header + "\nGO\n".join(batches) + "\n", file)
    return chain.ChainEntry(file, "0" * 64, "tx"), found


LEGACY = migration("0002__legacy_old.sql", "ALTER TABLE [legacy].[Old] ADD [Note] nvarchar(200) NULL;")
SALES = migration("0003__order_note.sql", "ALTER TABLE [sales].[Order] ADD [Note] nvarchar(200) NULL;")
BOTH = migration(
    "0004__both.sql",
    "ALTER TABLE [legacy].[Old] ADD [Flag] bit NULL;",
    "ALTER TABLE [sales].[Customer] ADD [Flag] bit NULL;",
)
SCOPE = Scope(("*",), (), (), ("legacy.*",), "test")


def test_a_migration_that_touches_only_not_managed_objects_is_skipped_and_others_run():
    found = not_managed.skips([LEGACY, SALES], SCOPE)
    assert list(found) == [LEGACY[0].file]
    item = found[LEGACY[0].file]
    assert item.reason == set_aside.NOT_MANAGED
    assert item.why == "it touches only objects that [env.test] not_managed matches: [legacy].[old]"


def test_a_migration_that_touches_managed_and_not_managed_objects_is_refused():
    with pytest.raises(ToolError) as e:
        not_managed.skips([SALES, BOTH], SCOPE)
    assert (e.value.exit_code, e.value.reason_code) == (Exit.REFUSED, "MIXED_NOT_MANAGED")
    assert BOTH[0].file in e.value.message and "[env.test] not_managed" in e.value.message
    assert e.value.detail["not_managed"] == ["[legacy].[old]"]
    assert e.value.detail["managed"] == ["[sales].[customer]"]


def test_a_held_migration_that_mixes_both_sides_is_not_refused_and_a_held_one_side_is_still_skipped():
    # a product holds it here, so it does not run here: the refusal waits for the hold to end
    assert not_managed.skips([SALES, BOTH, LEGACY], SCOPE, held={BOTH[0].file, LEGACY[0].file}) == {
        LEGACY[0].file: not_managed.skips([LEGACY], SCOPE)[LEGACY[0].file]
    }


def test_a_raw_batch_touches_every_name_of_its_text():
    raw = migration(
        "0005__copy.sql",
        "-- azsqlcd:raw TABLE:[sales].[Order] reason: copy\n"
        "INSERT INTO [sales].[Order] ([OrderId]) SELECT [Id] FROM [legacy].[Old];",
    )
    with pytest.raises(ToolError) as e:
        not_managed.skips([raw], SCOPE)
    assert e.value.reason_code == "MIXED_NOT_MANAGED"


def test_without_not_managed_nothing_is_read_and_nothing_is_skipped():
    assert not_managed.skips([LEGACY, BOTH], None) == {}
    assert not_managed.skips([LEGACY, BOTH], dataclasses.replace(SCOPE, not_managed=())) == {}


# ------------------------------------------------------------------ what the names cannot prove
DYNAMIC = migration(
    "0005__fix_old.sql",
    "-- azsqlcd:data\n-- azsqlcd:allow DYNAMIC_SQL batch reason: reviewed\n"
    "EXEC (N'UPDATE [legacy].[Old] SET [Name] = N''x'';');",
)


def test_a_data_batch_whose_names_are_in_dynamic_sql_runs_with_a_note():
    """The names of a data or raw batch are all that the tool compares with the patterns: a name in
    a string is not seen. The plan says so, as for a park."""
    assert not_managed.skips([DYNAMIC], SCOPE) == {}
    assert not_managed.notes([DYNAMIC[1], SALES[1]], [], SCOPE) == [
        "not_managed not proven for a data batch: 0005__fix_old.sql (only the names of its text are "
        "compared with [env.test] not_managed)"
    ]


def test_the_object_of_a_raw_batch_is_touched():
    raw = migration(
        "0006__old.sql", "-- azsqlcd:raw TABLE:[legacy].[Old] reason: by hand\nEXEC (N'SELECT 1');"
    )
    assert list(not_managed.skips([raw], SCOPE)) == ["0006__old.sql"]


def test_what_is_sent_and_names_a_not_managed_object_gets_a_note():
    from azsqlcd.modules import read_module

    view = read_module(
        "schema/views/sales.vw_OldNote.sql",
        b"CREATE OR ALTER VIEW [sales].[vw_OldNote] AS\nSELECT [OldId], [Note] FROM [legacy].[Old];\n",
    )
    fk = migration(
        "0007__order_old.sql",
        "ALTER TABLE [sales].[Order] ADD CONSTRAINT [FK_Order_Old] FOREIGN KEY ([OldId]) "
        "REFERENCES [legacy].[Old] ([OldId]);",
    )
    assert not_managed.skips([fk], SCOPE) == {}
    assert not_managed.notes([fk[1]], [view], SCOPE) == [
        "not_managed: 0007__order_old.sql names [legacy].[old], which [env.test] not_managed matches: "
        "the tool does not know that object here",
        "not_managed: VIEW:[sales].[vw_OldNote] names [legacy].[old], which [env.test] not_managed "
        "matches: the tool does not know that object here",
    ]
    assert not_managed.notes([fk[1]], [view], None) == []
