"""The pure parts of scripts/live_spike.py and scripts/live_acceptance.py.

No test here connects to a database: the scripts get a FakeSession through their `connect`
argument. What is tested is what must hold before the owner runs a script: the guards come before
any write, the cleanup stays inside its schema, every item of the design has a probe, and the
repository of the acceptance run is one that the planner accepts.
"""

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import json
import multiprocessing
import re
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from azsqlcd import chain, lex, modules, names, plan, release, runner
from azsqlcd.config import load_config
from azsqlcd.errors import ToolError, retry_safe
from azsqlcd.session import AccessToken
from azsqlcd.sqlerrors import sql_error
from azsqlcd.state import Meta, ObjectRow, RunRow, State, StepRow
from support.fake_session import FakeSession

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SERVER = "contoso-scratch.database.windows.net"
DATABASE = "scratch"
LOGIN = "owner@example.org"

# The L ids of live_spikes in the blueprint (design/blueprint.meta.json), copied by hand. A new L
# item of the design is added here first; the test below then asks for its probe.
DESIGN_SPIKE_IDS = [
    "L1",
    "L2",
    "L3",
    "L4",
    "L5",
    "L6",
    "L7",
    "L8",
    "L9",
    "L10",
    "L11",
    "L12",
    "L13",
    "L14",
    "L15",
    "L16",
    "L17",
]


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # live_acceptance imports live_spike by this name
    spec.loader.exec_module(module)
    return module


live_spike = _load("live_spike")
live_acceptance = _load("live_acceptance")


SIGN_IN_VARIABLES = (
    "AZSQLCD_AUTH",
    "AZSQLCD_MANAGED_IDENTITY_CLIENT_ID",
    "AZSQLCD_SQL_USER",
    "AZSQLCD_SQL_PASSWORD",
)


@pytest.fixture(autouse=True)
def no_sign_in_of_the_machine(monkeypatch) -> None:
    """A workstation can have the sign-in variables of the tool set. No test reads them: a login
    with the name of an object of a test would be hidden in the log that the test reads."""
    for name in SIGN_IN_VARIABLES:
        monkeypatch.delenv(name, raising=False)


class Sessions:
    """The `connect` of a script: a new FakeSession for each call, all scripted the same way."""

    def __init__(
        self,
        *,
        edition: int = 5,
        db_name: str = DATABASE,
        environment: str | None = None,
        objects: list[tuple[str, str, str]] | None = None,
    ) -> None:
        self.opened: list[FakeSession] = []
        self._guard = (edition, db_name, 1 if environment else None)
        self._environment = environment
        self._objects = objects or []

    def __call__(self) -> FakeSession:
        session = FakeSession()
        session.respond("azsqlcd:live_guard_meta */", [[(self._environment,)]])
        session.respond("azsqlcd:live_guard */", [[self._guard]])
        session.respond("azsqlcd:live_logins */", [[(LOGIN, "dbo", LOGIN)]])
        left = [list(self._objects)]  # the objects are there once; a DROP removes them
        session.respond("azsqlcd:live_objects */", lambda _: [left.pop()] if left else [[]])
        self.opened.append(session)
        return session

    @property
    def batches(self) -> list[str]:
        return [batch for session in self.opened for batch in session.batches]


def first_word(batch: str) -> str:
    return lex.significant(lex.tokenize(batch))[0].text.upper()


def writes(batches: list[str]) -> list[str]:
    """The batches that are not one SELECT: everything that can change a database or a session."""
    return [batch for batch in batches if first_word(batch) != "SELECT"]


def never_connect() -> FakeSession:
    raise AssertionError("the script connected before its arguments were checked")


def spike(
    tmp_path: Path, connect: Any, *, database: str = DATABASE, confirm: str = DATABASE, items: str = "L13"
) -> int:
    arguments = ["--server", SERVER, "--database", database, "--confirm-disposable-database", confirm]
    return live_spike.main([*arguments, "--items", items, "--out", str(tmp_path)], connect=connect)


def acceptance(tmp_path: Path, connect: Any, *, database: str = DATABASE, confirm: str = DATABASE) -> int:
    arguments = ["--server", SERVER, "--database", database, "--confirm-disposable-database", confirm]
    return live_acceptance.main([*arguments, "--out", str(tmp_path)], connect=connect)


# ------------------------------------------------------------------ guards
@pytest.mark.parametrize("script", [spike, acceptance])
def test_a_confirm_value_that_differs_from_the_database_name_stops_before_any_connection(
    script, tmp_path, capsys
):
    assert script(tmp_path, never_connect, database="scratch", confirm="Scratch") == 22
    assert "LIVE_NOT_CONFIRMED" in capsys.readouterr().err


@pytest.mark.parametrize("script", [spike, acceptance])
def test_the_spike_refuses_a_database_whose_name_differs_from_the_confirm_value(script, tmp_path, capsys):
    # the arguments agree with each other; the session is in another database
    sessions = Sessions(db_name="sales")
    assert script(tmp_path, sessions) == 22
    assert "LIVE_DB_NAME" in capsys.readouterr().err
    assert writes(sessions.batches) == []


@pytest.mark.parametrize("script", [spike, acceptance])
@pytest.mark.parametrize("environment", ["dev", "sandbox", "test", "preprod", "prod", "Disposable"])
def test_it_refuses_a_database_bound_to_a_real_environment(script, environment, tmp_path, capsys):
    sessions = Sessions(environment=environment)
    assert script(tmp_path, sessions) == 22
    assert "LIVE_BOUND_ENVIRONMENT" in capsys.readouterr().err
    assert writes(sessions.batches) == []


@pytest.mark.parametrize("script", [spike, acceptance])
@pytest.mark.parametrize("edition", [2, 3, 4, 6, 8, 9, 11])
def test_it_refuses_when_engine_edition_is_not_5(script, edition, tmp_path, capsys):
    # 2 to 4 are SQL Server (D2: no SQL Server anywhere), 8 is Managed Instance
    sessions = Sessions(edition=edition)
    assert script(tmp_path, sessions) == 22
    assert "LIVE_ENGINE_EDITION" in capsys.readouterr().err
    assert writes(sessions.batches) == []


@pytest.mark.parametrize("script", [spike, acceptance])
@pytest.mark.parametrize("database", ["sales-prod", "PRODUCTION", "reproduction"])
def test_a_database_name_that_contains_prod_stops_before_any_connection(script, database, tmp_path, capsys):
    assert script(tmp_path, never_connect, database=database, confirm=database) == 22
    assert "LIVE_PROD_NAME" in capsys.readouterr().err


def test_the_guards_read_and_change_nothing_on_a_disposable_database():
    for sessions in (Sessions(), Sessions(environment="disposable")):
        session = sessions()
        live_spike.refuse_unless_disposable(session, DATABASE, DATABASE)
        assert writes(session.batches) == []


# ------------------------------------------------------------------ the items of the design
def test_every_spike_item_id_of_the_design_has_a_function():
    assert set(DESIGN_SPIKE_IDS) <= set(live_spike.ITEM_IDS)
    assert len(set(live_spike.ITEM_IDS)) == len(live_spike.ITEM_IDS)
    assert set(live_spike.ITEMS) == set(live_spike.ITEM_IDS) == set(live_spike.TITLES)
    probes = list(live_spike.ITEMS.values())
    assert all(callable(probe) for probe in probes)
    assert len(set(probes)) == len(probes), "two item ids share one probe"


def test_every_driver_gate_and_every_constant_is_decided_by_an_item_that_exists():
    assert sorted(live_spike.GATES) == ["G-D1", "G-D2", "G-D3", "G-D4", "G-D5"]
    assert set(live_spike.GATES.values()) | set(live_spike.CONSTANTS.values()) <= set(live_spike.ITEM_IDS)
    # each constant is a switch of runner.py; a rename there must not leave a dead name here
    assert all(isinstance(getattr(runner, constant), bool) for constant in live_spike.CONSTANTS)


def test_one_failed_gate_decides_for_the_other_driver_and_a_missing_gate_decides_nothing():
    passed = {item: "pass" for item in live_spike.GATES.values()}
    assert "keep mssql-python" in live_spike.summary(passed)["driver_decision"]
    assert "pyodbc" in live_spike.summary(passed | {"L4": "fail"})["driver_decision"]
    for open_result in ("inconclusive", "manual"):
        assert live_spike.summary(passed | {"X5": open_result})["driver_decision"].startswith("open")
    without_one = {item: result for item, result in passed.items() if item != "L2"}
    assert live_spike.summary(without_one)["driver_gates"]["G-D1"] == "not run"
    assert live_spike.summary(without_one)["driver_decision"].startswith("open")


def test_a_constant_is_unlocked_only_by_a_pass_of_its_item():
    assert live_spike.summary({"L7": "pass", "L6": "inconclusive"})["may_be_set_true"] == {
        "MODULE_TEXT_READBACK": True,
        "RECONCILE_BY_LOCKING_READ": False,
    }


def test_a_spike_session_has_the_options_of_a_runner_session():
    # a probe must see what a deploy sees. L1 also calls this function of the runner, by this name:
    # a change of the options or of the name must fail here, not in the run of the owner
    session = FakeSession()
    with contextlib.suppress(RuntimeError, ToolError):  # the fake has no option row to read back
        runner.set_session_options(session, 30000)
    assert session.batches[0] == live_spike.SESSION_OPTIONS


# ------------------------------------------------------------------ cleanup
SPIKE_DROP = re.compile(
    r"DROP (TABLE|VIEW|PROCEDURE|FUNCTION|SEQUENCE|SYNONYM) \[azsqlcd_spike\]\.\[[^\]]+\];"
    r"|ALTER INDEX \[[^\]]+\] ON \[azsqlcd_spike\]\.\[[^\]]+\] ABORT;"
    r"|IF SCHEMA_ID\(N'azsqlcd_spike'\) IS NOT NULL DROP SCHEMA \[azsqlcd_spike\];"
    r"|IF DATABASE_PRINCIPAL_ID\(N'azsqlcd_spike_user'\) IS NOT NULL DROP USER \[azsqlcd_spike_user\];"
)


def session_with(objects: list[tuple[str, str, str]], *, resumable: list[tuple[str, str, str]] | None = None):
    session = FakeSession()
    left = [objects]
    session.respond("azsqlcd:live_objects */", lambda _: [left.pop()] if left else [[]])
    session.respond("azsqlcd:live_resumable */", [resumable or []])
    return session


def test_cleanup_drops_only_objects_in_schema_azsqlcd_spike():
    session = session_with(
        [("azsqlcd_spike", "l2_t", "U"), ("azsqlcd_spike", "l7_v", "V"), ("azsqlcd_spike", "l11_f", "FN")],
        resumable=[("azsqlcd_spike", "l17_big", "l17_ix")],
    )
    sent = live_spike.remove_spike_objects(session)
    changed = writes(session.batches)
    assert changed == sent
    assert all(SPIKE_DROP.fullmatch(batch) for batch in changed), changed
    assert sum(batch.startswith("DROP ") for batch in changed) == 3
    # the two queries that find the objects name the schema as a literal
    for query in ("azsqlcd:live_objects */", "azsqlcd:live_resumable */"):
        assert all("WHERE s.[name] = N'azsqlcd_spike'" in batch for batch in session.sent(query))


@pytest.mark.parametrize("query", ["objects", "resumable"])
def test_a_row_of_another_schema_stops_the_cleanup_before_any_statement(query):
    foreign = [("dbo", "Orders", "U")] if query == "objects" else [("dbo", "Orders", "IX_Orders")]
    mine = [("azsqlcd_spike", "l2_t", "U")]
    session = session_with(
        foreign + mine if query == "objects" else mine, resumable=foreign if query == "resumable" else None
    )
    with pytest.raises(RuntimeError, match="schema 'dbo'"):
        live_spike.remove_spike_objects(session)
    assert writes(session.batches) == []


def test_an_object_type_that_the_cleanup_does_not_know_stops_it_before_any_drop():
    session = session_with([("azsqlcd_spike", "l2_t", "U"), ("azsqlcd_spike", "queue", "SQ")])
    with pytest.raises(RuntimeError, match="SQ"):
        live_spike.remove_spike_objects(session)
    assert writes(session.batches) == []


def test_objects_that_stay_after_the_drops_fail_the_cleanup_and_keep_the_schema():
    session = FakeSession()
    session.respond("azsqlcd:live_objects */", [[("azsqlcd_spike", "stuck", "U")]])  # never goes away
    with pytest.raises(RuntimeError, match="not complete"):
        live_spike.remove_spike_objects(session)
    assert not session.sent("DROP SCHEMA")


def test_views_are_dropped_before_tables_and_tables_before_functions():
    # a schema-bound view holds its table; a CHECK constraint holds the function that it calls
    session = session_with(
        [("azsqlcd_spike", "f", "FN"), ("azsqlcd_spike", "t", "U"), ("azsqlcd_spike", "v", "V")]
    )
    live_spike.remove_spike_objects(session)
    session.assert_order("DROP VIEW", "DROP TABLE", "DROP FUNCTION", "DROP SCHEMA")


ACCEPT_RESET = re.compile(
    r"DROP (TABLE|VIEW|PROCEDURE) \[azsqlcd_accept\]\.\[[^\]]+\];"
    r"|IF SCHEMA_ID\(N'azsqlcd_accept'\) IS NOT NULL DROP SCHEMA \[azsqlcd_accept\];"
    r"|IF OBJECT_ID\(N'\[azsqlcd\]\.\[(object|step|run|meta)\]', N'U'\) IS NOT NULL "
    r"DROP TABLE \[azsqlcd\]\.\[(object|step|run|meta)\];"
)


def test_the_acceptance_reset_drops_its_schema_and_the_four_state_tables_and_nothing_else():
    session = session_with([("azsqlcd_accept", "Item", "U"), ("azsqlcd_accept", "vw_Item", "V")])
    live_acceptance.reset(session)
    changed = writes(session.batches)
    assert all(ACCEPT_RESET.fullmatch(batch) for batch in changed), changed
    # object and step point at run with a foreign key
    session.assert_order("[azsqlcd].[object];", "[azsqlcd].[step];", "[azsqlcd].[run];", "[azsqlcd].[meta];")


# ------------------------------------------------------------------ a run of the spike
def test_the_spike_checks_the_guards_then_removes_leftovers_then_makes_its_schema_and_cleans_up(tmp_path):
    sessions = Sessions(environment="disposable", objects=[("azsqlcd_spike", "left_over", "U")])
    assert spike(tmp_path, sessions, items="L13") == 0  # L13 without its flag is manual: no probe runs
    first, last = sessions.opened[0], sessions.opened[-1]
    first.assert_order(
        "azsqlcd:live_guard */",
        "azsqlcd:live_guard_meta */",
        "DROP TABLE [azsqlcd_spike].[left_over];",
        "DROP SCHEMA [azsqlcd_spike];",
        "CREATE SCHEMA [azsqlcd_spike];",
    )
    assert first.index_of("azsqlcd:live_guard_meta */") < first.batches.index(writes(first.batches)[0])
    assert last is not first and last.sent("DROP SCHEMA [azsqlcd_spike];") and last.closed
    assert all(session.closed for session in sessions.opened)

    item = json.loads((tmp_path / "spike" / "L13.json").read_text(encoding="utf-8"))
    assert (item["id"], item["result"]) == ("L13", "manual")
    assert set(item) == {"id", "title", "result", "observed", "decides"}
    assert any("--soak-past-expiry-minutes 90" in step for step in item["observed"]["steps"])
    totals = json.loads((tmp_path / "spike" / "summary.json").read_text(encoding="utf-8"))
    assert totals["items"] == {"L13": "manual"}
    assert totals["driver_decision"].startswith("open")


def test_an_item_that_stops_on_an_unplanned_error_is_inconclusive_and_the_cleanup_still_runs(tmp_path):
    sessions = Sessions()
    # L16 asks the fake for fence facts that it does not have
    assert spike(tmp_path, sessions, items="L16,L12") == 0
    stopped = json.loads((tmp_path / "spike" / "L16.json").read_text(encoding="utf-8"))
    assert stopped["result"] == "inconclusive" and "stopped_by" in stopped["observed"]
    assert json.loads((tmp_path / "spike" / "L12.json").read_text(encoding="utf-8"))["result"] == "manual"
    assert sessions.opened[-1].sent("DROP SCHEMA [azsqlcd_spike];")


def test_an_unknown_item_id_is_refused_by_the_argument_parser(tmp_path):
    with pytest.raises(SystemExit) as stop:
        spike(tmp_path, never_connect, items="L1,L99")
    assert stop.value.code == 2


def test_the_item_list_needs_no_database_arguments(capsys):
    assert live_spike.main(["--list"], connect=never_connect) == 0
    listed = capsys.readouterr().out
    assert all(item_id in listed for item_id in live_spike.ITEM_IDS)


# ------------------------------------------------------------------ files that can be committed
def test_server_database_and_login_names_do_not_reach_a_file(tmp_path):
    hide = live_spike.private_names(SERVER, DATABASE, [LOGIN])
    observed = {
        "error": {
            "text": f'Cannot open database "{DATABASE}" requested by the login. Login failed for {LOGIN}.',
            "other": f"Database 'SCRATCH' on server 'contoso-scratch' / {SERVER} is not available.",
        },
        "rows": [("scratchpad stays", b"\x01\xff", 7, None, True)],
    }
    result = live_spike.ItemResult("X1", "title", "pass", observed, "decides")
    live_spike.write_json(tmp_path / "X1.json", live_spike.item_json(result, hide))
    text = (tmp_path / "X1.json").read_text(encoding="utf-8")
    assert LOGIN not in text and SERVER not in text and "contoso-scratch" not in text
    assert '"scratch"' not in text.lower() and "'scratch'" not in text.lower()
    stored = json.loads(text)["observed"]
    assert stored["error"]["text"] == (
        'Cannot open database "DATABASE_NAME" requested by the login. Login failed for LOGIN_NAME.'
    )
    assert stored["rows"] == [["scratchpad stays", "0x01ff", 7, None, True]]  # a part of a word is not a name


def test_a_run_of_some_items_keeps_the_captured_texts_of_the_others(tmp_path):
    path = tmp_path / "error_texts.json"
    live_spike.merge_json(path, {"1222": {"text": "Lock request time out period exceeded."}})
    live_spike.merge_json(path, {"208": {"text": "Invalid object name 'x'."}})
    assert sorted(json.loads(path.read_text(encoding="utf-8"))) == ["1222", "208"]


def test_only_an_attempt_that_raised_is_kept_as_an_error_text():
    fixtures = live_spike.Fixtures()
    fixtures.error("no error", 208, {"raised": False, "result_sets": []})
    fixtures.error(
        "1222 case", 1222, {"raised": True, "error": {"text": "Lock request time out period exceeded."}}
    )
    assert fixtures.errors == {
        "1222 case": {"expected_number": 1222, "text": "Lock request time out period exceeded."}
    }


def test_normal_forms_of_two_databases_differ_only_where_the_engine_text_differs():
    here = {"default": [{"input": "int: 0", "engine": "((0))"}, {"input": "int: -1", "engine": "((-1))"}]}
    there = {"default": [{"input": "int: 0", "engine": "((0))"}, {"input": "int: -1", "engine": "(-(1))"}]}
    assert live_spike.normal_form_differences(here, here) == []
    assert live_spike.normal_form_differences(here, there) == [
        {"class": "default", "input": "int: -1", "here": "((-1))", "there": "(-(1))"}
    ]
    # an input that only one database has is a difference too
    assert len(live_spike.normal_form_differences(here, {"default": here["default"][:1]})) == 1


def test_a_token_is_fetched_again_only_when_it_has_less_than_ten_minutes_left():
    clock = [1000.0]
    minted: list[AccessToken] = []

    class Mint:
        def get(self) -> AccessToken:
            minted.append(AccessToken(f"token-{len(minted)}", int(clock[0]) + 3600))
            return minted[-1]

    provider = live_spike.CachedTokenProvider(Mint(), now=lambda: clock[0])
    assert provider.get() is provider.get() and len(minted) == 1
    clock[0] += 3600 - 601
    assert provider.get() is minted[0]
    clock[0] += 2  # 599 seconds left
    assert provider.get() is minted[1]


def test_the_later_statement_cases_run_each_in_a_transaction_that_is_rolled_back():
    session = FakeSession()
    cases = live_spike.later_statement_cases(session, "[azsqlcd_spike].[l2_t]")
    assert [case["case"] for case in cases] == [name for name, _ in live_spike.L2_CASES]
    assert session.trancount == 0
    assert len(session.sent("BEGIN TRANSACTION;")) == len(live_spike.L2_CASES)
    # the fake raises nothing and stays in the transaction: exactly the case that the gate must fail
    assert not any(case["detected"] for case in cases)


# ------------------------------------------------------------------ the session in a helper process
class ThreadContext:
    """Stands in for the spawn context: the body of the helper runs in a thread, over a real pipe."""

    Pipe = staticmethod(multiprocessing.Pipe)

    class Process:
        def __init__(self, target: Any, args: tuple[Any, ...], daemon: bool) -> None:
            self._thread = threading.Thread(target=target, args=args, daemon=daemon)

        def start(self) -> None:
            self._thread.start()

        def join(self, timeout: float | None = None) -> None:
            self._thread.join(timeout)

        def is_alive(self) -> bool:
            return self._thread.is_alive()

        def terminate(self) -> None:
            raise AssertionError("the helper did not end when it was told to")


def test_the_helper_session_gives_its_spid_runs_each_batch_and_ends_when_told(monkeypatch):
    session = FakeSession()
    session.fail_on("KILL", sql_error("Lock request time out period exceeded."))
    monkeypatch.setattr(live_spike.multiprocessing, "get_context", lambda method: ThreadContext)
    monkeypatch.setattr(live_spike, "live_connect", lambda server, database, provider: session)
    helper = live_spike.Background(SERVER, DATABASE)
    assert helper.spid == session.spid
    helper.submit("SELECT @@TRANCOUNT;")
    assert helper.result(5)["result_sets"] == [[[0]]]
    helper.submit("KILL 99;", delay_s=0.01)
    refused = helper.result(5)
    assert refused["raised"] and refused["error"]["number"] == 1222  # the error crosses the pipe as data
    assert helper.result(0.05) is None  # nothing was submitted: no answer in time
    helper.close()
    assert session.closed
    assert session.batches == [live_spike.SESSION_OPTIONS, live_spike.SPID, "SELECT @@TRANCOUNT;", "KILL 99;"]


def test_a_helper_that_gets_no_session_tells_the_parent_why(monkeypatch):
    def no_session(server: str, database: str, provider: Any) -> FakeSession:
        raise retry_safe("CONNECT_FAILED", "no connection after 1 attempt(s)")

    monkeypatch.setattr(live_spike.multiprocessing, "get_context", lambda method: ThreadContext)
    monkeypatch.setattr(live_spike, "live_connect", no_session)
    with pytest.raises(RuntimeError, match="CONNECT_FAILED"):
        live_spike.Background(SERVER, DATABASE)


# ------------------------------------------------------------------ the repository of the acceptance run
def recorded_after(bundle: release.Bundle | None) -> State:
    """The state of a database that holds everything of a release: every migration, every module."""
    meta = Meta(1, live_acceptance.PROJECT, live_acceptance.ENV)
    if bundle is None:
        return State(meta, (), None, (), {})
    entries = chain.parse_sum(bundle.files[chain.SUM_PATH].decode("utf-8")).entries
    steps = tuple(
        StepRow(
            number, 1, "nontx" if entry.mode == "nontx" else "migration", entry.file, entry.sha256, "ok", None
        )
        for number, entry in enumerate(entries, start=1)
    )
    objects = {}
    for path, data in bundle.files.items():
        if path.startswith("schema/") and path.endswith(".sql"):
            module = modules.read_module(path, data)
            objects[module.key] = ObjectRow("managed", module.checksum, 1, {}, "0" * 64)
    seq, commit = bundle.manifest.release_seq, bundle.manifest.commit
    run = RunRow(1, "deploy", "ok", 0, seq, commit, "2026-01-01T00:00:00.000")
    return State(meta, (), run, steps, objects)


@pytest.fixture(scope="module")
def acceptance_bundles(tmp_path_factory) -> list[release.Bundle]:
    work = tmp_path_factory.mktemp("acceptance")
    trees = live_acceptance.releases(SERVER, DATABASE)
    commits = live_acceptance.commit_releases(work / "repository", trees)
    return live_acceptance.build_bundles(work / "repository", commits, work / "releases")


def shape(work: plan.Work) -> tuple[str, list[str], list[str]]:
    return (
        work.outcome,
        [unit.kind for unit in work.units],
        [step.kind for unit in work.units for step in unit.steps],
    )


def test_each_release_of_the_acceptance_repository_is_the_unit_of_work_that_its_checks_need(
    acceptance_bundles,
):
    # each release is planned on a database that holds the release before it
    before = [None, *acceptance_bundles[:-1]]
    shapes = [
        shape(plan.pending_work(bundle, recorded_after(previous), 2))
        for bundle, previous in zip(acceptance_bundles, before, strict=True)
    ]
    assert shapes == [
        ("work", ["tx"], ["batch", "batch", "deploy_module", "deploy_module", "readback"]),
        ("record", [], []),  # A6: nothing to send, a run row to write
        ("work", ["tx"], ["deploy_module", "readback"]),  # one changed module
        ("work", ["tx"], ["batch", "batch"]),
        ("work", ["nontx"], ["batch"]),  # A8: alone in its release
        ("work", ["nontx"], ["batch"]),
        ("work", ["tx"], ["batch", "batch"]),
        ("work", ["tx"], ["batch", "batch"]),
        ("work", ["tx"], ["batch", "readback"]),  # a data batch: every managed object is read back
        ("work", ["tx"], ["deploy_module", "deploy_module", "drop_module", "readback"]),
        ("work", ["tx"], ["deploy_module", "readback"]),
        ("work", ["tx"], ["deploy_module", "readback"]),
    ]
    data_unit = plan.pending_work(acceptance_bundles[8], recorded_after(acceptance_bundles[7]), 2).units[0]
    assert data_unit.steps[-1].all_managed


def test_the_release_after_the_broken_module_deploys_on_a_database_that_skipped_it(acceptance_bundles):
    # r11 never deploys (its module does not parse); r12 must not need it (A7: no migration in it)
    work = plan.pending_work(acceptance_bundles[11], recorded_after(acceptance_bundles[9]), 2)
    assert shape(work) == ("work", ["tx"], ["deploy_module", "readback"])


def test_every_migration_of_the_acceptance_repository_belongs_to_the_release_that_added_it(
    acceptance_bundles,
):
    a = live_acceptance
    assert [bundle.manifest.release_seq for bundle in acceptance_bundles] == list(range(1, 13))
    assert acceptance_bundles[-1].manifest.chain_added_in == {
        a.M1: 1,
        a.M2: 4,
        a.M3: 5,
        a.M4: 6,
        a.M5: 7,
        a.M6: 8,
        a.M7: 9,
    }


def test_the_acceptance_config_names_the_target_and_allows_the_inline_plan(acceptance_bundles):
    a = live_acceptance
    config = load_config(acceptance_bundles[0].files[release.CONFIG_PATH].decode("utf-8"))
    environment = config.env[a.ENV]
    assert [(t.id, t.server, t.database) for t in environment.targets] == [(a.TARGET, SERVER, DATABASE)]
    assert not environment.gated  # deploy --inline-plan is refused where an environment is gated
    assert config.project.name == a.PROJECT and not config.project.table_model
    # ShortLivedToken says one minute: it must be less than the tool asks for
    assert config.project.min_token_minutes * 60 > 60
    assert config.identities[environment.deploy_identity] == a.DEPLOY_CLIENT_ID


def test_the_object_keys_of_the_checks_are_the_keys_of_the_module_files(acceptance_bundles):
    a = live_acceptance
    first, tenth = acceptance_bundles[0].files, acceptance_bundles[9].files
    assert modules.read_module(a.VIEW_PATH, first[a.VIEW_PATH]).key == a.VIEW_KEY
    assert modules.read_module(a.COUNT_PATH, first[a.COUNT_PATH]).key == a.COUNT_KEY
    assert modules.read_module(a.LEGACY_PATH, tenth[a.LEGACY_PATH]).key == a.LEGACY_KEY
    assert a.COUNT_PATH not in tenth
    assert [t.object_key for t in chain.parse_tombstones(tenth[chain.TOMBSTONES_PATH].decode())] == [
        a.COUNT_KEY
    ]
    assert a.ITEM == names.qualified(a.SCHEMA, "Item")


@pytest.mark.parametrize(
    ("migration", "batch", "trigger"),
    [("M2", 2, "[UQ_Item_name]"), ("M3", 1, "[IX_Item_qty]"), ("M5", 2, "[flag]")],
)
def test_the_text_that_starts_an_event_is_in_one_batch_of_the_repository_only(
    acceptance_bundles, migration, batch, trigger
):
    # a second match would start the kill or the second runner at another batch than the check says
    files = acceptance_bundles[-1].files
    hits = []
    for path in sorted(files):
        if path.startswith("migrations/") and path != chain.SUM_PATH:
            parsed = chain.parse_migration(files[path].decode("utf-8"), path.removeprefix("migrations/"))
            hits += [(parsed.file, n) for n, b in enumerate(parsed.batches, start=1) if trigger in b.text]
    assert hits == [(getattr(live_acceptance, migration), batch)]


# ------------------------------------------------------------------ the watched session and the checks
def test_a_watched_session_sends_every_batch_and_runs_each_event_once_at_its_batch():
    inner = FakeSession()
    seen: list[tuple[str, int]] = []
    watched = live_acceptance.Watched(
        inner,
        "[flag]",
        before=lambda w: seen.append(("before", len(inner.batches))),
        after=lambda w: seen.append(("after", len(inner.batches))),
    )
    batches = ["ALTER TABLE t ADD [note] int;", "ALTER TABLE t ADD [flag] bit;", "SELECT [flag] FROM t;"]
    for batch in batches:
        watched.execute(batch)
    assert inner.batches[1:] == batches and watched.batches == batches
    assert seen == [("before", 2), ("after", 3)]  # around the second batch, and not again at the third
    assert watched.spid == inner.spid and not watched.closed
    watched.close()
    assert inner.closed and watched.closed


def test_a_watched_session_counts_module_texts_and_nothing_else():
    watched = live_acceptance.Watched(FakeSession())
    watched.execute("CREATE OR ALTER VIEW [s].[v] AS SELECT 1 AS [x];")
    watched.execute("\n  create or alter procedure [s].[p] as select 1;")
    watched.execute("UPDATE [azsqlcd].[object] SET [catalog_capture] = N'CREATE OR ALTER VIEW ...';")
    assert watched.modules_sent() == 2


def new_acceptance() -> Any:
    return live_acceptance.Acceptance(
        database=DATABASE,
        config=load_config(live_acceptance.config_toml(SERVER, DATABASE)),
        bundles=[],
        trees=[],
        connect=FakeSession,
        provider=live_spike.CachedTokenProvider(None),  # type: ignore[arg-type]  # never asked
        inspector=FakeSession(),
        tool_digest="0" * 64,
    )


def test_only_the_first_session_of_a_run_is_watched():
    # with an inline plan the second session is the one of the syntax check: it gets the same text
    factory, seen = new_acceptance().watch("[flag]")
    first, second = factory(), factory()
    assert seen == [first] and isinstance(second, FakeSession)


def test_a_wrong_exit_code_stops_the_run_and_reads_no_fact():
    a, outcome = new_acceptance(), live_acceptance.Outcome(21, "BATCH_FAILED", "step 2 failed")

    def facts() -> dict[str, bool]:
        raise AssertionError("facts are read only when the exit code is the expected one")

    with pytest.raises(live_acceptance.Halt):
        a.expect("check", "row", outcome, 0, "OK", facts)
    assert [(c.result, c.expected, c.seen) for c in a.checks] == [("fail", "0 OK", "21 BATCH_FAILED")]


def test_a_wrong_reason_code_or_fact_fails_the_check_and_the_run_goes_on():
    a = new_acceptance()
    a.expect("reason", "row", live_acceptance.Outcome(23, "GUARD_FAILED"), 23, "CONNECTION_LOST_TX")
    a.expect(
        "fact", "row", live_acceptance.Outcome(0, "OK"), 0, "OK", lambda: {"x": True, "no step row": False}
    )
    a.expect("both right", "row", live_acceptance.Outcome(0, "OK"), 0, "OK", lambda: {"x": True})
    assert [c.result for c in a.checks] == ["fail", "fail", "pass"]
    assert a.checks[1].detail == "not true: no step row"


def test_after_a_stop_the_later_scenarios_are_reported_as_not_run(monkeypatch):
    a = new_acceptance()

    def stops(acceptance: Any) -> None:
        acceptance.expect("wrong exit", "row", live_acceptance.Outcome(22, "STALE_PLAN"), 0, "OK")

    def breaks(acceptance: Any) -> None:
        raise AssertionError("a scenario ran after the run was stopped")

    monkeypatch.setattr(live_acceptance, "SCENARIOS", (("one", stops), ("two", breaks), ("three", breaks)))
    live_acceptance.run_scenarios(a)
    assert [(c.name, c.result) for c in a.checks] == [
        ("wrong exit", "fail"),
        ("two", "not run"),
        ("three", "not run"),
    ]


def test_a_scenario_that_raises_is_a_failed_check_and_stops_the_run(monkeypatch):
    a = new_acceptance()

    def raises(acceptance: Any) -> None:
        raise RuntimeError("a catalog query gave 0 rows")

    monkeypatch.setattr(live_acceptance, "SCENARIOS", (("one", raises), ("two", raises)))
    live_acceptance.run_scenarios(a)
    assert [(c.name, c.result) for c in a.checks] == [("one", "fail"), ("two", "not run")]
    assert "RuntimeError" in a.checks[0].detail


# ------------------------------------------------------------------ probes against a scripted engine
# The answers below are the ones that the live run of 2026-10-07 captured on Azure SQL Database
# (live spike report, engine facts 1 to 3, 7 and 13). A probe must pass on them, and must fail when
# the tool or the driver gives another answer.
S = "[azsqlcd_spike]"
# (batch that was sent, sys.sql_modules.definition after it): rows of catalog_rows.json of that run
LIVE_MODULE_PAIRS = [
    (
        f"-- comment above the header\n/* block comment */\nCREATE OR ALTER PROCEDURE {S}.[l7_p]\nAS\n",
        f"-- comment above the header\n/* block comment */\nCREATE   PROCEDURE {S}.[l7_p]\nAS\n",
    ),
    (
        f"ALTER VIEW {S}.[l7_v]\r\nAS\r\nSELECT 1 AS [x]   \r\n;\r\n",
        f"CREATE VIEW {S}.[l7_v]\r\nAS\r\nSELECT 1 AS [x]   \r\n;\r\n",
    ),
    (
        f"create   or   alter   procedure {S}.[l7_lc] as select 1 as [x];",
        f"create         procedure {S}.[l7_lc] as select 1 as [x];",
    ),
    (
        f"alter   procedure {S}.[l7_lc] as select 1 as [x];",
        f"CREATE   procedure {S}.[l7_lc] as select 1 as [x];",
    ),
    (
        f"CREATE /* c1 */ OR /* c2 */ ALTER -- c3\n VIEW {S}.[l7_cm] AS SELECT 1 AS [x];",
        f"CREATE /* c1 */  /* c2 */  -- c3\n VIEW {S}.[l7_cm] AS SELECT 1 AS [x];",
    ),
    (
        f"CREATE\nOR\tALTER\r\nPROCEDURE {S}.[l7_ws] AS SELECT 1 AS [x];",
        f"CREATE\n\t\r\nPROCEDURE {S}.[l7_ws] AS SELECT 1 AS [x];",
    ),
    (
        f"\n\n  Create Or Alter Proc {S}.[l7_mx] As Select N'é中\t' As [x];  \n\n\t",
        f"\n\n  Create   Proc {S}.[l7_mx] As Select N'é中\t' As [x];  \n\n\t",
    ),
    (
        f"CREATE VIEW {S}.[l7_c] AS SELECT 1 AS [x];",
        f"CREATE VIEW {S}.[l7_c] AS SELECT 1 AS [x];",
    ),
]
_GAP = r"(?:\s|/\*.*?\*/|--[^\n]*\n)+"
_ENGINE_VERB = re.compile(rf"\b(CREATE)({_GAP})OR({_GAP})ALTER\b|\bALTER\b", re.IGNORECASE | re.DOTALL)


def engine_keeps(batch: str) -> str:
    """The engine side of the scripted database: the same fact as LIVE_MODULE_PAIRS, by pattern."""
    return _ENGINE_VERB.sub(lambda m: m[1] + m[2] + m[3] if m[1] else "CREATE", batch, count=1)


def spike_ctx(connect: Any) -> Any:
    options = live_spike.Options(
        server=SERVER, database=DATABASE, confirm=DATABASE, out=Path("not-written"), items=()
    )
    return live_spike.Ctx(options, connect, connect())


@pytest.mark.parametrize(("sent", "definition"), LIVE_MODULE_PAIRS)
def test_the_spike_expects_the_module_text_that_the_live_engine_stored(sent, definition):
    assert live_spike.engine_stored_text(sent) == definition
    assert engine_keeps(sent) == definition  # the scripted engine of this file says the same


# ------------------------------------------------------------------ L4 (S1)
def l4_session(stores: Any) -> FakeSession:
    session = FakeSession()

    def stored(_: str) -> list[list[tuple[Any, ...]]]:
        sent = next(batch for batch in session.batches if batch.startswith("CREATE OR ALTER PROCEDURE"))
        return [[(live_spike._L4_NAME, stores(sent))]]

    session.respond("FROM sys.objects AS o JOIN sys.sql_modules", stored)
    session.respond(lambda batch: batch.startswith("EXEC "), [[(live_spike.L4_LITERAL, "x")]])
    session.respond("sys.dm_exec_sql_text", [[(live_spike.L4_LITERAL, live_spike.L4_ECHO_BATCH)]])
    return session


def test_l4_passes_when_the_definition_is_the_batch_with_the_verb_that_the_engine_stores():
    # S1: the engine stores CREATE in place of CREATE OR ALTER; that is no change by the driver
    result = live_spike.l4_batch_text(spike_ctx(lambda: l4_session(engine_keeps)))
    assert result.observed["definition_equal"] and result.result == "pass"


@pytest.mark.parametrize(
    "stores",
    [
        lambda sent: sent,  # not what the engine does: the probe would no longer measure the engine
        lambda sent: engine_keeps(sent).replace("?", "@P1", 1),  # a driver that read ? as a marker
        lambda sent: engine_keeps(sent).replace("{d '2021-02-03'}", "'2021-02-03'"),  # an escape clause
    ],
)
def test_l4_fails_when_one_byte_of_the_stored_text_differs(stores):
    result = live_spike.l4_batch_text(spike_ctx(lambda: l4_session(stores)))
    assert not result.observed["definition_equal"] and result.result == "fail"


# ------------------------------------------------------------------ L7 (S2)
class ModuleStore(FakeSession):
    """A FakeSession that keeps module text in sys.sql_modules, by the rule that it is given."""

    def __init__(self, keeps: Any = engine_keeps) -> None:
        super().__init__()
        self.definitions: dict[str, tuple[str, str]] = {}
        self._keeps = keeps
        self.respond(self._is_module, self._store)
        self.respond("FROM sys.sql_modules AS m WHERE m.[object_id] = OBJECT_ID(", self._definition)
        self.respond("azsqlcd:capture_modules */", self._capture)

    @staticmethod
    def _is_module(batch: str) -> bool:
        try:
            lex.module_header(batch)
        except lex.LexError:
            return False
        return True

    def _store(self, batch: str) -> list[list[tuple[Any, ...]]]:
        header = lex.module_header(batch)
        self.definitions[header.name] = (header.kind, self._keeps(batch))
        return []

    def _definition(self, batch: str) -> list[list[tuple[Any, ...]]]:
        found = re.search(r"OBJECT_ID\(N'\[azsqlcd_spike\]\.\[(\w+)\]'\)", batch)
        assert found, batch
        return [[(self.definitions[found[1]][1],)]] if found[1] in self.definitions else [[]]

    def _capture(self, batch: str) -> list[list[tuple[Any, ...]]]:
        found = re.search(r"N'(\w+:\[azsqlcd_spike\]\.\[(\w+)\])'", batch)
        assert found, batch
        kind, definition = self.definitions[found[2]]
        code = {"PROCEDURE": "P", "VIEW": "V", "FUNCTION": "FN", "TRIGGER": "TR"}[kind]
        parent = ("azsqlcd_spike", "l7_t", 0, "INSERT:0:0") if kind == "TRIGGER" else (None,) * 4
        return [[(found[1], "azsqlcd_spike", found[2], code, definition, 1, 1, 0, None, 1, *parent)]]


def l7_result(monkeypatch: Any, keeps: Any = engine_keeps, problems: list[str] | None = None) -> Any:
    if problems is not None:
        # the answer of the read-back is given: the test is then about the rule of the probe alone
        monkeypatch.setattr(runner, "_module_problems", lambda module, capture: list(problems))
    return live_spike.l7_stored_module_text(spike_ctx(lambda: ModuleStore(keeps)))


def test_l7_passes_when_the_engine_stores_the_measured_text_and_the_read_back_accepts_it(monkeypatch):
    result = l7_result(monkeypatch, problems=[])
    assert result.result == "pass"
    assert result.observed["not_as_stored_text"] == []
    assert runner.MODULE_TEXT_READBACK is True  # the value of the tool; the probe gave it back


def test_l7_fails_when_the_read_back_of_the_runner_refuses_what_the_engine_stored(monkeypatch):
    # the state before modules.stored_text: the runner compares with the file text, verb included
    result = l7_result(monkeypatch, problems=["definition"])
    assert result.result == "fail"
    assert result.observed["not_as_stored_text"] == []
    assert runner.MODULE_TEXT_READBACK is True


@pytest.mark.parametrize("value", [False, True])
def test_l7_switches_the_compare_on_for_the_probe_and_gives_the_constant_its_value_back(monkeypatch, value):
    monkeypatch.setattr(runner, "MODULE_TEXT_READBACK", value)
    seen: list[bool] = []

    def problems(module: Any, capture: Any) -> list[str]:
        seen.append(runner.MODULE_TEXT_READBACK)
        return []

    monkeypatch.setattr(runner, "_module_problems", problems)
    live_spike.l7_stored_module_text(spike_ctx(lambda: ModuleStore()))
    assert seen and all(seen) and runner.MODULE_TEXT_READBACK is value


def test_l7_fails_when_the_engine_stores_another_text_than_the_measured_one(monkeypatch):
    result = l7_result(monkeypatch, keeps=lambda batch: engine_keeps(batch).rstrip(), problems=[])
    assert result.result == "fail"
    assert "view with CRLF line ends and spaces at line ends: ALTER" in result.observed["not_as_stored_text"]


def test_l7_asks_the_read_back_of_the_runner_itself_with_the_compare_switched_on(monkeypatch):
    # no stand-in here: the constant may be set True only when this function of the tool accepts
    # every text that the engine stored. The result follows the tool, whatever its state is.
    result = l7_result(monkeypatch)
    answers = [o["runner_read_back_problems"] for o in result.observed["modules"] if o["step"] != "ALTER"]
    assert len(answers) == 2 * len(live_spike._l7_modules())
    assert (result.result == "pass") == all(answer == [] for answer in answers)
    assert runner.MODULE_TEXT_READBACK is True


@pytest.mark.skipif(
    "stored_checksum" not in inspect.getsource(runner._module_problems),
    reason="runner._module_problems still compares with the file text (spike patch 04, defect T1): "
    "on this tree L7 must fail, which test_l7_asks_the_read_back_of_the_runner_itself checks",
)
def test_l7_unlocks_the_constant_with_the_read_back_of_this_tool(monkeypatch):
    assert l7_result(monkeypatch).result == "pass"


def test_l7_sends_every_header_shape_also_as_alter_and_covers_the_shapes_of_the_live_run():
    session = ModuleStore()
    live_spike.l7_stored_module_text(spike_ctx(lambda: session))
    sent = [batch for batch in session.batches if ModuleStore._is_module(batch)]
    cases = live_spike._l7_modules()
    # new, again, ALTER: also for the lower-case verb (S2). The one case with comments inside the
    # verb has no ALTER step, as in the live run.
    assert len(sent) == 3 * len(cases) - 1
    altered = [batch for batch in sent if lex.module_header(batch).verb == "ALTER"]
    assert len(altered) == len(cases) - 1
    assert any(batch.startswith("alter   procedure") for batch in altered)
    texts = [text for _, _, text in cases]
    assert any("/* c1 */ OR /* c2 */ ALTER" in text for text in texts)
    assert any("CREATE\nOR\tALTER\r\nPROCEDURE" in text for text in texts)
    assert any("Create Or Alter Proc" in text for text in texts)


# ------------------------------------------------------------------ X2 (S3)
def guard_session(*, error_ends_transaction: bool = True, swallowed_raises: bool = True) -> FakeSession:
    session = FakeSession()

    def guard_row(_: str) -> list[list[tuple[Any, ...]]]:
        if session.trancount == 0:
            # outside a transaction CURRENT_TRANSACTION_ID() gives the statement a transaction of
            # its own, and XACT_STATE() reads 1 (engine fact 2)
            session.transaction_id += 1
        return [[(session.trancount, 1, session.transaction_id)]]

    session.respond(live_spike.GUARD, guard_row)
    session.fail_on(
        lambda batch: batch == "SELECT 1/0;",
        sql_error("Divide by zero error encountered."),
        keeps_transaction=not error_ends_transaction,
    )
    if swallowed_raises:
        session.fail_on(
            "BEGIN TRY",
            sql_error(
                "Uncommittable transaction is detected at the end of the batch. "
                "The transaction is rolled back."
            ),
        )
    return session


def test_x2_passes_on_the_guard_values_of_the_live_engine():
    # S3: after a run-time error the guard reads (0, 1, new id), and an error that a TRY block
    # swallowed raises 3998 at the end of its batch: the engine gives neither (0, 0) nor (1, -1)
    ctx = spike_ctx(guard_session)
    result = live_spike.x2_guard_values(ctx)
    assert result.result == "pass", result.observed
    assert result.observed["after_a_run_time_error"][:2] == [0, 1]
    assert result.observed["outside_a_transaction_without_the_transaction_id"] == [0, 0]
    assert result.observed["error_swallowed_by_try_catch_raised"]
    assert any(label.startswith("3998") for label in ctx.fixtures.errors)


@pytest.mark.parametrize(
    "engine", [{"error_ends_transaction": False}, {"swallowed_raises": False}], ids=["error", "try"]
)
def test_x2_fails_when_an_error_leaves_the_guard_values_of_a_sound_transaction(engine):
    # then the guard (1, 1, id) would not tell the runner that a statement failed
    result = live_spike.x2_guard_values(spike_ctx(lambda: guard_session(**engine)))
    assert result.result == "fail"


# ------------------------------------------------------------------ X1 (S5)
X1_TEXT = {
    208: "Invalid object name 'azsqlcd_spike.x1_missing'.",
    2714: "There is already an object named 'x1_t' in the database.",
    1222: "Lock request time out period exceeded.",
    1205: "Transaction (Process ID 98) was deadlocked on lock resources with another process and has "
    "been chosen as the deadlock victim. Rerun the transaction.",
    2627: "Violation of PRIMARY KEY constraint 'x1_pk'. Cannot insert duplicate key in object "
    "'azsqlcd_spike.x1_t'. The duplicate key value is (1).",
    245: "Conversion failed when converting the varchar value 'abc' to data type int.",
}


def x1_result(monkeypatch: Any, texts: dict[int, str], provider: Any = None) -> tuple[Any, Any]:
    from azsqlcd import sqlerrors

    exists: list[bool] = []  # the table of the probe, in the one database behind every session

    def connect() -> FakeSession:
        session = FakeSession()

        def create(_: str) -> list[list[tuple[Any, ...]]]:
            if exists:
                raise sql_error(texts[2714])
            exists.append(True)
            return []

        session.fail_on("x1_missing", sql_error(texts[208]))
        session.respond(lambda batch: batch.startswith("CREATE TABLE"), create)
        session.fail_on(lambda batch: batch.endswith("VALUES (1);"), sql_error(texts[2627]))
        session.fail_on("CAST('abc' AS int)", sql_error(texts[245]))
        session.fail_on("WITH (READCOMMITTEDLOCK, ROWLOCK)", sql_error(texts[1222]))
        return session

    victim = {"raised": True, "error": live_spike.error_facts(sql_error(texts[1205]))}
    monkeypatch.setattr(live_spike, "_deadlock", lambda ctx, table: [victim])
    ctx = spike_ctx(connect)
    ctx.provider = provider
    result = live_spike.x1_error_texts(ctx)
    unread = [number for number, text in texts.items() if sqlerrors.known_number(text) != number]
    return result, (ctx, unread)


def test_x1_checks_the_six_numbers_that_the_probe_raises(monkeypatch):
    # S5: 2627 and 245 were raised and written to the file, and left out of the check
    result, (_, unread) = x1_result(monkeypatch, X1_TEXT)
    assert result.observed["not_raised_by_the_probe"] == []
    assert sorted(result.observed["not_recognised"]) == sorted(unread)
    assert result.result == ("fail" if unread else "pass")


@pytest.mark.parametrize("number", [2627, 245])
def test_x1_fails_when_the_text_of_a_duplicate_key_or_a_conversion_is_not_read(monkeypatch, number):
    result, _ = x1_result(monkeypatch, X1_TEXT | {number: "a text that sqlerrors does not know"})
    assert number in result.observed["not_recognised"] and result.result == "fail"


def test_x1_keeps_the_text_of_a_refused_token_login_under_its_own_number(monkeypatch):
    class Token:
        def get(self) -> AccessToken:
            return AccessToken("not-a-token", 2**31)

    def refuse(driver: Any, keywords: Any, token_struct: bytes) -> FakeSession:
        raise sql_error("Login failed for user '<token-identified principal>'.")

    monkeypatch.setattr(live_spike, "load_driver", lambda: None)
    monkeypatch.setattr(live_spike.db, "open_session", refuse)
    _, (ctx, _) = x1_result(monkeypatch, X1_TEXT, provider=Token())
    kept = ctx.fixtures.errors
    assert kept["18456 connect to a database that does not exist"]["expected_number"] == 18456
    assert "4060" not in kept  # the engine did not give the text of 4060


# ------------------------------------------------------------------ X3 (S4, RO-2)
class Dependant:
    def __init__(self, key: str) -> None:
        self.key = key


def x3_result(monkeypatch: Any, broken: Any = None) -> tuple[Any, Any]:
    """X3 on a scripted engine. `broken` stands in for catalog.broken_references of the tool."""
    s = "[azsqlcd_spike]"

    def since_begin(session: FakeSession, text: str) -> bool:
        """The open transaction of the session holds a batch with this text."""
        if session.trancount == 0:
            return False
        last = len(session.batches) - 1 - session.batches[::-1].index("BEGIN TRANSACTION;")
        return any(text in batch for batch in session.batches[last:])

    def live_findings(session: FakeSession, module_key: str) -> list[str]:
        name = module_key.rsplit("[", 1)[1].rstrip("]")
        if name == "x3_p_gone":
            return ["ERROR_2020"]  # the engine raises 2020 and gives no row (engine fact 7)
        if since_begin(session, "DROP TABLE"):
            return ["ERROR_208"]
        dropped = since_begin(session, "DROP COLUMN")
        if name in ("x3_p_tmp", "x3_p_tmp_b"):  # RO-2: a sound procedure with a #temp table has findings
            return [f"COLUMNS_NOT_FOUND {s}.[x3_t]"]
        if name == "x3_p_upd":
            return [f"COLUMNS_NOT_FOUND {s}.[x3_t]", "UNRESOLVED [x]"]
        return ["ERROR_207"] if dropped and name.endswith("_b") else []

    def connect() -> FakeSession:
        session = FakeSession()

        read = "FROM sys.dm_sql_referenced_entities("
        # the first error of the read, as the driver gave it; it does not end the transaction
        for text, when in (
            (
                "Invalid object name 'azsqlcd_spike.x3_t'.",
                lambda batch: since_begin(session, "DROP TABLE"),
            ),
            (
                'The dependencies reported for entity "[azsqlcd_spike].[x3_p_gone]" might not include '
                "references to all columns.",
                lambda batch: "x3_p_gone" in batch,
            ),
            (
                "Invalid column name 'b'.",
                lambda batch: since_begin(session, "DROP COLUMN") and re.search(r"x3_\w+_b\]", batch),
            ),
        ):
            session.fail_on(
                lambda batch, when=when: read in batch and bool(when(batch)),
                sql_error(text),
                times=99,
                keeps_transaction=True,
            )
        session.respond(read, [[]])
        return session

    keys = [live_spike.key("VIEW", name) for name in ("x3_v_b", "x3_v_a", "x3_v_star")]
    monkeypatch.setattr(live_spike.catalog, "dependants_of", lambda session, k: [Dependant(k) for k in keys])
    monkeypatch.setattr(live_spike.catalog, "broken_references", broken or live_findings)
    ctx = spike_ctx(connect)
    return live_spike.x3_dependants_after_column_drop(ctx), ctx


def test_x3_passes_on_the_answers_of_the_live_engine(monkeypatch):
    # S4: a procedure that names a missing table gives error 2020 and no row, so no UNRESOLVED entry
    result, ctx = x3_result(monkeypatch)
    assert result.result == "pass", result.observed
    assert result.observed["module_that_names_no_object"]["value"] == ["ERROR_2020"]
    assert result.observed["view_whose_table_was_dropped"]["broken_references"]["value"] == ["ERROR_208"]
    assert result.observed["view_whose_table_was_dropped"]["guard_after_the_raw_read"][:2] == [1, 1]
    labels = sorted(ctx.fixtures.errors)
    assert "207 referenced entities after a column drop: view that uses the column" in labels
    assert "208 referenced entities of a view whose table was dropped" in labels
    assert "2020 referenced entities of a procedure that names a missing table" in labels
    assert not any(label.startswith("2020 referenced entities of a view") for label in labels)
    kept = ctx.fixtures.errors
    assert all(
        kept[label]["expected_number"] == int(label[:4].strip()) for label in labels if label[0] == "2"
    )


@pytest.mark.parametrize("case", ["missing table", "dropped column", "dropped table"])
def test_x3_fails_when_the_tool_does_not_report_a_broken_dependant(monkeypatch, case):
    def blind(session: FakeSession, module_key: str) -> list[str]:
        dropped_table = session.trancount > 0 and any("DROP TABLE" in b for b in session.batches[-3:])
        dropped_column = session.trancount > 0 and "DROP COLUMN" in session.batches[-1]
        if case == "dropped column" and dropped_column and module_key.endswith("[x3_v_b]"):
            # T3: the read re-raises the error of the engine in place of a finding
            raise sql_error("Invalid column name 'b'.")
        if case == "dropped table" and dropped_table:
            return []
        if module_key.endswith("[x3_p_gone]"):
            return [] if case == "missing table" else ["ERROR_2020"]
        if dropped_table:
            return ["ERROR_208"]
        return ["ERROR_207"] if dropped_column and module_key.endswith("_b]") else []

    result, _ = x3_result(monkeypatch, blind)
    assert result.result == "fail"


def test_x3_reads_the_temp_table_cases_before_and_after_the_change(monkeypatch):
    # RO-2: what the differential check of the tool compares, for the two shapes that a sound
    # procedure of WideWorldImporters has
    result, _ = x3_result(monkeypatch)
    cases = result.observed["cases"]
    wanted = [
        "procedure with a #temp table joined to the table",
        "procedure with a #temp table that uses the column",
        "procedure with UPDATE alias FROM #temp AS alias",
    ]
    assert set(wanted) <= set(cases)
    for name in wanted:
        before = cases[name]["broken_references_before"]
        after = cases[name]["broken_references_after_the_drop"]
        assert before["ok"] and after["ok"] and before["value"]
        assert cases[name]["findings_added_by_the_drop"] == sorted(set(after["value"]) - set(before["value"]))
    assert cases["view that uses the column"]["findings_added_by_the_drop"] == ["ERROR_207"]


def test_x3_makes_the_two_procedure_shapes_that_use_a_temp_table(monkeypatch):
    sessions: list[FakeSession] = []
    original = FakeSession.execute

    def record(self: FakeSession, batch: str) -> Any:
        if self not in sessions:
            sessions.append(self)
        return original(self, batch)

    monkeypatch.setattr(FakeSession, "execute", record)
    x3_result(monkeypatch)
    made = [b for session in sessions for b in session.batches if b.startswith("CREATE OR ALTER PROCEDURE")]
    assert any("JOIN #t AS t" in batch and "[x3_t]" in batch for batch in made)
    assert any(re.search(r"UPDATE x SET .* FROM #t AS x", batch) for batch in made)


# ------------------------------------------------------------------ the probes that the build asked for
BUILD_PROBE_IDS = ["X1", "X2", "X3", "X4", "X5", "X6", "X7", "X8", "X9", "X10"]


def test_every_probe_that_the_build_asked_for_has_an_item():
    assert set(BUILD_PROBE_IDS) <= set(live_spike.ITEM_IDS)
    assert all(live_spike.TITLES[item_id] for item_id in BUILD_PROBE_IDS)
    for word, item_id in (
        ("sequence", "X6"),
        ("numbered", "X7"),
        ("IMPLICIT_TRANSACTIONS", "X8"),
        ("temporal", "X9"),
        ("masked", "X10"),
    ):
        assert word in live_spike.TITLES[item_id]


def test_the_probes_send_the_texts_of_the_tool():
    # the probe must measure the batch that plan sends and the row that the runner reads
    assert live_spike.PARSEONLY_IS_OFF == plan._PARSEONLY_IS_OFF
    assert live_spike.PARSEONLY_OFF == plan._PARSEONLY_OFF


# L10: the parse-only session of plan
class ParseOnly(FakeSession):
    """SET PARSEONLY as the live engine handles it (engine fact 13)."""

    def __init__(self, *, off_works: bool = True) -> None:
        super().__init__()
        self.parse_only = False
        self._off_works = off_works

    def execute(self, batch: str) -> Any:
        if "SET PARSEONLY ON" in batch:
            self.parse_only = True
        if "SET PARSEONLY OFF" in batch and self._off_works:
            self.parse_only = False  # the setting is read when the batch is parsed
        if self.parse_only:
            self.batches.append(batch)
            if "SELECT FROM" in batch or "ADD COLUMN" in batch:
                raise sql_error("Incorrect syntax near the keyword 'FROM'.")
            return []
        if batch.endswith("SELECT 1 AS [ran];"):
            self.batches.append(batch)
            return [[(1,)]]
        return super().execute(batch)


def l10_result(**engine: Any) -> Any:
    def connect() -> FakeSession:
        session = ParseOnly(**engine)
        session.respond("SELECT COUNT(*) FROM sys.objects", [[(0,)]])
        return session

    return live_spike.l10_parse_only(spike_ctx(connect))


def test_l10_reads_the_check_of_plan_under_parseonly_on_and_after_off():
    result = l10_result()
    assert result.result == "pass", result.observed
    assert result.observed["parseonly_off_check_while_on"]["result_sets"] == []
    assert result.observed["parseonly_off_check_after_off"]["result_sets"] == [[[57]]]


def test_l10_fails_when_the_check_of_plan_cannot_tell_that_the_session_still_parses_only():
    result = l10_result(off_works=False)
    assert result.result == "fail"
    assert result.observed["parseonly_off_check_after_off"]["result_sets"] == []


# X6: sequence
def x6_result(after_restart: tuple[Any, ...]) -> Any:
    def connect() -> FakeSession:
        session = FakeSession()
        reads = [(10, 10, False), (10, 10, False), after_restart, (after_restart[0], after_restart[0], False)]
        session.respond("FROM sys.sequences", lambda _: [[reads.pop(0)]])
        session.respond("SELECT NEXT VALUE FOR", [[(10,)]])
        return session

    return live_spike.x6_sequence_restart(spike_ctx(connect))


@pytest.mark.parametrize(("after", "moves"), [((500, 500, False), True), ((10, 500, False), False)])
def test_x6_says_if_restart_with_moves_the_start_value_of_a_sequence(after, moves):
    result = x6_result(after)
    assert result.result == "pass"  # a fact for RO-3: both answers are a clear answer
    assert result.observed["start_value_moves_with_restart_with"] is moves
    assert result.observed["after_restart_with"][:2] == list(after[:2])


def test_x6_is_inconclusive_when_the_catalog_gives_no_number():
    assert x6_result((None, None, False)).result == "inconclusive"


# X7: catalog views that the export reads
def x7_result(monkeypatch: Any, *, numbered: Any = None, has_index: bool = True) -> Any:
    def connect() -> FakeSession:
        session = FakeSession()
        if numbered is None:
            session.respond("FROM sys.numbered_procedures", [[(0,)]])
        else:
            session.fail_on("FROM sys.numbered_procedures", numbered)
        session.respond("x7_v]') ORDER BY", [[("x7_cx", 1, "CLUSTERED", True)]])
        session.respond("x7_plain]') ORDER BY", [[]])
        return session

    indexed = live_spike.key("VIEW", "x7_v")
    monkeypatch.setattr(live_spike.catalog, "has_index", lambda session, k: has_index and k == indexed)
    monkeypatch.setattr(live_spike.catalog, "indexed_views", lambda session: [indexed] if has_index else [])
    fact = live_spike.catalog.ExportFact(live_spike.catalog.INDEXED_VIEW, "azsqlcd_spike", "x7_v", "V")
    monkeypatch.setattr(live_spike.catalog, "export_facts", lambda session: [fact] if has_index else [])
    return live_spike.x7_catalog_views(spike_ctx(connect))


def test_x7_passes_when_both_catalog_views_answer_and_the_tool_finds_the_indexed_view(monkeypatch):
    result = x7_result(monkeypatch)
    assert result.result == "pass", result.observed
    assert result.observed["sys_indexes_of_the_indexed_view"] == [["x7_cx", 1, "CLUSTERED", True]]
    assert result.observed["sys_indexes_of_a_plain_view"] == []


def test_x7_passes_when_the_database_has_no_view_of_numbered_procedures(monkeypatch):
    # error 208 is the one answer that catalog.export_facts reads as "no numbered procedure"
    missing = sql_error("Invalid object name 'sys.numbered_procedures'.")
    assert missing.number == 208
    assert x7_result(monkeypatch, numbered=missing).result == "pass"


@pytest.mark.parametrize(
    "engine",
    [{"numbered": sql_error("The SELECT permission was denied on the object.")}, {"has_index": False}],
    ids=["numbered procedures not readable", "the tool does not see the index"],
)
def test_x7_fails_when_the_export_could_not_read_its_facts(monkeypatch, engine):
    assert x7_result(monkeypatch, **engine).result == "fail"


# X8: the session-option row
def x8_result(*, off_works: bool = True) -> Any:
    def connect() -> FakeSession:
        session = FakeSession()
        state = {"implicit": 0, "set": False}

        def note(batch: str) -> list[list[tuple[Any, ...]]]:
            if "SET XACT_ABORT ON" in batch:
                state["set"] = True
            state["implicit"] = 2 if "IMPLICIT_TRANSACTIONS ON" in batch else 0 if off_works else 2
            return []

        def row(_: str) -> list[list[tuple[Any, ...]]]:
            if state["set"]:
                return [[(1, 1, 1, 1, 1, 1, 0, 16384, 512, state["implicit"], 30000, "us_english")]]
            return [[(1, 1, 1, 1, 1, 1, 0, 0, 0, state["implicit"], -1, "us_english")]]

        def table_read(_: str) -> list[list[tuple[Any, ...]]]:
            if state["implicit"] and session.trancount == 0:
                session.trancount, session.xact_state = 1, 1  # the statement opened a transaction
            return [[("x",)]]

        session.respond(lambda batch: batch.startswith("SET "), note)
        session.respond("azsqlcd:session_options */", row)
        session.respond("FROM sys.objects", table_read)
        return session

    return live_spike.x8_implicit_transactions(spike_ctx(connect))


def test_x8_reads_the_option_row_of_the_runner_with_implicit_transactions_on_and_off():
    result = x8_result()
    assert result.result == "pass", result.observed
    assert result.observed["with_implicit_transactions_on"]["IMPLICIT_TRANSACTIONS"] == 2
    assert result.observed["after_set_implicit_transactions_off"]["IMPLICIT_TRANSACTIONS"] == 0
    assert result.observed["trancount_after_a_table_read_while_on"] == 1
    assert result.observed["trancount_after_a_table_read_after_off"] == 0
    assert result.observed["runner_session_options"] == {"asserted": True}


def test_x8_fails_when_the_row_does_not_show_that_the_option_is_off():
    result = x8_result(off_works=False)
    assert result.result == "fail"
    assert result.observed["runner_session_options"]["reason_code"] == "SESSION_OPTIONS"


# X9: temporal table
def x9_result(*, temporal_type: int = 2, drop_refused: bool = True) -> tuple[Any, list[FakeSession]]:
    opened: list[FakeSession] = []

    def connect() -> FakeSession:
        session = FakeSession()
        versioned = [False]
        exist: list[str] = []

        def tables(_: str) -> list[list[tuple[Any, ...]]]:
            if not session.trancount:
                return [[]]  # the transaction of the item was rolled back
            if versioned[0]:
                return [
                    [
                        ("x9_t", temporal_type, "SYSTEM_VERSIONED_TEMPORAL_TABLE", "x9_t_history"),
                        ("x9_t_history", 1, "HISTORY_TABLE", None),
                    ]
                ]
            return [[(name, 0, "NON_TEMPORAL_TABLE", None) for name in exist]]

        def switch(batch: str) -> list[list[tuple[Any, ...]]]:
            versioned[0] = "SYSTEM_VERSIONING = ON" in batch
            if batch.startswith("CREATE TABLE"):
                exist[:] = ["x9_t", "x9_t_history"]
            return []

        def drop(batch: str) -> list[list[tuple[Any, ...]]]:
            if versioned[0] and drop_refused:
                raise sql_error(
                    "Drop table operation failed on table because it is not a supported operation."
                )
            exist.remove(re.search(r"\[(x9_\w+)\];", batch)[1])  # type: ignore[index]
            return []

        session.respond("SYSTEM_VERSIONING = O", switch)
        session.respond(lambda batch: batch.startswith("DROP TABLE"), drop)
        session.respond("FROM sys.tables AS t", tables)
        session.respond("FROM sys.periods AS p", [[("SYSTEM_TIME", 1, "valid_from", "valid_to")]])
        session.respond(
            "[generated_always_type]",
            [[("valid_from", 1, "AS_ROW_START", True), ("valid_to", 2, "AS_ROW_END", True)]],
        )
        session.respond("SELECT COUNT(*) FROM sys.objects", [[(0,)]])
        opened.append(session)
        return session

    return live_spike.x9_temporal_table(spike_ctx(connect)), opened


def test_x9_reads_the_catalog_rows_of_a_temporal_table_and_leaves_nothing():
    result, opened = x9_result()
    assert result.result == "pass", result.observed
    assert result.observed["tables_while_versioned"][0][:2] == ["x9_t", 2]
    assert result.observed["tables_while_versioned"][1][:2] == ["x9_t_history", 1]
    assert result.observed["period"] == [["SYSTEM_TIME", 1, "valid_from", "valid_to"]]
    assert [row[1] for row in result.observed["tables_after_versioning_off"]] == [0, 0]
    assert result.observed["objects_left"] == 0
    # every statement of the item runs in a transaction that is rolled back: the cleanup of the
    # spike sends a plain DROP TABLE, which a versioned table refuses
    for session in opened:
        made = [n for n, batch in enumerate(session.batches) if "SYSTEM_VERSIONING = ON" in batch]
        for position in made:
            before = session.batches[:position]
            assert before.count("BEGIN TRANSACTION;") > sum("ROLLBACK" in batch for batch in before)
        assert session.trancount == 0


def test_x9_fails_when_the_catalog_does_not_show_the_table_as_versioned():
    result, _ = x9_result(temporal_type=0)
    assert result.result == "fail"


# X10: masked columns
def x10_result(*, refused: str = "", unmask_works: bool = True) -> tuple[Any, Any]:
    def connect() -> FakeSession:
        session = FakeSession()
        masks: dict[str, str] = {}

        def add(batch: str) -> list[list[tuple[Any, ...]]]:
            found = re.search(r"ADD \[(\w+)\] .* MASKED WITH \(FUNCTION = '(.*)'\) NULL;", batch)
            assert found, batch
            if refused and refused in found[2]:
                raise sql_error("Incorrect syntax near 'datetime'.")
            masks[found[1]] = found[2].replace(", ", ",")  # the engine writes its own form
            return []

        def unmask(batch: str) -> list[list[tuple[Any, ...]]]:
            if unmask_works:
                masks.pop(re.search(r"ALTER COLUMN \[(\w+)\] DROP MASKED", batch)[1])  # type: ignore[index]
            return []

        session.respond("MASKED WITH (FUNCTION", add)
        session.respond("DROP MASKED", unmask)
        session.respond(
            "sys.masked_columns", lambda _: [[("id", False, None), *((n, True, f) for n, f in masks.items())]]
        )
        return session

    ctx = spike_ctx(connect)
    return live_spike.x10_masked_columns(ctx), ctx


def test_x10_reads_the_masking_functions_as_the_engine_writes_them():
    result, ctx = x10_result()
    assert result.result == "pass", result.observed
    forms = ctx.fixtures.catalog_rows["masked_columns"]
    assert [form["input"] for form in forms] == [function for _, function in live_spike.X10_MASKS]
    assert {"input": "random(1, 5)", "engine": "random(1,5)"} in forms
    assert result.observed["masked_after_drop_masked"] == len(live_spike.X10_MASKS) - 1


def test_x10_keeps_a_function_that_the_engine_refuses_as_a_fact_and_still_passes():
    # datetime() is the newest function of the list; a database without it must not fail the item
    result, ctx = x10_result(refused="datetime")
    assert result.result == "pass", result.observed
    assert result.observed["refused"] == ['datetime("Y")']
    assert any(label.startswith("X10 ") for label in ctx.fixtures.errors)


@pytest.mark.parametrize("engine", [{"refused": "email"}, {"unmask_works": False}])
def test_x10_fails_when_a_function_of_the_model_is_refused_or_the_mask_stays(engine):
    result, _ = x10_result(**engine)
    assert result.result == "fail"


# ------------------------------------------------------------------ files on Windows (N4-15)
def test_a_json_file_of_the_spike_has_lf_line_ends_on_every_platform(tmp_path, monkeypatch):
    written: list[dict[str, Any]] = []
    original = Path.write_text

    def spy(self: Path, data: str, **keywords: Any) -> int:
        written.append(keywords)
        return original(self, data, **keywords)

    monkeypatch.setattr(Path, "write_text", spy)
    live_spike.write_json(tmp_path / "a.json", {"a": [1, 2]})
    assert written == [{"encoding": "utf-8", "newline": "\n"}]  # the default writes CRLF on Windows
    assert b"\r" not in (tmp_path / "a.json").read_bytes()


def test_the_work_folder_of_the_acceptance_run_is_removed_without_an_error_on_a_held_file():
    # a scanner that holds a file of the git repository must not stop the script (Windows)
    source = (SCRIPTS / "live_acceptance.py").read_text(encoding="utf-8")
    calls = re.findall(r"TemporaryDirectory\(([^)]*)\)", source)
    assert calls and all("ignore_cleanup_errors=True" in call for call in calls)


def test_the_error_texts_of_the_spike_are_recorded_as_the_tool_stores_them_with_no_batch():
    """tests/fixtures/live/error_texts.json is this output. Its key `redacted` is compared with
    redact(text): the form with no batch, in which a name as the statement wrote it is redacted.
    The message of the session that sent the batch can keep such a name."""
    error = sql_error("[Microsoft][SQL Server]Invalid column name 'b'.").for_batch("SELECT b FROM t;")
    assert error.message.endswith("Invalid column name 'b'.")
    facts = live_spike.error_facts(error)
    assert facts["text"] == "[Microsoft][SQL Server]Invalid column name 'b'."
    assert facts["redacted"] == "[Microsoft][SQL Server]Invalid column name <redacted>."


# ------------------------------------------------------------------ the sign-in of the live scripts
SQL_USER, SQL_PASSWORD = "deploy_login", "a-password-of-the-test"
SQL_ENVIRONMENT = {"AZSQLCD_AUTH": "sql", "AZSQLCD_SQL_USER": SQL_USER, "AZSQLCD_SQL_PASSWORD": SQL_PASSWORD}


def a_sql_login() -> Any:
    return live_spike.db.SqlLogin(SQL_USER, SQL_PASSWORD)


@pytest.mark.parametrize(
    ("environ", "kind"),
    [
        ({}, "entra"),
        ({"AZSQLCD_AUTH": "entra"}, "entra"),
        ({"AZSQLCD_AUTH": "managed-identity"}, "managed-identity"),
        (SQL_ENVIRONMENT, "sql"),
    ],
)
def test_the_live_scripts_sign_in_as_the_variable_of_the_tool_says(environ, kind):
    credential = live_spike.live_credential(environ)
    assert live_spike.db.auth_kind(credential) == kind
    # one call of az for many connects; the other two sign-ins start no process
    assert isinstance(credential, live_spike.CachedTokenProvider) is (kind == "entra")


def test_a_cached_token_provider_is_of_the_kind_of_the_provider_that_it_asks():
    assert live_spike.db.auth_kind(live_spike.CachedTokenProvider(None)) == "entra"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "environ",
    [
        {"AZSQLCD_AUTH": "password"},
        {"AZSQLCD_AUTH": "sql"},
        SQL_ENVIRONMENT | {"GITHUB_ACTIONS": "true"},
    ],
)
def test_a_live_script_refuses_the_sign_in_that_the_tool_refuses(environ):
    with pytest.raises(ToolError) as stop:
        live_spike.live_credential(environ)
    assert stop.value.reason_code in ("AUTH_INVALID", "SQL_AUTH_MISSING")


def test_every_live_script_reads_the_sign_in_from_the_environment_and_names_no_azure_cli_provider():
    for name in ("live_spike", "live_acceptance", "live_tables"):
        source = (SCRIPTS / f"{name}.py").read_text(encoding="utf-8")
        assert "AzureCliTokenProvider" not in source, name
        assert "live_credential(" in source, name
        assert "AZSQLCD_AUTH" in source.split('"""')[1], name  # the usage text names the variable


def test_l13_is_not_applicable_for_a_sql_login_and_never_a_pass(monkeypatch):
    def never(*args: Any, **options: Any) -> Any:
        raise AssertionError("the soak of a token ran for a sign-in that has no token")

    monkeypatch.setattr(live_spike, "live_connect", never)
    ctx = spike_ctx(FakeSession)
    ctx.provider = a_sql_login()
    ctx.options = live_spike.Options(
        server=SERVER,
        database=DATABASE,
        confirm=DATABASE,
        out=Path("x"),
        items=(),
        soak_past_expiry_minutes=90,
    )
    result = live_spike.l13_token_soak(ctx)
    assert (result.id, result.result) == ("L13", "not applicable") == ("L13", live_spike.NOT_APPLICABLE)
    assert "SQL authentication" in result.observed["reason"]
    assert SQL_USER not in json.dumps(live_spike.item_json(result, {}))
    # not a pass of a gate, and not a failure of the run
    assert live_spike.summary({"L13": result.result})["items"] == {"L13": "not applicable"}


def test_the_token_minutes_of_the_first_item_are_none_for_a_sql_login():
    assert live_spike.token_minutes_left(a_sql_login()) is None
    assert live_spike.token_minutes_left(None) is None  # the connect function was given by a test

    class Token:
        def get(self) -> AccessToken:
            return AccessToken("t", 2**31)

    assert live_spike.token_minutes_left(Token()) > 0


def test_x1_does_not_probe_the_token_login_with_a_sql_login(monkeypatch):
    def never(*args: Any, **options: Any) -> Any:
        raise AssertionError("the probe of a refused token login ran for a SQL login")

    monkeypatch.setattr(live_spike, "load_driver", never)
    monkeypatch.setattr(live_spike.db, "open_session", never)
    _, (ctx, _) = x1_result(monkeypatch, X1_TEXT, provider=a_sql_login())
    assert "18456 connect to a database that does not exist" not in ctx.fixtures.errors


def test_the_recorded_connect_of_the_spike_opens_a_session_without_a_token(monkeypatch):
    opened: list[tuple[Any, Any]] = []
    monkeypatch.setattr(live_spike, "load_driver", lambda: "driver")
    monkeypatch.setattr(
        live_spike.db, "open_session", lambda driver, keywords, token: opened.append((keywords, token))
    )
    log: list[dict[str, Any]] = []
    keywords = live_spike.db.connection_keywords(SERVER, DATABASE, "app", a_sql_login())
    live_spike.recording_connect(log)(keywords, None)
    assert opened == [(keywords, None)] and log[0]["connected"] is True
    assert SQL_PASSWORD not in json.dumps(log)


def test_the_token_life_check_of_the_acceptance_is_not_applicable_for_a_sql_login(monkeypatch):
    a = new_acceptance()
    a.provider = a_sql_login()

    def never(*args: Any, **options: Any) -> Any:
        raise AssertionError("a deploy ran for a check that is not applicable")

    monkeypatch.setattr(live_acceptance.Acceptance, "deploy", never)
    live_acceptance.token_life_check(a)
    [check] = a.checks
    assert (check.result, check.expected, check.seen) == ("not applicable", "24 TOKEN_TOO_SHORT", "")
    assert check.name == "a token with too little life stops before the connect"
    assert "SQL authentication has no access token" in check.detail


def test_the_token_life_check_of_the_acceptance_runs_for_a_token(monkeypatch):
    a = new_acceptance()
    seen: list[Any] = []

    def deploy(self: Any, number: int, **options: Any) -> Any:
        seen.append(options["provider"])
        return live_acceptance.Outcome(24, "TOKEN_TOO_SHORT")

    monkeypatch.setattr(live_acceptance.Acceptance, "deploy", deploy)
    monkeypatch.setattr(live_acceptance.Acceptance, "run_count", lambda self: 0)
    live_acceptance.token_life_check(a)
    assert [check.result for check in a.checks] == ["pass"]
    assert isinstance(seen[0], live_acceptance.ShortLivedToken)


def test_a_check_that_is_not_applicable_is_counted_apart_and_is_no_pass_and_no_failure():
    checks = [
        live_acceptance.Check("a", "", "pass", "", ""),
        live_acceptance.Check("b", "", "not applicable", "", ""),
    ]
    totals = live_acceptance.totals_of(checks)
    assert totals == {"pass": 1, "fail": 0, "not run": 0, "not applicable": 1}
    assert live_acceptance.exit_code_of(totals) == 0
    assert live_acceptance.exit_code_of(totals | {"fail": 1}) == 1
    assert live_acceptance.exit_code_of(totals | {"not run": 1}) == 1


# L9: catalog reads inside the open transaction. Owner decision: a deploy reads modules by key
# only; the read of every module (export, baseline) is timed and reported apart
READ_OF_EVERY_MODULE = "the read of every module, used by export and baseline, took "


def l9_connect(connect: Any = FakeSession) -> Any:
    def scripted() -> FakeSession:
        session = connect()
        session.respond("FROM sys.columns", [[(2,)]])
        session.respond("FROM sys.indexes", [[(1,)]])
        session.respond("[name] LIKE N'l9[_]%'", [[(0,)]])
        return session

    return scripted


def l9_engine(
    monkeypatch: Any, *, by_key: float = 0.6, every: float = 0.6, listed: float = 0.2, without_keys: int = 500
) -> None:
    """A catalog that answers as the live engine did, and takes this many seconds for each read."""
    clock = [100.0]
    real = live_spike.time

    class Clock:  # the clock of the script only: the time module of the test run stays as it is
        monotonic = staticmethod(lambda: clock[0])

        def __getattr__(self, name: str) -> Any:
            return getattr(real, name)

    procedures = [live_spike.key("PROCEDURE", f"l9_p{n}") for n in range(1, live_spike.L9_OBJECTS + 1)]
    trigger = {"parent": live_spike.obj("l9_t"), "events": {"INSERT": {"is_first": False, "is_last": False}}}

    def capture(session: Any, keys: Any) -> dict[str, dict[str, Any]]:
        clock[0] += every if keys is None else by_key
        if keys is not None:
            return {key: {} for key in keys}
        return {key: {} for key in procedures[:without_keys]} | {live_spike.key("TRIGGER", "l9_tr"): trigger}

    def list_objects(session: Any) -> list[Any]:
        clock[0] += listed
        return [live_spike.catalog.UserObject("TABLE", "azsqlcd_spike", "l9_t", "U")]

    monkeypatch.setattr(live_spike.catalog, "capture_modules", capture)
    monkeypatch.setattr(live_spike.catalog, "list_user_objects", list_objects)
    monkeypatch.setattr(live_spike.catalog, "object_exists", lambda session, key: False)
    monkeypatch.setattr(live_spike, "time", Clock())


def l9_result(monkeypatch: Any, **engine: Any) -> Any:
    l9_engine(monkeypatch, **engine)
    return live_spike.l9_catalog_in_transaction(spike_ctx(l9_connect()))


def test_l9_passes_when_the_reads_are_right_and_every_read_is_within_the_limit(monkeypatch):
    result = l9_result(monkeypatch)
    assert result.result == "pass", result.observed
    assert result.observed["seconds_of_the_deploy_path"] == {
        "capture_modules(keys)": 0.6,
        "list_user_objects": 0.2,
    }
    assert result.observed["seconds_of_the_read_of_every_module"] == 0.6
    assert result.note == READ_OF_EVERY_MODULE + "0.6 s"
    assert result.observed["read_of_every_module"] == result.note


def test_l9_passes_when_only_the_read_of_every_module_is_over_the_limit_and_says_so_in_words(monkeypatch):
    """Pilot: the reads by key took 0.6 s and capture_modules(session, None) took 12 s. No step of
    a deploy makes that read, so the time under the schema locks of a deploy is the 0.6 s."""
    result = l9_result(monkeypatch, every=12.0)
    assert result.result == "pass", result.observed
    assert result.observed["seconds_of_the_deploy_path"] == {
        "capture_modules(keys)": 0.6,
        "list_user_objects": 0.2,
    }
    assert result.observed["seconds_of_the_read_of_every_module"] == 12.0
    assert result.note == (
        READ_OF_EVERY_MODULE + "12.0 s: more than the limit of 10 s for a read of the deploy path. "
        "A deploy reads modules by key only, so this time does not decide the item"
    )
    assert result.observed["read_of_every_module"] == result.note
    assert "export and baseline" in result.decides and "does not decide" in result.decides


@pytest.mark.parametrize("slow", [{"by_key": 12.0}, {"listed": 10.5}, {"by_key": 11.0, "every": 30.0}])
def test_l9_is_inconclusive_when_a_read_of_the_deploy_path_is_over_the_limit(monkeypatch, slow):
    result = l9_result(monkeypatch, **slow)
    assert result.result == "inconclusive"
    assert result.note.startswith(READ_OF_EVERY_MODULE)


@pytest.mark.parametrize("times", [{}, {"every": 12.0}, {"by_key": 12.0}])
def test_l9_fails_when_a_read_does_not_show_the_objects_of_the_open_transaction(monkeypatch, times):
    assert l9_result(monkeypatch, without_keys=499, **times).result == "fail"


def test_the_limit_of_l9_is_ten_seconds_and_the_two_deploy_reads_are_the_ones_a_deploy_makes():
    assert live_spike.L9_MAX_SECONDS == 10.0  # owner decision: the limit stays
    # the read without keys is not in the planner and not in the runner
    for module in (plan, runner):
        calls = re.findall(r"catalog\.capture_modules\(\s*[\w.]+,\s*([^)\n]*)", inspect.getsource(module))
        assert calls and not [call for call in calls if call.strip().startswith("None")], module.__name__


def test_a_pass_of_l9_with_a_slow_read_of_every_module_is_printed_and_written_with_its_note(
    monkeypatch, tmp_path, capsys
):
    l9_engine(monkeypatch, every=12.0)
    assert spike(tmp_path, l9_connect(Sessions()), items="L9,L13") == 0
    out = capsys.readouterr().out
    assert "L9: pass" in out and f"L9 note: {READ_OF_EVERY_MODULE}12.0 s: more than the limit" in out
    item = json.loads((tmp_path / "spike" / "L9.json").read_text(encoding="utf-8"))
    assert (item["result"], item["note"]) == ("pass", item["observed"]["read_of_every_module"])
    # an item with nothing to add keeps the keys that it had
    other = json.loads((tmp_path / "spike" / "L13.json").read_text(encoding="utf-8"))
    assert set(other) == {"id", "title", "result", "observed", "decides"}
