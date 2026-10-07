"""One release, from object files to a database, through the command line only.

Every step of the scenario is a call of cli.main in a temporary git repository: gen, lint, verify,
build, targets, deploy, plan. No database and no network: where a command opens a session it gets
a FakeSession that is scripted as an empty database with the state tables of the setup script.
The fake is test_runner.Db (state rows, the modules that were sent) with the table catalog of
test_catalog_tables.rows_from_model, which is visible from the moment CREATE TABLE was sent.
"""

import dataclasses
import json
import shutil
from pathlib import Path

import pytest

from azsqlcd import chain, cli, emit, lex, names, release, state
from azsqlcd.model import Model, Schema
from azsqlcd.modules import read_module
from azsqlcd.release import Bundle
from azsqlcd.state import ObjectRow, State, StepRow
from unit.test_catalog_tables import COLUMNS, CUSTOMER, ORDER, SALES, rows_from_model
from unit.test_cli import TOKEN, Ci, Sessions, Tokens, ci_files, git, write
from unit.test_runner import RUN_ID, Db, parse_only_session, recorded, row, run_row
from unit.test_tables import captures_of

REPO = Path(__file__).resolve().parents[2]
MODEL = Model([SALES, ORDER, CUSTOMER])
VIEW = (
    "schema/views/sales.vw_OpenOrders.sql",
    "CREATE OR ALTER VIEW [sales].[vw_OpenOrders] AS\n"
    "SELECT [OrderId] FROM [sales].[Order] WHERE [Status] = 0;\n",
)
PROCEDURE = (
    "schema/procedures/sales.usp_CountOrders.sql",
    "CREATE OR ALTER PROCEDURE [sales].[usp_CountOrders] AS\n"
    "SELECT COUNT(*) AS [n] FROM [sales].[vw_OpenOrders];\n",
)
VIEW_KEY = names.object_key("VIEW", "sales", "vw_OpenOrders")
PROCEDURE_KEY = names.object_key("PROCEDURE", "sales", "usp_CountOrders")
TOML = """
[project]
name = "sales"
tenant_id = "11111111-1111-1111-1111-111111111111"
table_model = true
module_chunk = 100
min_token_minutes = 20

[identities]
plan = "22222222-2222-2222-2222-222222222222"
deploy = "33333333-3333-3333-3333-333333333333"

[env.dev]
plan_identity = "plan"
deploy_identity = "deploy"
drift = "report"
lock_timeout_ms = 30000
applock_wait_s = 600
job_timeout_minutes = 120
gated = false
targets = [{ id = "sales-dev", server = "sql-sales-dev.database.windows.net", database = "sales" }]

[env.prod]
plan_identity = "plan"
deploy_identity = "deploy"
drift = "block"
lock_timeout_ms = 10000
applock_wait_s = 600
job_timeout_minutes = 120
gated = true
targets = [{ id = "sales-prod", server = "sql-sales-prod.database.windows.net", database = "sales" }]
"""


def words(batch: str) -> set[str]:
    """The unquoted words of a batch, upper case."""
    return {t.text.upper() for t in lex.significant(lex.tokenize(batch)) if t.kind == "word"}


def database(bundle: Bundle, st: State, *, tables_exist: bool = False) -> Db:
    """The session of a deploy or a plan on a database with this recorded state.

    The modules that exist are those of the object rows; a module text that is sent creates its
    module. The table catalog is the model of the release, from the moment CREATE TABLE was sent
    (tables_exist: from the start).
    """
    module_files = {path: data for path, data in bundle.files.items() if "/tables/" not in path}
    module_files = {p: d for p, d in module_files.items() if "/schemas/" not in p and p.count("/") == 2}
    user_objects = [("sales", "Order", "U"), ("sales", "Customer", "U")] if tables_exist else []
    db = Db(Bundle(bundle.manifest, module_files), st, user_objects=user_objects)
    rows = rows_from_model(MODEL)

    def answer(name: str):
        def result(batch: str) -> list:
            visible = tables_exist or db.sent("CREATE TABLE [sales].[Order]")
            return [[tuple(found.values()) for found in rows[name]] if visible else []]

        return result

    for name in COLUMNS:
        db.respond(f"/* azsqlcd:read_{name} */", answer(name))
    return db


@pytest.fixture
def ci(tmp_path, monkeypatch) -> Ci:
    return ci_files(tmp_path, monkeypatch)


def test_a_release_goes_from_files_to_a_database_through_the_command_line(tmp_path, ci, capsys):
    repo, dist = tmp_path / "db-sales", tmp_path / "dist"
    shutil.copytree(REPO / "templates" / "db-repo", repo)
    write(repo, "azsqlcd.toml", TOML)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "repository from the template")
    first = git(repo, "rev-parse", "HEAD")

    # ---- a pull request: two tables in a new schema, a view and a procedure
    for obj in MODEL.values():
        path = names.path_for(obj.kind, None if isinstance(obj, Schema) else obj.schema, obj.name)
        write(repo, path, emit.emit_object_file(obj))
    write(repo, *VIEW)
    write(repo, *PROCEDURE)
    root = ["--root", str(repo)]
    assert cli.main(["gen", "--base", first, "--name", "create_sales", *root]) == 0
    migration_file = "0001__create_sales.sql"
    migration = chain.parse_migration((repo / "migrations" / migration_file).read_text(), migration_file)
    assert cli.main(["lint", *root]) == 0
    assert cli.main(["verify", "--base", first, *root, "--ci", "github"]) == 0
    assert ci.outputs() == {"exit_code": "0", "reason_code": "OK"}

    # ---- the merge to main, and the release
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "sales schema")
    commit = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/remotes/origin/main", commit)
    assert cli.main(["build", "--commit", "HEAD", "--out", str(dist), *root, "--ci", "github"]) == 0
    built = ci.outputs()
    assert built["release"] == "r2" and built["exit_code"] == "0"
    digest = built["digest"]
    bundle = release.read_bundle(dist, digest)
    assert bundle.manifest.commit == commit
    where = ["--bundle", str(dist), "--digest", digest]

    assert cli.main(["targets", *where, "--env", "dev", "--ci", "github"]) == 0
    targets = ci.outputs()
    assert [target["id"] for target in json.loads(targets["matrix"])] == ["sales-dev"]
    assert targets["timeout"] == "120"

    # ---- dev: deploy with an inline plan, on an empty database that has the state tables
    dev = [*where, "--env", "dev", "--target", "sales-dev"]
    db, second = database(bundle, recorded(seq=0)), parse_only_session()
    sessions = Sessions(db, second)
    capsys.readouterr()
    code = cli.main(
        ["deploy", *dev, "--inline-plan", "--out", str(tmp_path / "report"), "--ci", "github"],
        session_factory=sessions,
        token_provider=Tokens(),
    )
    printed = capsys.readouterr()
    assert code == 0, printed.err
    deployed = ci.outputs()
    assert (deployed["exit_code"], deployed["reason_code"]) == ("0", "OK")

    batches = [batch.text for batch in migration.batches]
    module_texts = [read_module(path, text.encode()).text for path, text in (VIEW, PROCEDURE)]
    assert len(batches) >= 3 and any(text.startswith("CREATE TABLE [sales].[Order]") for text in batches)
    # the order of a unit of work: migration batches, then modules, then state rows, then COMMIT
    db.assert_order(
        "BEGIN TRANSACTION",
        *batches,
        *module_texts,
        "UPDATE [azsqlcd].[object]",
        "INSERT INTO [azsqlcd].[step]",
        "COMMIT TRANSACTION;",
        "UPDATE [azsqlcd].[run] SET [status] = N'ok'",
    )
    last_work = max(db.batches.index(text) for text in (*batches, *module_texts))
    first_state = db.index_of(lambda batch: batch.startswith("UPDATE [azsqlcd].[object]"))
    assert last_work < first_state < db.batches.index("COMMIT TRANSACTION;")
    # no batch is sent twice; the second session parsed each text once and ran nothing
    for text in (*batches, *module_texts):
        assert db.batches.count(text) == 1 and second.batches.count(text) == 1
    assert second.batches[0] == "SET PARSEONLY ON;" and len(db.sent("COMMIT TRANSACTION;")) == 1

    report = json.loads((tmp_path / "report" / "report.json").read_text())
    assert (report["exit_code"], report["reason_code"], report["run_id"]) == (0, "OK", RUN_ID)
    assert report["modules_deployed"] == [VIEW_KEY, PROCEDURE_KEY]
    plan_doc = json.loads((tmp_path / "report" / "plan.json").read_text())
    assert plan_doc["plan_sha256"] == deployed["plan_sha256"] == report["plan_sha256"]
    # nothing of the batches, of the definitions or of the token is in what the command printed
    for secret in (TOKEN, "SELECT [OrderId]", "COUNT(*)", "CREATE TABLE"):
        assert secret not in printed.out + printed.err

    # ---- the same release again: the database records it, so nothing is sent
    objects = {key: row(key, text) for key, (_, text) in ((VIEW_KEY, VIEW), (PROCEDURE_KEY, PROCEDURE))}
    objects |= {
        key: ObjectRow("managed", None, 1, captured, state.capture_sha256(captured))
        for key, captured in captures_of(MODEL).items()
    }
    for key, object_row in objects.items():  # the state of the second run is what the first run wrote
        (written,) = db.sent(
            lambda b, key=key: b.startswith("UPDATE [azsqlcd].[object]") and f"N'{key}'" in b
        )
        assert f"N'{object_row.catalog_sha256}'" in written
    sha = chain.file_sha256((repo / "migrations" / migration_file).read_bytes())
    steps = [
        StepRow(1, RUN_ID, "migration", migration_file, sha, "ok", None),
        StepRow(2, RUN_ID, "modules", None, None, "ok", "deployed 2, dropped 0"),
    ]
    latest = dataclasses.replace(run_row(RUN_ID, "ok", seq=2), git_sha=commit)
    after = State(recorded().meta, (), latest, tuple(steps), objects)
    again = database(bundle, after, tables_exist=True)
    code = cli.main(
        ["deploy", *dev, "--inline-plan", "--out", str(tmp_path / "report2"), "--ci", "github"],
        session_factory=Sessions(again, parse_only_session()),
        token_provider=Tokens(),
    )
    assert code == 0 and ci.outputs()["reason_code"] == "OK"
    ddl = {"CREATE", "ALTER", "DROP", "BEGIN", "COMMIT", "INSERT", "EXEC", "EXECUTE"}
    assert [batch for batch in again.batches if words(batch) & ddl and "sp_getapplock" not in batch] == [
        "EXEC sys.sp_releaseapplock @Resource = N'azsqlcd:deploy', @LockOwner = N'Session';"
    ]
    assert json.loads((tmp_path / "report2" / "report.json").read_text())["run_id"] is None

    # ---- prod is gated: a plan job, then the deploy of that plan. An inline plan is refused
    prod = [*where, "--env", "prod", "--target", "sales-prod"]
    plan_db = database(bundle, recorded(seq=0, env="prod"))
    code = cli.main(
        ["plan", *prod, "--out", str(tmp_path / "plan"), "--ci", "github"],
        session_factory=Sessions(plan_db, parse_only_session()),
        token_provider=Tokens(),
    )
    planned = ci.outputs()
    assert code == 0 and (planned["pending"], planned["reason_code"]) == ("true", "OK")
    assert json.loads((tmp_path / "plan" / "plan.json").read_text())["plan_sha256"] == planned["plan_sha256"]
    assert not words(" ".join(plan_db.batches)) & {"CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "BEGIN"}

    never = Sessions()
    code = cli.main(
        ["deploy", *prod, "--inline-plan", "--out", str(tmp_path / "refused"), "--ci", "github"],
        session_factory=never,
        token_provider=Tokens(),
    )
    refused = ci.outputs()
    assert (code, refused["exit_code"], refused["reason_code"]) == (22, "22", "INLINE_PLAN_GATED")
    assert never.opened == []
    assert (
        json.loads((tmp_path / "refused" / "report.json").read_text())["reason_code"] == "INLINE_PLAN_GATED"
    )

    prod_db = database(bundle, recorded(seq=0, env="prod"))
    code = cli.main(
        [
            "deploy",
            *prod,
            *("--expect-plan-file", str(tmp_path / "plan" / "plan.json")),
            *("--out", str(tmp_path / "report-prod")),
            *("--approved-by", "dba-1", "--approved-utc", "2026-10-07T09:00:00Z"),
            *("--triggering-actor", "dev-1", "--ci-run-url", "https://github.example/runs/7"),
            *("--ci", "github"),
        ],
        session_factory=Sessions(prod_db),
        token_provider=Tokens(),
    )
    approved = ci.outputs()
    assert code == 0 and approved["plan_sha256"] == planned["plan_sha256"]
    (run_insert,) = prod_db.sent("INSERT INTO [azsqlcd].[run]")
    assert "N'dba-1'" in run_insert and "N'dev-1'" in run_insert and "'2026-10-07T09:00:00" in run_insert
    for text in (*batches, *module_texts):
        assert prod_db.batches.count(text) == 1
