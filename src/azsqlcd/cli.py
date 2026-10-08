"""The command line: arguments, the wiring of every command, exit codes and CI outputs.

This is the only module of the tool that prints. What it holds to:
  - the exit code is a decision of the tool (errors.Exit) and every non-zero exit has a reason
    code: `REASON_CODE: message` on stderr, and with `--ci github` the keys exit_code and
    reason_code in the file of GITHUB_OUTPUT, written before the process ends (A1). argparse keeps
    exit 2 for a wrong argument: the tool did not start;
  - nothing here prints batch text, definition text, a token or a connection string. An engine
    message is printed redacted (A25). `--show-error-text` prints the full message, and is
    refused for prod;
  - an offline command imports no driver. A session is opened only by a database command, with
    the sign-in that the variable AZSQLCD_AUTH names: the token of the Azure CLI login (the
    default), the token of a managed identity, or a SQL login of the environment, which is
    refused in GitHub Actions. The plan, the report and the log name the kind of sign-in, never
    a login or a client id;
  - the exit code tells what happened to the database. A report file that cannot be written
    after the run is a warning on stderr and changes no exit code;
  - a command that opens a session writes a triage log (module trace): one file per call, with
    every batch as its statement class and its hash, never its text. A non-zero exit prints
    `log: <path>` on stderr. A log that cannot be written is a warning and changes no exit code.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import inspect
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import quote

from azsqlcd import (
    __version__,
    catalog,
    chain,
    gen,
    lint,
    onboard,
    plan,
    release,
    runner,
    state,
    tables,
    trace,
)
from azsqlcd.config import Config, load_config, resolve_target, targets_matrix
from azsqlcd.errors import Exit, ToolError, refused, retry_safe, unknown
from azsqlcd.session import (
    AUTH_SQL,
    Credential,
    Session,
    SqlLogin,
    auth_kind,
    connect,
    credential_from_environment,
    refuse_sql_in_github_actions,
)
from azsqlcd.sqlerrors import ErrorClass, SqlError

type SessionFactory = Callable[[], Session]

OK = "OK"
_OUTPUT_FILE, _SUMMARY_FILE = "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY"
_RESOLVE_ACTIONS = (
    "mark_applied",
    "mark_not_applied",
    "accept_drift",
    "adopt_module",
    "clear_run",
    "rebind_environment",
)
_SUMMARY_BYTES = 900_000  # GitHub drops a step summary above 1 MiB: the approver would see nothing
_REASON_CODE = re.compile(r"[A-Z][A-Z0-9_]*")
_MARKUP = re.compile(r"([\\`*\[\]~|])")
_URL_SAFE = "/:?#@!$&'*+,;=%"  # not ( ) < > [ ] and space: they would end the address
# The commands that open a session: each call of one writes a triage log.
_DATABASE_COMMANDS = frozenset({"plan", "deploy", "drift", "export", "baseline", "resolve"})
# Their --out holds plan.json and report.json, so the log goes to <out>/logs beside them. The --out
# of export and of drift is a directory of repository files: no log goes there.
_OUT_OF_THE_RUN = frozenset({"plan", "deploy", "baseline", "resolve"})
_COMMIT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_NOT_A_PROOF = (
    "Lint is not a safety proof. Data and raw batches are not modelled. The first deploy to dev and "
    "the read-back in its transaction prove that the engine accepts a migration."
)


# ------------------------------------------------------------------ one call of the tool
@dataclass
class _Call:
    """One call of main(): the arguments, where sessions come from, and what the call reports."""

    args: argparse.Namespace
    session_factory: SessionFactory | None  # None: the real connect
    token_provider: Credential | None  # None: the sign-in that AZSQLCD_AUTH names
    auth: str | None = None  # entra | managed-identity | sql, of a database command
    reason_code: str = OK  # of exit 0: OK, or a note such as ALREADY_PAST
    connected: bool = False  # a session was asked for; from then on an unknown error text is not printed
    token_asked: bool = False  # a token was asked for; the same rule for an unknown error text
    outputs: dict[str, str] = field(default_factory=dict)  # keys for GITHUB_OUTPUT
    summary: list[str] = field(default_factory=list)  # Markdown for GITHUB_STEP_SUMMARY
    trace: trace.Trace = field(default_factory=lambda: trace.Trace(None))  # the triage log

    def say(self, text: str = "") -> None:
        _print(text, sys.stdout)

    def warn(self, text: str) -> None:
        _print(text, sys.stderr)


def _log_text(text: str) -> str:
    """Text for stdout or stderr of a workflow step. The runner reads a line that starts with `::`,
    and a line that holds `##[`, as a command (set an output, mask, stop commands). A name of a
    file or of a database object can hold a line break, so no printed line may have that form."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for at, line in enumerate(lines):
        if line.lstrip().startswith("::"):
            cut = len(line) - len(line.lstrip())
            line = line[:cut] + ": :" + line[cut + 2 :]
        lines[at] = line.replace("##[", "## [")
    return "\n".join(lines)


def _lenient_streams() -> None:
    """stdout and stderr keep their encoding and escape what it cannot write. On Windows a pipe or
    a file is in the code page, and strict: a name outside it raised UnicodeEncodeError in a print
    after the COMMIT, and the command ended 22 "nothing was executed" (N4-02)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)  # a stream that a host put in place has none
        if reconfigure is not None:
            with contextlib.suppress(Exception):
                reconfigure(errors="backslashreplace")


def _print(text: str, stream: TextIO) -> None:
    """Print, and never raise: the exit code tells the state of the database, so a closed pipe or
    a character that the stream cannot encode must not turn the end of a run into a traceback."""
    text = _log_text(text)
    try:
        try:
            print(text, file=stream)
        except UnicodeEncodeError:
            print(text.encode("ascii", "backslashreplace").decode("ascii"), file=stream)
        stream.flush()  # a pipe that is closed fails here, and not when the interpreter ends
    except Exception:
        # The line is still in the buffer of the stream. Python writes it again when the process
        # ends, and an error there makes the exit code 120 after a run that committed. So the
        # stream gets a place where that write works.
        with contextlib.suppress(Exception):
            null = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(null, stream.fileno())
            finally:
                os.close(null)


# ------------------------------------------------------------------ CI outputs
def write_github_output(path: str | os.PathLike[str], values: Mapping[str, str]) -> None:
    """Append key=value lines to the file of GITHUB_OUTPUT.

    A value with a line break is written in the delimiter form, `key<<delimiter`, the value,
    `delimiter`, with a delimiter that no value holds. So a value cannot add a key of its own.
    """
    lines: list[str] = []
    for key, value in values.items():
        if "\n" in value or "\r" in value:
            delimiter = f"azsqlcd_{uuid.uuid4().hex}"
            while delimiter in value:
                delimiter = f"azsqlcd_{uuid.uuid4().hex}"
            lines += [f"{key}<<{delimiter}", value, delimiter]
        else:
            lines.append(f"{key}={value}")
    with open(path, "a", encoding="utf-8", newline="\n") as out:
        out.write("".join(line + "\n" for line in lines))


def _write_ci(call: _Call, exit_code: int, reason_code: str, message: str) -> None:
    """exit_code and reason_code are written for every end of a command, before the exit (A1)."""
    if call.args.ci != "github":
        return
    name = call.args.command
    told = reason_code if _REASON_CODE.fullmatch(reason_code) else _md(reason_code)
    head = f"### azsqlcd {name}: exit {exit_code} {told}\n"
    summary = "\n".join([head, *([_md(message) + "\n"] if message else []), *call.summary])
    if len(summary.encode("utf-8")) > _SUMMARY_BYTES:
        # the head, the message and the destructive list come first, so they stay
        kept = summary.encode("utf-8")[:_SUMMARY_BYTES].decode("utf-8", "ignore").rpartition("\n")[0]
        summary = (
            f"{kept}\n\n> **This summary was cut at {_SUMMARY_BYTES} bytes. It is not complete: read "
            "plan.json or report.json of the run before you approve.**\n"
        )
    try:
        write_github_output(
            os.environ[_OUTPUT_FILE],
            call.outputs | {"exit_code": str(exit_code), "reason_code": reason_code},
        )
        with open(os.environ[_SUMMARY_FILE], "a", encoding="utf-8", newline="\n") as out:
            out.write(summary + "\n")
    except OSError as error:
        call.warn(f"azsqlcd: the CI outputs could not be written ({type(error).__name__}: {error})")


@dataclass(frozen=True)
class _Link:
    """A link that the tool builds for the summary: the text is a name, the address is of the tool."""

    text: str
    url: str


def _md(text: object) -> str:
    """One line of plain text for the summary. A name, a reason or a message comes from a file of
    the repository or from the database: it must not add markup (a link, an image, HTML, a comment
    that hides the rows after it) and must not end a table cell."""
    flat = " ".join("".join(ch if ch.isprintable() else " " for ch in str(text)).split())
    flat = flat.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _MARKUP.sub(r"\\\1", flat)


def _cell(value: object) -> str:
    if isinstance(value, _Link):
        return f"[{_md(value.text)}]({quote(value.url, safe=_URL_SAFE)})"
    return _md(value)


def _table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(_cell(value) for value in row) + " |" for row in rows]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ files
def _write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)


def _write_after_run(call: _Call, path: Path, data: bytes | str) -> None:
    """A report of a run that is over. The exit code tells the state of the database, so a file
    that cannot be written is a warning and not another exit code."""
    try:
        _write(path, data)
    except OSError as error:
        call.warn(f"azsqlcd: {path} could not be written ({type(error).__name__}: {error})")


def _out_dir(call: _Call) -> Path | None:
    """--out, made before any session opens: a directory that cannot be made stops the command."""
    if call.args.out is None:
        return None
    out = Path(call.args.out)
    out.mkdir(parents=True, exist_ok=True)
    return out


def _config_of(files: Mapping[str, bytes], where: str) -> Config:
    data = files.get(release.CONFIG_PATH)
    if data is None:
        raise refused("CONFIG_INVALID", f"{release.CONFIG_PATH} does not exist in {where}")
    try:
        return load_config(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise refused("CONFIG_INVALID", f"{release.CONFIG_PATH} is not UTF-8") from None


def _root_config(root: str) -> Config:
    path = Path(root, release.CONFIG_PATH)
    files = {release.CONFIG_PATH: path.read_bytes()} if path.is_file() else {}
    where = f"the directory {Path(root).resolve()}"
    if not files:
        where += ". Run the command in the root of the database repository, or give --root DIR"
    return _config_of(files, where)


def _release(call: _Call) -> tuple[release.Bundle, Config]:
    """The release that --bundle and --digest name, checked, and its azsqlcd.toml."""
    bundle = release.read_bundle(call.args.bundle, call.args.digest)
    return bundle, _config_of(bundle.files, "the release")


def _hooks(bundle: release.Bundle, config: Config) -> tables.Hooks | None:
    return tables.Hooks(bundle, config) if config.project.table_model else None


def _repo_url() -> str | None:
    server, repository = os.environ.get("GITHUB_SERVER_URL"), os.environ.get("GITHUB_REPOSITORY")
    return f"{server.rstrip('/')}/{repository}" if server and repository else None


# ------------------------------------------------------------------ sessions
def _sign_in(call: _Call) -> Credential:
    """The sign-in of a database command: the credential that the caller gave, else the one that
    AZSQLCD_AUTH names. Nothing is asked of an identity here. Raises ToolError REFUSED:
    AUTH_INVALID, SQL_AUTH_MISSING."""
    ci_github = call.args.ci == "github"
    if call.token_provider is None:
        call.token_provider = credential_from_environment(os.environ, ci_github=ci_github)
    call.auth = auth_kind(call.token_provider)
    if call.auth == AUTH_SQL:  # also a SQL login that the caller gave: the refusal is of the sign-in
        refuse_sql_in_github_actions(os.environ, ci_github=ci_github)
    return call.token_provider


def _token_provider(call: _Call) -> Credential:
    call.token_asked = True
    return _sign_in(call)


def _sessions(call: _Call, config: Config, *, read_only: bool = False) -> SessionFactory:
    """Opens a session on the target of the call. Each call of the result is one new session,
    recorded in the triage log: the first one as "main", the second one (the syntax check) as
    "parse".

    read_only: the command sends no batch that writes (plan, drift, export, baseline
    --report-only). A session module that knows the keyword then asks for a short token life in
    place of min_token_minutes: a read ends in seconds, and the Azure CLI on a workstation gives
    out its cached token until a few minutes before the end of that token (TOKEN_TOO_SHORT of the
    live runs).
    """
    environment, target = resolve_target(config, call.args.env, call.args.target)
    provider = _token_provider(call)
    with contextlib.suppress(Exception):  # the log never changes what a command does
        trace.config_event(
            call.trace,
            project=config.project.name,
            environment=environment.name,
            target=target.id,
            server=target.server,
            database=target.database,
            table_model=config.project.table_model,
            auth=call.auth,
        )
    given = call.session_factory
    app_name = f"azsqlcd/{__version__} run={os.environ.get('GITHUB_RUN_ID', 'local')}"
    options: dict[str, Any] = {"min_token_minutes": config.project.min_token_minutes}
    if read_only and "read_only" in inspect.signature(connect).parameters:
        options["read_only"] = True

    def open_session() -> Session:
        call.connected = True
        if given is not None:
            return given()
        return connect(target.server, target.database, provider, app_name, **options)

    return lambda: trace.open_traced(call.trace, open_session)


@contextlib.contextmanager
def _read_session(call: _Call, config: Config) -> Iterator[Session]:
    """The session of a read-only command, with the options of a runner session: the same lock
    timeout, and the language in which the tool reads engine errors."""
    environment, _ = resolve_target(config, call.args.env, call.args.target)
    db = _sessions(call, config, read_only=True)()
    try:
        runner.set_session_options(db, environment.lock_timeout_ms)
        yield db
    finally:
        db.close()


def _audit(call: _Call) -> runner.Audit:
    """Who asked for the run (A28). A resolve run has no flags for it: the workflow variables tell."""
    args, env = call.args, os.environ
    run_url = None
    if _repo_url() and env.get("GITHUB_RUN_ID"):
        run_url = f"{_repo_url()}/actions/runs/{env['GITHUB_RUN_ID']}"
    return runner.Audit(
        approved_by=getattr(args, "approved_by", None),
        approved_utc=getattr(args, "approved_utc", None),
        triggering_actor=getattr(args, "triggering_actor", None) or env.get("GITHUB_TRIGGERING_ACTOR"),
        ci_actor=env.get("GITHUB_ACTOR"),
        ci_run_url=getattr(args, "ci_run_url", None) or run_url,
    )


# ------------------------------------------------------------------ findings
def _report_findings(call: _Call, findings: Sequence[lint.Finding]) -> str | None:
    """Print every finding. Returns why the command fails when one of them is an error, else None."""
    for finding in findings:
        call.say(f"{finding.path}:{finding.line}: {finding.severity} {finding.code}: {finding.message}")
    errors = sum(finding.severity == lint.ERROR for finding in findings)
    call.say(f"{errors} error(s), {len(findings) - errors} warning(s)")
    call.summary.append(_NOT_A_PROOF + "\n")
    if findings:
        rows = [(f.severity, f.code, f"{f.path}:{f.line}", f.message) for f in findings]
        call.summary.append(_table(("Severity", "Code", "Where", "Finding"), rows))
    if not errors:
        return None
    first = next(finding for finding in findings if finding.severity == lint.ERROR)
    return f"{errors} error(s) in the files; first: {first.code} at {first.path}:{first.line}"


def _lint(files: Mapping[str, bytes]) -> list[lint.Finding]:
    """lint_repo, with the table-file rules when azsqlcd.toml loads and says table_model = true."""
    try:
        table_model = _config_of(files, "the files").project.table_model
    except ToolError:
        table_model = False  # lint reports CONFIG_INVALID
    return lint.lint_repo(files, table_file_check=gen.table_file_check if table_model else None)


# ------------------------------------------------------------------ offline commands
def _cmd_lint(call: _Call) -> None:
    errors = _report_findings(call, _lint(gen.read_working_tree(call.args.root)))
    if errors:
        raise refused("LINT_FAILED", errors)


def _base_commit(root: str, revision: str) -> str:
    """--base of verify as gen.verify takes it: a full commit id or a full ref name pass as they
    are. Any other revision that git reads (origin/main, main, HEAD, a short commit id) is
    resolved here to its full commit id, so verify takes what gen and build take (CU-05).

    One case stays refused (MAIN_REF_INVALID): a short name that is also the name of a tag. git
    reads refs/tags/<name> before any branch, and anyone who can push a tag can make one."""
    if _COMMIT_ID.fullmatch(revision) or revision.startswith("refs/"):
        return revision
    name = re.split(r"[~^@:]", revision, maxsplit=1)[0]
    if name and release.git(["for-each-ref", "--format=%(refname)", f"refs/tags/{name}"], root).strip():
        raise refused(
            "MAIN_REF_INVALID",
            f"--base {revision!r}: the repository has a tag with the name {name!r}, and git reads the tag "
            f"first. Give the full commit id, or the full ref name, for example {release.MAIN_REF}",
            ref=revision,
        )
    try:
        found = release.git(["rev-parse", "--verify", "--end-of-options", revision + "^{commit}"], root)
    except ToolError as error:
        raise refused(
            "GIT_FAILED",
            f"--base {revision!r} is not a commit of this repository: {error.message}",
            revision=revision,
        ) from None
    return found.decode().strip()


def _cmd_verify(call: _Call) -> None:
    errors = _report_findings(call, gen.verify(call.args.root, _base_commit(call.args.root, call.args.base)))
    if errors:
        raise refused("VERIFY_FAILED", errors)


def _cmd_gen(call: _Call) -> None:
    args = call.args
    if args.resum:
        resummed = gen.resum(args.root, args.base)
        for file in resummed.dropped:
            call.say(
                f"dropped the chain line of migrations/{file}: the migration is new on this branch "
                "and its file is gone"
            )
        for path in gen.write_resum(args.root, resummed):
            call.say(f"wrote {path}")
        return
    if not _root_config(args.root).project.table_model:
        raise refused(
            "GEN_NEEDS_TABLE_MODEL",
            "gen writes a migration from the table model, and azsqlcd.toml has table_model = false. "
            "Write the migration by hand; gen --resum renumbers and re-hashes it",
        )
    try:
        result = gen.generate(args.root, args.base, args.name, args.rename)
    except ToolError as error:
        for refusal in error.detail.get("refusals", []) if error.reason_code == "GEN_REFUSED" else []:
            call.warn(f"{refusal['code']} {refusal['object']}: {refusal['message']} Hint: {refusal['hint']}")
        raise
    for path in gen.write_result(args.root, result):
        call.say(f"wrote {path}")
    for hint in result.hints:
        call.warn(hint)
    if result.text is None:
        call.say(f"no migration: {result.reason}")
        return
    todo = result.text.count("reason: TODO")
    if todo:
        call.say(f"{todo} allow line(s) need a reason: replace TODO, then run azsqlcd gen --resum")


def _out_problem(out: Path) -> str | None:
    """Why --out cannot become a directory, read from what exists. Nothing is made here: a build
    that is refused leaves no directory."""
    for place in (out, *out.parents):
        if place.is_dir():
            return None if os.access(place, os.W_OK) else f"the directory {place} cannot be written"
        if place.exists() or place.is_symlink():
            return f"{place} exists and is not a directory"
    return None


def _cmd_build(call: _Call) -> None:
    args = call.args
    out = Path(args.out)
    problem = _out_problem(out)
    if problem:  # CU-07: an error of the user, told before the build and not as a defect of the tool
        raise refused(
            "OUT_NOT_WRITABLE", f"--out {args.out} cannot be a directory: {problem}. Nothing was built"
        )
    prefix = release.git(["rev-parse", "--show-prefix"], args.root).decode().strip()
    if prefix:
        # a release is read from the root of the commit; from below it git names a missing file
        raise refused(
            "CONFIG_INVALID",
            f"{Path(args.root).resolve()} is the sub-directory {prefix} of the repository, and "
            f"{release.CONFIG_PATH} is in its root. Run the command in the root of the database "
            "repository, or give --root DIR",
        )
    built = release.build(args.root, args.commit)
    missing = [path for path in (release.CONFIG_PATH, chain.SUM_PATH) if path not in built.files]
    if missing:
        raise refused(
            "BUNDLE_INCOMPLETE",
            f"commit {built.manifest.commit} holds no {missing[0]}; a release needs {release.CONFIG_PATH} "
            f"and {chain.SUM_PATH}",
            missing=missing,
        )
    errors = _report_findings(call, _lint(built.files))
    if errors:  # a release never holds a file that lint refuses
        raise refused("LINT_FAILED", errors)
    try:
        release.write(built, out)
    except OSError as error:
        raise refused(
            "OUT_NOT_WRITABLE",
            f"the release could not be written under --out {args.out} ({type(error).__name__}: "
            f"{error.strerror or error}). The files there are not a release",
        ) from None
    name, digest = f"r{built.manifest.release_seq}", release.digest(built.manifest)
    call.outputs |= {"release": name, "digest": digest}
    call.say(f"release {name} of commit {built.manifest.commit}: {len(built.files)} file(s)")
    call.say(f"digest {digest}")
    call.summary.append(
        _table(
            ("Release", "Commit", "Files", "Digest"),
            [(name, built.manifest.commit, len(built.files), digest)],
        )
    )


def _largest_nontx_minutes(files: Mapping[str, bytes]) -> int | None:
    """The largest expected-minutes of a non-transactional migration of the chain; None: no such one."""
    if chain.SUM_PATH not in files:
        return None
    try:
        entries = chain.parse_sum(files[chain.SUM_PATH].decode("utf-8")).entries
        minutes: list[int] = []
        for entry in entries:
            if entry.mode != "nontx":
                continue
            data = files.get(f"migrations/{entry.file}")
            if data is None:
                raise refused("CHAIN_INVALID", f"migrations/{entry.file} is not in the release")
            expected = chain.parse_migration(data.decode("utf-8"), entry.file).expected_minutes
            if expected is None:
                raise refused(
                    "MIGRATION_INVALID",
                    f"{entry.file} is non-transactional and states no expected-minutes; the job timeout "
                    "cannot be computed",
                    file=entry.file,
                )
            minutes.append(expected)
    except UnicodeDecodeError:
        raise refused("CHAIN_INVALID", "a file of the chain is not UTF-8") from None
    return max(minutes, default=None)


def _cmd_targets(call: _Call) -> None:
    bundle = release.read_bundle(call.args.bundle, call.args.digest)
    config = _config_of(bundle.files, "the release")
    matrix = targets_matrix(config, call.args.env)
    # a job timeout must not act as a client timeout on a long build (Part 2 (i))
    timeout = config.env[call.args.env].job_timeout_minutes
    largest = _largest_nontx_minutes(bundle.files)
    if largest is not None:
        timeout = max(timeout, 2 * largest + 30)
    call.outputs |= {"matrix": json.dumps(matrix, separators=(",", ":")), "timeout": str(timeout)}
    for row in matrix:
        call.say(f"{row['id']}: {row['database']} on {row['server']}")
    call.say(f"job timeout: {timeout} min")
    call.summary.append(
        _table(("Target", "Server", "Database"), [(r["id"], r["server"], r["database"]) for r in matrix])
    )
    call.summary.append(f"Job timeout: {timeout} min\n")


def _cmd_setup_sql(call: _Call) -> None:
    call.say(state.setup_sql(_root_config(call.args.root), call.args.env, call.args.target))


# ------------------------------------------------------------------ plan and deploy
def _dependants(computed: plan.Plan) -> list[tuple[str, tuple[str, ...]]]:
    """A12: each managed dependant of a table that the release changes, with the findings that the
    engine gave for it before the change. The run fails for a finding that is not in that list."""
    keys = sorted({*computed.dependants, *computed.pre_broken, *computed.dependant_findings})
    return [(key, tuple(computed.dependant_findings.get(key, ()))) for key in keys]


_NO_TOKEN = "not applicable (SQL authentication has no access token)"


def _signed(computed: plan.Plan, auth: str | None) -> plan.Plan:
    """The plan with a note that names the kind of sign-in, for plan.json. A note is not in
    plan_sha256, so the plan job and the deploy job may sign in in different ways."""
    if auth is None:
        return computed
    return dataclasses.replace(computed, notes=(*computed.notes, f"sign-in: {auth}"))


def _plan_lines(computed: plan.Plan, auth: str | None = None) -> list[str]:
    """The plan for a log: names, hashes and counts. A plan holds no SQL text."""
    lines = [
        f"plan of r{computed.release_seq} for {computed.target_id} ({computed.environment}): "
        f"{computed.outcome}, pending = {str(computed.pending).lower()}",
        f"plan_sha256 {computed.plan_sha256}",
        f"recorded: r{computed.recorded_release_seq} {computed.recorded_git_sha or '(no run ended ok)'}",
        f"release: r{computed.release_seq} {computed.git_sha}",
    ]
    if auth is not None:
        lines.append(f"sign-in: {auth}")
    if auth == AUTH_SQL:
        lines.append(f"token minutes left: {_NO_TOKEN}")
    if computed.compare_url:
        lines.append(f"compare: {computed.compare_url}")
    if computed.destructive:
        lines.append(f"DESTRUCTIVE: {len(computed.destructive)} item(s)")
        lines += [f"  {item.code} {item.object}: {item.reason}" for item in computed.destructive]
    for number, unit in enumerate(computed.units, start=1):
        lines.append(f"unit {number} ({unit.kind}): {len(unit.steps)} step(s)")
        lines += [f"  {step.kind} {step.id}" for step in unit.steps]
    for drift in computed.drift:
        properties = ", ".join(difference.property for difference in drift.differences)
        lines.append(f"drift: {drift.key} ({properties})")
    if computed.unmanaged:
        lines.append(f"unmanaged objects: {len(computed.unmanaged)}")
    for key, before in _dependants(computed):
        if key in computed.pre_broken:
            lines.append(f"pre-broken dependant: {key}")
        told = "; ".join(before) or "none"
        lines.append(f"dependant to check: {key} (findings before the change: {told})")
    lines += [f"note: {note}" for note in computed.notes]
    return lines


def _plan_summary(computed: plan.Plan, auth: str | None = None) -> list[str]:
    """A27, and plan step 12: what the approver reads. The destructive list is first, with a
    banner; then the plan, the dependants that the run checks (A12), the steps with their links,
    and the notes. Every value goes through _md: a name or a reason cannot hide a row.

    auth: the kind of sign-in of the command. A SQL login has no token: its token minutes are
    shown as not applicable, never as a value."""
    minutes: object = "" if computed.token_minutes_left is None else computed.token_minutes_left
    if auth == AUTH_SQL:
        minutes = _NO_TOKEN
    parts: list[str] = []
    if computed.destructive:
        parts.append(f"> **DESTRUCTIVE: {len(computed.destructive)} item(s). Read each one.**\n")
        rows = [
            (item.code, item.object, f"{item.migration}:{item.line}" if item.migration else "", item.reason)
            for item in computed.destructive
        ]
        parts.append(_table(("Code", "Object", "Where", "Reason"), rows))
    else:
        parts.append("Destructive items (allow lines, module drops, module overwrites): none.\n")
    compare = (
        _Link("recorded commit ... release commit", computed.compare_url) if computed.compare_url else ""
    )
    facts = [
        ("Target", f"{computed.target_id} ({computed.environment})"),
        ("Outcome", f"{computed.outcome}, pending = {str(computed.pending).lower()}"),
        ("plan_sha256", computed.plan_sha256),
        ("Recorded", f"r{computed.recorded_release_seq} {computed.recorded_git_sha or ''}"),
        ("Release", f"r{computed.release_seq} {computed.git_sha}"),
        ("Compare", compare),
        ("Service objective", computed.service_objective or ""),
        *([("Sign-in", auth)] if auth is not None else []),
        ("Token minutes left", minutes),
        ("Unmanaged objects", len(computed.unmanaged)),
    ]
    parts.append(_table(("Plan", "Value"), facts))
    dependants = _dependants(computed)
    if dependants:
        rows = [
            (
                key,
                "PRE_BROKEN: it had findings before the change" if key in computed.pre_broken else "sound",
                "; ".join(before) or "none",
            )
            for key, before in dependants
        ]
        parts.append(
            "Dependants (A12). After the change the run reads each one again and fails for a finding "
            "that is not in its list here.\n"
        )
        parts.append(_table(("Dependant", "Before the change", "Findings before the change"), rows))
    steps = [
        (number, unit.kind, step.kind, _Link(step.id, step.url) if step.url else step.id)
        for number, unit in enumerate(computed.units, start=1)
        for step in unit.steps
    ]
    if steps:
        parts.append(_table(("Unit", "Kind", "Step", "Id"), steps))
    if computed.table_facts:
        rows = [(fact.key, fact.rows, fact.reserved_pages) for fact in computed.table_facts]
        parts.append(_table(("Table", "Rows", "Reserved pages"), rows))
    if computed.drift:
        rows = [(d.key, ", ".join(x.property for x in d.differences)) for d in computed.drift]
        parts.append(_table(("Drifted object", "Properties"), rows))
    parts += [f"- {_md(note)}\n" for note in computed.notes]
    return parts


def _cmd_plan(call: _Call) -> None:
    args = call.args
    out = _out_dir(call)
    bundle, config = _release(call)
    sessions = _sessions(call, config, read_only=True)
    provider = _token_provider(call)
    token = None if isinstance(provider, SqlLogin) else provider.get()  # a SQL login has no token
    with _read_session(call, config) as db:
        computed = plan.compute_plan(
            bundle,
            config,
            args.env,
            args.target,
            db,
            tool_version=__version__,
            tool_digest=release.tool_digest(),
            repo_url=_repo_url(),
            open_second_session=sessions,
            token_minutes_left=None if token is None else int((token.expires_on - time.time()) // 60),
            table_hooks=_hooks(bundle, config),
        )
    if out is not None:
        _write(out / "plan.json", _signed(computed, call.auth).to_json())
    call.outputs |= {"pending": str(computed.pending).lower(), "plan_sha256": computed.plan_sha256}
    for line in _plan_lines(computed, call.auth):
        call.say(line)
    call.summary += _plan_summary(computed, call.auth)


def _report_run(call: _Call, report: runner.Report, out: Path | None, plan_file: bool) -> None:
    """What a run reports, for exit 0 and for every other end: report.json, the outputs, the summary.

    The run is over when this is called, so nothing here may change its exit code: an error of
    this function is a warning (an "unknown error" would say that nothing was executed).
    """
    try:
        _run_report(call, report, out, plan_file)
    except Exception as error:
        call.warn(f"azsqlcd: the report of the run is not complete ({type(error).__name__})")


def _run_report(call: _Call, report: runner.Report, out: Path | None, plan_file: bool) -> None:
    if out is not None:
        told = dataclasses.replace(report, auth=call.auth)  # the kind of sign-in of this call
        _write_after_run(call, out / "report.json", told.to_json())
        if plan_file and report.plan is not None:
            _write_after_run(call, out / "plan.json", _signed(report.plan, call.auth).to_json())
    if report.plan_sha256:
        call.outputs["plan_sha256"] = report.plan_sha256
    facts = [
        ("Target", f"{report.target_id} ({report.environment})"),
        ("Release", f"r{report.release_seq} {report.git_sha}"),
        ("Run", "" if report.run_id is None else report.run_id),
        *([("Sign-in", call.auth)] if call.auth is not None else []),
        # the reference for a point-in-time restore (Part 2 (f))
        ("Started (UTC, server)", report.started_utc or ""),
        # the steps of the plan: each batch, module and refresh. Not the rows of azsqlcd.step, which
        # has one row for a migration and one for the modules of a run (E2E-11)
        ("Plan steps applied (batches, modules, refreshes)", len(report.steps_applied)),
        ("Modules deployed", len(report.modules_deployed)),
        ("Modules dropped", len(report.modules_dropped)),
        ("Failed step", report.failed_step or ""),
    ]
    call.summary.append(_table(("Run", "Value"), facts))
    if report.plan is not None:
        # its notes are the warnings of the report; the sign-in is in the table of the run above
        call.summary += _plan_summary(report.plan, AUTH_SQL if call.auth == AUTH_SQL else None)
    else:
        call.summary += [f"- {_md(note)}\n" for note in report.warnings]


def _run(
    call: _Call, out: Path | None, body: Callable[[], runner.Report], *, plan_file: bool = False
) -> None:
    """A command that writes to the database through the runner: its Report is written for every end."""
    try:
        report = body()
    except runner.RunError as error:
        _report_run(call, error.report, out, plan_file)
        raise
    except ToolError:
        raise  # a refusal before the run: it has its exit code
    except Exception as error:
        if not call.connected:
            raise  # no session was asked for: nothing was sent (22 TOOL_DEFECT)
        # A1. The runner gives a RunError for every end. Anything else, after a session was asked
        # for in a command that writes, is never "nothing was executed".
        raise unknown(
            "TOOL_DEFECT_AFTER_DISPATCH",
            f"the tool stopped on an error that it does not know ({type(error).__name__}) in a command "
            "that writes to the database, and it has no report of the run. What was applied is not "
            "known. Run azsqlcd plan, then azsqlcd resolve",
            exception=type(error).__name__,
        ) from error
    _report_run(call, report, out, plan_file)
    call.reason_code = report.reason_code
    call.say(report.message)
    if report.started_utc:
        call.say(f"run {report.run_id} started {report.started_utc} UTC (server time)")


def _expected_plan(path: str) -> plan.Plan:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise refused(
            "PLAN_INVALID", f"the plan file {path} cannot be read ({type(error).__name__})"
        ) from None
    return plan.Plan.from_json(text)


def _cmd_deploy(call: _Call) -> None:
    args = call.args
    out = _out_dir(call)
    bundle, config = _release(call)
    expected = None if args.inline_plan else _expected_plan(args.expect_plan_file)
    sessions = _sessions(call, config)
    _run(
        call,
        out,
        lambda: runner.deploy(
            bundle,
            config,
            args.env,
            args.target,
            expect_plan=expected,
            inline_plan=args.inline_plan,
            session_factory=sessions,
            token_provider=_token_provider(call),
            audit=_audit(call),
            tool_version=__version__,
            tool_digest=release.tool_digest(),
            repo_url=_repo_url(),
            table_hooks=_hooks(bundle, config),
        ),
        plan_file=args.inline_plan,
    )


# ------------------------------------------------------------------ drift and onboarding
def _cmd_drift(call: _Call) -> None:
    args = call.args
    out = _out_dir(call)
    bundle, config = _release(call)
    _, target = resolve_target(config, args.env, args.target)
    with _read_session(call, config) as db:
        if args.export is not None and out is not None:
            plan.check_fence(catalog.fence_facts(db), target.database)
            path, data = onboard.export_drift(db, args.export)
            _write(out / path, data)
            call.say(f"wrote {out / path}")
            return
        found = onboard.drift(db, bundle, config, args.env, args.target, _hooks(bundle, config))
    # object, property and two hashes; never definition text (A25)
    for item in found.items:  # '-': an unmanaged object has no recorded hash and no live hash
        hashes = f"{item.stored_hash or '-'} {item.live_hash or '-'}"
        call.say(f"{item.cls} {item.object_key} {item.property} {hashes}")
    if found.items:
        rows = [
            (i.cls, i.object_key, i.property, i.stored_hash or "", i.live_hash or "") for i in found.items
        ]
        call.summary.append(_table(("Class", "Object", "Property", "Recorded hash", "Live hash"), rows))
    if found.has_drift:
        drifted = sorted({item.object_key for item in found.items if item.cls != onboard.UNMANAGED})
        raise ToolError(
            Exit.DRIFT,
            "DRIFT_FOUND",
            f"{drifted[0]} differs from what the tool recorded, or is gone ({len(drifted)} object(s) in "
            "all). Nothing was changed",
            detail={"objects": drifted},
        )
    call.say("no drift")


def _cmd_export(call: _Call) -> None:
    args = call.args
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    config = _root_config(args.root)
    _, target = resolve_target(config, args.env, args.target)
    with _read_session(call, config) as db:
        plan.check_fence(catalog.fence_facts(db), target.database)
        exported = onboard.export(db, config)
    reports = out / "onboarding" / args.env
    files: dict[Path, bytes | str] = {out / path: data for path, data in exported.files.items()}
    files[reports / "export.md"] = exported.report_md
    files[reports / "snapshot.json"] = json.dumps(exported.snapshot, indent=2, sort_keys=True) + "\n"
    if exported.rename_constraints_sql:
        files[reports / "rename-constraints.sql"] = exported.rename_constraints_sql
    for file, data in files.items():
        try:
            _write(file, data)
        except OSError as error:
            # N4-05. On Windows without long paths: a full path above 259 characters
            raise refused(
                "EXPORT_INCOMPLETE",
                f"{file} could not be written ({type(error).__name__}: {error}); the export is not "
                "complete. The database was only read",
                path=str(file),
            ) from None
    counts = f"{len(exported.files)} object file(s); {len(exported.unmanaged)} object(s) stay unmanaged"
    if exported.history_tables:
        # not unmanaged and not exported: the file of each temporal table names its history table
        counts += (
            f"; {len(exported.history_tables)} history table(s) of temporal tables are owned by the "
            "engine and get no file"
        )
    call.say(f"{counts}. Written under {out}")
    call.say(f"report: {reports / 'export.md'}")
    call.summary.append(counts + "\n")


def _cmd_baseline(call: _Call) -> None:
    args = call.args
    out = _out_dir(call)
    bundle, config = _release(call)
    options: dict[str, Any] = {
        "confirm_database": args.confirm_database,
        "tool_version": __version__,
        "tool_digest": release.tool_digest(),
        "table_hooks": _hooks(bundle, config),
    }
    done: list[onboard.BaselineResult] = []

    def record() -> runner.Report:
        db = _sessions(call, config)()
        try:  # the frame of the runner closes the session; a refusal before it does not
            found = onboard.baseline(db, bundle, config, args.env, args.target, report_only=False, **options)
        finally:
            db.close()
        done.append(found)
        if found.report is None:
            raise RuntimeError("a baseline that writes gave no report")
        return found.report

    if args.report_only:
        with _read_session(call, config) as db:
            done.append(
                onboard.baseline(db, bundle, config, args.env, args.target, report_only=True, **options)
            )
    else:
        try:
            _run(call, out, record)
        except ToolError as error:
            script = error.detail.get("rename_constraints_sql")
            if error.reason_code == "CONSTRAINT_NAMES" and isinstance(script, str) and script:
                # names and EXEC sys.sp_rename only: the file that a human reviews and runs
                path = Path("onboarding", args.env, "rename-constraints.sql")
                _write_after_run(call, path, script)
                call.warn(f"the script that gives the constraints the names of the files: {path}")
            raise
    try:
        _baseline_told(call, done[0])
    except Exception as error:
        if args.report_only:
            raise
        # the baseline is recorded: its exit code is 0, whatever happens to the report files
        call.warn(f"azsqlcd: the report of the baseline is not complete ({type(error).__name__})")


def _baseline_told(call: _Call, result: onboard.BaselineResult) -> None:
    """The files and the lines of a baseline that ran: the diff report, the live text of each
    module that differs, the rename script."""
    args = call.args
    reports = Path("onboarding", args.env)
    _write_after_run(call, reports / "baseline-diff.md", result.report_md)
    for path, data in result.live_files.items():
        _write_after_run(call, Path(path), data)
    if result.rename_constraints_sql:
        _write_after_run(call, reports / "rename-constraints.sql", result.rename_constraints_sql)
    counts: dict[str, int] = {}
    for item in result.items:
        counts[item.status] = counts.get(item.status, 0) + 1
    call.say("modules: " + (", ".join(f"{n} {status}" for status, n in sorted(counts.items())) or "none"))
    refusing = [i.object_key for i in result.table_items if i.state in (tables.DIFFERS, tables.MISSING_HERE)]
    if result.table_items:
        call.say(
            f"table-class objects: {len(result.table_items)} compared, {len(refusing)} differ or are missing"
        )
    if result.unacknowledged:
        call.say(f"{len(result.unacknowledged)} module(s) differ and are not acknowledged")
    call.say(f"report: {reports / 'baseline-diff.md'}")
    call.summary.append(result.report_md)


# ------------------------------------------------------------------ resolve
def _cmd_resolve(call: _Call) -> None:
    args = call.args
    out = _out_dir(call)
    bundle, config = _release(call)
    action = next(name for name in _RESOLVE_ACTIONS if getattr(args, name) is not None)
    subject = getattr(args, action)
    if args.force_no_readback and action != "mark_applied":
        raise refused("RESOLVE_NOT_APPLICABLE", "--force-no-readback is for --mark-applied only")
    if action == "rebind_environment" and subject != args.env:
        raise refused(
            "REBIND_ENV_MISMATCH",
            f"--rebind-environment names {subject!r} and --env names {args.env!r}. A database is bound to "
            "the environment of the target: give both the same name",
        )
    common: dict[str, Any] = {
        "confirm_database": args.confirm_database,
        "reason": args.reason,
        "session_factory": _sessions(call, config),
        "token_provider": _token_provider(call),
        "audit": _audit(call),
        "tool_version": __version__,
        "tool_digest": release.tool_digest(),
    }
    where = (bundle, config, args.env, args.target)
    hooks = _hooks(bundle, config)
    bodies: dict[str, Callable[[], runner.Report]] = {
        "mark_applied": lambda: runner.mark_applied(
            *where, subject, table_hooks=hooks, force_no_readback=args.force_no_readback, **common
        ),
        "mark_not_applied": lambda: runner.mark_not_applied(*where, subject, **common),
        "accept_drift": lambda: runner.accept_drift(*where, subject, table_hooks=hooks, **common),
        "adopt_module": lambda: runner.adopt_module(*where, subject, **common),
        "clear_run": lambda: runner.clear_run(*where, subject, **common),
        "rebind_environment": lambda: runner.rebind_environment(*where, **common),
    }
    _run(call, out, bodies[action])


# ------------------------------------------------------------------ the triage log
def _open_trace(call: _Call, argv: Sequence[str]) -> None:
    """Start the log of a command that opens a session. Never raises: a log that cannot be
    started is a warning, and the command runs as it does with --no-log."""
    args = call.args
    if args.command not in _DATABASE_COMMANDS or args.no_log:
        return
    try:
        out = args.out if args.command in _OUT_OF_THE_RUN else None
        if args.log_dir is not None:
            folder = Path(args.log_dir)
        elif call.session_factory is not None and out is None and not os.environ.get(trace.LOG_DIR_VARIABLE):
            # The caller gives the sessions (a test, a program that embeds the tool) and named no
            # directory: the working directory is not the tool's to fill with .azsqlcd/logs.
            return
        else:
            folder = trace.default_log_dir(out)
        call.trace = trace.Trace(folder / trace.log_file_name(args.command, datetime.now(UTC)))
        if isinstance(call.token_provider, SqlLogin):  # a SQL login that the caller gave
            call.trace.hide_sign_in(call.token_provider.user, call.token_provider.password)
        if not call.trace.enabled:
            return  # _end_trace tells why
        call.outputs["log"] = str(call.trace.path)
        trace.header(
            call.trace,
            command=args.command,
            argv=argv,
            tool_version=__version__,
            tool_digest=release.tool_digest(),
        )
    except Exception as error:
        call.warn(f"azsqlcd: the triage log could not be started ({type(error).__name__})")


def _trace_exception(call: _Call, error: BaseException) -> None:
    with contextlib.suppress(Exception):
        trace.exception_event(call.trace, error)


def _end_trace(call: _Call, exit_code: int, reason_code: str, message: str) -> None:
    """The last event of the log, and for a non-zero exit the line `log: <path>` on stderr.
    Never raises, and never changes the exit code."""
    log = call.trace
    if log.path is None:
        return
    with contextlib.suppress(Exception):
        trace.end(log, exit_code=exit_code, reason_code=reason_code, message=message)
    error = log.close()
    if error is not None:
        call.outputs.pop("log", None)
        call.warn(
            f"azsqlcd: the triage log {log.path} could not be written ({type(error).__name__}). The "
            "exit code is that of the command"
        )
    elif exit_code != 0:
        call.warn(f"log: {log.path}")


def _the_log(call: _Call) -> Path:
    """The log that show-log and support-bundle read: --log, else the latest one of --log-dir."""
    args = call.args
    if args.log is not None:
        log = Path(args.log)
        if not log.is_file():
            raise refused("LOG_NOT_FOUND", f"--log {args.log} is not a file")
        return log
    folder = Path(args.log_dir) if args.log_dir is not None else trace.default_log_dir(None)
    found = trace.latest_log(folder)
    if found is None:
        raise refused(
            "LOG_NOT_FOUND",
            f"the directory {folder} holds no log azsqlcd-*.jsonl. Give --log FILE, or --log-dir DIR: "
            "plan, deploy, baseline and resolve write their log to <--out>/logs, and a non-zero exit "
            "prints the path as `log: <path>`",
        )
    return found


def _cmd_show_log(call: _Call) -> None:
    log = _the_log(call)
    try:
        call.say(trace.summarize(log))
    except OSError as error:
        raise refused("LOG_NOT_FOUND", f"{log} cannot be read ({type(error).__name__})") from None


def _files_of_the_run(log: Path) -> list[Path]:
    """plan.json, report.json and manifest.json of the run that the log records, where
    trace.support_bundle does not look: the --out and the --bundle that the first event names.
    A path that is not absolute is read from the working directory."""
    try:
        events, _ = trace.read_events(log)
    except OSError:
        return []
    run = next((event for event in reversed(events) if event.get("kind") == "run"), {})
    argv = run.get("argv")
    words = [str(word) for word in argv] if isinstance(argv, list) else []
    beside = {log.parent.resolve()} | ({log.parent.parent.resolve()} if log.parent.name == "logs" else set())
    found: dict[str, Path] = {}
    for option in ("--out", "--bundle"):
        value = next((words[at + 1] for at, word in enumerate(words[:-1]) if word == option), None)
        if value is None or Path(value).resolve() in beside:
            continue
        for name in trace.REPORT_FILES:
            file = Path(value, name)
            small = (
                file.is_file()
                and not file.is_symlink()
                and file.stat().st_size <= trace.MAX_BUNDLE_FILE_BYTES
            )
            if small and name not in found and not any((place / name).exists() for place in beside):
                found[name] = file
    return list(found.values())


def _cmd_support_bundle(call: _Call) -> None:
    log = _the_log(call)
    try:
        names = trace.support_bundle(
            log, call.args.out, _files_of_the_run(log), tool_digest=release.tool_digest()
        )
    except trace.BundleRefused as error:
        raise refused("BUNDLE_REFUSED", f"{error}. No zip was written") from None
    except OSError as error:
        raise refused(
            "BUNDLE_REFUSED",
            f"the zip {call.args.out} could not be written ({type(error).__name__}: "
            f"{error.strerror or error})",
        ) from None
    call.say(f"wrote {call.args.out}: {len(names)} file(s)")
    for name in names:
        call.say(f"  {name}")
    call.say(
        "The log holds statement classes, object names, hashes, engine error numbers and redacted "
        "messages; no SQL text, no data value, no token. plan.json and report.json hold object names "
        "and hashes. Read the files before you send the zip."
    )


# ------------------------------------------------------------------ arguments
def _utc(text: str) -> datetime:
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        value = None
    if value is None or value.tzinfo is None:
        raise argparse.ArgumentTypeError("needs a time with a time zone, for example 2026-10-07T09:00:00Z")
    return value


_LOG_DIR_HELP = (
    "directory of the triage logs. Default: the variable AZSQLCD_LOG_DIR; else <--out>/logs for "
    "plan, deploy, baseline and resolve; else .azsqlcd/logs in the working directory"
)
_NO_LOG_HELP = "write no triage log"


def parsers() -> tuple[argparse.ArgumentParser, dict[str, argparse.ArgumentParser]]:
    """The parser of the tool, and the parser of each command by its name."""
    parser = argparse.ArgumentParser(
        prog="azsqlcd",
        description="CI/CD for Azure SQL Database. Exit codes: 0 ok, 21 failed and rolled back, 22 "
        "refused, 23 outcome unknown, 24 clean stop, 25 locked, 30 drift found. A command that opens "
        "a database session writes a triage log and prints its path on a non-zero exit: "
        "azsqlcd show-log, azsqlcd support-bundle.",
    )
    parser.add_argument("--version", action="version", version=f"azsqlcd {__version__}")
    # The two options of the log are read before and after the name of the command.
    parser.add_argument("--log-dir", metavar="DIR", default=None, help=_LOG_DIR_HELP)
    parser.add_argument("--no-log", action="store_true", default=False, help=_NO_LOG_HELP)
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    by_name: dict[str, argparse.ArgumentParser] = {}

    ci = argparse.ArgumentParser(add_help=False)
    ci.add_argument("--ci", choices=["github"], help="write outputs and a summary for GitHub Actions")
    # SUPPRESS: a command that does not get the option keeps the value given before its name
    ci.add_argument("--log-dir", metavar="DIR", default=argparse.SUPPRESS, help=_LOG_DIR_HELP)
    ci.add_argument("--no-log", action="store_true", default=argparse.SUPPRESS, help=_NO_LOG_HELP)
    root = argparse.ArgumentParser(add_help=False)
    root.add_argument("--root", default=".", metavar="DIR", help="the database repository (default: .)")
    bundle = argparse.ArgumentParser(add_help=False)
    bundle.add_argument(
        "--bundle", required=True, metavar="DIR", help="directory of bundle.tar and manifest.json"
    )
    bundle.add_argument("--digest", required=True, metavar="D", help="digest of the release")
    bundle.add_argument("--env", required=True, metavar="E", help="environment of azsqlcd.toml")
    target = argparse.ArgumentParser(add_help=False)
    target.add_argument("--target", required=True, metavar="T", help="target id of azsqlcd.toml")
    target.add_argument(
        "--show-error-text",
        action="store_true",
        help="print engine messages in full, not redacted. Refused for prod",
    )
    confirm = "name of the database, typed by a human. The tool compares it with the target"

    def add(
        name: str, handler: Callable[[_Call], None], *parents: argparse.ArgumentParser, help: str
    ) -> argparse.ArgumentParser:
        command = commands.add_parser(name, parents=[*parents, ci], help=help, description=help)
        command.set_defaults(handler=handler, out=None, env=None, show_error_text=False)
        by_name[name] = command
        return command

    add("lint", _cmd_lint, root, help="file rules, batch classifier, allow lines, tombstones")
    verify = add(
        "verify", _cmd_verify, root, help="lint, chain, module rules and the proof of a pull request"
    )
    verify.add_argument(
        "--base",
        required=True,
        metavar="REV",
        help="base of the pull request, as gen and build take a revision: origin/main, HEAD, a short "
        "or a full commit id, or a full ref name such as refs/remotes/origin/main. The tool resolves "
        "it to the full commit id (git rev-parse) before the check. Usual value: the commit of "
        "`git merge-base origin/main HEAD`. A short name that a tag also has is refused "
        "(MAIN_REF_INVALID): git reads the tag first",
    )

    generate = add("gen", _cmd_gen, root, help="write a migration from the model diff, or renumber (--resum)")
    generate.add_argument(
        "--base", default="origin/main", metavar="REV", help="revision to compare with (default: origin/main)"
    )
    what = generate.add_mutually_exclusive_group(required=True)
    what.add_argument("--name", metavar="NAME", help="name of the new migration: letters, digits and _")
    what.add_argument("--resum", action="store_true", help="renumber and re-hash the new migrations")
    generate.add_argument(
        "--rename",
        action="append",
        default=[],
        metavar="KIND:OLD=NEW",
        help="a rename; the tool infers none. KIND is table, column, index or constraint. A column "
        "and an index: column:[schema].[table].[old]=[new]. A table and a constraint have no "
        "[table] part: table:[schema].[old]=[new]. May be given more than once",
    )

    build = add("build", _cmd_build, root, help="bundle.tar and manifest.json from one commit of main")
    build.add_argument(
        "--commit",
        required=True,
        metavar="REV",
        help="the commit of the release: a commit id, HEAD or a ref. It must be on the first-parent "
        "chain of refs/remotes/origin/main",
    )
    build.add_argument(
        "--out", required=True, metavar="DIR", help="directory for bundle.tar and manifest.json"
    )

    add("targets", _cmd_targets, bundle, help="the targets of an environment, for the workflow matrix")
    setup = add("setup-sql", _cmd_setup_sql, root, help="print the script that an administrator runs once")
    setup.add_argument("--env", required=True, metavar="E", help="environment of azsqlcd.toml")
    setup.add_argument("--target", required=True, metavar="T", help="target id of azsqlcd.toml")

    planned = add("plan", _cmd_plan, bundle, target, help="read-only plan of a release for a target")
    planned.add_argument("--out", required=True, metavar="DIR", help="directory for plan.json")

    deploy = add("deploy", _cmd_deploy, bundle, target, help="apply a release to a target")
    which = deploy.add_mutually_exclusive_group(required=True)
    which.add_argument("--expect-plan-file", metavar="FILE", help="plan.json of the plan job")
    which.add_argument("--inline-plan", action="store_true", help="plan under the lock (not gated only)")
    deploy.add_argument("--out", required=True, metavar="DIR", help="directory for report.json")
    deploy.add_argument(
        "--approved-by", metavar="LOGINS", help="who approved the run, for the run row (audit)"
    )
    deploy.add_argument(
        "--approved-utc",
        metavar="TIME",
        type=_utc,
        help="when the run was approved: a time with a time zone, for example 2026-10-07T09:00:00Z",
    )
    deploy.add_argument("--triggering-actor", metavar="LOGIN", help="who started the run, for the run row")
    deploy.add_argument("--ci-run-url", metavar="URL", help="address of the CI run, for the run row")

    drift = add(
        "drift", _cmd_drift, bundle, target, help="report drift; exit 30 when a managed object differs"
    )
    drift.add_argument("--export", metavar="KEY", help="write the live text of one module (needs --out)")
    drift.add_argument("--out", metavar="DIR", help="repository root for the file of --export")

    export = add("export", _cmd_export, root, target, help="catalog to object files (onboarding)")
    export.add_argument("--env", required=True, metavar="E", help="environment of azsqlcd.toml")
    export.add_argument("--out", required=True, metavar="DIR", help="schema/ and onboarding/<env>/ go here")

    baseline = add("baseline", _cmd_baseline, bundle, target, help="record an existing database")
    baseline.add_argument(
        "--report-only",
        action="store_true",
        help="compare only: nothing is changed in the database. The report files go to "
        "onboarding/<env>/ of the working directory",
    )
    baseline.add_argument("--confirm-database", required=True, metavar="NAME", help=confirm)
    baseline.add_argument("--out", metavar="DIR", help="directory for report.json")

    resolve = add("resolve", _cmd_resolve, bundle, target, help="human recovery: one action (A16)")
    resolve.add_argument("--confirm-database", required=True, metavar="NAME", help=confirm)
    resolve.add_argument("--reason", required=True, metavar="TEXT", help="why, for the run row (audit)")
    action = resolve.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--mark-applied",
        metavar="MIGRATION",
        help="record a migration as applied; the tool reads the objects back first",
    )
    action.add_argument(
        "--mark-not-applied", metavar="MIGRATION", help="remove the step row of a migration that did not run"
    )
    action.add_argument(
        "--accept-drift", metavar="KEY", help="record the live definition of one object as the known one"
    )
    action.add_argument(
        "--adopt-module", metavar="KEY", help="record a live module that the tool did not deploy"
    )
    action.add_argument(
        "--clear-run", metavar="RUN_ID", type=int, help="close a run row that says running or unknown"
    )
    action.add_argument(
        "--rebind-environment",
        metavar="ENV",
        help="bind the database to the environment of --env (after a copy from another environment)",
    )
    resolve.add_argument("--force-no-readback", action="store_true", help="--mark-applied only")
    resolve.add_argument("--out", metavar="DIR", help="directory for report.json")

    for name, handler, text in (
        ("show-log", _cmd_show_log, "print what a triage log holds: the command, the steps, the end"),
        (
            "support-bundle",
            _cmd_support_bundle,
            "pack a triage log with plan.json, report.json and manifest.json into a zip to send",
        ),
    ):
        reader = add(name, handler, help=text)
        which_log = reader.add_mutually_exclusive_group()
        which_log.add_argument("--log", metavar="FILE", help="the log file")
        which_log.add_argument(
            "--latest", action="store_true", help="the log of --log-dir that was written last (the default)"
        )
        if name == "support-bundle":
            reader.add_argument("--out", required=True, metavar="ZIP", help="the zip file to write")
    return parser, by_name


def _parser() -> argparse.ArgumentParser:
    return parsers()[0]


def parse(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """The arguments of one call. A wrong argument ends the process with exit 2 (argparse), with
    the usage of the command that it belongs to."""
    parser, commands = parsers()
    args = parser.parse_args(argv)
    if args.command == "gen" and args.resum and args.rename:
        commands["gen"].error("--rename is not used with --resum")
    if args.command == "drift" and (args.export is None) != (args.out is None):
        commands["drift"].error("--export and --out are given together")
    return args


# ------------------------------------------------------------------ exit handling
def _sql_cause(error: BaseException | None) -> SqlError | None:
    """The engine error behind an error of the tool, if one is in its chain of causes."""
    seen: set[int] = set()  # the runner raises an unknown outcome from itself: the chain can be a loop
    while error is not None and not isinstance(error, SqlError) and id(error) not in seen:
        seen.add(id(error))
        error = error.__cause__
    return error if isinstance(error, SqlError) else None


def _read_failure(error: SqlError) -> ToolError:
    """A SqlError that a read-only command let pass: nothing was changed. str(error) is redacted."""
    if error.cls in (ErrorClass.TRANSIENT_CONNECT, ErrorClass.SESSION_LOST):
        return retry_safe(
            "CONNECTION_LOST",
            f"the connection was lost, or the database was not available, in a read: {error}. Nothing "
            "was changed. Start again",
        )
    return refused("READ_FAILED", f"the database refused a read of the tool: {error}. Nothing was changed")


def _told_detail(failure: ToolError) -> list[str]:
    """The lines of a refusal that the message only counts: each object, blocker or batch.

    By the rule of every module that builds a ToolError, a detail holds names, property names,
    hashes and redacted engine messages; never definition or batch text (A25).
    """
    lines: list[str] = []
    for name in ("objects", "blockers", "failures"):
        found = failure.detail.get(name)
        for item in found if isinstance(found, list) else []:
            told = item if isinstance(item, str) else json.dumps(item, sort_keys=True, default=str)
            lines.append("  " + told)
    return lines


def _defect(call: _Call, error: Exception) -> ToolError:
    """A1: an error that the tool does not know, where nothing was executed. After a session was
    asked for, its text can hold anything, so only its type is told."""
    name = type(error).__name__
    told = name if call.connected or call.token_asked else f"{name}: {error}"
    return refused(
        "TOOL_DEFECT",
        f"the tool stopped on an error that it does not know ({told}). Nothing was executed",
        exception=name,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    session_factory: SessionFactory | None = None,
    token_provider: Credential | None = None,
) -> int:
    """Run one command and return its exit code. session_factory and token_provider are for
    tests; None means the real connect and the sign-in that the variable AZSQLCD_AUTH names (the
    Azure CLI login when it is not set), made when a command needs them."""
    _lenient_streams()  # first: argparse prints too
    args = parse(argv)
    call = _Call(args, session_factory, token_provider)
    if args.ci == "github" and not (os.environ.get(_OUTPUT_FILE) and os.environ.get(_SUMMARY_FILE)):
        # as a wrong argument: the tool did not start, and it has no place to report a reason code
        call.warn(f"azsqlcd: --ci github needs the variables {_OUTPUT_FILE} and {_SUMMARY_FILE}")
        return 2
    _open_trace(call, list(sys.argv[1:] if argv is None else argv))
    try:
        if args.show_error_text and (args.env == "prod" or getattr(args, "rebind_environment", None)):
            raise refused(
                "SHOW_ERROR_TEXT_REFUSED",
                "--show-error-text is refused for prod, and for a database that is bound to another "
                "environment (--rebind-environment): a full engine message can hold data",
            )
        if args.command in _DATABASE_COMMANDS:
            _sign_in(call)  # a sign-in that cannot be used is refused before anything is read
        args.handler(call)
        failure = None
    except ToolError as error:
        _trace_exception(call, error)
        failure = error
    except SqlError as error:
        _trace_exception(call, error)
        failure = _read_failure(error)
        failure.__cause__ = error
    except Exception as error:
        _trace_exception(call, error)
        failure = _defect(call, error)
    if failure is None:
        _end_trace(call, int(Exit.OK), call.reason_code, "")
        _write_ci(call, int(Exit.OK), call.reason_code, "")
        return int(Exit.OK)
    call.warn(str(failure))
    try:  # the exit code and the CI outputs are written whatever the detail holds
        for line in _told_detail(failure):
            call.warn(line)
        full = _sql_cause(failure) if args.show_error_text else None
        if full is not None:
            call.warn(f"engine message in full (--show-error-text): {full.raw_message}")
    except Exception as error:
        call.warn(f"azsqlcd: the detail of the error could not be printed ({type(error).__name__})")
    _end_trace(call, int(failure.exit_code), failure.reason_code, failure.message)
    call.warn(f"azsqlcd {args.command}: exit {int(failure.exit_code)}")
    _write_ci(call, int(failure.exit_code), failure.reason_code, failure.message)
    return int(failure.exit_code)
