"""The command line: the argument contract with the workflow files, exit codes, CI outputs.

Every test calls cli.main or cli.parse. A release is an in-memory bundle of test_runner, written
to disk with release.write; a session is a FakeSession (test_runner.Db). No test opens a
connection, and none starts the tool as a process.
"""

import argparse
import dataclasses
import hashlib
import io
import itertools
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from azsqlcd import catalog, cli, plan, release, runner, state, trace
from azsqlcd.config import load_config
from azsqlcd.errors import refused
from azsqlcd.release import Bundle
from azsqlcd.session import AccessToken
from azsqlcd.sqlerrors import ErrorClass, SqlError, sql_error
from support.fake_session import FakeSession
from unit import test_onboard as onboard_db
from unit.test_resolve import ACTIONS, REASON
from unit.test_runner import (
    ADD_C,
    BUILD_INDEX,
    M1,
    OLD,
    RUN_ID,
    TOML,
    Db,
    a_release,
    bundle,
    key,
    live_row,
    mig,
    parse_only_session,
    proc,
    recorded,
    row,
)

REPO = Path(__file__).resolve().parents[2]
TOKEN = "eyJ-the-access-token-of-the-test"
WORKFLOW_FILES = [
    REPO / "action.yml",
    *sorted((REPO / ".github" / "workflows").glob("*.yml")),
    *sorted((REPO / "templates" / "db-repo" / ".github" / "workflows").glob("*.yml")),
]
COMMANDS_OF_THE_WORKFLOWS = {
    "verify",
    "build",
    "targets",
    "plan",
    "deploy",
    "drift",
    "export",
    "baseline",
    "resolve",
}


# ------------------------------------------------------------------ helpers
class Tokens:
    def get(self) -> AccessToken:
        return AccessToken(TOKEN, int(time.time()) + 3600)


class Sessions:
    """The session_factory of a call: the sessions in the order in which the command opens them."""

    def __init__(self, *queue: FakeSession) -> None:
        self.queue = list(queue)
        self.opened: list[FakeSession] = []

    def __call__(self) -> FakeSession:
        self.opened.append(self.queue[len(self.opened)])
        return self.opened[-1]


def git(repo: Path, *args: str) -> str:
    identity = ["-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]
    done = subprocess.run(["git", *identity, *args], cwd=repo, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def write(repo: Path, path: str, text: str) -> None:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_bytes(text.encode("utf-8"))


def read_outputs(text: str) -> dict[str, str]:
    """The keys of a GITHUB_OUTPUT file, read as the runner reads it: `key=value`, or `key<<delimiter`
    with the value in the lines up to the delimiter."""
    found: dict[str, str] = {}
    lines = text.split("\n")
    at = 0
    while at < len(lines):
        line = lines[at]
        at += 1
        equals, heredoc = line.find("="), line.find("<<")
        if equals >= 0 and (heredoc < 0 or equals < heredoc):
            found[line[:equals]] = line[equals + 1 :]
        elif heredoc >= 0:
            end = lines.index(line[heredoc + 2 :], at)
            found[line[:heredoc]] = "\n".join(lines[at:end])
            at = end + 1
        else:
            assert line == "", f"a line of the output file is neither form: {line!r}"
    return found


class Ci:
    """The files of --ci github. outputs(): the keys that were written since the last call."""

    def __init__(self, folder: Path) -> None:
        self.output, self.summary = folder / "github_output", folder / "github_summary"
        self.log: str | None = None

    def outputs(self) -> dict[str, str]:
        """The keys without `log`. The path of the triage log differs per call: it is kept in
        self.log (None: the call wrote no such key), and the tests of the log read it there."""
        found = read_outputs(self.output.read_text(encoding="utf-8"))
        self.output.write_text("", encoding="utf-8")
        self.log = found.pop("log", None)
        return found

    def text(self) -> str:
        return self.output.read_text(encoding="utf-8") + self.summary.read_text(encoding="utf-8")


def ci_files(folder: Path, monkeypatch: pytest.MonkeyPatch) -> Ci:
    """The environment of a step of a workflow, as far as the tool reads it."""
    files = Ci(folder)
    monkeypatch.setenv("GITHUB_OUTPUT", str(files.output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(files.summary))
    for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_ACTOR"):
        monkeypatch.delenv(name, raising=False)
    return files


@pytest.fixture
def ci(tmp_path, monkeypatch) -> Ci:
    return ci_files(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def log_dir(tmp_path, monkeypatch) -> Path:
    """Where the triage log of every database command of a test goes: never into the checkout."""
    folder = tmp_path / "triage-logs"
    monkeypatch.setenv(trace.LOG_DIR_VARIABLE, str(folder))
    return folder


def on_disk(folder: Path, b: Bundle) -> list[str]:
    """Write the release as `build` does; the arguments that name it."""
    release.write(release.Release(b.manifest, b.files), folder / "release")
    return ["--bundle", str(folder / "release"), "--digest", release.digest(b.manifest)]


def call(argv: list[str], *sessions: FakeSession) -> tuple[int, Sessions]:
    factory = Sessions(*sessions)
    return cli.main(argv, session_factory=factory, token_provider=Tokens()), factory


def target(env: str = "dev") -> list[str]:
    return ["--env", env, "--target", f"sales-{env}"]


def a_repository(folder: Path, toml: str = TOML) -> Path:
    """A database repository from the template, with one commit that is the head of origin/main."""
    repo = folder / "db-sales"
    shutil.copytree(REPO / "templates" / "db-repo", repo)
    write(repo, "azsqlcd.toml", toml)
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "repository from the template")
    return repo


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/remotes/origin/main", sha)
    return sha


# ------------------------------------------------------------------ (a) the contract with the workflows
def walk(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def args_blocks() -> list[tuple[str, str]]:
    """(file, args block) of every step that runs the action of the tool."""
    found = []
    for path in WORKFLOW_FILES:
        for node in walk(yaml.safe_load(path.read_text(encoding="utf-8"))):
            if str(node.get("uses", "")).startswith("akaalholdings/azsqlcd@"):
                found.append((path.name, node["with"]["args"]))
    return found


def literal_calls() -> list[tuple[str, list[str]]]:
    """(file, arguments) of every `azsqlcd ...` or `python -m azsqlcd ...` line of a run script.

    A call with a shell variable is not literal: the composite action passes the args block that
    way, and args_blocks() reads those.
    """
    found = []
    for path in WORKFLOW_FILES:
        for node in walk(yaml.safe_load(path.read_text(encoding="utf-8"))):
            for line in str(node.get("run", "")).splitlines():
                words = line.split()
                for at, word in enumerate(words):
                    module = word == "azsqlcd" and at > 0 and words[at - 1] == "-m"
                    rest = " ".join(words[at + 1 :])
                    if word == "azsqlcd" and (at == 0 or module) and rest and "$" not in rest:
                        found.append((path.name, shlex.split(rest)))
    return found


EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}")
SWITCHES = ("inputs.gated", "steps.audit.outputs.approved_by", "inputs.force_no_readback")
RESOLVE_ACTIONS = yaml.safe_load(
    (REPO / "templates" / "db-repo" / ".github" / "workflows" / "resolve.yml").read_text(encoding="utf-8")
)[True]["workflow_dispatch"]["inputs"]["action"]["options"]  # YAML reads the key `on` as true


def evaluate(expression: str, values: dict[str, str]) -> str:
    """A GitHub expression of the forms that the args blocks use: a context value, 'a literal',
    format('a literal with {0}', a context value, ...) and `a && b || c`. A value that is not
    listed is a dummy."""

    def operand(text: str) -> str:
        text = text.strip()
        call = re.fullmatch(r"format\('([^']*)'((?:\s*,\s*[A-Za-z_.-]+)*)\)", text)
        if call:
            names = [name.strip() for name in call[2].split(",")[1:]]
            return call[1].format(*(values.get(name, "x") for name in names))
        if text.startswith("'"):
            assert text.endswith("'"), text
            return text[1:-1]
        assert re.fullmatch(r"[A-Za-z_.-]+", text), f"the test cannot read the expression {expression!r}"
        return values.get(text, "x")

    result = ""
    for alternative in expression.split("||"):
        result = ""
        for part in alternative.split("&&"):
            result = operand(part)
            if not result:
                break
        if result:
            break
    return result


def scenarios(block: str) -> Iterator[list[str]]:
    """The argument lists that a block gives: one for each setting of the inputs that switch an
    argument on or off, and for each resolve action. Empty lines are dropped, as the action does."""
    used = [name for name in SWITCHES if name in block]
    actions = RESOLVE_ACTIONS if "inputs.action" in block else ["x"]
    for settings in itertools.product(("true", ""), repeat=len(used)):
        for action in actions:
            values = dict(zip(used, settings, strict=True)) | {
                "inputs.action": action,
                "inputs.subject": "7",  # a migration, an object key, an environment or a run id
                "steps.audit.outputs.approved_utc": "2026-10-07T09:00:00Z",
            }
            lines = [
                EXPRESSION.sub(lambda m, v=values: evaluate(m[1], v), line) for line in block.splitlines()
            ]
            yield [line.strip() for line in lines if line.strip()]


def test_every_argument_list_of_the_workflow_files_is_accepted_by_the_parser():
    blocks = args_blocks()
    lists = [(file, argv) for file, block in blocks for argv in scenarios(block)] + literal_calls()
    assert len(blocks) >= 15  # the files hold this many steps of the action; fewer means the walk is wrong
    for file, argv in lists:
        try:
            parsed = cli.parse(argv)
        except SystemExit:
            pytest.fail(f"{file}: the tool does not accept the arguments {argv}")
        assert parsed.command == argv[0] and parsed.ci == "github"
    assert {argv[0] for _, argv in lists} == COMMANDS_OF_THE_WORKFLOWS
    resolve_lists = [argv for _, argv in lists if argv[0] == "resolve"]
    assert {argv[argv.index("--reason") + 2] for argv in resolve_lists} == {f"--{a}" for a in RESOLVE_ACTIONS}


def test_the_stage_workflow_gives_a_deploy_exactly_one_plan_flag_in_each_mode():
    (block,) = [block for file, block in args_blocks() if file == "stage.yml" and "deploy" in block.split()]
    found = {(a.inline_plan, a.expect_plan_file, a.approved_by) for a in map(cli.parse, scenarios(block))}
    assert found == {
        # x: the dummy of the environment and of the target id; true: the dummy of the approver
        (False, "plan/plan-x-x/plan.json", "true"),
        (False, "plan/plan-x-x/plan.json", None),
        (True, None, "true"),
        (True, None, None),
    }


def test_the_composite_action_passes_the_args_block_and_no_argument_of_its_own():
    script = "\n".join(
        str(node.get("run", "")) for node in walk(yaml.safe_load(WORKFLOW_FILES[0].read_text()))
    )
    # -P is a flag of the interpreter: the checkout that the tool reads is not on its module path
    assert 'python -P -m azsqlcd "${ARGS[@]}"' in script
    assert literal_calls() == []  # nothing else in the files starts the tool


@pytest.mark.parametrize(
    "argv",
    [
        [
            "plan",
            "--bundle",
            "r",
            "--digest",
            "d",
            "--env",
            "dev",
            "--target",
            "t",
            "--out",
            "o",
            "--previous-reports",
            "p",
        ],
        ["plan", "--bundle", "r", "--digest", "d", "--env", "dev", "--out", "o"],  # no --target
        ["align", "--script", "x.sql"],  # cut from this build (A30)
        [
            "resolve",
            "--bundle",
            "r",
            "--digest",
            "d",
            "--env",
            "dev",
            "--target",
            "t",
            "--reason",
            "x",
            "--clear-run",
            "7",
        ],
        ["gen", "--base", "origin/main"],  # neither --name nor --resum
        ["gen", "--base", "origin/main", "--resum", "--rename", "column:[s].[t].[a]=[b]"],
        [
            "drift",
            "--bundle",
            "r",
            "--digest",
            "d",
            "--env",
            "dev",
            "--target",
            "t",
            "--export",
            "VIEW:[s].[v]",
        ],
        ["lint", "--ci", "gitlab"],
    ],
)
def test_an_argument_that_the_tool_does_not_know_is_a_usage_error_with_exit_2(argv, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.parse(argv)
    assert stopped.value.code == 2
    assert "usage: azsqlcd" in capsys.readouterr().err


# ------------------------------------------------------------------ (f), (e) and other refusals of arguments
DEPLOY = ["deploy", "--bundle", "r", "--digest", "d", *target(), "--out", "o"]


def test_deploy_needs_exactly_one_of_expect_plan_file_and_inline_plan(capsys):
    assert cli.parse([*DEPLOY, "--inline-plan"]).expect_plan_file is None
    assert cli.parse([*DEPLOY, "--expect-plan-file", "plan.json"]).inline_plan is False
    for flags in ([], ["--inline-plan", "--expect-plan-file", "plan.json"]):
        with pytest.raises(SystemExit) as stopped:
            cli.main([*DEPLOY, *flags], session_factory=Sessions(), token_provider=Tokens())
        assert stopped.value.code == 2
    assert "--expect-plan-file" in capsys.readouterr().err


def test_gen_compares_with_origin_main_unless_another_base_is_given():
    # the README of a database repository says: merge main, then run `azsqlcd gen --resum`
    assert cli.parse(["gen", "--resum"]).base == "origin/main"
    assert cli.parse(["gen", "--name", "add_x", "--base", "release/1"]).base == "release/1"


def test_resolve_takes_exactly_one_action():
    base = ["resolve", "--bundle", "r", "--digest", "d", *target(), "--confirm-database", "sales"]
    base += ["--reason", REASON]
    assert cli.parse([*base, "--clear-run", "9"]).clear_run == 9
    for flags in ([], ["--clear-run", "9", "--adopt-module", "VIEW:[s].[v]"], ["--clear-run", "nine"]):
        with pytest.raises(SystemExit) as stopped:
            cli.parse([*base, *flags])
        assert stopped.value.code == 2


def test_the_approval_time_is_read_as_a_time_with_a_zone():
    parsed = cli.parse([*DEPLOY, "--inline-plan", "--approved-utc", "2026-10-07T09:00:00Z"])
    assert parsed.approved_utc.isoformat() == "2026-10-07T09:00:00+00:00"
    for text in ("2026-10-07T09:00:00", "yesterday"):  # no zone: the tool would have to guess one
        with pytest.raises(SystemExit):
            cli.parse([*DEPLOY, "--inline-plan", "--approved-utc", text])


@pytest.mark.parametrize("command", ["plan", "deploy", "drift", "baseline", "resolve", "export"])
def test_show_error_text_is_refused_for_prod_before_a_session_opens(command, tmp_path, ci, capsys):
    b = bundle(mig(M1))
    more = {
        "plan": ["--out", str(tmp_path / "o")],
        "deploy": ["--inline-plan", "--out", str(tmp_path / "o")],
        "drift": [],
        "baseline": ["--confirm-database", "sales"],
        "resolve": ["--confirm-database", "sales", "--reason", REASON, "--clear-run", "9"],
        "export": ["--root", str(tmp_path), "--out", str(tmp_path / "o")],
    }[command]
    where = [] if command == "export" else on_disk(tmp_path, b)
    code, sessions = call([command, *where, *target("prod"), *more, "--show-error-text", "--ci", "github"])
    assert code == 22 and sessions.opened == []
    assert ci.outputs() == {"exit_code": "22", "reason_code": "SHOW_ERROR_TEXT_REFUSED"}
    assert capsys.readouterr().err.startswith("SHOW_ERROR_TEXT_REFUSED: ")


def test_show_error_text_is_refused_for_a_database_that_is_bound_to_another_environment(tmp_path, ci):
    # after a refresh from prod the database holds prod data, and --env names the new environment
    argv = ["resolve", *on_disk(tmp_path, bundle(mig(M1))), *target("dev"), "--confirm-database", "sales"]
    argv += ["--reason", REASON, "--rebind-environment", "dev", "--show-error-text", "--ci", "github"]
    code, sessions = call(argv)
    assert code == 22 and sessions.opened == []
    assert ci.outputs()["reason_code"] == "SHOW_ERROR_TEXT_REFUSED"


def test_ci_github_without_the_files_of_the_runner_does_not_start(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert cli.main(["lint", "--ci", "github"]) == 2
    assert "GITHUB_OUTPUT" in capsys.readouterr().err


# ------------------------------------------------------------------ (b) exit codes
def test_a_tool_error_gives_its_exit_code_and_its_reason_code_on_stderr_and_in_the_outputs(
    tmp_path, ci, capsys
):
    where = on_disk(tmp_path, bundle(mig(M1)))
    where[-1] = "0" * 64  # another digest than the release has

    code = cli.main(["targets", *where, "--env", "dev", "--ci", "github"])

    assert code == 22
    err = capsys.readouterr().err.splitlines()
    assert err[0].startswith("DIGEST_MISMATCH: manifest.json has digest ")
    assert err[-1] == "azsqlcd targets: exit 22"
    assert ci.outputs() == {"exit_code": "22", "reason_code": "DIGEST_MISMATCH"}
    assert "### azsqlcd targets: exit 22 DIGEST_MISMATCH" in ci.text()


def a_deploy(tmp_path: Path, db_of: Any = Db, *more: str) -> tuple[list[str], Db, Bundle]:
    b = bundle(mig(M1, ADD_C), modules=[proc("usp_new")])
    db = db_of(b, recorded())
    argv = ["deploy", *on_disk(tmp_path, b), *target(), "--inline-plan", "--out", str(tmp_path / "report")]
    return [*argv, "--ci", "github", *more], db, b


def lock_is_taken(db: Db) -> None:
    db.applock_result = -1


def batch_fails(db: Db) -> None:
    db.fail_on(ADD_C, sql_error("Invalid column name 'c'.", number=207))


def lock_timeout_in_a_batch(db: Db) -> None:
    db.fail_on(ADD_C, sql_error("Lock request time out period exceeded."))


def a_batch_ends_the_transaction(db: Db) -> None:
    db.respond(ADD_C, lambda _: db.execute("COMMIT TRANSACTION;"))


@pytest.mark.parametrize(
    ("stage", "exit_code", "reason_code"),
    [
        (batch_fails, 21, "BATCH_FAILED"),
        (a_batch_ends_the_transaction, 23, "GUARD_FAILED"),
        (lock_timeout_in_a_batch, 24, "LOCK_TIMEOUT"),
        (lock_is_taken, 25, "LOCK_NOT_GRANTED"),
    ],
)
def test_a_run_error_gives_its_exit_code_and_writes_its_report(stage, exit_code, reason_code, tmp_path, ci):
    argv, db, _ = a_deploy(tmp_path)
    stage(db)

    code, _ = call(argv, db, parse_only_session())

    assert code == exit_code
    report = json.loads((tmp_path / "report" / "report.json").read_text())
    assert (report["exit_code"], report["reason_code"]) == (exit_code, reason_code)
    outputs = ci.outputs()
    assert (outputs["exit_code"], outputs["reason_code"]) == (str(exit_code), reason_code)
    if exit_code != 25:  # the plan was computed under the lock: the hash and plan.json are there
        plan_doc = json.loads((tmp_path / "report" / "plan.json").read_text())
        assert outputs["plan_sha256"] == report["plan_sha256"] == plan_doc["plan_sha256"]


def a_plan(tmp_path: Path) -> tuple[list[str], Db]:
    b = bundle(mig(M1, ADD_C))
    argv = ["plan", *on_disk(tmp_path, b), *target(), "--out", str(tmp_path / "plan"), "--ci", "github"]
    return argv, Db(b, recorded())


SECRET_VALUE = "the-value-of-a-row"


@pytest.mark.parametrize(
    ("error", "exit_code", "reason_code"),
    [
        (
            SqlError(f"Database '{SECRET_VALUE}' is not available.", cls=ErrorClass.TRANSIENT_CONNECT),
            24,
            "CONNECTION_LOST",
        ),
        (SqlError(f"Link failure near '{SECRET_VALUE}'", cls=ErrorClass.SESSION_LOST), 24, "CONNECTION_LOST"),
        (SqlError(f"Invalid object name '{SECRET_VALUE}'.", number=208), 22, "READ_FAILED"),
        (sql_error(f"Lock request time out period exceeded. ('{SECRET_VALUE}')"), 22, "READ_FAILED"),
        (SqlError(f"The log for '{SECRET_VALUE}' is full.", cls=ErrorClass.GOVERNANCE), 22, "READ_FAILED"),
    ],
)
def test_an_engine_error_in_a_read_only_command_is_24_for_a_lost_connection_and_22_otherwise(
    error, exit_code, reason_code, tmp_path, ci, capsys
):
    argv, db = a_plan(tmp_path)
    db.fail_on("azsqlcd:list_user_objects", error)

    code, sessions = call(argv, db, parse_only_session())

    assert code == exit_code
    assert ci.outputs() == {"exit_code": str(exit_code), "reason_code": reason_code}
    printed = capsys.readouterr()
    assert printed.err.startswith(f"{reason_code}: ") and "<redacted>" in printed.err
    assert SECRET_VALUE not in printed.out + printed.err + ci.text()  # the redacted text only (A25)
    assert not (tmp_path / "plan" / "plan.json").exists()
    assert sessions.opened[0].closed


def test_a_connection_that_dies_in_a_read_is_exit_24(tmp_path, ci):
    argv, db = a_plan(tmp_path)
    db.kill_on("azsqlcd:read_state.steps")
    code, _ = call(argv, db, parse_only_session())
    assert (code, ci.outputs()["reason_code"]) == (24, "CONNECTION_LOST")


def test_an_error_that_the_tool_does_not_know_is_22_tool_defect_and_its_text_is_told_before_a_session(
    tmp_path, ci, capsys
):
    (tmp_path / "file").write_text("not a directory")
    argv, db = a_plan(tmp_path)
    argv[argv.index("--out") + 1] = str(tmp_path / "file" / "plan")

    code, sessions = call(argv, db)

    assert code == 22 and sessions.opened == []
    assert ci.outputs() == {"exit_code": "22", "reason_code": "TOOL_DEFECT"}
    err = capsys.readouterr().err
    assert err.startswith("TOOL_DEFECT: ") and "Error: " in err and "file" in err  # type and text


def test_after_a_session_was_asked_for_only_the_type_of_an_unknown_error_is_told(tmp_path, ci, capsys):
    argv, _ = a_plan(tmp_path)

    def broken_driver() -> FakeSession:
        raise RuntimeError(f"Server=tcp:x;AccessToken={TOKEN}")

    code = cli.main(argv, session_factory=broken_driver, token_provider=Tokens())

    assert (code, ci.outputs()["reason_code"]) == (22, "TOOL_DEFECT")
    printed = capsys.readouterr()
    assert "RuntimeError" in printed.err and TOKEN not in printed.out + printed.err + ci.text()


# ------------------------------------------------------------------ (c) GITHUB_OUTPUT
def test_a_value_with_a_line_break_cannot_add_a_key_to_the_output_file(tmp_path):
    hostile = "first line\nexit_code=0\nreason_code=OK\r\nlast line"
    path = tmp_path / "output"
    path.write_bytes(b"earlier=1\n")

    cli.write_github_output(path, {"matrix": '[{"id":"a"}]', "note": hostile, "exit_code": "22"})

    text = path.read_bytes().decode()  # as written: no translation of line ends
    assert read_outputs(text) == {
        "earlier": "1",
        "matrix": '[{"id":"a"}]',
        "note": hostile,
        "exit_code": "22",
    }
    assert text.startswith('earlier=1\nmatrix=[{"id":"a"}]\nnote<<azsqlcd_')
    delimiter = text.split("note<<")[1].split("\n")[0]
    assert re.fullmatch(r"azsqlcd_[0-9a-f]{32}", delimiter) and delimiter not in hostile
    # a lone carriage return is a line end for some readers: the same form
    cli.write_github_output(tmp_path / "other", {"note": "one\rtwo"})
    assert (tmp_path / "other").read_bytes().startswith(b"note<<azsqlcd_")


def test_targets_writes_the_matrix_and_the_timeout(tmp_path, ci):
    code = cli.main(["targets", *on_disk(tmp_path, bundle(mig(M1))), "--env", "prod", "--ci", "github"])

    outputs = ci.outputs()
    assert code == 0 and set(outputs) == {"matrix", "timeout", "exit_code", "reason_code"}
    assert json.loads(outputs["matrix"]) == [
        {
            "id": "sales-prod",
            "server": "sql-sales-prod.database.windows.net",
            "database": "sales",
            "plan_client_id": "22222222-2222-2222-2222-222222222222",
            "deploy_client_id": "33333333-3333-3333-3333-333333333333",
            "tenant_id": "11111111-1111-1111-1111-111111111111",
            "gated": True,
        }
    ]
    assert "\n" not in outputs["matrix"]
    assert (outputs["timeout"], outputs["exit_code"], outputs["reason_code"]) == ("120", "0", "OK")


def nontx(file: str, minutes: int | None) -> Any:
    line = mig(file, BUILD_INDEX, mode="nontx")
    stated = "" if minutes is None else f" expected-minutes: {minutes}"
    text = line.text.replace("-- azsqlcd:mode nontx", f"-- azsqlcd:mode nontx{stated}")
    return type(line)(file, text, "nontx")


@pytest.mark.parametrize(("minutes", "timeout"), [(200, "430"), (20, "120"), (45, "120"), (46, "122")])
def test_the_job_timeout_is_the_larger_of_the_environment_value_and_twice_the_longest_build_plus_30(
    minutes, timeout, tmp_path, ci
):
    b = bundle(mig(M1), nontx("0002__ix.sql", 10), nontx("0003__ix.sql", minutes))
    assert cli.main(["targets", *on_disk(tmp_path, b), "--env", "dev", "--ci", "github"]) == 0
    assert ci.outputs()["timeout"] == timeout


def test_targets_refuses_a_long_build_that_states_no_minutes_and_an_environment_that_is_not_configured(
    tmp_path, ci
):
    where = on_disk(tmp_path, bundle(nontx(M1, None)))
    assert cli.main(["targets", *where, "--env", "dev", "--ci", "github"]) == 22
    assert ci.outputs() == {"exit_code": "22", "reason_code": "MIGRATION_INVALID"}
    assert cli.main(["targets", *where, "--env", "sandbox", "--ci", "github"]) == 22
    assert ci.outputs()["reason_code"] == "ENV_NOT_CONFIGURED"


def test_plan_writes_pending_and_the_hash_of_the_plan_file(tmp_path, ci, capsys):
    argv, db = a_plan(tmp_path)
    second = parse_only_session()

    code, sessions = call(argv, db, second)

    outputs = ci.outputs()
    plan_doc = json.loads((tmp_path / "plan" / "plan.json").read_text())
    assert code == 0 and set(outputs) == {"pending", "plan_sha256", "exit_code", "reason_code"}
    assert (outputs["pending"], outputs["plan_sha256"]) == ("true", plan_doc["plan_sha256"])
    assert plan_doc["token_minutes_left"] in (58, 59) and plan_doc["units"][0]["kind"] == "tx"
    # the plan session has the options of a runner session, before anything is read
    assert db.batches[0].startswith("SET XACT_ABORT ON; SET LOCK_TIMEOUT 30000;")
    assert db.batches[0].endswith("SET LANGUAGE us_english;") and "azsqlcd:session_options" in db.batches[1]
    assert (
        second.batches[:3] == ["SET PARSEONLY ON;", "SELECT 1/0;", "SELECT FROM;"]
        and ADD_C in second.batches[3]
    )
    assert [s.closed for s in sessions.opened] == [True, True]
    assert ADD_C not in capsys.readouterr().out + ci.text()  # a plan holds no SQL text


def test_a_plan_with_nothing_to_do_says_pending_false(tmp_path, ci):
    b = bundle(seq=6)
    argv = ["plan", *on_disk(tmp_path, b), *target(), "--out", str(tmp_path / "plan"), "--ci", "github"]
    code, _ = call(argv, Db(b, recorded(seq=6)))
    assert code == 0 and ci.outputs()["pending"] == "false"


def test_the_plan_links_each_step_to_its_file_at_the_commit_of_the_release(tmp_path, ci, monkeypatch):
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.example")
    monkeypatch.setenv("GITHUB_REPOSITORY", "akaal/db-sales")
    argv, db = a_plan(tmp_path)

    call(argv, db, parse_only_session())

    step = json.loads((tmp_path / "plan" / "plan.json").read_text())["units"][0]["steps"][0]
    assert step["url"] == f"https://github.example/akaal/db-sales/blob/{'c' * 40}/migrations/{M1}#L1"
    assert f"]({step['url']})" in ci.text()


# ------------------------------------------------------------------ (d) what is never printed
BATCH = "ALTER TABLE [sales].[Order] ADD [batch_marker] int NULL;"
DEFINITION = "SELECT 'definition-marker';"


def a_failing_deploy(tmp_path: Path, *more: str) -> tuple[list[str], Db]:
    b = bundle(mig(M1, BATCH), modules=[proc("usp_new", DEFINITION)])
    db = Db(b, recorded())
    db.fail_on(BATCH, sql_error("Invalid column name 'engine_marker'.", number=207))
    argv = ["deploy", *on_disk(tmp_path, b), *target(), "--inline-plan", "--out", str(tmp_path / "report")]
    return [*argv, "--ci", "github", *more], db


def test_no_token_batch_text_or_definition_text_reaches_what_a_failing_deploy_prints(tmp_path, ci, capsys):
    argv, db = a_failing_deploy(tmp_path)

    code, _ = call(argv, db, parse_only_session())

    assert code == 21 and db.sent(BATCH) != []
    printed = capsys.readouterr()
    written = "".join(path.read_text() for path in (tmp_path / "report").iterdir())
    everything = printed.out + printed.err + ci.text() + written
    for never in (TOKEN, "batch_marker", "definition-marker", "engine_marker", "Server="):
        assert never not in everything
    assert (
        "BATCH_FAILED: step 0001__a.sql#1 failed: [OTHER 207] Invalid column name <redacted>." in printed.err
    )
    # in Markdown a raw <redacted> is an HTML tag that GitHub does not show: the summary escapes it
    assert "Invalid column name &lt;redacted&gt;." in ci.summary.read_text(encoding="utf-8")


def test_show_error_text_prints_the_engine_message_in_full_on_stderr_only(tmp_path, ci, capsys):
    argv, db = a_failing_deploy(tmp_path, "--show-error-text")

    code, _ = call(argv, db, parse_only_session())

    printed = capsys.readouterr()
    assert code == 21 and "(--show-error-text): Invalid column name 'engine_marker'." in printed.err
    written = "".join(path.read_text() for path in (tmp_path / "report").iterdir())
    assert "engine_marker" not in printed.out + ci.text() + written
    assert TOKEN not in printed.err and "batch_marker" not in printed.err


def test_show_error_text_ends_for_an_error_that_has_no_engine_message_behind_it(tmp_path, ci, capsys):
    # the runner raises a guard failure from itself: its chain of causes is a loop
    argv, db, _ = a_deploy(tmp_path, Db, "--show-error-text")
    a_batch_ends_the_transaction(db)

    code, _ = call(argv, db, parse_only_session())

    assert code == 23 and ci.outputs()["reason_code"] == "GUARD_FAILED"
    assert "--show-error-text" not in capsys.readouterr().err


# ------------------------------------------------------------------ offline commands
TABLE_MODEL = TOML.replace("table_model = false", "table_model = true")
PROCEDURE_FILE = "schema/procedures/sales.usp_x.sql"
PROCEDURE_TEXT = "CREATE OR ALTER PROCEDURE [sales].[usp_x] AS\nSELECT 1;\n"


def test_build_writes_the_bundle_and_the_outputs_release_and_digest(tmp_path, ci, capsys):
    repo = a_repository(tmp_path)
    write(repo, PROCEDURE_FILE, PROCEDURE_TEXT)
    sha = commit(repo, "a procedure")

    code = cli.main(
        ["build", "--commit", sha, "--out", str(tmp_path / "dist"), "--root", str(repo), "--ci", "github"]
    )

    outputs = ci.outputs()
    assert code == 0 and set(outputs) == {"release", "digest", "exit_code", "reason_code"}
    found = release.read_bundle(tmp_path / "dist", outputs["digest"])
    assert (outputs["release"], found.manifest.release_seq, found.manifest.commit) == ("r2", 2, sha)
    assert found.files[PROCEDURE_FILE] == PROCEDURE_TEXT.encode()
    assert "SELECT 1" not in capsys.readouterr().out + ci.text()


@pytest.mark.parametrize("missing", ["azsqlcd.toml", "migrations/migrations.sum"])
def test_build_refuses_a_commit_without_the_config_or_without_the_chain(missing, tmp_path, ci):
    repo = a_repository(tmp_path)
    (repo / missing).unlink()
    commit(repo, f"no {missing}")

    code = cli.main(
        ["build", "--commit", "HEAD", "--out", str(tmp_path / "dist"), "--root", str(repo), "--ci", "github"]
    )

    assert code == 22 and ci.outputs() == {"exit_code": "22", "reason_code": "BUNDLE_INCOMPLETE"}
    assert not (tmp_path / "dist").exists()


def test_build_runs_lint_over_the_files_of_the_commit_and_refuses_an_error(tmp_path, ci, capsys):
    repo = a_repository(tmp_path)
    write(repo, PROCEDURE_FILE, "CREATE PROCEDURE [sales].[usp_x] AS\nSELECT 1;\n")  # not CREATE OR ALTER
    commit(repo, "a file that lint refuses")
    write(repo, PROCEDURE_FILE, PROCEDURE_TEXT)  # the working tree is sound: build reads the commit

    code = cli.main(
        ["build", "--commit", "HEAD", "--out", str(tmp_path / "dist"), "--root", str(repo), "--ci", "github"]
    )

    assert code == 22 and ci.outputs() == {"exit_code": "22", "reason_code": "LINT_FAILED"}
    printed = capsys.readouterr()
    assert f"{PROCEDURE_FILE}:1: error " in printed.out and printed.err.startswith("LINT_FAILED: 1 error(s)")
    assert not (tmp_path / "dist").exists()


def test_build_refuses_a_commit_that_is_not_on_main(tmp_path, ci):
    repo = a_repository(tmp_path)
    git(repo, "commit", "-q", "--allow-empty", "-m", "a commit of a branch")
    argv = [
        "build",
        "--commit",
        "HEAD",
        "--out",
        str(tmp_path / "dist"),
        "--root",
        str(repo),
        "--ci",
        "github",
    ]
    assert cli.main(argv) == 22 and ci.outputs()["reason_code"] == "NOT_ON_MAIN"


def test_lint_prints_each_finding_and_fails_only_for_an_error(tmp_path, capsys):
    repo = a_repository(tmp_path)
    assert cli.main(["lint", "--root", str(repo)]) == 0
    assert capsys.readouterr().out == "0 error(s), 0 warning(s)\n"

    write(repo, PROCEDURE_FILE, "CREATE PROCEDURE [sales].[usp_x] AS\nSELECT 1;\n")
    assert cli.main(["lint", "--root", str(repo)]) == 22
    printed = capsys.readouterr()
    assert printed.out.splitlines()[-1] == "1 error(s), 0 warning(s)"
    assert printed.err.splitlines() == [
        f"LINT_FAILED: 1 error(s) in the files; first: {printed.out.split(' error ')[1].split(':')[0]} at "
        f"{PROCEDURE_FILE}:1",
        "azsqlcd lint: exit 22",
    ]


def test_lint_uses_the_table_file_rules_only_with_a_table_model(tmp_path, capsys):
    not_canonical = "CREATE TABLE [sales].[T] (\n    [a] int\n);\n"  # NF001: NULL or NOT NULL is not said
    for toml, expected in ((TOML, 0), (TABLE_MODEL, 22)):
        repo = a_repository(tmp_path / str(expected), toml)
        write(repo, "schema/tables/sales.T.sql", not_canonical)
        assert cli.main(["lint", "--root", str(repo)]) == expected
    assert "schema/tables/sales.T.sql:2: error NF001: " in capsys.readouterr().out


def test_verify_says_in_the_summary_that_lint_is_not_a_safety_proof_and_fails_for_an_error(tmp_path, ci):
    repo = a_repository(tmp_path)
    base = git(repo, "rev-parse", "HEAD")
    assert cli.main(["verify", "--base", base, "--root", str(repo), "--ci", "github"]) == 0
    assert "Lint is not a safety proof" in ci.text() and "PRF000" in ci.text()
    assert ci.outputs() == {"exit_code": "0", "reason_code": "OK"}

    write(repo, "migrations/migrations.sum", "azsqlcd-sum 1\n0001__gone.sql sha256:" + "0" * 64 + " tx\n")
    assert cli.main(["verify", "--base", base, "--root", str(repo), "--ci", "github"]) == 22
    assert ci.outputs() == {"exit_code": "22", "reason_code": "VERIFY_FAILED"}


def test_gen_needs_a_table_model_and_resum_does_not(tmp_path, capsys):
    repo = a_repository(tmp_path)
    base = git(repo, "rev-parse", "HEAD")

    assert cli.main(["gen", "--base", base, "--name", "add_x", "--root", str(repo)]) == 22
    assert capsys.readouterr().err.startswith("GEN_NEEDS_TABLE_MODEL: ")
    assert list((repo / "migrations").iterdir()) == [repo / "migrations" / "migrations.sum"]

    # a hand-written migration with no chain line: --resum numbers it and writes the line
    header = "-- azsqlcd:migration 0001__by_hand\n-- azsqlcd:mode tx\n"
    write(repo, "migrations/0001__by_hand.sql", header + "CREATE SCHEMA [sales];\nGO\n")
    assert cli.main(["gen", "--base", base, "--resum", "--root", str(repo)]) == 0
    assert "0001__by_hand.sql sha256:" in (repo / "migrations" / "migrations.sum").read_text()
    assert "wrote migrations/migrations.sum" in capsys.readouterr().out
    assert cli.main(["lint", "--root", str(repo)]) == 0


def test_gen_writes_the_migration_of_a_model_change_and_says_when_there_is_none(tmp_path, capsys):
    repo = a_repository(tmp_path, TABLE_MODEL)
    base = git(repo, "rev-parse", "HEAD")
    assert cli.main(["gen", "--base", base, "--name", "nothing", "--root", str(repo)]) == 0
    assert capsys.readouterr().out == "no migration: no change\n"

    write(repo, "schema/schemas/sales.sql", "CREATE SCHEMA [sales];\n")
    assert cli.main(["gen", "--base", base, "--name", "add_schema", "--root", str(repo)]) == 0
    assert "wrote migrations/0001__add_schema.sql" in capsys.readouterr().out
    assert "CREATE SCHEMA [sales];" in (repo / "migrations" / "0001__add_schema.sql").read_text()


def test_gen_prints_code_object_and_hint_of_each_change_that_it_does_not_write(tmp_path, capsys):
    table = "CREATE TABLE [dbo].[T] (\n    [a] int NOT NULL,\n    [b] int NOT NULL\n);\n"
    repo = a_repository(tmp_path, TABLE_MODEL)
    write(repo, "schema/tables/dbo.T.sql", table)
    assert cli.main(["gen", "--base", "HEAD", "--name", "create_t", "--root", str(repo)]) == 0
    base = commit(repo, "a table")
    write(
        repo,
        "schema/tables/dbo.T.sql",
        table.replace("[a] int NOT NULL,\n    [b] int NOT NULL", "[b] int NOT NULL,\n    [a] int NOT NULL"),
    )

    assert cli.main(["gen", "--base", base, "--name", "reorder", "--root", str(repo)]) == 22

    err = capsys.readouterr().err
    assert "ORD001 TABLE:[dbo].[T]: " in err.splitlines()[0] and " Hint: " in err.splitlines()[0]
    assert "GEN_REFUSED: " in err and err.splitlines()[-1] == "azsqlcd gen: exit 22"


@pytest.mark.parametrize("command", [["gen", "--name", "add_x"], ["setup-sql", *target()]])
def test_a_command_outside_a_database_repository_names_the_directory_and_the_root_flag(
    command, tmp_path, capsys
):
    # N3-F8: from a sub-directory the message said only that azsqlcd.toml does not exist
    (tmp_path / "schema").mkdir()

    assert cli.main([*command, "--root", str(tmp_path / "schema")]) == 22

    err = capsys.readouterr().err
    assert err.startswith("CONFIG_INVALID: azsqlcd.toml does not exist in the directory ")
    assert str(tmp_path / "schema") in err and "--root DIR" in err


def test_setup_sql_prints_the_script_of_the_target_of_the_working_tree(tmp_path, capsys):
    repo = a_repository(tmp_path)
    assert cli.main(["setup-sql", "--env", "prod", "--target", "sales-prod", "--root", str(repo)]) == 0
    assert capsys.readouterr().out == state.setup_sql(load_config(TOML), "prod", "sales-prod") + "\n"
    assert cli.main(["setup-sql", "--env", "prod", "--target", "other", "--root", str(repo)]) == 22
    assert capsys.readouterr().err.startswith("TARGET_NOT_CONFIGURED: ")


# ------------------------------------------------------------------ deploy
def test_deploy_with_the_plan_of_the_plan_job_records_who_approved_and_writes_no_plan_file(tmp_path, ci):
    argv, plan_db = a_plan(tmp_path)
    assert call(argv, plan_db, parse_only_session())[0] == 0
    planned = ci.outputs()
    b = release.read_bundle(tmp_path / "release", argv[argv.index("--digest") + 1])
    db = Db(Bundle(b.manifest, {}), recorded())
    argv = ["deploy", *argv[1:5], *target(), "--expect-plan-file", str(tmp_path / "plan" / "plan.json")]
    argv += ["--out", str(tmp_path / "report"), "--approved-by", "dba-1,dba-2"]
    argv += ["--approved-utc", "2026-10-07T09:00:00Z", "--triggering-actor", "dev-1"]
    argv += [
        "--ci-run-url",
        "https://github.example/akaal/db-sales/actions/runs/7/attempts/1",
        "--ci",
        "github",
    ]

    code, sessions = call(argv, db)

    assert code == 0 and len(sessions.opened) == 1  # the plan job ran the syntax check
    assert ci.outputs() == {"plan_sha256": planned["plan_sha256"], "exit_code": "0", "reason_code": "OK"}
    (run_insert,) = db.sent("INSERT INTO [azsqlcd].[run]")
    for value in ("N'dba-1,dba-2'", "'2026-10-07T09:00:00", "N'dev-1'", "/actions/runs/7/attempts/1'"):
        assert value in run_insert
    assert sorted(path.name for path in (tmp_path / "report").iterdir()) == ["report.json"]
    assert "2026-10-07T09:00:00.000" in ci.text()  # started_utc: the restore reference


def test_a_plan_file_that_was_changed_or_that_is_absent_is_refused_and_no_session_opens(tmp_path, ci):
    argv, plan_db = a_plan(tmp_path)
    call(argv, plan_db, parse_only_session())
    plan_file = tmp_path / "plan" / "plan.json"
    deploy = ["deploy", *argv[1:5], *target(), "--out", str(tmp_path / "report"), "--ci", "github"]
    plan_file.write_text(plan_file.read_text().replace('"database": "sales"', '"database": "other"'))
    for path in (plan_file, tmp_path / "plan" / "absent.json"):
        code, sessions = call([*deploy, "--expect-plan-file", str(path)])
        assert code == 22 and sessions.opened == []
        assert ci.outputs()["reason_code"] == "PLAN_INVALID"


def test_a_deploy_that_the_database_is_past_of_reports_exit_0_with_the_note(tmp_path, ci):
    b = bundle(seq=5)
    argv = ["deploy", *on_disk(tmp_path, b), *target(), "--inline-plan", "--out", str(tmp_path / "r")]
    code, _ = call([*argv, "--ci", "github"], Db(b, recorded(seq=6)), parse_only_session())
    assert code == 0 and ci.outputs()["reason_code"] == "ALREADY_PAST"


# ------------------------------------------------------------------ drift
CHANGED = key("PROCEDURE", "usp_changed")


def a_drift(tmp_path: Path, drifted: bool) -> tuple[list[str], Db]:
    path, text = proc("usp_changed")
    b = bundle(modules=[(path, text)])
    db = Db(b, recorded(objects={CHANGED: row(CHANGED, text)}))
    if drifted:
        db.catalog[CHANGED] = live_row(CHANGED, "CREATE PROCEDURE [sales].[usp_changed] AS SELECT 'hot-fix';")
    return ["drift", *on_disk(tmp_path, b), *target(), "--ci", "github"], db


def test_drift_is_exit_30_and_prints_object_property_and_two_hashes(tmp_path, ci, capsys):
    argv, db = a_drift(tmp_path, drifted=True)

    code, sessions = call(argv, db)

    assert code == 30 and ci.outputs() == {"exit_code": "30", "reason_code": "DRIFT_FOUND"}
    printed = capsys.readouterr()
    assert re.fullmatch(
        rf"managed {re.escape(CHANGED)} definition [0-9a-f]{{64}} [0-9a-f]{{64}}\n", printed.out
    )
    assert printed.err.startswith(f"DRIFT_FOUND: {CHANGED} differs")
    assert "hot-fix" not in printed.out + printed.err + ci.text()
    assert (
        all(batch.startswith(("SET ", "/* azsqlcd:")) for batch in db.batches) and sessions.opened[0].closed
    )


def test_no_drift_is_exit_0(tmp_path, ci, capsys):
    argv, db = a_drift(tmp_path, drifted=False)
    assert call(argv, db)[0] == 0
    assert capsys.readouterr().out == "no drift\n" and ci.outputs()["reason_code"] == "OK"


def test_drift_prints_a_dash_where_an_unmanaged_object_has_no_hash(tmp_path, ci, capsys):
    # live acceptance, by hand: the line read 'unmanaged <key> exists None None'
    legacy = key("PROCEDURE", "usp_legacy")
    b = bundle(modules=[proc("usp_changed")])
    path, text = proc("usp_changed")
    db = Db(b, recorded(objects={CHANGED: row(CHANGED, text)}))
    db.catalog[legacy] = live_row(legacy, "CREATE PROCEDURE [sales].[usp_legacy] AS SELECT 0;")

    code, _ = call(["drift", *on_disk(tmp_path, b), *target(), "--ci", "github"], db)

    printed = capsys.readouterr().out
    assert code == 0 and f"unmanaged {legacy} exists - -\n" in printed and "None" not in printed
    assert "None" not in ci.text()


def test_drift_export_writes_the_live_text_of_one_module_as_its_object_file(tmp_path, ci, capsys):
    argv, db = a_drift(tmp_path, drifted=True)

    code, _ = call([*argv, "--export", CHANGED, "--out", str(tmp_path / "tree")], db)

    assert code == 0
    written = tmp_path / "tree" / "schema" / "procedures" / "sales.usp_changed.sql"
    assert written.read_text() == "CREATE OR ALTER PROCEDURE [sales].[usp_changed] AS SELECT 'hot-fix';"
    assert "hot-fix" not in capsys.readouterr().out + ci.text()
    db.assert_order("azsqlcd:fence_facts", "azsqlcd:capture_modules")


# ------------------------------------------------------------------ resolve
FLAGS = {
    runner.mark_applied: "--mark-applied",
    runner.mark_not_applied: "--mark-not-applied",
    runner.accept_drift: "--accept-drift",
    runner.adopt_module: "--adopt-module",
    runner.clear_run: "--clear-run",
    runner.rebind_environment: "--rebind-environment",
}


@pytest.mark.parametrize("scenario", ACTIONS)
def test_each_resolve_flag_runs_its_action_and_writes_the_report(scenario, tmp_path, ci, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTOR", "dba-9")
    action, b, db, subject, change, step_note, run_note = scenario()
    argv = ["resolve", *on_disk(tmp_path, b), *target(), "--confirm-database", "sales", "--reason", REASON]
    argv += [FLAGS[action], *(str(part) for part in subject or ("dev",))]

    code, _ = call([*argv, "--out", str(tmp_path / "report"), "--ci", "github"], db)

    assert code == 0 and ci.outputs() == {"exit_code": "0", "reason_code": "OK"}
    (run_insert,) = db.sent("INSERT INTO [azsqlcd].[run]")
    assert (
        f"N'{run_note}: {REASON}'" in run_insert and "N'resolve'" in run_insert and "N'dba-9'" in run_insert
    )
    db.assert_order("BEGIN TRANSACTION", change, f"N'{step_note}'", "COMMIT TRANSACTION;")
    report = json.loads((tmp_path / "report" / "report.json").read_text())
    assert (report["command"], report["exit_code"], report["run_id"]) == ("resolve", 0, RUN_ID)


def test_rebind_environment_must_name_the_environment_of_the_target(tmp_path, ci):
    argv = ["resolve", *on_disk(tmp_path, bundle(mig(M1))), *target("dev"), "--confirm-database", "sales"]
    code, sessions = call([*argv, "--reason", REASON, "--rebind-environment", "prod", "--ci", "github"])
    assert code == 22 and sessions.opened == []
    assert ci.outputs() == {"exit_code": "22", "reason_code": "REBIND_ENV_MISMATCH"}


def test_force_no_readback_is_for_mark_applied_only(tmp_path, ci):
    argv = ["resolve", *on_disk(tmp_path, bundle(mig(M1))), *target(), "--confirm-database", "sales"]
    argv += ["--reason", REASON, "--clear-run", "9", "--force-no-readback", "--ci", "github"]
    code, sessions = call(argv)
    assert code == 22 and sessions.opened == []
    assert ci.outputs()["reason_code"] == "RESOLVE_NOT_APPLICABLE"


def test_force_no_readback_reaches_mark_applied(tmp_path, ci):
    b = bundle(mig(M1, ADD_C))
    db = Db(b, recorded())
    argv = ["resolve", *on_disk(tmp_path, b), *target(), "--confirm-database", "sales", "--reason", REASON]

    refused, _ = call([*argv, "--mark-applied", M1, "--ci", "github"], db)
    assert refused == 22 and ci.outputs()["reason_code"] == "READBACK_REQUIRED"

    accepted, _ = call(
        [*argv, "--mark-applied", M1, "--force-no-readback", "--ci", "github"], Db(b, recorded())
    )
    assert accepted == 0 and ci.outputs()["reason_code"] == "OK"


def test_a_refused_resolve_action_writes_the_report_of_the_refusal(tmp_path, ci):
    b = bundle(mig(M1))
    argv = ["resolve", *on_disk(tmp_path, b), *target(), "--confirm-database", "other", "--reason", REASON]
    code, _ = call(
        [*argv, "--clear-run", "9", "--out", str(tmp_path / "report"), "--ci", "github"], Db(b, recorded())
    )
    assert code == 22 and ci.outputs()["reason_code"] == "CONFIRM_MISMATCH"
    assert json.loads((tmp_path / "report" / "report.json").read_text())["reason_code"] == "CONFIRM_MISMATCH"


# ------------------------------------------------------------------ onboarding: where the files go
def an_existing_database(tmp_path: Path, **state_of: Any) -> tuple[list[str], onboard_db.Db]:
    """A release with two procedures, and a database that holds one of them with another text."""
    b = onboard_db.bundle(
        onboard_db.proc("usp_same"), onboard_db.proc("usp_changed", "SELECT 2;"), **state_of
    )
    rows = [
        onboard_db.live(key("PROCEDURE", "usp_same"), onboard_db.stored("usp_same")),
        onboard_db.live(key("PROCEDURE", "usp_changed"), onboard_db.stored("usp_changed", "SELECT 'live';")),
    ]
    return [*on_disk(tmp_path, b), *target(), "--confirm-database", "sales"], onboard_db.Db(*rows)


def test_a_baseline_report_goes_to_onboarding_env_of_the_working_directory_and_writes_nothing_to_the_database(
    tmp_path, ci, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    where, db = an_existing_database(tmp_path)

    code, sessions = call(["baseline", "--report-only", *where, "--ci", "github"], db)

    assert code == 0 and ci.outputs() == {"exit_code": "0", "reason_code": "OK"}
    folder = tmp_path / "onboarding" / "dev"
    assert sorted(path.name for path in folder.iterdir()) == ["baseline-diff.md", "modules"]
    report = (folder / "baseline-diff.md").read_text()
    assert "Report only. Nothing was written." in report and "usp_changed" in report
    # the live text of the module that differs is a file for the review, and it is in no log
    assert "SELECT 'live';" in (folder / "modules" / "sales.usp_changed.sql").read_text()
    assert "'live'" not in capsys.readouterr().out + ci.text()
    assert db.batches[0].startswith("SET XACT_ABORT ON;") and sessions.opened[0].closed
    assert all(batch.startswith(("SET ", "/* azsqlcd:")) for batch in db.batches)


def test_a_baseline_that_writes_records_the_run_and_writes_its_report_under_out(tmp_path, ci, monkeypatch):
    monkeypatch.chdir(tmp_path)
    where, db = an_existing_database(tmp_path, ack=[key("PROCEDURE", "usp_changed")])

    code, sessions = call(["baseline", *where, "--out", str(tmp_path / "report"), "--ci", "github"], db)

    assert code == 0 and ci.outputs() == {"exit_code": "0", "reason_code": "OK"}
    report = json.loads((tmp_path / "report" / "report.json").read_text())
    assert (report["command"], report["exit_code"], report["run_id"]) == ("baseline", 0, RUN_ID)
    assert f"Recorded as run {RUN_ID}" in (tmp_path / "onboarding" / "dev" / "baseline-diff.md").read_text()
    db.assert_order(
        "sp_getapplock", "BEGIN TRANSACTION", "N'baseline', NULL, NULL, N'ok'", "COMMIT TRANSACTION;"
    )
    assert sessions.opened[0].closed


def test_a_refused_baseline_writes_the_report_of_the_refusal_and_no_diff(tmp_path, ci, monkeypatch):
    monkeypatch.chdir(tmp_path)
    where, db = an_existing_database(tmp_path)  # the module that differs is not acknowledged

    code, sessions = call(["baseline", *where, "--out", str(tmp_path / "report"), "--ci", "github"], db)

    assert code == 22 and ci.outputs() == {"exit_code": "22", "reason_code": "BASELINE_ACK_REQUIRED"}
    report = json.loads((tmp_path / "report" / "report.json").read_text())
    assert (report["exit_code"], report["reason_code"]) == (22, "BASELINE_ACK_REQUIRED")
    assert not (tmp_path / "onboarding").exists() and sessions.opened[0].closed


def test_export_writes_the_object_files_under_out_and_its_reports_under_onboarding_env_of_out(
    tmp_path, ci, capsys
):
    write(tmp_path / "repo", "azsqlcd.toml", TOML)
    db = onboard_db.Db(
        onboard_db.live(key("PROCEDURE", "usp_x"), onboard_db.stored("usp_x", "SELECT 'live';"))
    )
    db.fail_on("SELECT FROM;", sql_error("Incorrect syntax near the keyword 'FROM'.", number=156))
    argv = ["export", "--root", str(tmp_path / "repo"), *target(), "--out", str(tmp_path / "export")]

    code, sessions = call([*argv, "--ci", "github"], db)

    assert code == 0 and ci.outputs() == {"exit_code": "0", "reason_code": "OK"}
    written = sorted(
        path.relative_to(tmp_path / "export").as_posix() for path in (tmp_path / "export").rglob("*.*")
    )
    assert written == [
        "onboarding/dev/export.md",
        "onboarding/dev/snapshot.json",
        "schema/procedures/sales.usp_x.sql",
    ]
    module = (tmp_path / "export" / "schema" / "procedures" / "sales.usp_x.sql").read_text()
    assert module.startswith("CREATE OR ALTER PROCEDURE [sales].[usp_x] AS\nSELECT 'live';")
    snapshot = json.loads((tmp_path / "export" / "onboarding" / "dev" / "snapshot.json").read_text())
    assert snapshot == {"format": 1, "captures": {}, "column_order": {}}
    assert "'live'" not in capsys.readouterr().out + ci.text()
    # the fence comes before any read of a definition, and the session is given back closed
    db.assert_order("SET XACT_ABORT ON;", "azsqlcd:fence_facts", "azsqlcd:capture_modules")
    assert sessions.opened[0].closed


def test_an_export_file_that_cannot_be_written_is_a_refusal_that_names_the_file(tmp_path, ci, capsys):
    # N4-05: before, OSError ended as 22 TOOL_DEFECT "(OSError)", with no path. On Windows the cause
    # is a full path above 259 characters; here a directory lies where the file must go.
    write(tmp_path / "repo", "azsqlcd.toml", TOML)
    db = onboard_db.Db(
        onboard_db.live(key("PROCEDURE", "usp_x"), onboard_db.stored("usp_x", "SELECT 'live';"))
    )
    db.fail_on("SELECT FROM;", sql_error("Incorrect syntax near the keyword 'FROM'.", number=156))
    out = tmp_path / "export"
    (out / "schema" / "procedures" / "sales.usp_x.sql").mkdir(parents=True)

    code, _ = call(
        ["export", "--root", str(tmp_path / "repo"), *target(), "--out", str(out), "--ci", "github"], db
    )

    assert code == 22 and ci.outputs() == {"exit_code": "22", "reason_code": "EXPORT_INCOMPLETE"}
    err = capsys.readouterr().err
    assert "sales.usp_x.sql could not be written" in err and "the export is not complete" in err
    assert not (out / "onboarding" / "dev" / "export.md").exists()  # no report of a part of the files


def test_the_help_of_verify_says_which_base_it_takes(capsys):
    with pytest.raises(SystemExit):
        cli.parse(["verify", "--help"])
    told = " ".join(capsys.readouterr().out.split())
    assert "a full commit id" in told and "refs/remotes/origin/main" in told and "MAIN_REF_INVALID" in told


def test_the_export_count_line_names_the_history_tables_that_the_engine_owns(tmp_path, ci, capsys):
    # a reader of '2 object(s) stay unmanaged' must not look for the history tables in that count,
    # and must not think that the export lost them
    write(tmp_path / "repo", "azsqlcd.toml", TOML)
    argv = ["export", "--root", str(tmp_path / "repo"), *target(), "--out", str(tmp_path / "export")]
    parse_error = sql_error("Incorrect syntax near the keyword 'FROM'.", number=156)

    db = onboard_db.with_tables(onboard_db.Db(), onboard_db.price_rows())
    db.fail_on("SELECT FROM;", parse_error)
    code, _ = call([*argv, "--ci", "github"], db)
    line = (
        "3 object file(s); 0 object(s) stay unmanaged; 1 history table(s) of temporal tables are "
        "owned by the engine and get no file"
    )
    assert code == 0 and line in capsys.readouterr().out and line in ci.text()
    assert (tmp_path / "export" / "schema" / "tables" / "sales.Price.sql").is_file()
    assert not (tmp_path / "export" / "schema" / "tables" / "sales.Price_History.sql").exists()

    # a database with no temporal table: the line says nothing of history tables
    plain = onboard_db.with_tables(onboard_db.Db())
    plain.fail_on("SELECT FROM;", parse_error)
    code, _ = call([*argv, "--ci", "github"], plain)
    out = capsys.readouterr().out
    assert code == 0 and "3 object file(s); 0 object(s) stay unmanaged." in out
    assert "history table" not in out


def test_export_refuses_a_database_that_is_not_azure_sql_database(tmp_path, ci):
    write(tmp_path / "repo", "azsqlcd.toml", TOML)
    db = onboard_db.Db(facts=(3, "READ_WRITE", "sales", "SQL_Latin1_General_CP1_CI_AS", 0))
    argv = ["export", "--root", str(tmp_path / "repo"), *target(), "--out", str(tmp_path / "export")]
    code, _ = call([*argv, "--ci", "github"], db)
    assert code == 22 and ci.outputs()["reason_code"] == "FENCE_ENGINE_EDITION"
    assert db.sent("azsqlcd:capture_modules") == []


# ------------------------------------------------------------------ what a refusal tells
def test_a_refusal_prints_each_object_with_property_and_hashes_and_never_the_text(tmp_path, ci, capsys):
    b = bundle(modules=[proc("usp_changed", "SELECT 2;")])
    recorded_row = row(CHANGED, "CREATE PROCEDURE [sales].[usp_changed] AS SELECT 1;", source="1" * 64)
    db = Db(b, recorded(objects={CHANGED: recorded_row}))
    db.catalog[CHANGED] = live_row(CHANGED, "CREATE PROCEDURE [sales].[usp_changed] AS SELECT 'hot-fix';")
    argv = ["deploy", *on_disk(tmp_path, b), *target(), "--inline-plan", "--out", str(tmp_path / "report")]

    code, _ = call([*argv, "--ci", "github"], db, parse_only_session())

    err = capsys.readouterr().err.splitlines()
    assert code == 22 and err[0].startswith(f"DRIFT_TOUCHED: {CHANGED} differs")
    told = json.loads(err[1])
    assert told["object"] == CHANGED and [d["property"] for d in told["differences"]] == ["definition"]
    stored, live = told["differences"][0]["stored_sha256"], told["differences"][0]["live_sha256"]
    assert re.fullmatch(r"[0-9a-f]{64}", stored) and re.fullmatch(r"[0-9a-f]{64}", live) and stored != live
    assert "hot-fix" not in "\n".join(err) + ci.text()


def test_a_syntax_error_of_the_engine_is_told_for_each_batch_with_file_and_line(tmp_path, ci, capsys):
    b = bundle(mig(M1, ADD_C, "ALTER TABLE [sales].[Order] ADD [d] int NULL;"), modules=[proc("usp_new")])
    second = parse_only_session()
    second.fail_on(ADD_C, sql_error("Incorrect syntax near 'secret-literal'.", number=102))
    second.fail_on("usp_new", sql_error("Incorrect syntax near the keyword 'AS'.", number=156))
    argv = ["plan", *on_disk(tmp_path, b), *target(), "--out", str(tmp_path / "plan"), "--ci", "github"]

    code, _ = call(argv, Db(b, recorded()), second)

    err = capsys.readouterr().err
    assert code == 22 and ci.outputs()["reason_code"] == "PARSEONLY_FAILED"
    told = [json.loads(line) for line in err.splitlines()[1:3]]
    assert [(t["file"], t["line"], t["error_number"]) for t in told] == [
        (f"migrations/{M1}", 1, 102),
        ("schema/procedures/sales.usp_new.sql", 1, 156),
    ]
    assert "secret-literal" not in err and ADD_C not in err


def test_a_report_file_that_cannot_be_written_is_a_warning_and_the_exit_code_tells_the_database(
    tmp_path, ci, capsys
):
    argv, db, _ = a_deploy(tmp_path)
    (tmp_path / "report" / "report.json").mkdir(parents=True)  # a directory where the file must go

    code, _ = call(argv, db, parse_only_session())

    assert code == 0 and db.sent("COMMIT TRANSACTION;") != []  # the release is deployed and recorded
    assert ci.outputs()["exit_code"] == "0"
    assert "report.json could not be written" in capsys.readouterr().err
    assert (tmp_path / "report" / "plan.json").is_file()


# ------------------------------------------------------------------ (e) what the approver sees
def a_plan_doc(**parts: Any) -> plan.Plan:
    base: dict[str, Any] = {
        "plan_sha256": "0" * 64,
        "outcome": "work",
        "tool_version": "0.1.0",
        "tool_digest": "1" * 64,
        "environment": "prod",
        "target_id": "sales-prod",
        "server": "s.database.windows.net",
        "database": "sales",
        "manifest_digest": "2" * 64,
        "release_seq": 7,
        "git_sha": "3" * 40,
        "recorded_release_seq": 6,
        "recorded_git_sha": "4" * 40,
        "compare_url": None,
        "applied": (),
    }
    return plan.Plan(**(base | parts))


DRIFTED = plan.Drift("VIEW:[sales].[v]", (catalog.Difference("definition", "a" * 64, "b" * 64),))


def test_the_summary_for_the_approver_starts_with_the_destructive_banner_and_holds_drift_and_notes():
    # plan step 12 of the design: the approver reads the summary, so the destructive list is first
    computed = a_plan_doc(
        destructive=(
            plan.Destructive("DROP_TABLE", "[sales].[Old]", "replaced", "0007__x.sql", 4),
            plan.Destructive("OVERWRITE_MODULE", "PROCEDURE:[sales].[usp_live]", "the live text differs"),
        ),
        drift=(DRIFTED,),
        table_facts=(plan.TableFact("TABLE:[sales].[Old]", 1234567, 890),),
        unmanaged=("TABLE:[audit].[Log]", "TABLE:[audit].[Old]"),
        notes=("the syntax check (SET PARSEONLY ON) was skipped: this run has no second session",),
    )

    parts = cli._plan_summary(computed)

    assert parts[0] == "> **DESTRUCTIVE: 2 item(s). Read each one.**\n"
    assert parts[1].splitlines()[2:] == [
        r"| DROP_TABLE | \[sales\].\[Old\] | 0007__x.sql:4 | replaced |",
        r"| OVERWRITE_MODULE | PROCEDURE:\[sales\].\[usp_live\] |  | the live text differs |",
    ]
    text = "\n".join(parts)
    assert text.index("DESTRUCTIVE") < text.index("| Target |") < text.index("| Drifted object |")
    assert r"| VIEW:\[sales\].\[v\] | definition |" in text
    assert r"| TABLE:\[sales\].\[Old\] | 1234567 | 890 |" in text
    assert "| Unmanaged objects | 2 |" in text
    assert parts[-1] == "- the syntax check (SET PARSEONLY ON) was skipped: this run has no second session\n"


def test_a_plan_with_no_destructive_item_says_so_in_the_first_line_of_its_summary():
    parts = cli._plan_summary(a_plan_doc())
    assert parts[0] == "Destructive items (allow lines, module drops, module overwrites): none.\n"
    assert "DESTRUCTIVE" not in "\n".join(parts)


def test_the_summary_lists_each_dependant_that_the_run_checks_with_its_findings_before_the_change():
    # A12: the run fails only for a finding that is not in this list, so the approver must see the list
    broken, sound = "VIEW:[sales].[vw_broken]", "VIEW:[sales].[vw_sound]"
    computed = a_plan_doc(
        dependants=(broken, sound),
        dependant_findings={broken: ("COLUMNS_NOT_FOUND [sales].[Order]", "UNRESOLVED [cc]"), sound: ()},
        pre_broken=(broken,),
    )

    text = "\n".join(cli._plan_summary(computed))

    assert "| Dependant | Before the change | Findings before the change |" in text
    assert (
        r"| VIEW:\[sales\].\[vw\_broken\] | PRE\_BROKEN: it had findings before the change | "
        r"COLUMNS\_NOT\_FOUND \[sales\].\[Order\]; UNRESOLVED \[cc\] |"
    ).replace(r"\_", "_") in text
    assert r"| VIEW:\[sales\].\[vw_sound\] | sound | none |" in text
    assert "fails for a finding that is not in its list" in text
    assert "Dependant" not in "\n".join(cli._plan_summary(a_plan_doc()))  # no table for no dependant


def test_the_summary_links_the_compare_of_the_two_commits_and_each_step_to_its_file():
    url = "https://github.example/akaal/db-sales"
    step = plan.Step("0001__a.sql#1", "batch", url=f"{url}/blob/{'3' * 40}/migrations/0001__a.sql#L3")
    module = plan.Step("module:PROCEDURE:[sales].[usp_x]", "deploy_module")
    computed = a_plan_doc(
        compare_url=f"{url}/compare/{'4' * 40}...{'3' * 40}",
        units=(plan.Unit("tx", (step, module)),),
    )

    text = "\n".join(cli._plan_summary(computed))

    assert (
        f"| Compare | [recorded commit ... release commit]({url}/compare/{'4' * 40}...{'3' * 40}) |" in text
    )
    assert f"| 1 | tx | batch | [0001__a.sql#1]({step.url}) |" in text
    assert r"| 1 | tx | deploy_module | module:PROCEDURE:\[sales\].\[usp_x\] |" in text  # no link: plain
    assert f"compare: {computed.compare_url}" in cli._plan_lines(computed)


def test_the_plan_log_names_each_destructive_item_drift_notes_and_dependants():
    computed = a_plan_doc(
        destructive=(plan.Destructive("DROP_MODULE", "PROCEDURE:[sales].[usp_old]", "gone"),),
        drift=(DRIFTED,),
        unmanaged=("TABLE:[audit].[Log]",),
        dependants=("VIEW:[sales].[broken]", "VIEW:[sales].[sound]"),
        dependant_findings={"VIEW:[sales].[broken]": ("ERROR_2020",)},
        pre_broken=("VIEW:[sales].[broken]",),
        notes=("run 3 has the status running and no run holds the deploy lock",),
    )

    lines = cli._plan_lines(computed)

    assert "DESTRUCTIVE: 1 item(s)" in lines
    assert "  DROP_MODULE PROCEDURE:[sales].[usp_old]: gone" in lines
    assert "drift: VIEW:[sales].[v] (definition)" in lines
    assert "unmanaged objects: 1" in lines
    assert "pre-broken dependant: VIEW:[sales].[broken]" in lines
    assert "dependant to check: VIEW:[sales].[broken] (findings before the change: ERROR_2020)" in lines
    assert "dependant to check: VIEW:[sales].[sound] (findings before the change: none)" in lines
    assert "note: run 3 has the status running and no run holds the deploy lock" in lines


HOSTILE_REASONS = [
    "retired <!--",  # an HTML comment would hide every row after it
    "retired | OK | [x](https://evil.example) |",  # more cells, and a link
    "retired\n\n# Approved by the DBA team\n",  # a heading of its own
    "retired ![x](https://evil.example/pixel.png) `code` <img src=x> **bold** ~~gone~~",
]


@pytest.mark.parametrize("reason", HOSTILE_REASONS)
def test_a_reason_or_a_name_from_a_file_cannot_add_markup_or_hide_a_row_of_the_summary(reason):
    # the reason of an allow line and the name of an object are text of a pull request
    computed = a_plan_doc(
        destructive=(
            plan.Destructive("DROP_COLUMN", "[sales].[Order].[a]", reason, "0007__x.sql", 4),
            plan.Destructive("DROP_TABLE", "[sales].[Order]", "second item", "0007__x.sql", 9),
        ),
        notes=(reason,),
    )

    parts = cli._plan_summary(computed)

    rows = parts[1].splitlines()
    assert len(rows) == 4 and "second item" in rows[3]  # header, rule and one line for each item
    for line in rows[2:]:
        assert len(re.findall(r"(?<!\\)\|", line)) == 5  # four cells, whatever the reason holds
    text = "\n".join(parts)
    assert "<" not in text and "`" not in text.replace("\\`", "")
    assert not re.search(r"(?<!\\)\]\(", text)  # no link and no image
    assert not re.search(
        r"(?<!\\)(\*\*|~~|\[)", text.replace("> **DESTRUCTIVE: 2 item(s). Read each one.**", "")
    )
    assert parts[-1].count("\n") == 1 and parts[-1].startswith("- retired")  # the note is one list item


def test_a_link_of_the_summary_cannot_be_ended_by_its_text_or_its_address():
    step = plan.Step("x](https://evil.example) [y", "batch", url="https://github.example/a b)(c<d>")
    text = "\n".join(cli._plan_summary(a_plan_doc(units=(plan.Unit("tx", (step,)),))))
    assert r"[x\](https://evil.example) \[y](https://github.example/a%20b%29%28c%3Cd%3E)" in text


def test_the_plan_command_shows_the_destructive_list_first_in_the_summary_and_names_it_in_the_log(
    tmp_path, ci, capsys
):
    b, recorded_state = a_release()  # a tombstone drops PROCEDURE:[sales].[usp_old]
    argv = ["plan", *on_disk(tmp_path, b), *target(), "--out", str(tmp_path / "plan"), "--ci", "github"]

    code, _ = call(argv, Db(b, recorded_state), parse_only_session())

    assert code == 0
    doc = json.loads((tmp_path / "plan" / "plan.json").read_text())
    assert [item["object"] for item in doc["destructive"]] == [OLD]
    summary = ci.summary.read_text(encoding="utf-8")
    banner = f"> **DESTRUCTIVE: {len(doc['destructive'])} item(s). Read each one.**"
    assert summary.index("### azsqlcd plan: exit 0 OK") < summary.index(banner) < summary.index("| Plan |")
    assert r"| DROP_MODULE | PROCEDURE:\[sales\].\[usp_old\] |" in summary
    out = capsys.readouterr().out.splitlines()
    assert "DESTRUCTIVE: 1 item(s)" in out and any(line.startswith(f"  DROP_MODULE {OLD}: ") for line in out)


def test_a_deploy_with_an_inline_plan_shows_the_destructive_list_of_the_plan_that_it_ran(tmp_path, ci):
    b, recorded_state = a_release()
    argv = ["deploy", *on_disk(tmp_path, b), *target(), "--inline-plan", "--out", str(tmp_path / "report")]

    code, _ = call([*argv, "--ci", "github"], Db(b, recorded_state), parse_only_session())

    assert code == 0
    summary = ci.summary.read_text(encoding="utf-8")
    assert "> **DESTRUCTIVE: 1 item(s). Read each one.**" in summary
    assert r"| DROP_MODULE | PROCEDURE:\[sales\].\[usp_old\] |" in summary


def test_a_summary_above_the_size_that_github_shows_is_cut_after_its_first_part_and_says_so(ci):
    # GitHub drops a step summary above 1 MiB without failing the step: the approver would see nothing
    quiet = cli._Call(argparse.Namespace(ci="github", command="plan"), None, None)
    quiet.summary = ["> **DESTRUCTIVE: 1 item(s). Read each one.**\n", *["- a note of the plan\n"] * 60_000]

    cli._write_ci(quiet, 0, "OK", "")

    written = ci.summary.read_bytes()
    assert len(written) < 1_000_000 and written.startswith(b"### azsqlcd plan: exit 0 OK\n")
    text = written.decode("utf-8")
    assert "> **DESTRUCTIVE: 1 item(s). Read each one.**" in text
    assert "This summary was cut at 900000 bytes. It is not complete" in text[-300:]
    assert ci.outputs() == {"exit_code": "0", "reason_code": "OK"}


# ------------------------------------------------------------------ (f) an end that the tool does not know
def a_runner_that_stops(error: Exception, *, after_a_session: bool) -> Any:
    def deploy(*_: Any, session_factory: Any, **__: Any) -> runner.Report:
        if after_a_session:
            session_factory()
        raise error

    return deploy


@pytest.mark.parametrize(
    "error",
    [RuntimeError("the native layer of the driver"), sql_error("Invalid object name 'x'.", number=208)],
    ids=["RuntimeError", "SqlError"],
)
def test_an_error_that_the_runner_does_not_report_in_a_command_that_writes_is_exit_23(
    error, tmp_path, ci, capsys, monkeypatch
):
    # A1 (RS-3): the runner gives a RunError for every end. If anything else ever leaves it after a
    # session was asked for, the command line must not say that nothing was executed.
    argv, db, _ = a_deploy(tmp_path)
    monkeypatch.setattr(runner, "deploy", a_runner_that_stops(error, after_a_session=True))

    code, sessions = call(argv, db)

    assert code == 23 and len(sessions.opened) == 1
    assert ci.outputs() == {"exit_code": "23", "reason_code": "TOOL_DEFECT_AFTER_DISPATCH"}
    err = capsys.readouterr().err
    assert err.startswith("TOOL_DEFECT_AFTER_DISPATCH: ") and type(error).__name__ in err
    assert "What was applied is not known" in err and "azsqlcd deploy: exit 23" in err
    assert "Nothing was executed" not in err and "Nothing was changed" not in err
    assert "native layer" not in err and "Invalid object name" not in err  # only the type is told


def test_an_unknown_error_of_the_runner_before_any_session_was_asked_for_is_still_22(
    tmp_path, ci, monkeypatch
):
    argv, db, _ = a_deploy(tmp_path)
    monkeypatch.setattr(runner, "deploy", a_runner_that_stops(KeyError("x"), after_a_session=False))

    code, sessions = call(argv, db)

    assert code == 22 and sessions.opened == []
    assert ci.outputs() == {"exit_code": "22", "reason_code": "TOOL_DEFECT"}


def test_an_unknown_error_of_a_resolve_action_after_a_session_is_23(tmp_path, ci, monkeypatch):
    action, b, db, subject, *_ = ACTIONS[0]()
    argv = ["resolve", *on_disk(tmp_path, b), *target(), "--confirm-database", "sales", "--reason", REASON]
    argv += [FLAGS[action], *(str(part) for part in subject), "--ci", "github"]
    stops = a_runner_that_stops(RuntimeError("x"), after_a_session=True)
    monkeypatch.setattr(runner, action.__name__, stops)

    code, _ = call(argv, db)

    assert (code, ci.outputs()["reason_code"]) == (23, "TOOL_DEFECT_AFTER_DISPATCH")


def test_the_text_of_an_unknown_error_is_not_told_after_a_token_was_asked_for(tmp_path, ci, capsys):
    argv, db = a_plan(tmp_path)

    class BrokenLogin:
        def get(self) -> AccessToken:
            raise RuntimeError(f"az returned: accessToken={TOKEN}")

    code = cli.main(argv, session_factory=Sessions(db), token_provider=BrokenLogin())

    assert (code, ci.outputs()["reason_code"]) == (22, "TOOL_DEFECT")
    printed = capsys.readouterr()
    assert "RuntimeError" in printed.err and TOKEN not in printed.out + printed.err + ci.text()


def test_a_detail_that_cannot_be_printed_still_ends_with_the_exit_code_and_the_outputs(
    tmp_path, ci, capsys, monkeypatch
):
    def refuse(*_: Any) -> Bundle:
        raise refused("BUNDLE_INVALID", "the bundle is not a release", objects=[{"key": {1, 2}}, object()])

    monkeypatch.setattr(release, "read_bundle", refuse)

    code = cli.main(
        ["targets", "--bundle", str(tmp_path), "--digest", "0" * 64, "--env", "dev", "--ci", "github"]
    )

    assert code == 22 and ci.outputs() == {"exit_code": "22", "reason_code": "BUNDLE_INVALID"}
    assert capsys.readouterr().err.splitlines()[-1] == "azsqlcd targets: exit 22"


class ClosedPipe(io.TextIOBase):
    def write(self, _: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")


def test_a_closed_stdout_does_not_change_the_exit_code_of_a_deploy(tmp_path, ci, monkeypatch):
    # `azsqlcd deploy | head`: the release is applied, so the end is exit 0 and not 22 TOOL_DEFECT
    argv, db, _ = a_deploy(tmp_path)
    monkeypatch.setattr("sys.stdout", ClosedPipe())

    code, _ = call(argv, db, parse_only_session())

    assert code == 0 and ci.outputs()["exit_code"] == "0" and db.sent("COMMIT TRANSACTION;")


def test_a_character_that_the_stream_cannot_encode_is_printed_escaped(monkeypatch):
    raw = io.BytesIO()
    monkeypatch.setattr("sys.stdout", io.TextIOWrapper(raw, encoding="ascii", write_through=True))
    cli._Call(argparse.Namespace(), None, None).say("drift: VIEW:[sales].[vw_größe]")
    # splitlines: the text layer of Windows ends the line with CR LF
    assert raw.getvalue().splitlines() == [b"drift: VIEW:[sales].[vw_gr\\xf6\\xdfe]"]


def code_page_streams(monkeypatch: pytest.MonkeyPatch) -> tuple[io.BytesIO, io.BytesIO]:
    """stdout and stderr of a Windows process whose output is a pipe: cp1252, and strict."""
    out, err = io.BytesIO(), io.BytesIO()
    for name, raw in (("stdout", out), ("stderr", err)):
        stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", write_through=True)
        monkeypatch.setattr(f"sys.{name}", stream)
    return out, err


OMEGA = "usp_caf\u00e9_\u03a9"  # e acute is in cp1252, the Greek omega is not


def test_main_makes_both_streams_escape_what_their_encoding_cannot_write(tmp_path, ci, monkeypatch):
    # N4-02: on Windows a pipe is cp1252 and strict. A name outside the code page must not raise in
    # a print: `drift` has to end with 30, and the name stays readable as far as the stream can.
    path, text = proc(OMEGA)
    changed = key("PROCEDURE", OMEGA)
    b = bundle(modules=[(path, text)])
    db = Db(b, recorded(objects={changed: row(changed, text)}))
    db.catalog[changed] = live_row(changed, f"CREATE PROCEDURE [sales].[{OMEGA}] AS SELECT 'hot-fix';")
    out, err = code_page_streams(monkeypatch)

    code, _ = call(["drift", *on_disk(tmp_path, b), *target(), "--ci", "github"], db)

    assert code == 30 and ci.outputs() == {"exit_code": "30", "reason_code": "DRIFT_FOUND"}
    told = "PROCEDURE:[sales].[usp_caf\u00e9_\\u03a9]".encode("cp1252")  # the code page is kept
    assert out.getvalue().startswith(b"managed " + told + b" definition ")
    assert err.getvalue().startswith(b"DRIFT_FOUND: " + told + b" differs")


def test_a_resolve_action_that_committed_is_exit_0_on_a_stream_that_cannot_write_its_message(
    tmp_path, ci, monkeypatch
):
    # N4-02: before, UnicodeEncodeError after COMMIT became 22 TOOL_DEFECT "Nothing was executed"
    path, text = proc(OMEGA)
    adopted = key("PROCEDURE", OMEGA)
    b = bundle(modules=[(path, text)])
    db = Db(b, recorded())
    db.catalog[adopted] = live_row(adopted, text)
    out, _ = code_page_streams(monkeypatch)
    argv = ["resolve", *on_disk(tmp_path, b), *target(), "--confirm-database", "sales", "--reason", REASON]

    code, _ = call(
        [*argv, "--adopt-module", adopted, "--out", str(tmp_path / "report"), "--ci", "github"], db
    )

    report = json.loads((tmp_path / "report" / "report.json").read_text(encoding="utf-8"))
    assert db.sent("COMMIT TRANSACTION;") and report["exit_code"] == 0
    assert code == 0 and ci.outputs()["exit_code"] == "0"
    assert b"\\u03a9" in out.getvalue()  # the message of the run names the module


def test_a_stream_that_cannot_be_reconfigured_is_left_as_it_is(tmp_path, monkeypatch, capsys):
    # a stream that a host program put in place (no reconfigure, or one that refuses): the tool runs
    class Fixed(io.StringIO):
        def reconfigure(self, **_: Any) -> None:
            raise io.UnsupportedOperation("not this stream")

    repo = a_repository(tmp_path)
    monkeypatch.setattr("sys.stdout", Fixed())
    monkeypatch.setattr("sys.stderr", io.StringIO())
    assert cli.main(["lint", "--root", str(repo)]) == 0


def test_a_stdout_that_is_closed_before_the_first_line_leaves_the_exit_code_of_the_command(tmp_path):
    # N4-02: a line that stays in the buffer of a closed pipe fails when the interpreter ends, and
    # Python then makes the exit code 120. `azsqlcd deploy | head -1` must still tell the database.
    repo = a_repository(tmp_path)
    program = (
        "import sys; from azsqlcd import cli; sys.stdin.readline(); "
        f"sys.exit(cli.main(['lint', '--root', {str(repo)!r}]))"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", program], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    assert child.stdin is not None and child.stdout is not None and child.stderr is not None
    child.stdout.close()  # the reader is gone before the command prints
    child.stdin.write(b"go\n")
    child.stdin.close()
    err = child.stderr.read().decode("utf-8", "replace")
    child.stderr.close()

    assert child.wait(timeout=120) == 0, err
    assert "Exception ignored" not in err and "BrokenPipeError" not in err


def test_on_one_pipe_the_findings_come_before_the_reason_line_and_the_exit_line_is_last(tmp_path):
    # N3-F4: stdout is block-buffered on a pipe, so the log of a CI step showed 'LINT_FAILED' and
    # the exit line first and the findings after them
    repo = a_repository(tmp_path)
    write(repo, PROCEDURE_FILE, "CREATE PROCEDURE [sales].[usp_x] AS\nSELECT 1;\n")
    program = f"import sys; from azsqlcd import cli; sys.exit(cli.main(['lint', '--root', {str(repo)!r}]))"

    done = subprocess.run(
        [sys.executable, "-c", program], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False
    )

    lines = done.stdout.decode("utf-8").splitlines()
    assert done.returncode == 22 and lines[-1] == "azsqlcd lint: exit 22"
    assert lines[-2].startswith("LINT_FAILED: ") and lines[-3] == "1 error(s), 0 warning(s)"
    assert f"{PROCEDURE_FILE}:1: error " in lines[0]


@pytest.mark.parametrize(
    ("text", "printed"),
    [
        (
            "schema/views/a\n::set-output name=pending::false",
            "schema/views/a\n: :set-output name=pending::false",
        ),
        ("x\r\n   ::add-mask::DROP", "x\n   : :add-mask::DROP"),
        ("x\r::stop-commands::t", "x\n: :stop-commands::t"),
        ("::error::forged", ": :error::forged"),
        ("name ##[set-output name=pending;]false", "name ## [set-output name=pending;]false"),
        ("TABLE:[sales].[a::b] is [dbo]::x", "TABLE:[sales].[a::b] is [dbo]::x"),  # not at a line start
    ],
)
def test_no_printed_line_has_the_form_of_a_workflow_command(text, printed, capsys):
    # The runner reads a stdout or stderr line that starts with `::`, or holds `##[`, as a command.
    # A file path and an object name can hold a line break.
    quiet = cli._Call(argparse.Namespace(), None, None)
    quiet.say(text)
    quiet.warn(text)
    told = capsys.readouterr()
    assert told.out == printed + "\n" and told.err == printed + "\n"


# ------------------------------------------------------------------ (g) wiring that had no test
def test_a_read_only_command_connects_with_the_minimum_token_life_of_the_project(tmp_path, ci, monkeypatch):
    # plan, drift, export and baseline --report-only get their token-life check only here
    argv, db = a_plan(tmp_path)
    asked: list[dict[str, Any]] = []

    def connect(server: str, database: str, provider: Any, app_name: str, **options: Any) -> FakeSession:
        asked.append({"server": server, "database": database, "provider": provider, **options})
        return [db, parse_only_session()][len(asked) - 1]

    monkeypatch.setattr(cli, "connect", connect)
    tokens = Tokens()

    code = cli.main(argv, token_provider=tokens)

    config = load_config(TOML)
    wanted = config.env["dev"].targets[0]
    assert code == 0 and config.project.min_token_minutes == 20
    assert asked[0] == {
        "server": wanted.server,
        "database": wanted.database,
        "provider": tokens,
        "min_token_minutes": 20,
    }


def test_the_triggering_actor_of_a_resolve_run_comes_from_the_workflow_variable(tmp_path, ci, monkeypatch):
    monkeypatch.setenv("GITHUB_TRIGGERING_ACTOR", "dba-who-started-it")
    action, b, db, subject, *_ = ACTIONS[0]()
    argv = ["resolve", *on_disk(tmp_path, b), *target(), "--confirm-database", "sales", "--reason", REASON]

    code, _ = call([*argv, FLAGS[action], *(str(part) for part in subject), "--ci", "github"], db)

    (run_insert,) = db.sent("INSERT INTO [azsqlcd].[run]")
    assert code == 0 and "N'dba-who-started-it'" in run_insert


def test_baseline_report_only_refuses_another_database_name_before_it_reads_the_state(
    tmp_path, ci, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    where, db = an_existing_database(tmp_path)
    where[where.index("--confirm-database") + 1] = "sales_copy"

    code, sessions = call(["baseline", "--report-only", *where, "--ci", "github"], db)

    assert code == 22 and ci.outputs() == {"exit_code": "22", "reason_code": "CONFIRM_MISMATCH"}
    assert not (tmp_path / "onboarding").exists() and sessions.opened[0].closed
    assert not any("azsqlcd:read_state" in batch for batch in db.batches)


def test_a_baseline_that_engine_made_constraint_names_refuse_writes_the_rename_script_for_the_dba(
    tmp_path, ci, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    b, _ = onboard_db.a_table_release()
    files = dict(b.files) | {
        release.CONFIG_PATH: onboard_db.TOML.replace("table_model = false", "table_model = true").encode()
    }
    listed = tuple(sorted((path, hashlib.sha256(data).hexdigest()) for path, data in files.items()))
    b = Bundle(dataclasses.replace(b.manifest, files=listed), files)
    db = onboard_db.with_tables(
        onboard_db.Db(onboard_db.live(onboard_db.USP_X, onboard_db.stored("usp_x"))),
        onboard_db.engine_named_primary_key(),
    )
    argv = ["baseline", *on_disk(tmp_path, b), *target(), "--confirm-database", "sales"]

    code, _ = call([*argv, "--out", str(tmp_path / "report"), "--ci", "github"], db)

    assert code == 22 and ci.outputs() == {"exit_code": "22", "reason_code": "CONSTRAINT_NAMES"}
    script = tmp_path / "onboarding" / "dev" / "rename-constraints.sql"
    assert script.read_text(encoding="utf-8") == onboard_db.RENAME_PRIMARY_KEY
    printed = capsys.readouterr()
    assert "onboarding/dev/rename-constraints.sql" in printed.err.replace("\\", "/")
    # the script is a file, never a log
    assert "N'OBJECT'" not in printed.out + printed.err + ci.text()
    assert db.sent("INSERT INTO [azsqlcd].[object]") == [] and db.sent("UPDATE [azsqlcd].[object]") == []


def test_a_baseline_that_is_recorded_ends_with_exit_0_when_its_report_cannot_be_made(
    tmp_path, ci, monkeypatch, capsys
):
    # the run row is written: an error in the report after it must not read "nothing was executed"
    monkeypatch.chdir(tmp_path)
    where, db = an_existing_database(tmp_path, ack=[key("PROCEDURE", "usp_changed")])

    def broken(*_: Any) -> None:
        raise RuntimeError("the report")

    monkeypatch.setattr(cli, "_write_after_run", broken)

    code, _ = call(["baseline", *where, "--ci", "github"], db)

    assert code == 0 and ci.outputs() == {"exit_code": "0", "reason_code": "OK"}
    assert db.sent("COMMIT TRANSACTION;") != []
    assert "the report of the baseline is not complete (RuntimeError)" in capsys.readouterr().err


# ------------------------------------------------------------------ (h) the triage log
def events_of(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_a_failing_deploy_leaves_a_log_that_names_the_last_batch_and_holds_no_batch_text(
    tmp_path, ci, log_dir
):
    # the owner sends this file for triage: it must tell where the run stopped, and nothing of the data
    argv, db = a_failing_deploy(tmp_path)

    code, _ = call(argv, db, parse_only_session())

    assert code == 21
    [log] = log_dir.glob("azsqlcd-*-deploy.jsonl")
    text = log.read_text(encoding="utf-8")
    events = events_of(log)
    kinds = [event["kind"] for event in events]
    assert kinds[0] == "run" and kinds[-1] == "end" and "exception" in kinds
    assert events[0]["command"] == "deploy" and events[0]["argv"][0] == "deploy"
    [config] = [event for event in events if event["kind"] == "config"]
    wanted = load_config(TOML).env["dev"].targets[0]
    assert {name: config[name] for name in trace.CONFIG_KEYS} == {
        "project": load_config(TOML).project.name,
        "environment": "dev",
        "target": "sales-dev",
        "server": wanted.server,
        "database": wanted.database,
        "table_model": False,
    }
    batches = [event for event in events if event["kind"] == "batch"]
    assert {event["session"] for event in batches} == {"main", "parse"}
    last_failed = [event for event in batches if "error" in event][-1]
    assert last_failed["session"] == "main" and last_failed["error"]["number"] == 207
    assert last_failed["head"].startswith("ALTER TABLE") and "[sales].[Order]" in last_failed["head"]
    assert last_failed["sha256"] == hashlib.sha256(db.sent(BATCH)[-1].encode()).hexdigest()
    assert (events[-1]["exit_code"], events[-1]["reason_code"]) == (21, "BATCH_FAILED")
    assert "0001__a.sql#1" in events[-1]["message"]
    for never in (TOKEN, "batch_marker", "definition-marker", "engine_marker", "Server="):
        assert never not in text


def test_the_log_path_is_printed_on_a_non_zero_exit_and_is_an_output_of_every_exit(
    tmp_path, ci, capsys, log_dir
):
    argv, db = a_failing_deploy(tmp_path)
    code, _ = call(argv, db, parse_only_session())
    [log] = log_dir.glob("azsqlcd-*-deploy.jsonl")
    err = capsys.readouterr().err.splitlines()
    assert code == 21 and err.count(f"log: {log}") == 1
    assert err[-1] == "azsqlcd deploy: exit 21"  # the exit line stays the last one
    ci.outputs()
    assert ci.log == str(log)

    argv, db = a_plan(tmp_path)
    code, _ = call(argv, db, parse_only_session())
    [log] = log_dir.glob("azsqlcd-*-plan.jsonl")
    assert code == 0 and "log: " not in capsys.readouterr().err  # exit 0: the output names it
    ci.outputs()
    assert ci.log == str(log) and events_of(log)[-1]["exit_code"] == 0


@pytest.mark.parametrize("first", [True, False])
def test_no_log_writes_nothing(first, tmp_path, ci, capsys, log_dir, monkeypatch):
    monkeypatch.chdir(tmp_path)
    argv, db = a_failing_deploy(tmp_path)

    code, _ = call(["--no-log", *argv] if first else [*argv, "--no-log"], db, parse_only_session())

    assert code == 21 and "log: " not in capsys.readouterr().err
    assert not log_dir.exists() and not (tmp_path / ".azsqlcd").exists()
    assert not (tmp_path / "report" / "logs").exists()
    ci.outputs()
    assert ci.log is None


def test_the_log_goes_to_log_dir_then_to_the_variable_then_under_out_then_under_the_working_directory(
    tmp_path, ci, log_dir, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    argv, db = a_plan(tmp_path)
    assert call([*argv, "--log-dir", str(tmp_path / "given")], db, parse_only_session())[0] == 0
    assert len(list((tmp_path / "given").glob("azsqlcd-*-plan.jsonl"))) == 1 and not log_dir.exists()

    monkeypatch.delenv(trace.LOG_DIR_VARIABLE)
    argv, db = a_plan(tmp_path)
    assert call(argv, db, parse_only_session())[0] == 0
    assert len(list((tmp_path / "plan" / "logs").glob("azsqlcd-*-plan.jsonl"))) == 1

    # a caller that gives the sessions and names no directory gets no directory in its working directory
    argv, db = a_drift(tmp_path, drifted=False)
    assert call(argv, db)[0] == 0
    assert not (tmp_path / ".azsqlcd").exists()

    # --out of drift is the root of a repository, and export writes object files there: no log in it
    argv, fresh = a_drift(tmp_path, drifted=False)
    monkeypatch.setattr(cli, "connect", lambda *args, **options: fresh)
    assert cli.main(argv, token_provider=Tokens()) == 0
    assert len(list((tmp_path / ".azsqlcd" / "logs").glob("azsqlcd-*-drift.jsonl"))) == 1


def test_a_command_that_opens_no_session_writes_no_log(tmp_path, ci, log_dir):
    repo = a_repository(tmp_path)
    assert cli.main(["lint", "--root", str(repo), "--ci", "github"]) == 0
    assert not log_dir.exists()
    ci.outputs()
    assert ci.log is None


def test_a_log_directory_that_cannot_be_written_does_not_change_the_exit_code(
    tmp_path, ci, capsys, monkeypatch
):
    (tmp_path / "a-file").write_text("not a directory")
    monkeypatch.setenv(trace.LOG_DIR_VARIABLE, str(tmp_path / "a-file" / "logs"))

    argv, db = a_failing_deploy(tmp_path)
    code, _ = call(argv, db, parse_only_session())
    err = capsys.readouterr().err
    outputs = ci.outputs()
    assert code == 21 and (outputs["exit_code"], outputs["reason_code"]) == ("21", "BATCH_FAILED")
    assert ci.log is None and "log: " not in err and "triage log" in err

    argv, db = a_plan(tmp_path)
    code, _ = call(argv, db, parse_only_session())
    assert code == 0 and ci.outputs()["reason_code"] == "OK"


def test_an_error_inside_the_recorder_does_not_change_the_exit_code(tmp_path, ci, monkeypatch):
    def broken(*args: Any, **fields: Any) -> None:
        raise RuntimeError("the recorder is broken")

    for name in ("header", "config_event", "exception_event", "end"):
        monkeypatch.setattr(trace, name, broken)
    argv, db = a_failing_deploy(tmp_path)
    code, _ = call(argv, db, parse_only_session())
    outputs = ci.outputs()
    assert code == 21 and (outputs["exit_code"], outputs["reason_code"]) == ("21", "BATCH_FAILED")
    assert db.sent(BATCH) != []  # the command ran as it does with no recorder


def test_support_bundle_packs_the_log_with_the_report(tmp_path, ci, capsys, log_dir, monkeypatch):
    # the log is in a directory of its own here: the reports are found under the --out of the run
    argv, db = a_failing_deploy(tmp_path)
    assert call(argv, db, parse_only_session())[0] == 21
    [log] = log_dir.glob("azsqlcd-*-deploy.jsonl")
    capsys.readouterr()

    code = cli.main(["support-bundle", "--log", str(log), "--out", str(tmp_path / "send" / "bundle.zip")])

    assert code == 0
    with zipfile.ZipFile(tmp_path / "send" / "bundle.zip") as bundle_zip:
        names = sorted(bundle_zip.namelist())
        everything = b"".join(bundle_zip.read(name) for name in names).decode("utf-8")
    assert names == sorted([log.name, "manifest.json", "plan.json", "report.json", trace.VERSIONS_FILE])
    assert "BATCH_FAILED" in everything
    for never in (TOKEN, "batch_marker", "definition-marker", "engine_marker", "Server="):
        assert never not in everything
    printed = capsys.readouterr().out
    assert str(tmp_path / "send" / "bundle.zip") in printed and "report.json" in printed
    assert not list(log_dir.glob("*support-bundle*"))  # a command that reads logs writes none

    # the log under --out of the run, found as the latest one of that directory
    monkeypatch.delenv(trace.LOG_DIR_VARIABLE)
    argv, db = a_failing_deploy(tmp_path)
    assert call(argv, db, parse_only_session())[0] == 21
    logs = tmp_path / "report" / "logs"
    code = cli.main(["support-bundle", "--latest", "--log-dir", str(logs), "--out", str(tmp_path / "b.zip")])
    assert code == 0
    with zipfile.ZipFile(tmp_path / "b.zip") as bundle_zip:
        assert {"plan.json", "report.json", trace.VERSIONS_FILE} < set(bundle_zip.namelist())


def test_show_log_prints_the_summary_of_the_latest_log_and_no_log_is_a_refusal(tmp_path, ci, capsys, log_dir):
    assert cli.main(["show-log", "--log-dir", str(log_dir)]) == 22
    assert capsys.readouterr().err.startswith("LOG_NOT_FOUND: ")
    assert (
        cli.main(["support-bundle", "--log", str(log_dir / "none.jsonl"), "--out", str(tmp_path / "z")]) == 22
    )
    assert capsys.readouterr().err.startswith("LOG_NOT_FOUND: ") and not (tmp_path / "z").exists()

    argv, db = a_failing_deploy(tmp_path)
    assert call(argv, db, parse_only_session())[0] == 21
    [log] = log_dir.glob("azsqlcd-*-deploy.jsonl")
    capsys.readouterr()

    assert cli.main(["show-log", "--latest", "--log-dir", str(log_dir)]) == 0
    printed = capsys.readouterr().out
    assert printed.rstrip("\n") == trace.summarize(log)
    assert "BATCH_FAILED" in printed and "batch_marker" not in printed
    assert cli.main(["show-log", "--log", str(log)]) == 0


def test_log_and_latest_are_not_given_together(capsys):
    with pytest.raises(SystemExit):
        cli.parse(["show-log", "--log", "x.jsonl", "--latest"])
    assert "usage: azsqlcd show-log" in capsys.readouterr().err


# ------------------------------------------------------------------ (i) live runs and usability review
@pytest.mark.parametrize(
    ("command", "read_only"),
    [("plan", True), ("drift", True), ("baseline-report", True), ("export", True), ("deploy", False)],
)
def test_a_read_only_command_asks_for_the_short_token_life_when_the_session_module_has_it(
    command, read_only, tmp_path, ci, monkeypatch
):
    # TOKEN_TOO_SHORT of the live runs: a read needs minutes, a deploy needs min_token_minutes
    monkeypatch.chdir(tmp_path)
    asked: list[dict[str, Any]] = []

    def connect(
        server: str,
        database: str,
        provider: Any,
        app_name: str,
        *,
        min_token_minutes: int = 0,
        read_only: bool = False,
    ) -> FakeSession:
        asked.append({"min_token_minutes": min_token_minutes, "read_only": read_only})
        return queue[len(asked) - 1]

    if command == "plan":
        argv, db = a_plan(tmp_path)
        queue = [db, parse_only_session()]
    elif command == "drift":
        argv, db = a_drift(tmp_path, drifted=False)
        queue = [db]
    elif command == "baseline-report":
        where, db = an_existing_database(tmp_path)
        argv, queue = ["baseline", "--report-only", *where], [db]
    elif command == "export":
        repo = a_repository(tmp_path)
        argv = ["export", "--root", str(repo), *target(), "--out", str(tmp_path / "exported")]
        queue = [onboard_db.Db()]
    else:
        argv, db, _ = a_deploy(tmp_path)
        queue = [db, parse_only_session()]
    monkeypatch.setattr(cli, "connect", connect)

    cli.main(argv, token_provider=Tokens())

    assert asked and all(one == {"min_token_minutes": 20, "read_only": read_only} for one in asked)


@pytest.mark.parametrize("base", ["origin/main", "main", "HEAD", "short", "refs/remotes/origin/main", "full"])
def test_verify_takes_the_revisions_that_gen_and_build_take(base, tmp_path, capsys):
    repo = a_repository(tmp_path)
    full = git(repo, "rev-parse", "HEAD")
    base = {"short": full[:7], "full": full}.get(base, base)

    code = cli.main(["verify", "--base", base, "--root", str(repo)])

    assert code == 0, capsys.readouterr()
    assert "MAIN_REF_INVALID" not in capsys.readouterr().out


def test_verify_refuses_a_short_name_that_a_tag_also_has(tmp_path, capsys):
    # git reads origin/main as refs/tags/origin/main first, and anyone who can push a tag can make one
    repo = a_repository(tmp_path)
    write(repo, "note.txt", "x")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "not on main")
    git(repo, "tag", "origin/main", "HEAD")

    code = cli.main(["verify", "--base", "origin/main", "--root", str(repo)])

    assert code == 22
    err = capsys.readouterr().err
    assert err.startswith("MAIN_REF_INVALID: ") and "refs/remotes/origin/main" in err


def test_verify_names_a_base_that_git_cannot_read(tmp_path, capsys):
    repo = a_repository(tmp_path)
    assert cli.main(["verify", "--base", "no-such-branch", "--root", str(repo)]) == 22
    assert "'no-such-branch' is not a commit of this repository" in capsys.readouterr().err


@pytest.mark.parametrize("out", ["azsqlcd.toml", "azsqlcd.toml/below"])
def test_build_with_an_out_that_cannot_be_a_directory_is_a_refusal_that_names_it(out, tmp_path, ci, capsys):
    repo = a_repository(tmp_path)

    code = cli.main(
        ["build", "--commit", "HEAD", "--out", str(repo / out), "--root", str(repo), "--ci", "github"]
    )

    assert code == 22 and ci.outputs() == {"exit_code": "22", "reason_code": "OUT_NOT_WRITABLE"}
    printed = capsys.readouterr()
    assert "TOOL_DEFECT" not in printed.err and "--out" in printed.err
    assert "0 error(s)" not in printed.out  # refused before the build, not after its lint summary


def test_build_from_a_sub_directory_says_that_the_root_is_elsewhere(tmp_path, ci, capsys):
    repo = a_repository(tmp_path)

    code = cli.main(
        [
            "build",
            "--commit",
            "HEAD",
            "--out",
            str(tmp_path / "dist"),
            "--root",
            str(repo / "schema"),
            "--ci",
            "github",
        ]
    )

    assert code == 22 and ci.outputs() == {"exit_code": "22", "reason_code": "CONFIG_INVALID"}
    err = capsys.readouterr().err
    assert "sub-directory" in err and "--root" in err and "GIT_FAILED" not in err


def every_option() -> list[tuple[str, argparse.Action]]:
    parser, commands = cli.parsers()
    found = [("azsqlcd", action) for action in parser._actions if action.option_strings]
    for name, command in commands.items():
        found += [(name, action) for action in command._actions if action.option_strings]
    return found


def test_every_flag_of_every_command_has_a_description():
    missing = [f"{name} {action.option_strings[0]}" for name, action in every_option() if not action.help]
    assert not missing


def test_the_help_of_gen_shows_the_form_and_the_kinds_of_a_rename(capsys):
    with pytest.raises(SystemExit):
        cli.parse(["gen", "--help"])
    told = " ".join(capsys.readouterr().out.split())
    for part in ("table, column, index or constraint", "[schema].[table].[old]=[new]", "more than once"):
        assert part in told


def test_the_help_of_baseline_says_where_report_only_writes_its_files(capsys):
    with pytest.raises(SystemExit):
        cli.parse(["baseline", "--help"])
    told = " ".join(capsys.readouterr().out.split())
    assert "onboarding/<env>/" in told and "write nothing" not in told


@pytest.mark.parametrize(
    ("argv", "command"),
    [
        (["gen", "--resum", "--rename", "column:[a].[b].[c]=[d]"], "gen"),
        (
            ["drift", "--bundle", "r", "--digest", "d", "--env", "dev", "--target", "t", "--export", "K"],
            "drift",
        ),
    ],
)
def test_an_error_of_a_sub_command_prints_the_usage_of_that_sub_command(argv, command, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.parse(argv)
    err = capsys.readouterr().err
    assert stopped.value.code == 2 and f"usage: azsqlcd {command} " in err
    assert "usage: azsqlcd [-h]" not in err


def test_the_run_table_of_the_summary_counts_plan_steps_and_not_step_rows(tmp_path, ci):
    # E2E-11: 66 "steps applied" stood beside 2 rows of azsqlcd.step
    argv, db, _ = a_deploy(tmp_path)
    assert call(argv, db, parse_only_session())[0] == 0
    summary = ci.summary.read_text(encoding="utf-8")
    assert (
        "| Plan steps applied (batches, modules, refreshes) |" in summary
        and "| Steps applied |" not in summary
    )
