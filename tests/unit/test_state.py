"""Schema azsqlcd: the administrator script, the read of the recorded state, the write statements.

No test here runs T-SQL. The script and the write statements are text, so the tests read that
text with the lexer of the tool: what is a word, what is a literal, what is a quoted name.
"""

import dataclasses
import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from azsqlcd import lex, names, state
from azsqlcd.config import Config, load_config
from azsqlcd.errors import Exit, ToolError
from support.fake_session import FakeSession

PLAN_ID = "6F9619FF-8B86-D011-B42D-00C04FC964FF"
DEPLOY_ID = "aaaaaaaa-0000-0000-0000-000000000002"
PLAN_USER = "azsqlcd-sales-prod-plan"
DEPLOY_USER = "azsqlcd-sales-prod-deploy"
SHA40 = "a1" * 20
SHA64 = "b2" * 32
KEY = names.object_key("PROCEDURE", "sales", "usp_x")
MIGRATION = "0001__add_order_status.sql"
HOSTILE = "x'; DROP TABLE [azsqlcd].[run]; --]"
HOSTILE_KEY = names.object_key("PROCEDURE", "sa]les'", HOSTILE)
WHEN = datetime(2026, 10, 7, 4, 37, 12, 123000, tzinfo=UTC)
RUN = {
    "command": "deploy",
    "release_seq": 57,
    "git_sha": SHA40,
    "manifest_sha256": SHA64,
    "plan_sha256": SHA64,
    "tool_version": "0.1.0",
    "tool_digest": SHA64,
}


# ------------------------------------------------------------------ reading SQL text
def tokens(sql: str) -> list[lex.Tok]:
    return lex.significant(lex.tokenize(sql))


def words(sql: str) -> list[str]:
    return [t.text.upper() for t in tokens(sql) if t.kind == "word"]


def strings(sql: str) -> list[str]:
    return [t.value for t in tokens(sql) if t.kind in ("string", "nstring")]


def quoted_names(sql: str) -> list[str]:
    return [t.value for t in tokens(sql) if t.kind == "bident"]


def shape(sql: str) -> list[str]:
    """The statement without its values: every literal and every quoted name is a placeholder."""
    kinds = {"string": "'?'", "nstring": "'?'", "bident": "[?]"}
    return [kinds.get(t.kind, t.text) for t in tokens(sql)]


def batches(script: str) -> list[str]:
    return [batch.text for batch in lex.split_batches(script)]


def _split(items: list[lex.Tok]) -> list[str]:
    """Token texts between two parentheses or after SET, split at top-level commas."""
    parts: list[list[str]] = [[]]
    depth = 0
    for t in items:
        depth += (t.text == "(") - (t.text == ")")
        if t.text == "," and depth == 0:
            parts.append([])
        else:
            parts[-1].append(t.text)
    return ["".join(part) for part in parts]


def _group(toks: list[lex.Tok], start: int) -> tuple[list[lex.Tok], int]:
    """Tokens inside the parenthesis that opens at start, and the index after its end."""
    depth, i = 0, start
    while True:
        depth += (toks[i].text == "(") - (toks[i].text == ")")
        i += 1
        if depth == 0:
            return toks[start + 1 : i - 1], i


def inserted(sql: str) -> dict[str, str]:
    """column -> value text of the INSERT in the batch."""
    toks = tokens(sql)
    at = next(i for i, t in enumerate(toks) if t.text.upper() == "INSERT")
    open_columns = next(i for i in range(at, len(toks)) if toks[i].text == "(")
    columns, after = _group(toks, open_columns)
    assert toks[after].text.upper() == "VALUES"
    values, _ = _group(toks, after + 1)
    return dict(zip([t.value for t in columns if t.kind == "bident"], _split(values), strict=True))


def updated(sql: str) -> dict[str, str]:
    """column -> value text of the UPDATE ... SET list in the batch."""
    toks = tokens(sql)
    start = next(i for i, t in enumerate(toks) if t.text.upper() == "SET") + 1
    end = next(i for i in range(start, len(toks)) if toks[i].text.upper() == "WHERE")
    pairs = (part.split("=", 1) for part in _split(toks[start:end]))
    return {column.strip("[]"): value for column, value in pairs}


# ------------------------------------------------------------------ the administrator script
def config(
    project: str = "sales", database: str = "sales", plan: str = PLAN_ID, deploy: str = DEPLOY_ID
) -> Config:
    name = json.dumps(database)
    return load_config(f"""
[project]
name = {json.dumps(project)}
tenant_id = "11111111-2222-3333-4444-555555555555"
table_model = false
module_chunk = 100
min_token_minutes = 20

[identities]
the_plan = "{plan}"
the_deploy = "{deploy}"

[env.dev]
plan_identity = "the_plan"
deploy_identity = "the_deploy"
drift = "report"
lock_timeout_ms = 30000
applock_wait_s = 600
job_timeout_minutes = 120
targets = [{{ id = "sales-dev", server = "sql-dev.database.windows.net", database = "sales_dev" }}]

[env.prod]
plan_identity = "the_plan"
deploy_identity = "the_deploy"
drift = "block"
lock_timeout_ms = 10000
applock_wait_s = 600
job_timeout_minutes = 120
targets = [{{ id = "sales-prod", server = "sql-prod.database.windows.net", database = {name} }}]
""")


def setup(**values: str) -> str:
    return state.setup_sql(config(**values), "prod", "sales-prod")


def index_of(script: str, *needles: str) -> int:
    """Position of the first batch whose words hold every needle."""
    for index, batch in enumerate(batches(script)):
        if set(needles) <= set(words(batch)):
            return index
    raise AssertionError(f"no batch with {needles}")


def test_the_setup_script_grants_no_delete_on_the_state_schema():
    script = setup()
    assert "DELETE" not in words(script)  # in no statement at all; the header comment is not a statement
    (grant,) = [
        b for b in batches(script) if words(b)[0] == "GRANT" and quoted_names(b) == ["azsqlcd", DEPLOY_USER]
    ]
    assert words(grant) == ["GRANT", "SELECT", "INSERT", "UPDATE", "ON", "SCHEMA", "TO"]


def test_the_plan_identity_gets_no_right_to_write_or_to_change_objects():
    plan_batches = [b for b in batches(setup()) if PLAN_USER in quoted_names(b)]
    assert len(plan_batches) == 5  # the user and four grants
    for batch in plan_batches:
        assert not {"INSERT", "UPDATE", "DELETE", "ALTER", "CONTROL", "EXECUTE", "ROLE"} & set(words(batch))


def test_no_pipeline_identity_gets_a_role_other_than_ddladmin_for_deploy():
    script = setup()
    role_batches = [b for b in batches(script) if "ROLE" in words(b)]
    assert [quoted_names(b) for b in role_batches] == [["db_ddladmin", DEPLOY_USER]]
    every_name = {t.value.lower() for t in tokens(script) if t.kind in ("bident", "word")}
    assert not {"db_securityadmin", "db_owner", "db_accessadmin", "control"} & every_name


def test_both_identities_can_read_the_dependency_view():
    grants = [b for b in batches(setup()) if "sql_expression_dependencies" in quoted_names(b)]
    assert [words(b) for b in grants] == [["GRANT", "SELECT", "ON", "OBJECT", "TO"]] * 2
    assert [quoted_names(b)[-1] for b in grants] == [PLAN_USER, DEPLOY_USER]


def test_a_second_setup_with_another_environment_name_raises_instead_of_rebinding():
    script = setup()
    statements = [b for b in batches(script) if words(b)[0] != "GRANT"]
    # Nothing in the script can change a meta row that exists: no UPDATE, one INSERT, behind a check.
    assert not {"UPDATE", "MERGE", "DELETE"} & {word for b in statements for word in words(b)}
    (meta,) = [b for b in statements if "INSERT" in words(b)]
    assert words(meta)[:5] == ["IF", "NOT", "EXISTS", "SELECT", "FROM"]
    assert inserted(meta) == {
        "id": "1",
        "state_version": "1",
        "project": "N'sales'",
        "environment": "N'prod'",
        "created_utc": "SYSUTCDATETIME()",
    }
    # The other branch compares the environment of the row with the one of this script and raises.
    other_branch = meta[meta.index("ELSE") :]
    assert words(other_branch)[:3] == ["ELSE", "IF", "EXISTS"]
    assert "RAISERROR" in words(other_branch)
    assert quoted_names(other_branch) == ["azsqlcd", "meta", "environment"]
    assert strings(other_branch)[0] == "prod"
    assert strings(state.setup_sql(config(), "dev", "sales-dev")).count("dev") == 2  # insert and compare


def test_state_of_another_project_stops_the_script_before_any_user_or_grant():
    script = setup()
    guard = next(
        i for i, b in enumerate(batches(script)) if "project" in quoted_names(b) and "NOEXEC" in words(b)
    )
    text = batches(script)[guard]
    assert words(text)[:2] == ["IF", "EXISTS"] and "sales" in strings(text)
    assert "state_version" in quoted_names(text)
    assert words(text)[-5:] == ["RAISERROR", "SET", "NOEXEC", "ON", "END"]
    assert guard < index_of(script, "CREATE", "USER")
    assert guard < index_of(script, "GRANT")
    assert guard < index_of(script, "ALTER", "ROLE")


def test_another_database_stops_the_script_before_anything_is_created():
    script = setup(database="sales_prod_db")
    guard = index_of(script, "DB_NAME")
    text = batches(script)[guard]
    assert {"sales_prod_db", "EngineEdition"} <= set(strings(text))  # D2: Azure SQL Database only
    assert words(text)[-5:] == ["RAISERROR", "SET", "NOEXEC", "ON", "END"]
    assert guard < index_of(script, "EXEC")  # CREATE SCHEMA
    assert guard < index_of(script, "CREATE")
    assert words(batches(script)[-1]) == ["SET", "NOEXEC", "OFF"]  # the session is given back


def test_rights_go_only_to_the_principal_with_the_name_and_the_sid_of_the_identity():
    script = setup()
    user = batches(script)[index_of(script, "CREATE", "USER")]
    sid = "0xFF19966F868B11D0B42D00C04FC964FF"  # the client id in the byte order of uniqueidentifier
    assert f"CREATE USER [{PLAN_USER}] WITH SID = {sid}, TYPE = E;" in user
    assert "Needs live check" in user
    # a principal with this name and another SID stops the script; this SID under another name is
    # the user of the same identity (E2E-2): the stop (2), the test and the read of that user (2), CREATE (1)
    assert words(user)[:2] == ["IF", "EXISTS"] and "NOEXEC" in words(user)
    assert [t.text for t in tokens(user) if t.kind == "number"].count(sid) == 5
    assert index_of(script, "CREATE", "USER") < index_of(script, "GRANT")


def test_one_identity_for_plan_and_deploy_gets_one_user_with_the_rights_of_deploy():
    script = setup(plan=DEPLOY_ID.upper(), deploy=DEPLOY_ID)
    users = [b for b in batches(script) if {"CREATE", "USER"} <= set(words(b))]
    assert len(users) == 1 and DEPLOY_USER in quoted_names(users[0])  # two users with one SID cannot exist
    assert PLAN_USER not in quoted_names(script)
    assert "db_ddladmin" in quoted_names(script)


def test_every_batch_of_the_setup_script_says_why():
    for batch in batches(setup()):
        lines = batch.splitlines()
        comments = [line for line in lines if line.startswith("--")]
        code = [line for line in lines if not line.startswith("--")]
        assert code, batch
        assert any(line.startswith("-- Why: ") and len(line) > 30 for line in comments), batch
        assert lines.index(code[0]) > lines.index(comments[-1])  # the reason stands above the statement


def test_the_script_can_run_again_because_every_create_is_behind_a_check():
    for batch in batches(setup()):
        if {"CREATE", "EXEC", "INSERT"} & set(words(batch)) and words(batch)[0] != "GRANT":
            assert words(batch)[0] == "IF", batch


def _flat(sql: str) -> str:
    return " ".join(t.text for t in tokens(sql))


def test_a_second_run_on_a_database_with_the_same_binding_changes_nothing_and_raises_nothing():
    """The refresh flow of the runbook runs the script on a database that holds the state tables.

    Read as text: what a batch does when its objects exist and the meta row names this project and
    this environment. A statement that the engine repeats without an error and without a change
    (SET, GRANT, ALTER ROLE ... ADD MEMBER) stands alone; everything else is behind a condition
    that is false on the second run.
    """
    script = setup()
    seen: set[str] = set()
    for batch in batches(script):
        first, flat = words(batch)[0], _flat(batch)
        if first in ("SET", "GRANT"):
            seen.add(first)
        elif first == "ALTER":
            assert words(batch)[:4] == ["ALTER", "ROLE", "ADD", "MEMBER"], batch
            seen.add("ADD MEMBER")
        elif "CREATE TABLE" in flat:
            table = quoted_names(batch)[1]
            assert flat.startswith(f"IF OBJECT_ID ( N'[azsqlcd].[{table}]' , N'U' ) IS NULL CREATE TABLE")
            seen.add(f"table {table}")
        elif "INDEX" in words(batch):
            assert flat.startswith("IF NOT EXISTS ( SELECT 1 FROM sys . indexes WHERE")
            assert "[name] = N'UX_step_migration' ) CREATE UNIQUE" in flat
            seen.add("index")
        elif "CREATE SCHEMA" in "".join(strings(batch)):
            assert flat.startswith("IF SCHEMA_ID ( N'azsqlcd' ) IS NULL EXEC (")
            seen.add("schema")
        elif "USER" in words(batch):
            user = next(name for name in (PLAN_USER, DEPLOY_USER) if name in quoted_names(batch))
            # the stop needs a principal with this name and another SID, or this SID and another name
            assert flat.count("<>") == 2 and flat.count("RAISERROR") == 1
            assert f"ELSE IF DATABASE_PRINCIPAL_ID ( N'{user}' ) IS NULL CREATE USER [{user}]" in flat
            seen.add(f"user {user}")
        elif "INSERT" in words(batch):
            assert flat.startswith("IF NOT EXISTS ( SELECT 1 FROM [azsqlcd] . [meta] ) INSERT INTO")
            # the one raise of this batch needs a meta row with another environment
            other = flat[flat.index("ELSE") :]
            assert other.startswith(
                "ELSE IF EXISTS ( SELECT 1 FROM [azsqlcd] . [meta] WHERE [environment] <> N'prod'"
            )
            assert flat.count("RAISERROR") == 1 and "RAISERROR" in other
            seen.add("meta")
        else:
            # a guard: it raises only on another database, another engine, another project or version
            assert first == "IF" and flat.count("RAISERROR") == 1 and "NOEXEC ON" in flat, batch
            assert "<>" in flat[: flat.index("BEGIN")]
            assert not {"CREATE", "INSERT", "UPDATE", "ALTER", "DROP", "EXEC"} & set(words(batch)), batch
            seen.add("guard")
    assert seen == {
        *("SET", "GRANT", "ADD MEMBER", "schema", "index", "meta", "guard"),
        *(f"table {table}" for table in state.TABLES),
        *(f"user {user}" for user in (PLAN_USER, DEPLOY_USER)),
    }


def test_the_state_tables_have_the_columns_and_the_closed_lists_of_the_design():
    script = setup()
    run = batches(script)[index_of(script, "CREATE", "TABLE", "IDENTITY", "SYSNAME")]
    for declared in (  # A3
        "[approved_by] nvarchar(400) NULL,",
        "[approved_utc] datetime2(3) NULL,",
        "[triggering_actor] nvarchar(128) NULL,",
        "[previous_git_sha] char(40) NULL,",
        "[tool_digest] char(64) NOT NULL,",
        "[segments_committed] int NOT NULL,",
    ):
        assert declared in " ".join(run.split()), declared
    assert strings(run)[2:] == ["deploy", "baseline", "resolve", "running", "ok", "failed", "unknown"]
    step = batches(script)[index_of(script, "CREATE", "TABLE", "REFERENCES", "VARCHAR", "NVARCHAR")]
    assert quoted_names(step)[:2] == ["azsqlcd", "step"]
    assert strings(step)[2:] == [
        *("baseline", "migration", "nontx", "modules", "resolve"),
        *("ok", "started", "unknown", "not_applied"),
    ]
    assert {"meta", "run", "step", "object"} == {
        quoted_names(b)[1] for b in batches(script) if {"CREATE", "TABLE"} <= set(words(b))
    }


def test_a_migration_can_be_recorded_only_once():
    script = setup()
    index = " ".join(batches(script)[index_of(script, "CREATE", "UNIQUE", "INDEX")].split())
    assert index.endswith(
        "CREATE UNIQUE NONCLUSTERED INDEX [UX_step_migration] ON [azsqlcd].[step] ([migration_id]) "
        "WHERE [migration_id] IS NOT NULL;"
    )
    # a filtered index needs these settings in the session that creates it
    assert index_of(script, "QUOTED_IDENTIFIER", "ANSI_NULLS", "ARITHABORT") < index_of(script, "UNIQUE")
    assert index_of(script, "NUMERIC_ROUNDABORT", "OFF") < index_of(script, "UNIQUE")


def test_a_quote_or_bracket_in_a_config_value_stays_inside_a_literal_or_a_quoted_name():
    project, database = "sa'les]; DROP TABLE x --", "db]'; DROP TABLE y --"
    script = setup(project=project, database=database)
    assert shape(script) == shape(setup())  # the same statements, whatever the values are
    assert "DROP" not in words(script)
    assert project in strings(script) and database in strings(script)
    assert f"azsqlcd-{project}-prod-deploy" in quoted_names(script)
    assert not [
        t for t in lex.tokenize(script) if t.kind == "comment" and ("DROP" in t.text or "sa'" in t.text)
    ]


@pytest.mark.parametrize(
    ("values", "key"),
    [
        ({"project": "sales\nGO\nDROP TABLE x"}, "project.name"),
        ({"database": "sales\r\nGO\r\nDROP TABLE x"}, "env.prod.targets[].database"),
        ({"project": "s" * 120}, "project.name"),  # the user name would pass 128 characters
    ],
)
def test_a_value_that_a_script_cannot_hold_safely_is_refused(values, key):
    # the client tool splits at GO lines, perhaps without reading literals: a line break could start a batch.
    # load_config refuses such a value too; a Config that was built in code must not get past the script
    loaded = config()
    project = dataclasses.replace(loaded.project, name=values.get("project", "sales"))
    prod = loaded.env["prod"]
    target = dataclasses.replace(prod.targets[0], database=values.get("database", "sales"))
    made = dataclasses.replace(
        loaded, project=project, env={"prod": dataclasses.replace(prod, targets=(target,))}
    )
    with pytest.raises(ToolError) as e:
        state.setup_sql(made, "prod", "sales-prod")
    assert (e.value.exit_code, e.value.reason_code) == (Exit.REFUSED, "CONFIG_INVALID")
    assert e.value.detail["key"] == key


@pytest.mark.parametrize(
    ("env", "target", "reason"),
    [("test", "sales-prod", "ENV_NOT_CONFIGURED"), ("prod", "sales-dev", "TARGET_NOT_CONFIGURED")],
)
def test_a_script_is_made_only_for_a_target_of_the_environment(env, target, reason):
    with pytest.raises(ToolError) as e:
        state.setup_sql(config(), env, target)
    assert (e.value.exit_code, e.value.reason_code) == (Exit.REFUSED, reason)


# ------------------------------------------------------------------ read_state
def recorded(
    db: FakeSession,
    *,
    tables: tuple[str, ...] = state.TABLES,
    unreadable: tuple[str, ...] = (),
    meta: tuple[tuple, ...] = ((1, "sales", "prod"),),
    runs: tuple[tuple, ...] = (),
    steps: tuple[tuple, ...] = (),
    objects: tuple[tuple, ...] = (),
) -> FakeSession:
    db.respond("azsqlcd:read_state.tables", [[(table, 0 if table in unreadable else 1) for table in tables]])
    db.respond("azsqlcd:read_state.meta", [list(meta)])
    db.respond("azsqlcd:read_state.runs", [list(runs)])
    db.respond("azsqlcd:read_state.steps", [list(steps)])
    db.respond("azsqlcd:read_state.objects", [list(objects)])
    return db


def run_row(
    run_id: int, status: str, release_seq: int, git_sha: str = SHA40, command: str = "deploy"
) -> tuple:
    return (run_id, command, status, 1, release_seq, git_sha, "2026-10-07T04:37:12.123")


def refusal(db: FakeSession) -> ToolError:
    with pytest.raises(ToolError) as e:
        state.read_state(db)
    assert e.value.exit_code == Exit.REFUSED
    return e.value


def test_a_missing_state_schema_is_a_refusal_not_an_empty_state():
    db = FakeSession()  # a database that nobody set up: every read gives nothing
    error = refusal(db)
    assert error.reason_code == "STATE_MISSING"
    assert error.detail["missing"] == ["meta", "run", "step", "object"]
    assert "setup-sql" in error.message
    assert db.sent("FROM [azsqlcd]") == []  # no read of a table that is not there


def test_one_missing_table_or_a_missing_meta_row_is_also_state_missing():
    error = refusal(recorded(FakeSession(), tables=("META", "run", "step")))
    assert (error.reason_code, error.detail["missing"]) == ("STATE_MISSING", ["object"])
    error = refusal(recorded(FakeSession(), meta=()))
    assert (error.reason_code, error.detail["missing"]) == ("STATE_MISSING", ["meta row"])


def test_tables_that_the_principal_may_not_read_are_state_missing_not_an_engine_error():
    # A2: the grants are part of what the setup script makes
    db = recorded(FakeSession(), unreadable=("run", "object"))
    error = refusal(db)
    assert error.reason_code == "STATE_MISSING"
    assert error.detail["missing"] == ["SELECT right on run", "SELECT right on object"]
    assert "N'[azsqlcd].' + QUOTENAME(t.[name]), N'OBJECT', N'SELECT')" in db.batches[0]
    assert db.sent("FROM [azsqlcd]") == []


def test_a_state_version_that_the_tool_does_not_know_is_refused():
    error = refusal(recorded(FakeSession(), meta=((2, "sales", "prod"),)))
    assert error.reason_code == "STATE_VERSION_UNSUPPORTED"
    assert error.detail == {"state_version": 2, "supported": 1}


def test_meta_is_read_as_recorded():
    got = state.read_state(recorded(FakeSession(), meta=((1, "Sales", "preprod"),)))
    assert got.meta == state.Meta(state_version=1, project="Sales", environment="preprod")


def test_the_recorded_release_is_the_highest_release_of_an_ok_run():
    other = "c3" * 20
    db = recorded(
        FakeSession(),
        runs=(
            run_row(2, "ok", 58, other),
            run_row(5, "unknown", 59),
            run_row(8, "ok", 57, command="resolve"),  # a later run with an older release
            run_row(9, "running", 59),
        ),
    )
    got = state.read_state(db)
    assert (got.recorded_release_seq, got.recorded_git_sha) == (58, other)
    assert got.latest_ok_run == state.RunRow(2, "deploy", "ok", 1, 58, other, "2026-10-07T04:37:12.123")
    assert [(run.run_id, run.status) for run in got.open_runs] == [(5, "unknown"), (9, "running")]
    (batch,) = db.sent("azsqlcd:read_state.runs")
    assert "ORDER BY k.[release_seq] DESC, k.[run_id] DESC" in batch  # the engine picks by the same rule
    # a failed run is read only when it is the deploy run of the highest release that committed
    assert strings(batch) == ["running", "unknown", "ok", "deploy", "baseline", "deploy"]


def test_a_deploy_run_that_committed_a_unit_and_did_not_end_ok_is_read_as_the_committed_release():
    """A6: the fence is committed with the unit of work. A run that committed and never became ok
    (RUN_NOT_CLOSED, a session lost at COMMIT) left its release in the database: the planner must
    not take the database for one that an older release can still deploy to."""
    committed = (9, "deploy", "failed", 1, 59, SHA40, "2026-10-07T04:37:12.123")
    db = recorded(FakeSession(), runs=(run_row(2, "ok", 58), committed))
    got = state.read_state(db)
    assert (got.recorded_release_seq, got.committed_release_seq) == (58, 59)
    assert got.open_runs == ()  # a failed run is not an open run
    (batch,) = db.sent("azsqlcd:read_state.runs")
    assert (
        "c.[command] = N'deploy' AND c.[segments_committed] > 0 "
        "ORDER BY c.[release_seq] DESC, c.[run_id] DESC" in batch
    )


def test_a_run_that_committed_nothing_and_a_run_that_is_not_a_deploy_are_no_committed_release():
    rolled_back = (9, "deploy", "running", 0, 59, SHA40, "2026-10-07T04:37:12.123")
    resolve = (10, "resolve", "unknown", 1, 61, SHA40, "2026-10-07T04:37:12.123")
    baseline = (11, "baseline", "running", 1, 60, SHA40, "2026-10-07T04:37:12.123")
    got = state.read_state(recorded(FakeSession(), runs=(rolled_back, resolve, baseline)))
    assert got.committed_release_seq == 0
    assert state.read_state(recorded(FakeSession())).committed_release_seq == 0


def test_a_resolve_run_can_never_move_the_recorded_release():
    """A resolve run deploys nothing. Its row may hold any release number; it records none."""
    other = "c3" * 20
    db = recorded(
        FakeSession(),
        runs=(
            run_row(2, "ok", 58, other),
            run_row(6, "ok", 61, command="resolve"),  # a resolve run that was written with a newer release
        ),
    )
    got = state.read_state(db)
    assert (got.recorded_release_seq, got.recorded_git_sha) == (58, other)
    assert got.latest_ok_run is not None and got.latest_ok_run.run_id == 2
    # the engine picks the run by the same rule, so the newest release of a deploy is always read
    (batch,) = db.sent("azsqlcd:read_state.runs")
    assert "k.[status] = N'ok' AND k.[command] IN (N'deploy', N'baseline') ORDER BY" in batch


def test_a_baseline_run_records_its_release_and_a_resolve_run_alone_records_none():
    baseline = state.read_state(recorded(FakeSession(), runs=(run_row(1, "ok", 3, command="baseline"),)))
    assert baseline.recorded_release_seq == 3
    resolve = state.read_state(recorded(FakeSession(), runs=(run_row(1, "ok", 3, command="resolve"),)))
    assert (resolve.recorded_release_seq, resolve.recorded_git_sha, resolve.latest_ok_run) == (0, None, None)


def test_a_database_with_no_ok_run_has_release_zero():
    got = state.read_state(recorded(FakeSession(), runs=(run_row(1, "running", 57),)))
    assert (got.recorded_release_seq, got.recorded_git_sha, got.latest_ok_run) == (0, None, None)


def test_the_steps_are_read_in_step_id_order():
    """TQ-06: the planner reads the applied migrations in this order (prefix rule of A6), and the
    newest run is the last of the runs. A fake gives its rows as scripted, so the text is the proof."""
    db = recorded(FakeSession())
    state.read_state(db)
    (steps,) = db.sent("azsqlcd:read_state.steps")
    assert steps.endswith("FROM [azsqlcd].[step] ORDER BY [step_id];")
    assert "SELECT [step_id], [run_id], [kind], [migration_id], [file_sha256], [status], [note] FROM" in steps
    (runs,) = db.sent("azsqlcd:read_state.runs")
    assert runs.endswith(" ORDER BY r.[run_id];") and "DESC) ORDER BY r.[run_id];" in runs
    (objects,) = db.sent("azsqlcd:read_state.objects")
    assert objects.endswith("FROM [azsqlcd].[object] ORDER BY [object_key];")


def test_steps_and_objects_are_read_with_their_capture():
    capture = {"kind": "PROCEDURE", "definition": "CREATE PROCEDURE sales.usp_x AS SELECT 'é';"}
    text = state.capture_json(capture)
    db = recorded(
        FakeSession(),
        steps=(
            (1, 3, "migration", MIGRATION, SHA64, "ok", None),
            (2, 3, "modules", None, None, "ok", None),
            (3, 4, "resolve", None, None, "ok", "clear-run 2"),
        ),
        objects=(
            (KEY, "managed", SHA64, 1, text, state.capture_sha256(capture)),
            ("VIEW:[sales].[v]", "dropped", None, 1, "{}", state.capture_sha256({})),
        ),
    )
    got = state.read_state(db)
    assert got.steps[0] == state.StepRow(1, 3, "migration", MIGRATION, SHA64, "ok", None)
    assert [(step.kind, step.migration_id, step.note) for step in got.steps[1:]] == [
        ("modules", None, None),
        ("resolve", None, "clear-run 2"),
    ]
    assert got.objects[KEY] == state.ObjectRow("managed", SHA64, 1, capture, state.capture_sha256(capture))
    assert got.objects["VIEW:[sales].[v]"].status == "dropped"
    assert got.objects["VIEW:[sales].[v]"].source_sha256 is None


@pytest.mark.parametrize("text", ["not json", "[1, 2]", '"text"'])
def test_a_capture_that_is_not_a_json_object_is_a_refusal(text):
    error = refusal(recorded(FakeSession(), objects=((KEY, "managed", None, 1, text, SHA64),)))
    assert (error.reason_code, error.detail) == ("STATE_INVALID", {"object": KEY})


def test_reading_the_state_sends_only_selects():
    db = recorded(FakeSession(), runs=(run_row(1, "ok", 57),))
    state.read_state(db)
    assert len(db.batches) == 5
    for batch in db.batches:
        assert words(batch)[0] == "SELECT"
        assert not {"INSERT", "UPDATE", "DELETE", "MERGE", "EXEC", "CREATE", "ALTER", "DROP"} & set(
            words(batch)
        )


# ------------------------------------------------------------------ write statements
def writes(text: str, key: str) -> dict[str, str]:
    """Every write helper that takes text, with that text in every argument that is free text."""
    return {
        "insert_run": state.insert_run(
            command="deploy",
            release_seq=57,
            git_sha=SHA40,
            manifest_sha256=SHA64,
            plan_sha256=SHA64,
            tool_version="0.1.0",
            tool_digest=SHA64,
            previous_git_sha=SHA40,
            approved_by=text,
            approved_utc=WHEN,
            triggering_actor=text,
            ci_actor=text,
            ci_run_url=text,
            note=text,
        ),
        "set_run_status": state.set_run_status(
            7, "failed", failed_step=text, error_number=1222, error_text=text, note=text
        ),
        "insert_step": state.insert_step(
            run_id=7, kind="migration", status="ok", migration_id=text, file_sha256=SHA64, note=text
        ),
        "set_step_status": state.set_step_status(text, "unknown", run_id=8, note=text),
        "upsert_object": state.upsert_object(
            key, run_id=7, capture={"definition": text}, source_sha256=SHA64
        ),
        "mark_dropped": state.mark_dropped(key, 7),
        "set_source_null": state.set_source_null(key),
    }


ALL_WRITES = {
    **writes("plain", KEY),
    "bump_fence": state.bump_fence(7),
    "set_meta_environment": state.set_meta_environment("test"),
}


@pytest.mark.parametrize("helper", sorted(writes("plain", KEY)))
def test_a_quote_or_bracket_in_a_value_cannot_break_out_of_the_batch_text(helper):
    plain = writes("plain", KEY)[helper]
    hostile = writes(HOSTILE, HOSTILE_KEY)[helper]
    assert shape(hostile) == shape(plain)  # the same statements: the value moved no token
    assert "DROP" not in words(hostile)
    # and the value arrives whole, as the engine will read the literal
    whole = HOSTILE_KEY if helper in ("upsert_object", "mark_dropped", "set_source_null") else HOSTILE
    assert whole in strings(hostile)


@pytest.mark.parametrize("bad", ["7", "7; DROP TABLE x", 7.0, True, -1, 0])
def test_a_number_reaches_batch_text_only_as_a_checked_whole_number(bad):
    calls = [
        lambda: state.bump_fence(bad),
        lambda: state.read_fence(bad),
        lambda: state.read_fence_locking(bad),
        lambda: state.set_run_status(bad, "ok"),
        lambda: state.set_step_status(MIGRATION, "ok", run_id=bad),
        lambda: state.insert_step(run_id=bad, kind="modules", status="ok"),
        lambda: state.upsert_object(KEY, run_id=bad, capture={}, source_sha256=None),
        lambda: state.mark_dropped(KEY, bad),
    ]
    if bad != 0:  # 0 is a release number and an error number, never a run
        calls.append(lambda: state.set_run_status(7, "failed", error_number=bad))
        calls.append(lambda: state.insert_run(**{**RUN, "release_seq": bad}))
    for call in calls:
        with pytest.raises(ValueError):
            call()


@pytest.mark.parametrize("bad", ["A1" * 20, "a1" * 19, "g1" * 20, "a1" * 19 + "a'", ""])
def test_a_digest_is_lower_case_hex_of_the_full_length(bad):
    with pytest.raises(ValueError):
        state.insert_run(**{**RUN, "git_sha": bad})
    with pytest.raises(ValueError):
        state.insert_run(**RUN, previous_git_sha=bad)
    for column in ("manifest_sha256", "plan_sha256", "tool_digest"):
        with pytest.raises(ValueError):
            state.insert_run(**{**RUN, column: bad + SHA40[:24]})
    with pytest.raises(ValueError):
        state.upsert_object(KEY, run_id=7, capture={}, source_sha256=bad)


def test_only_the_commands_kinds_and_statuses_of_the_design_can_be_written():
    for command in ("init", "align", "plan", "deploy'"):  # init and align are cut (A30)
        with pytest.raises(ValueError):
            state.insert_run(**{**RUN, "command": command})
    for kind in ("init", "align", "module"):
        with pytest.raises(ValueError):
            state.insert_step(run_id=7, kind=kind, status="ok")
    with pytest.raises(ValueError):
        state.insert_step(run_id=7, kind="nontx", status="running")
    with pytest.raises(ValueError):
        state.set_step_status(MIGRATION, "failed")
    with pytest.raises(ValueError):
        state.set_run_status(7, "running")  # a run is never set back to running
    for env in ("staging", "prod'; --", ""):
        with pytest.raises(ValueError):
            state.set_meta_environment(env)


def test_a_new_run_starts_running_with_fence_zero_and_gives_its_id():
    batch = state.insert_run(**RUN, approved_by="octocat", approved_utc=WHEN, ci_run_url="https://x/1")
    row = inserted(batch)
    assert row["command"] == "N'deploy'" and row["status"] == "N'running'"
    assert row["segments_committed"] == "0"
    assert row["started_utc"] == "SYSUTCDATETIME()"  # server time: the restore reference
    assert (row["release_seq"], row["git_sha"], row["tool_digest"]) == ("57", f"N'{SHA40}'", f"N'{SHA64}'")
    assert (row["previous_git_sha"], row["triggering_actor"], row["note"]) == ("NULL", "NULL", "NULL")
    assert row["approved_by"] == "N'octocat'"
    assert "finished_utc" not in row
    assert words(batch)[-5:] == ["SELECT", "CAST", "SCOPE_IDENTITY", "AS", "BIGINT"]


def test_the_approval_time_is_stored_as_utc():
    cairo = datetime(2026, 10, 7, 6, 37, 12, 123456, tzinfo=timezone(timedelta(hours=2)))
    row = inserted(state.insert_run(**RUN, approved_utc=cairo))
    assert row["approved_utc"] == "CONVERT(datetime2(3),N'2026-10-07T04:37:12.123',126)"
    with pytest.raises(ValueError):
        state.insert_run(**RUN, approved_utc=datetime(2026, 10, 7, 6, 37))  # no zone: which UTC time?


def test_a_run_ends_with_its_status_the_server_time_and_only_the_given_columns():
    assert updated(state.set_run_status(7, "ok")) == {"status": "N'ok'", "finished_utc": "SYSUTCDATETIME()"}
    failed = state.set_run_status(7, "failed", failed_step=MIGRATION, error_number=1222, note="x")
    assert updated(failed) == {
        "status": "N'failed'",
        "finished_utc": "SYSUTCDATETIME()",
        "failed_step": f"N'{MIGRATION}'",
        "error_number": "1222",
        "note": "N'x'",
    }
    assert "WHERE [run_id] = 7;" in failed


def test_error_text_is_stored_redacted():
    message = "Violation of PRIMARY KEY constraint 'PK_card'. The duplicate key value is (4111-1111)."
    (stored,) = [
        s for s in strings(state.set_run_status(7, "failed", error_text=message)) if "Violation" in s
    ]
    assert "4111" not in stored and "PK_card" not in stored
    assert stored == "Violation of PRIMARY KEY constraint <redacted>. The duplicate key value is <redacted>"


def test_the_fence_bump_adds_one_and_fails_loud_when_no_run_row_changes():
    batch = state.bump_fence(7)
    assert batch.startswith(
        "UPDATE [azsqlcd].[run] SET [segments_committed] = [segments_committed] + 1 WHERE [run_id] = 7;"
    )
    assert "IF @@ROWCOUNT <> 1 THROW" in batch


@pytest.mark.parametrize(
    "helper",
    [
        "set_run_status",
        "bump_fence",
        "set_step_status",
        "mark_dropped",
        "set_source_null",
        "set_meta_environment",
    ],
)
def test_an_update_of_one_state_row_fails_loud_when_it_changes_no_row(helper):
    batch = ALL_WRITES[helper]
    assert words(batch)[0] == "UPDATE"
    assert batch.endswith(
        "IF @@ROWCOUNT <> 1 THROW 51001, N'azsqlcd: a state write did not change one row', 1;"
    )


def test_the_locking_fence_read_waits_for_a_writer_and_the_plain_read_does_not():
    assert state.read_fence(7) == (
        "/* azsqlcd:read_fence */ SELECT [segments_committed] FROM [azsqlcd].[run] WHERE [run_id] = 7;"
    )
    locking = state.read_fence_locking(7)
    assert "FROM [azsqlcd].[run] WITH (READCOMMITTEDLOCK, ROWLOCK) WHERE [run_id] = 7;" in locking
    assert "WITH" not in words(state.read_fence(7))


def test_an_object_is_updated_first_and_inserted_only_when_no_row_was_updated():
    batch = state.upsert_object(KEY, run_id=7, capture={"kind": "PROCEDURE"}, source_sha256=None)
    sequence = [w for w in words(batch) if w in ("UPDATE", "INSERT", "MERGE", "IF")]
    assert sequence == ["UPDATE", "IF", "INSERT"]
    assert "; IF @@ROWCOUNT = 0 INSERT INTO [azsqlcd].[object]" in batch
    assert f"WHERE [object_key] = N'{KEY}';" in batch
    assert updated(batch)["source_sha256"] == "NULL" == inserted(batch)["source_sha256"]
    assert inserted(batch) == {"object_key": f"N'{KEY}'"} | updated(batch)  # both branches record the same
    assert inserted(batch)["status"] == "N'managed'" and inserted(batch)["capture_format"] == "1"


def test_the_stored_hash_is_the_hash_of_the_stored_capture_text():
    capture = {"kind": "VIEW", "definition": "CREATE VIEW v AS SELECT N'é' AS [a]\n"}
    literals = strings(state.upsert_object(KEY, run_id=7, capture=capture, source_sha256=SHA64))
    stored = [text for text in literals if text.startswith("{")]
    assert len(stored) == 2 and stored[0] == stored[1]
    assert json.loads(stored[0]) == capture
    assert literals.count(hashlib.sha256(stored[0].encode()).hexdigest()) == 2
    assert state.capture_sha256(capture) == hashlib.sha256(stored[0].encode()).hexdigest()


def test_a_dropped_object_keeps_its_row():
    batch = state.mark_dropped(KEY, 9)
    assert updated(batch) == {"status": "N'dropped'", "run_id": "9", "recorded_utc": "SYSUTCDATETIME()"}
    assert f"WHERE [object_key] = N'{KEY}';" in batch


def test_no_write_statement_removes_a_row():
    for helper, batch in ALL_WRITES.items():
        assert not {"DELETE", "MERGE", "TRUNCATE", "DROP"} & set(words(batch)), helper


def test_a_step_is_inserted_with_the_server_time_and_can_move_to_another_run():
    row = inserted(state.insert_step(run_id=7, kind="nontx", status="started", migration_id=MIGRATION))
    assert row == {
        "run_id": "7",
        "kind": "N'nontx'",
        "migration_id": f"N'{MIGRATION}'",
        "file_sha256": "NULL",
        "status": "N'started'",
        "applied_utc": "SYSUTCDATETIME()",
        "note": "NULL",
    }
    again = state.set_step_status(MIGRATION, "started", run_id=8)
    assert updated(again) == {"status": "N'started'", "applied_utc": "SYSUTCDATETIME()", "run_id": "8"}
    assert f"WHERE [migration_id] = N'{MIGRATION}';" in again
    assert "run_id" not in updated(state.set_step_status(MIGRATION, "ok"))


def test_the_source_checksum_is_cleared_and_nothing_else():
    assert updated(state.set_source_null(KEY)) == {"source_sha256": "NULL"}


def test_the_environment_of_meta_is_bound_again_only_to_a_known_name():
    batch = state.set_meta_environment("sandbox")
    assert batch.startswith("UPDATE [azsqlcd].[meta] SET [environment] = N'sandbox' WHERE [id] = 1;")


def test_a_note_is_one_line_cut_to_its_column_and_never_fails_the_write():
    (note,) = strings(
        state.set_run_status(7, "failed", note="line one\r\n  line\ttwo\x00\x1b " + "n" * 2000)
    )[1:2]
    assert note.startswith("line one line two n") and len(note) == 1000
    # nvarchar counts UTF-16 units: 600 characters outside the BMP are 1200 units
    (astral,) = strings(state.insert_step(run_id=7, kind="resolve", status="ok", note="\U0001f600" * 600))[2:]
    assert astral == "\U0001f600" * 200  # nvarchar(400), no half character at the cut
    assert updated(state.set_run_status(7, "ok", note="  \n "))["note"] == "NULL"


def test_a_name_that_does_not_fit_its_column_is_refused_not_cut():
    with pytest.raises(ValueError):
        state.insert_step(run_id=7, kind="migration", status="ok", migration_id="m" * 201)
    with pytest.raises(ValueError):
        state.set_step_status("", "ok")
    with pytest.raises(ValueError):
        state.mark_dropped(names.object_key("VIEW", "s", "v" * 300), 7)
    with pytest.raises(ValueError):
        state.set_source_null("VIEW:sales.v")  # not an object key
    with pytest.raises(ValueError):
        state.insert_run(**{**RUN, "tool_version": "0.1.0-" + "x" * 30})
    with pytest.raises(ValueError):
        state.insert_run(**{**RUN, "tool_version": "0.1.0\n"})


def test_text_that_the_engine_or_a_driver_would_change_is_refused_in_a_literal():
    assert state.literal("it's") == "N'it''s'"
    assert state.literal("a\\nb") == "N'a\\nb'"  # a backslash and the letter n
    for text in ("a\x00b", "a\\\nb", "a\\\r\nb"):  # NUL; a backslash before a line break is removed by T-SQL
        with pytest.raises(ValueError):
            state.literal(text)
    with pytest.raises(ValueError):
        state.mark_dropped(names.object_key("VIEW", "s", "a\x00b"), 7)


# ------------------------------------------------------------------ capture
def test_capture_json_does_not_depend_on_key_order():
    one = {"kind": "TRIGGER", "events": {"UPDATE": {"is_last": False, "is_first": True}}, "definition": "x"}
    two = {"definition": "x", "events": {"UPDATE": {"is_first": True, "is_last": False}}, "kind": "TRIGGER"}
    assert state.capture_json(one) == state.capture_json(two)
    assert state.capture_json(one) == (
        '{"definition":"x","events":{"UPDATE":{"is_first":true,"is_last":false}},"kind":"TRIGGER"}'
    )
    assert state.capture_sha256(one) == state.capture_sha256(two)
    assert state.capture_sha256(one) != state.capture_sha256({**one, "definition": "y"})


def test_capture_json_is_ascii_so_the_stored_text_is_the_hashed_text():
    capture = {"definition": "SELECT N'é\U0001f600' -- \\\n\x00\r\n"}
    text = state.capture_json(capture)
    assert text.isascii() and "\n" not in text and "\x00" not in text
    assert json.loads(text) == capture
    assert state.capture_sha256(capture) == hashlib.sha256(text.encode("ascii")).hexdigest()
    with pytest.raises(ValueError):
        state.capture_json({"x": float("nan")})


def test_the_fence_bump_can_join_the_batch_that_opens_the_transaction():
    db = FakeSession()
    db.respond("INSERT INTO [azsqlcd].[run]", [[(12,)]])
    assert state.rows(db, state.insert_run(**RUN)) == [(12,)]
    assert state.rows(db, "BEGIN TRANSACTION; " + state.bump_fence(12)) == []  # no result set is no rows
    assert db.trancount == 1  # the row-count guard of the bump does not read as the end of a transaction


def test_the_run_row_of_a_baseline_on_a_new_target_records_no_commit():
    """RP2-5: baseline on a target with no release writes release 0 and forty zeros. That is no
    commit: the plan must not show it, and must not link a compare to it."""
    run = state.RunRow(1, "baseline", "ok", 1, 0, "0" * 40, "2026-10-01T10:00:00.000")
    got = state.State(state.Meta(1, "sales", "dev"), (), run, (), {})
    assert (got.recorded_release_seq, got.recorded_git_sha) == (0, None)
    real = dataclasses.replace(run, release_seq=3, git_sha="c" * 40)
    assert dataclasses.replace(got, latest_ok_run=real).recorded_git_sha == "c" * 40


# ------------------------------------------------------------------ live defect E2E-2
def user_batch(script: str) -> str:
    return batches(script)[index_of(script, "CREATE", "USER")]


def test_a_name_under_another_sid_stops_the_script_and_says_what_to_do():
    # live run 2026-10-07: the old message named no action
    user = user_batch(state.setup_sql(config(), "dev", "sales-dev"))
    stop = user[: user.index("ELSE IF")]
    (message,) = [text for text in strings(stop) if text.startswith("azsqlcd setup:")]
    assert "taken by another database principal" in message
    assert "drop that user, then run this script again" in message
    assert "NOEXEC" in words(stop)  # no right goes to a principal with this name and another SID
    assert flat_sql(stop).count("[sid] <>") == 1 and "[name] <>" not in flat_sql(stop)


def test_a_sid_under_the_user_name_of_another_environment_does_not_stop_the_script():
    """E2E-2: dev, sandbox and test share the plan identity in the template. A SID is unique in a
    database, so the user of this environment cannot be made beside the user of the other one. The
    script went no further: no deploy user, no grant, no meta check. Now the user of the identity
    is kept and gets the rights through a role with the name of the user of this environment."""
    script = state.setup_sql(config(), "dev", "sales-dev")
    user = user_batch(script)
    twin = user[user.index("ELSE IF") : user.rindex("ELSE IF")]
    assert "NOEXEC" not in words(twin) and "RAISERROR" not in words(twin)
    name = next(n for n in quoted_names(user) if n.startswith("azsqlcd-") and n.endswith("-plan"))
    flat = flat_sql(twin)
    assert f"IF DATABASE_PRINCIPAL_ID ( N'{name}' ) IS NULL EXEC ( N'CREATE ROLE [{name}];' )" in flat
    assert f"N'ALTER ROLE [{name}] ADD MEMBER ' + @twin" in flat
    said = "".join(strings(twin[twin.index("PRINT") :]))  # the message is built with the name of the user
    assert said.startswith("azsqlcd setup:") and "That user is kept" in said and "its name is " in said
    assert "drop that user and the role, then run this script again" in said
    # the batches after it are the ones of every other run: grants to the name, then the deploy user
    rest = batches(script)[index_of(script, "CREATE", "USER") + 1 :]
    assert any(words(b)[:1] == ["GRANT"] and name in quoted_names(b) for b in rest)
    assert sum({"CREATE", "USER"} <= set(words(b)) for b in rest) == 1


def test_a_role_of_this_script_does_not_stop_the_second_run_of_the_script():
    user = user_batch(state.setup_sql(config(), "dev", "sales-dev"))
    stop = flat_sql(user[: user.index("ELSE IF")])
    # a principal with the name and another SID stops, but not the role that the script made for a twin
    assert "AND NOT ( [type] = N'R' AND EXISTS ( SELECT 1 FROM sys . database_principals AS t WHERE" in stop


def flat_sql(sql: str) -> str:
    return " ".join(t.text for t in tokens(sql) if t.kind not in lex.TRIVIA)
