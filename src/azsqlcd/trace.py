"""The triage log: what one call of the tool did, in order, and where it stopped.

One JSON object per line. The reader of a log was not there when the run failed and has no access
to the database, so the log holds every step. It also leaves the organisation, so it holds no SQL text,
no object definition, no data value, no token and no connection string (A1, A25):
  - a batch is recorded as its statement class (keywords and object names), its sha256, its length,
    its duration and the row count of each result set. Never its text, never a result value;
  - an engine error is recorded with the redacted message of SqlError, never raw_message;
  - an exception of an unknown type is recorded as its type and the place in the source, never
    its message;
  - the recorder refuses a field with a name such as `sql` or `token` (FORBIDDEN_FIELDS), so the
    rule does not depend on the care of a caller.

A log must not change what a command does: a file that cannot be written, and a batch that cannot
be read, never raise to the caller. Standard library only. This module imports errors, lex and
sqlerrors, and nothing that imports the command line.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import time
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from azsqlcd import __version__, lex
from azsqlcd.errors import ToolError
from azsqlcd.sqlerrors import SqlError

MAX_STRING = 500
MAX_ITEMS = 200
MAX_BUNDLE_FILE_BYTES = 5 * 1024 * 1024
LOG_DIR_VARIABLE = "AZSQLCD_LOG_DIR"
DEFAULT_LOG_DIR = Path(".azsqlcd", "logs")
FORBIDDEN_FIELDS = frozenset(
    {"text", "sql", "batch", "definition", "token", "password", "secret", "connection_string"}
)
RESERVED_FIELDS = frozenset({"ts", "seq", "kind"})
PACKAGES = ("mssql-python", "azure-identity")
NOT_INSTALLED = "not installed"
CI_VARIABLES = (
    "GITHUB_RUN_ID",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_REPOSITORY",
    "GITHUB_REF",
    "GITHUB_SHA",
    "GITHUB_WORKFLOW",
    "GITHUB_JOB",
    "RUNNER_OS",
    "RUNNER_NAME",
)
CONFIG_KEYS = ("project", "environment", "target", "server", "database", "table_model")
SESSION_NAMES = ("main", "parse")  # by the order in which a command opens its sessions
REPORT_FILES = ("plan.json", "report.json", "manifest.json")
VERSIONS_FILE = "versions.txt"
# A command that reads logs writes none: `--latest` must find the run, not the reader.
READER_COMMANDS = ("show-log", "support-bundle")

_READER_LOG = re.compile(rf"-(?:{'|'.join(READER_COMMANDS)})(?:-[0-9]+)?[.]jsonl$")
_PACKAGE_DIR = Path(__file__).resolve().parent


# ------------------------------------------------------------------ the recorder
def _clean(value: Any, depth: int = 0) -> Any:
    """A value that is safe to write: JSON scalars, and lists and dicts of them. A string is cut.
    Any other object is written as its type name, never as its text: str() of an unknown object
    can hold anything."""
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:MAX_STRING]
    if isinstance(value, os.PathLike):
        return str(os.fspath(value))[:MAX_STRING]
    if depth >= 6:
        return "<nested>"
    if isinstance(value, Mapping):
        _check_names(value)
        return {str(key)[:MAX_STRING]: _clean(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(item, depth + 1) for item in value[:MAX_ITEMS]]
    return f"<{type(value).__name__}>"


def _check_names(fields: Mapping[Any, Any]) -> None:
    for name in fields:
        if str(name).lower() in FORBIDDEN_FIELDS:
            raise ValueError(
                f"the triage log takes no field named {name!r}: it holds no SQL text, definition, "
                "token, password, secret or connection string"
            )


class Trace:
    """Writes one JSON object per line (UTF-8, LF). Trace(None) records nothing.

    The file is opened for append and flushed after each event, so the log of a process that is
    killed holds every event up to the last one. An error of the file never raises: the first one
    is kept and close() returns it.
    """

    def __init__(self, path: str | os.PathLike[str] | None, *, now: Callable[[], datetime] | None = None):
        self.path = None if path is None else Path(path)
        self.sessions_opened = 0  # open_traced names a session by this count
        self.config: dict[str, Any] | None = None  # the last config event, so it is written once
        self._now = now or (lambda: datetime.now(UTC))
        self._seq = 0
        self._file: Any = None
        self._error: Exception | None = None
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._file = open(self.path, "a", encoding="utf-8", newline="\n")  # noqa: SIM115
            except Exception as error:
                self._error = error

    @property
    def enabled(self) -> bool:
        """True while events go to a file."""
        return self._file is not None

    @property
    def error(self) -> Exception | None:
        """The first error of the file, if one happened."""
        return self._error

    def event(self, kind: str, **fields: Any) -> None:
        """Add one event: ts (UTC, ISO 8601, milliseconds), seq (1, 2, 3 ...), kind, then the fields.

        A field with a forbidden or reserved name raises ValueError, also on a recorder that
        writes nothing: the call is wrong wherever it runs. Nothing else raises.
        """
        for name in fields:
            if name in RESERVED_FIELDS:
                raise ValueError(f"the triage log sets the field {name!r} itself")
        _check_names(fields)
        cleaned = {name: _clean(value) for name, value in fields.items()}  # raises for a nested name
        if self._file is None:
            return
        self._seq += 1
        try:
            stamp = self._now().astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            line = json.dumps({"ts": stamp, "seq": self._seq, "kind": str(kind)[:64], **cleaned})
            self._file.write(line + "\n")
            self._file.flush()
        except Exception as error:
            if self._error is None:
                self._error = error

    def close(self) -> Exception | None:
        """Close the file. Returns the first error of the file, or None. Never raises."""
        if self._file is not None:
            try:
                self._file.close()
            except Exception as error:
                if self._error is None:
                    self._error = error
            self._file = None
        return self._error


# ------------------------------------------------------------------ names and places of log files
def default_log_dir(
    out_dir: str | os.PathLike[str] | None = None, environ: Mapping[str, str] | None = None
) -> Path:
    """The directory of the log: AZSQLCD_LOG_DIR when set; else <out_dir>/logs; else .azsqlcd/logs
    under the current directory."""
    given = (os.environ if environ is None else environ).get(LOG_DIR_VARIABLE)
    if given:
        return Path(given)
    if out_dir is not None:
        return Path(out_dir, "logs")
    return DEFAULT_LOG_DIR


def log_file_name(command: str, now: datetime) -> str:
    """azsqlcd-<UTC yyyymmddThhmmssZ>-<command>.jsonl. A time with no zone is taken as UTC."""
    utc = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    safe = re.sub(r"[^a-z0-9-]", "-", command.lower())[:40] or "command"
    return f"azsqlcd-{utc.strftime('%Y%m%dT%H%M%SZ')}-{safe}.jsonl"


def new_log_path(log_dir: str | os.PathLike[str], command: str, now: datetime) -> Path:
    """The path of the log of a new call: log_file_name in log_dir. When a file of that name is
    there already (two calls of one command in one second), the name gets -2, -3, ... before
    .jsonl, so one file holds one call."""
    first = Path(log_dir, log_file_name(command, now))
    path, number = first, 1
    try:
        while path.exists() and number < 1000:
            number += 1
            path = first.with_name(f"{first.stem}-{number}{first.suffix}")
    except OSError:
        return first
    return path


def latest_log(log_dir: str | os.PathLike[str]) -> Path | None:
    """The log in log_dir that was written last, or None. The logs of the commands that read logs
    (show-log, support-bundle) do not count."""
    found: list[tuple[int, str, Path]] = []
    try:
        for path in Path(log_dir).glob("azsqlcd-*.jsonl"):
            if _READER_LOG.search(path.name):
                continue
            if path.is_file() and not path.is_symlink():
                found.append((path.stat().st_mtime_ns, path.name, path))
    except OSError:
        return None
    return max(found)[2] if found else None


# ------------------------------------------------------------------ the first event of a call
# Options with no value. An option that is not in one of the two lists is taken to have a value.
_FLAGS = frozenset(
    {
        "--show-error-text",
        "--inline-plan",
        "--report-only",
        "--force-no-readback",
        "--resum",
        "--latest",
        "--no-log",
        "--version",
        "--help",
        "-h",
    }
)
# Options whose value is written: names of the configuration, commits, and paths on the runner.
SAFE_OPTIONS = frozenset(
    {
        "--env",
        "--target",
        "--base",
        "--name",
        "--commit",
        "--out",
        "--bundle",
        "--root",
        "--digest",
        "--ci",
        "--log",
        "--log-dir",
        "--expect-plan-file",
    }
)
_OPTION = re.compile(r"--[a-z][a-z0-9-]{0,40}")
HIDDEN = "<set>"
HIDDEN_ARGUMENT = "<arg>"


def safe_argv(argv: Sequence[str]) -> list[str]:
    """argv for the log. An option name stays. A value stays only for SAFE_OPTIONS; any other
    value is "<set>". The first word that is not an option is the command and stays; any other
    such word is "<arg>". A reason, a login, a database name or a URL never reaches the log."""
    out: list[str] = []
    command_seen = False
    at = 0
    while at < len(argv):
        word = str(argv[at])
        at += 1
        name, equals, value = word.partition("=")
        if word in _FLAGS:
            out.append(word)
        elif _OPTION.fullmatch(name):
            takes_next = not equals and at < len(argv) and not _is_option(str(argv[at]))
            if not equals and not takes_next:
                out.append(name)
                continue
            if takes_next:
                value = str(argv[at])
                at += 1
            shown = value[:200] if name in SAFE_OPTIONS else HIDDEN
            out.append(f"{name}={shown}" if equals else name)
            if not equals:
                out.append(shown)
        elif not word.startswith("-") and not command_seen and re.fullmatch(r"[a-z][a-z-]{0,30}", word):
            command_seen = True
            out.append(word)
        else:
            out.append(HIDDEN_ARGUMENT)
    return out


def _is_option(word: str) -> bool:
    return word in _FLAGS or _OPTION.fullmatch(word.partition("=")[0]) is not None


def package_versions() -> dict[str, str]:
    """The installed version of each package of the `db` extra, or "not installed"."""
    versions: dict[str, str] = {}
    for name in PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except Exception:  # PackageNotFoundError, and a broken metadata directory
            versions[name] = NOT_INSTALLED
    return versions


def _platform() -> dict[str, str]:
    return {"system": platform.system(), "release": platform.release(), "machine": platform.machine()}


def _config(summary: Mapping[str, Any]) -> dict[str, Any]:
    return {key: summary[key] for key in CONFIG_KEYS if key in summary}


def header(
    trace: Trace,
    *,
    command: str,
    argv: Sequence[str],
    tool_version: str,
    tool_digest: str,
    config_summary: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """The event "run": the command, argv (see safe_argv), the versions, the platform, the facts
    of the CI run when present, and the target when it is known already."""
    env = os.environ if environ is None else environ
    fields: dict[str, Any] = {
        "command": command,
        "argv": safe_argv(argv),
        "tool_version": tool_version,
        "tool_digest": tool_digest,
        "python": platform.python_version(),
        "platform": _platform(),
        "packages": package_versions(),
        "ci": {name: env[name] for name in CI_VARIABLES if env.get(name)},
    }
    if config_summary is not None:
        fields["config"] = _config(config_summary)
    trace.event("run", **fields)


def config_event(trace: Trace, **summary: Any) -> None:
    """The event "config": project, environment, target (its id), server, database, table_model.
    None of them is a secret. Written when the target of the call is known; the same facts twice
    are written once."""
    config = _config(summary)
    if trace.config != config:
        trace.config = config
        trace.event("config", **config)


# ------------------------------------------------------------------ the statement class of a batch
_TAG = re.compile(r"/\*\s*azsqlcd:([A-Za-z0-9_.-]{1,64})\s*\*/")
UNREADABLE = "unreadable"
EMPTY = "empty"
OTHER = "other"
MAX_LATER_STATEMENTS = 10

_DDL_VERBS = frozenset({"CREATE", "ALTER", "DROP", "ENABLE", "DISABLE", "TRUNCATE"})
# The last word of an object kind: the name follows it.
_KIND_END = frozenset(
    {
        "TABLE", "VIEW", "PROCEDURE", "PROC", "FUNCTION", "TRIGGER", "SCHEMA", "TYPE", "SEQUENCE",
        "SYNONYM", "STATISTICS", "USER", "ROLE", "INDEX", "DEFAULT", "RULE", "AGGREGATE", "COLLECTION",
        "SCHEME", "POLICY", "SOURCE", "FORMAT", "CREDENTIAL", "CONFIGURATION", "KEY", "CERTIFICATE",
        "CATALOG", "DATABASE", "ASSEMBLY", "LOGIN", "QUEUE", "SERVICE", "CONTRACT", "ROUTE",
    }
)  # fmt: skip
_KIND_MORE = frozenset(
    {
        "UNIQUE", "CLUSTERED", "NONCLUSTERED", "COLUMNSTORE", "PRIMARY", "XML", "SPATIAL", "FULLTEXT",
        "PARTITION", "SECURITY", "EXTERNAL", "DATA", "FILE", "MATERIALIZED", "SCOPED", "MASTER", "COLUMN",
        "ENCRYPTION", "SYMMETRIC", "ASYMMETRIC", "APPLICATION", "MESSAGE", "SELECTIVE",
    }
)  # fmt: skip
_MODULE_KINDS = frozenset({"PROCEDURE", "FUNCTION", "TRIGGER", "VIEW"})
_HAS_ON = frozenset({"INDEX", "TRIGGER", "STATISTICS"})
_SET_WORDS = frozenset(
    {
        "ANSI_NULLS", "ANSI_PADDING", "ANSI_WARNINGS", "ANSI_DEFAULTS", "ANSI_NULL_DFLT_ON",
        "ANSI_NULL_DFLT_OFF", "ARITHABORT", "ARITHIGNORE", "CONCAT_NULL_YIELDS_NULL", "QUOTED_IDENTIFIER",
        "NUMERIC_ROUNDABORT", "XACT_ABORT", "NOCOUNT", "LOCK_TIMEOUT", "LANGUAGE", "PARSEONLY", "NOEXEC",
        "FMTONLY", "DEADLOCK_PRIORITY", "TRANSACTION", "ISOLATION", "LEVEL", "READ", "COMMITTED",
        "UNCOMMITTED", "REPEATABLE", "SERIALIZABLE", "SNAPSHOT", "IDENTITY_INSERT", "DATEFORMAT",
        "DATEFIRST", "TEXTSIZE", "ROWCOUNT", "IMPLICIT_TRANSACTIONS", "CURSOR_CLOSE_ON_COMMIT",
        "CONTEXT_INFO", "STATISTICS", "IO", "TIME", "XML", "PROFILE", "SHOWPLAN_XML", "SHOWPLAN_ALL",
        "SHOWPLAN_TEXT", "ON", "OFF", "LOW", "NORMAL", "HIGH",
    }
)  # fmt: skip
_TRANSACTION_WORDS = frozenset({"TRAN", "TRANSACTION", "WORK"})
_DML = {"INSERT": "INTO", "DELETE": "FROM", "MERGE": "INTO", "UPDATE": "STATISTICS"}
# Statements that are recorded as their first word only.
_BARE = frozenset(
    {
        "SELECT", "WITH", "DECLARE", "WHILE", "PRINT", "THROW", "RAISERROR", "WAITFOR", "GRANT", "REVOKE",
        "DENY", "USE", "KILL", "RETURN", "GOTO", "CHECKPOINT", "DBCC", "BULK", "OPEN", "CLOSE", "FETCH",
        "DEALLOCATE", "ELSE", "BREAK", "CONTINUE", "BACKUP", "RESTORE", "ADD", "REVERT", "RECONFIGURE",
        "SHUTDOWN", "READTEXT", "WRITETEXT", "UPDATETEXT", "SETUSER", "GO", "RECEIVE", "SEND", "MOVE",
    }
)  # fmt: skip
# The statement that an IF guards starts at the first of these words outside parentheses.
_AFTER_IF = frozenset(
    {
        "ROLLBACK", "COMMIT", "BEGIN", "THROW", "RAISERROR", "EXEC", "EXECUTE", "INSERT", "UPDATE", "DELETE",
        "MERGE", "SET", "DROP", "CREATE", "ALTER", "TRUNCATE", "SELECT", "RETURN", "PRINT", "DECLARE",
    }
)  # fmt: skip
# An unquoted word at the place of a name that is a keyword of the statement, not a name.
_NOT_A_NAME = frozenset(
    {
        "SET", "CLEAR", "FOR", "ON", "AS", "WITH", "ADD", "FROM", "TOP", "ENCRYPTION", "AUTHORIZATION", "IF",
        "BY", "TO", "INTO", "VALUES", "SELECT", "DEFAULT", "OUTPUT", "USING", "WHERE", "EXEC", "EXECUTE",
    }
)  # fmt: skip
_IF_EXISTS = frozenset({"IF", "NOT", "EXISTS"})
# Every word that a head can hold outside a bracketed name. A test compares each head with it.
HEAD_VOCABULARY = frozenset(
    {
        *_DDL_VERBS, *_KIND_END, *_KIND_MORE, *_SET_WORDS, *_DML, *_DML.values(), *_BARE, *_AFTER_IF,
        *_IF_EXISTS, "OR", "ALTER", "ON", "EXEC", "BEGIN", "END", "TRY", "CATCH", "DISTRIBUTED", "SAVE",
        "IF", "TRANSACTION", "...",
    }
)  # fmt: skip


def batch_tag(batch: str) -> str | None:
    """The name inside a leading /* azsqlcd:<name> */ comment: the tool tags its own queries."""
    found = _TAG.match(batch.lstrip(" \t\r\n﻿"))
    return found.group(1) if found else None


def batch_outline(batch: str) -> tuple[str, list[str]]:
    """(head, later): the statement class of the first statement of a batch, and of each later
    statement that starts after a `;` outside parentheses (at most MAX_LATER_STATEMENTS).

    Each is built only from keywords of closed lists and from object names in brackets:
    "CREATE OR ALTER PROCEDURE [sales].[usp_x]", "ALTER TABLE [sales].[Order]",
    "CREATE INDEX [IX_a] ON [sales].[Order]", "SET XACT_ABORT ON", "SELECT",
    "EXEC [sys].[sp_rename]", "BEGIN TRANSACTION". Never a string, a number, a variable or
    anything after the name. A module definition has no later statements: its body is not read.
    A batch that the lexer refuses is "unreadable".
    """
    try:
        toks = lex.significant(lex.tokens(batch))
        statements: list[list[lex.Tok]] = [[]]
        depth = 0
        for tok in toks:
            if tok.kind == "op" and tok.text == "(":
                depth += 1
            elif tok.kind == "op" and tok.text == ")":
                depth = max(0, depth - 1)
            if tok.kind == "op" and tok.text == ";" and depth == 0:
                statements.append([])
            else:
                statements[-1].append(tok)
        statements = [statement for statement in statements if statement]
        if not statements:
            return EMPTY, []
        head = _head(statements[0])
        if _is_module(statements[0]):
            return head, []
        later = [_head(statement) for statement in statements[1 : MAX_LATER_STATEMENTS + 1]]
        more = len(statements) - 1 - len(later)
        return head, [*later, *([f"(+{more} more)"] if more > 0 else [])]
    except Exception:  # LexError, and anything else: a log must not stop a batch
        return UNREADABLE, []


def batch_head(batch: str) -> str:
    """The statement class of the first statement of a batch (see batch_outline)."""
    return batch_outline(batch)[0]


def _word(toks: Sequence[lex.Tok], at: int) -> str:
    return toks[at].text.upper() if at < len(toks) and toks[at].kind == "word" else ""


def _name(toks: Sequence[lex.Tok], at: int) -> tuple[str, int]:
    """A one- to four-part name at toks[at], in brackets, and the index after it; ("", at) if none."""
    parts: list[str] = []
    here = at
    while here < len(toks) and len(parts) < 4:
        tok = toks[here]
        if tok.kind not in ("word", "bident", "qident") or not tok.value:
            break
        if tok.kind == "word" and not parts and tok.text.upper() in _NOT_A_NAME:
            break
        parts.append("[" + tok.value[:128].replace("]", "]]") + "]")
        here += 1
        if here < len(toks) and toks[here].kind == "op" and toks[here].text == ".":
            here += 1
            continue
        break
    return (".".join(parts), here) if parts else ("", at)


def _is_module(toks: Sequence[lex.Tok]) -> bool:
    at = 3 if (_word(toks, 0), _word(toks, 1), _word(toks, 2)) == ("CREATE", "OR", "ALTER") else 1
    kind = _word(toks, at)
    return (
        _word(toks, 0) in ("CREATE", "ALTER") and ("PROCEDURE" if kind == "PROC" else kind) in _MODULE_KINDS
    )


def _ddl(toks: Sequence[lex.Tok]) -> str:
    out = [_word(toks, 0)]
    at = 1
    if out[0] == "CREATE" and _word(toks, 1) == "OR" and _word(toks, 2) == "ALTER":
        out += ["OR", "ALTER"]
        at = 3
    ended = False
    while not ended and (_word(toks, at) in _KIND_END or _word(toks, at) in _KIND_MORE):
        word = _word(toks, at)
        ended = (
            word in _KIND_END
            and not (word == "SCHEMA" and _word(toks, at + 1) == "COLLECTION")
            and not (word == "DATABASE" and _word(toks, at + 1) == "SCOPED")
        )
        out.append("PROCEDURE" if word == "PROC" else word)
        at += 1
    if not ended:
        return " ".join(out[: 3 if len(out) > 2 and out[1] == "OR" else 1])
    while _word(toks, at) in _IF_EXISTS:
        out.append(_word(toks, at))
        at += 1
    name, at = _name(toks, at)
    if name:
        out.append(name)
    kind = out[:-1] if name else out
    if any(word in _HAS_ON for word in kind) and _word(toks, at) == "ON":
        on, _ = _name(toks, at + 1)
        if on:
            out += ["ON", on]
    return " ".join(out)


def _head(toks: Sequence[lex.Tok], nested: bool = False) -> str:
    first = toks[0]
    if first.kind in ("bident", "qident"):  # a procedure call with no EXEC
        return f"EXEC {_name(toks, 0)[0]}".strip()
    if first.kind != "word":
        return OTHER
    word = first.text.upper()
    if word in _DDL_VERBS:
        return _ddl(toks)
    if word == "SET":
        out = ["SET"]
        while len(out) < 7 and _word(toks, len(out)) in _SET_WORDS:
            out.append(_word(toks, len(out)))
        return " ".join(out)
    if word in ("EXEC", "EXECUTE"):
        at = 1
        if at + 1 < len(toks) and toks[at].kind == "var" and toks[at + 1].text == "=":
            at += 2  # EXEC @result = procedure
        return f"EXEC {_name(toks, at)[0]}".strip()
    if word in _DML:
        at = 1
        out = [word]
        if _word(toks, at) == _DML[word]:
            if word == "UPDATE":
                out.append("STATISTICS")
            at += 1
        name, _ = _name(toks, at)
        return " ".join([*out, *([name] if name else [])])
    if word in ("COMMIT", "ROLLBACK", "SAVE"):
        return f"{word} TRANSACTION"
    if word == "BEGIN":
        following = _word(toks, 1)
        if following in _TRANSACTION_WORDS:
            return "BEGIN TRANSACTION"
        return f"BEGIN {following}" if following in ("TRY", "CATCH", "DISTRIBUTED") else "BEGIN"
    if word == "END":
        return f"END {_word(toks, 1)}" if _word(toks, 1) in ("TRY", "CATCH") else "END"
    if word == "IF":
        depth = 0
        for at in range(1, len(toks)):
            tok = toks[at]
            if tok.kind == "op" and tok.text in "()":
                depth += 1 if tok.text == "(" else -1
            elif depth <= 0 and not nested and _word(toks, at) in _AFTER_IF:
                return f"IF ... {_head(toks[at:], nested=True)}"
        return "IF"
    if word in _BARE:
        return word
    return f"EXEC {_name(toks, 0)[0]}".strip()  # a procedure call with no EXEC


# ------------------------------------------------------------------ sessions
def _sql_error_fields(error: SqlError) -> dict[str, Any]:
    """The facts of an engine error. message is the redacted text; raw_message is never read."""
    return {
        "number": error.number,
        "sqlstate": error.sqlstate,
        "class": error.cls.name,
        "message": error.message,
    }


class TracingSession:
    """A Session that records one event for each batch, and sends and returns what the inner
    session sends and returns. Any other attribute is the attribute of the inner session."""

    def __init__(
        self,
        inner: Any,
        trace: Trace,
        name: str,
        *,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._inner = inner
        self._trace = trace
        self._name = name
        self._monotonic = monotonic
        self._n = 0
        self._ms = 0.0
        self._told_closed = False

    @property
    def closed(self) -> bool:
        return self._inner.closed

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def execute(self, batch: str) -> Any:
        if not self._trace.enabled:
            return self._inner.execute(batch)
        self._n += 1
        fields = self._describe(batch)
        started = self._monotonic()
        try:
            result = self._inner.execute(batch)
        except BaseException as error:
            fields["ms"] = self._since(started)
            if isinstance(error, SqlError):
                fields["error"] = _sql_error_fields(error)
            else:
                fields["error"] = {"type": type(error).__name__}
            self._record("batch", fields)
            raise
        fields["ms"] = self._since(started)
        try:
            fields["result_sets"] = [len(rows) for rows in result]
        except Exception:
            fields["result_sets"] = None
        self._record("batch", fields)
        return result

    def close(self) -> None:
        try:
            self._inner.close()
        finally:
            if self._trace.enabled and not self._told_closed:
                self._told_closed = True
                self._record(
                    "session_closed", {"session": self._name, "batches": self._n, "ms": round(self._ms, 1)}
                )

    def _describe(self, batch: str) -> dict[str, Any]:
        fields: dict[str, Any] = {"session": self._name, "n": self._n}
        try:
            head, later = batch_outline(batch)
            fields |= {"tag": batch_tag(batch), "head": head}
            if later:
                fields["then"] = later
            fields["sha256"] = hashlib.sha256(batch.encode("utf-8", "surrogatepass")).hexdigest()
            fields["chars"] = len(batch)
        except Exception:
            fields.setdefault("head", UNREADABLE)
        return fields

    def _since(self, started: float) -> float:
        try:
            ms = round((self._monotonic() - started) * 1000, 1)
        except Exception:
            return 0.0
        self._ms += ms
        return ms

    def _record(self, kind: str, fields: dict[str, Any]) -> None:
        try:
            self._trace.event(kind, **fields)
        except Exception:  # the batch has its result or its error: the log does not replace it
            pass


def open_traced(
    trace: Trace, opener: Callable[[], Any], *, monotonic: Callable[[], float] = time.perf_counter
) -> Any:
    """Open one session with opener and wrap it. The sessions of a call are named by the order
    in which they open: "main", "parse", then "session-3" and so on. The event "connect" holds
    the time of the connect and, when it fails, the error; the error is raised unchanged.
    With a recorder that writes nothing, this is opener()."""
    if not trace.enabled:
        return opener()
    trace.sessions_opened += 1
    count = trace.sessions_opened
    name = SESSION_NAMES[count - 1] if count <= len(SESSION_NAMES) else f"session-{count}"
    started = monotonic()
    try:
        session = opener()
    except BaseException as error:
        _safely(
            trace, "connect", session=name, ms=_ms(monotonic, started), ok=False, error=_error_fields(error)
        )
        raise
    _safely(trace, "connect", session=name, ms=_ms(monotonic, started), ok=True)
    return TracingSession(session, trace, name, monotonic=monotonic)


def _ms(monotonic: Callable[[], float], started: float) -> float:
    try:
        return round((monotonic() - started) * 1000, 1)
    except Exception:
        return 0.0


def _safely(trace: Trace, kind: str, **fields: Any) -> None:
    try:
        trace.event(kind, **fields)
    except Exception:
        pass


# ------------------------------------------------------------------ errors and the end of a call
def _stack(error: BaseException) -> list[list[Any]]:
    """[file, line, function] of each frame, the place of the raise last. A file of the package is
    relative to it; any other file is its name only. No local value and no source line."""
    frames: list[list[Any]] = []
    tb = error.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        try:
            file = Path(code.co_filename).resolve().relative_to(_PACKAGE_DIR).as_posix()
        except (ValueError, OSError):
            file = Path(code.co_filename).name
        frames.append([file, tb.tb_lineno, code.co_name])
        tb = tb.tb_next
    return frames[-30:]


def _error_fields(error: BaseException) -> dict[str, Any]:
    fields: dict[str, Any] = {"type": type(error).__name__}
    if isinstance(error, SqlError):
        fields |= _sql_error_fields(error)
    elif isinstance(error, ToolError):
        # detail is not written: it can hold a script (rename_constraints_sql). Its key names are.
        fields |= {
            "reason_code": error.reason_code,
            "exit_code": int(error.exit_code),
            "detail_keys": sorted(str(key) for key in error.detail),
        }
    return fields


def exception_event(trace: Trace, exc: BaseException) -> None:
    """The event "exception". A SqlError: its number, SQLSTATE, class and redacted message. A
    ToolError: its reason code and exit code. Any error: its type and the stack (see _stack), and
    the same facts for each error in its chain of causes. The message of an error that the tool
    does not know is never written. Never raises."""
    try:
        fields = _error_fields(exc)
        fields["stack"] = _stack(exc)
        causes: list[dict[str, Any]] = []
        seen = {id(exc)}
        cause = exc.__cause__ or exc.__context__
        while cause is not None and id(cause) not in seen and len(causes) < 5:
            seen.add(id(cause))
            causes.append(_error_fields(cause) | {"stack": _stack(cause)[-5:]})
            cause = cause.__cause__ or cause.__context__
        if causes:
            fields["causes"] = causes
        trace.event("exception", **fields)
    except Exception:
        pass


def end(trace: Trace, *, exit_code: int, reason_code: str, message: str) -> None:
    """The event "end": the exit code, the reason code and the message that the command printed.
    A log with no "end" event is of a process that was stopped. Never raises."""
    _safely(trace, "end", exit_code=int(exit_code), reason_code=reason_code, message=message)


# ------------------------------------------------------------------ reading a log
def read_events(log_path: str | os.PathLike[str]) -> tuple[list[dict[str, Any]], int]:
    """(events, lines that are not a JSON object). A last line that was cut counts as such a line."""
    events: list[dict[str, Any]] = []
    bad = 0
    with open(log_path, encoding="utf-8", errors="replace") as lines:
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(event, dict):
                events.append(event)
            else:
                bad += 1
    return events, bad


def _last_run(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """The events of the last call in the file, and the count of calls in it."""
    starts = [at for at, event in enumerate(events) if event.get("kind") == "run"]
    return (events[starts[-1] :] if starts else events), len(starts)


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _flat(value: Any) -> str:
    """One printable line of a value of a log: a log can come from anywhere."""
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())[:MAX_STRING]


def _span_s(first: Any, last: Any) -> str:
    try:
        seconds = (datetime.fromisoformat(str(last)) - datetime.fromisoformat(str(first))).total_seconds()
    except ValueError:
        return "unknown"
    return f"{seconds:.1f} s"


def summarize(log_path: str | os.PathLike[str]) -> str:
    """A short summary of one log for a terminal: the command, the target, the count of batches
    of each session, the last batch (where the run stopped), the error, and the exit and reason
    code. For a file that holds more than one call, the last call."""
    events, bad = read_events(log_path)
    if not events:
        return f"{Path(log_path).name}: no event can be read ({bad} line(s) are not JSON)"
    events, runs = _last_run(events)
    run = next((event for event in events if event.get("kind") == "run"), {})
    config = next((event for event in events if event.get("kind") == "config"), None) or run.get("config")
    batches = [event for event in events if event.get("kind") == "batch"]
    ended = next((event for event in reversed(events) if event.get("kind") == "end"), None)
    lines = [
        f"log: {Path(log_path).name}",
        f"command: azsqlcd {_flat(run.get('command', '?'))}  (tool {_flat(run.get('tool_version', '?'))}, "
        f"digest {_flat(run.get('tool_digest', '?'))[:12]}, python {_flat(run.get('python', '?'))})",
        f"argv: {_flat(' '.join(str(word) for word in _list(run.get('argv'))))}",
    ]
    if isinstance(config, dict):
        lines.append(
            f"target: {_flat(config.get('target', '?'))} ({_flat(config.get('environment', '?'))}), database "
            f"{_flat(config.get('database', '?'))} on {_flat(config.get('server', '?'))}, project "
            f"{_flat(config.get('project', '?'))}, table_model {_flat(config.get('table_model', '?'))}"
        )
    else:
        lines.append(
            "target: not known (the command names none, or it stopped before it read the configuration)"
        )
    ci = run.get("ci")
    if isinstance(ci, dict) and ci:
        lines.append("ci: " + ", ".join(f"{_flat(key)}={_flat(value)}" for key, value in ci.items()))
    first_ts, last_ts = events[0].get("ts", "?"), events[-1].get("ts", "?")
    lines.append(
        f"time: {_flat(first_ts)} to {_flat(last_ts)} ({_span_s(first_ts, last_ts)}), {len(events)} event(s)"
    )
    for event in events:
        if event.get("kind") == "connect" and event.get("ok") is False:
            lines.append(
                f"connect failed: session {_flat(event.get('session'))}: {_flat(event.get('error'))}"
            )
    counts: dict[str, list[float]] = {}
    for batch in batches:
        entry = counts.setdefault(_flat(batch.get("session", "?")), [0, 0.0])
        entry[0] += 1
        entry[1] += batch["ms"] if isinstance(batch.get("ms"), int | float) else 0.0
    told = ", ".join(f"{name} {int(n)} ({ms:.0f} ms)" for name, (n, ms) in counts.items())
    lines.append(f"batches: {told or 'none: no batch was sent'}")
    if batches:
        last = batches[-1]
        lines.append(
            f"last batch: {_flat(last.get('session', '?'))} #{_flat(last.get('n', '?'))}: "
            f"{_flat(last.get('head', '?'))} (tag {_flat(last.get('tag') or 'none')}, sha256 "
            f"{_flat(last.get('sha256', '?'))[:12]}, {_flat(last.get('chars', '?'))} chars, "
            f"{_flat(last.get('ms', '?'))} ms)"
        )
        if last.get("then"):
            later = "; ".join(str(head) for head in _list(last["then"]))
            lines.append(f"  later statements of that batch: {_flat(later)}")
    failed = [batch for batch in batches if batch.get("error")]
    for batch in failed[-3:]:
        error = batch["error"] if isinstance(batch["error"], dict) else {"type": batch["error"]}
        facts = " ".join(
            _flat(error[key]) for key in ("type", "class", "number", "sqlstate") if error.get(key) is not None
        )
        lines.append(
            f"error in batch {_flat(batch.get('session', '?'))} #{_flat(batch.get('n', '?'))} "
            f"({_flat(batch.get('head', '?'))}): [{facts}] {_flat(error.get('message', ''))}".rstrip()
        )
    if not failed:
        lines.append("error in a batch: none")
    for event in events:
        if event.get("kind") == "exception":
            facts = [_flat(event.get("type", "?"))]
            facts += [
                _flat(event[key]) for key in ("reason_code", "class", "number") if event.get(key) is not None
            ]
            stack = event.get("stack")
            where = _list(_list(stack)[-1]) if _list(stack) else []
            place = f" at {_flat(where[0])}:{_flat(where[1])} in {_flat(where[2])}" if len(where) == 3 else ""
            lines.append(f"exception: {' '.join(facts)}{place}")
    if ended is None:
        lines.append(
            "end: none. The process stopped before it wrote its exit code (killed, runner lost, or a crash)"
        )
    else:
        lines.append(
            f"end: exit {_flat(ended.get('exit_code', '?'))} {_flat(ended.get('reason_code', '?'))}: "
            f"{_flat(ended.get('message', ''))}".rstrip(": ")
        )
    if bad:
        lines.append(f"note: {bad} line(s) of the file are not JSON and were skipped")
    if runs > 1:
        lines.append(f"note: the file holds {runs} calls; this is the last one")
    return "\n".join(lines)


# ------------------------------------------------------------------ the bundle that is sent
class BundleRefused(ValueError):
    """A file that a support bundle does not take: not a regular file, or over 5 MB."""


def _bundle_problem(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return "is not a regular file"
    if path.stat().st_size > MAX_BUNDLE_FILE_BYTES:
        return f"is over {MAX_BUNDLE_FILE_BYTES // (1024 * 1024)} MB"
    return None


def _versions_text(log: Path, tool_digest: str | None, left_out: Sequence[str]) -> str:
    here = _platform()
    lines = [
        "azsqlcd support bundle",
        f"made_utc: {datetime.now(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')}",
        f"log: {log.name}",
        "",
        "the tool that made this bundle:",
        f"  tool_version: {__version__}",
        f"  tool_digest: {tool_digest or 'not given'}",
        f"  python: {platform.python_version()}",
        f"  platform: {here['system']} {here['release']} {here['machine']}",
        *(f"  {name}: {version}" for name, version in package_versions().items()),
        "",
        "the run that the log records:",
    ]
    try:
        events, _ = read_events(log)
        run = next((event for event in _last_run(events)[0] if event.get("kind") == "run"), None)
    except OSError:
        run = None
    if run is None:
        lines.append("  the log holds no run event")
    else:
        there, packages = run.get("platform"), run.get("packages")
        there = there if isinstance(there, dict) else {}
        packages = packages if isinstance(packages, dict) else {}
        system = " ".join(str(there.get(key, "?")) for key in ("system", "release", "machine"))
        lines += [
            f"  command: {_flat(run.get('command', '?'))}",
            f"  tool_version: {_flat(run.get('tool_version', '?'))}",
            f"  tool_digest: {_flat(run.get('tool_digest', '?'))}",
            f"  python: {_flat(run.get('python', '?'))}",
            f"  platform: {_flat(system)}",
            *(f"  {_flat(name)}: {_flat(version)}" for name, version in packages.items()),
        ]
    if left_out:
        lines += ["", "left out:", *(f"  {item}" for item in left_out)]
    return "\n".join(lines) + "\n"


def support_bundle(
    log_path: str | os.PathLike[str],
    out_zip: str | os.PathLike[str],
    extra_paths: Iterable[str | os.PathLike[str]] = (),
    *,
    tool_digest: str | None = None,
) -> list[str]:
    """Write a zip to send for triage. Returns the names in it.

    It holds: the log; plan.json, report.json and manifest.json when they lie in the directory of
    the log, or, for a log in a directory named `logs`, in the directory above it (the --out of
    the command); each file of extra_paths; and versions.txt. Nothing else: no bundle.tar, no .sql
    file, and azsqlcd.toml only when extra_paths names it.

    The log and each extra path must be a regular file of at most 5 MB, else BundleRefused and no
    zip is written. A report file that breaks that rule is left out and versions.txt says so.
    """
    log = Path(log_path)
    members: dict[str, Path] = {}
    for path in (log, *(Path(extra) for extra in extra_paths)):
        problem = _bundle_problem(path)
        if problem:
            raise BundleRefused(f"{path} {problem}; a support bundle does not take it")
        name = path.name
        number = 2
        while name in members or name == VERSIONS_FILE:
            name = f"{path.stem}-{number}{path.suffix}"
            number += 1
        members[name] = path
    places = [log.parent, *([log.parent.parent] if log.parent.name == "logs" else [])]
    left_out: list[str] = []
    for name in REPORT_FILES:
        found = next((place / name for place in places if (place / name).exists()), None)
        if found is None or name in members:
            continue
        problem = _bundle_problem(found)
        if problem:
            left_out.append(f"{name} ({problem})")
        else:
            members[name] = found
    versions = _versions_text(log, tool_digest, left_out)
    out = Path(out_zip)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, path in members.items():
            bundle.write(path, arcname=name)
        bundle.writestr(VERSIONS_FILE, versions)
    return [*members, VERSIONS_FILE]
