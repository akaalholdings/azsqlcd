"""Onboarding and drift: export, baseline, drift (design Part 2 (c) and (f)).

export reads every module and every table-class object of a database and gives the object files
for a pull request. baseline records an existing database: one object row for each module that
has a file, with a table model one for each table-class object of the model, and a baseline
step. drift compares the catalog with the recorded captures.

What the code holds to:
  - no function here sends CREATE, ALTER or DROP. export_modules sends module text only under
    SET PARSEONLY ON, after the canary proved that the engine parses and does not run;
  - baseline writes through runner.state_run (session options, lock, run row, one transaction
    with the commit fence and the guard), so A1, A4 and A5 have one implementation;
  - a report, a message and a drift item hold names, codes and hashes, never definition text
    (A25). Live text leaves only as a file: ExportResult.files, BaselineResult.live_files and
    export_drift. A text with a PASSWORD = or SECRET = literal is never one of those files;
  - a view with an index is never managed: ALTER VIEW drops every index of the view. export
    quarantines it, baseline records no row for it, drift reports an index on a managed view;
  - export_modules, drift, export_drift and a report-only baseline are read-only and let a
    SqlError pass to the caller. A write baseline raises runner.RunError and closes the session.

The table model is in tables.py. This module calls it: export_tables for the files,
compare_with_snapshot for the report of a baseline, and the hooks for the read-back of a
baseline and for the drift of table-class objects.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

from azsqlcd import catalog, catalog_tables, lint, modules, names, plan, runner, state, tables
from azsqlcd.config import Config, resolve_target
from azsqlcd.errors import ToolError, refused
from azsqlcd.model import fold
from azsqlcd.release import Bundle
from azsqlcd.runner import Report, StateChange, TableHooks
from azsqlcd.session import Session
from azsqlcd.state import State

# BaselineItem.status
EQUAL, DIFFERS, ONLY_HERE, MISSING_HERE = "equal", "differs", "only here", "missing here"
# DriftItem.cls
MANAGED, MISSING, UNMANAGED = "managed", "missing", "unmanaged"
# DriftItem.property of an object that is gone, or that has no managed row
EXISTS = "exists"
# DriftItem.property of a managed view that has an index now: a deploy of its file would drop it
HAS_INDEX = "has_index"
INDEXED_VIEW = (
    "an indexed view: ALTER VIEW drops every index of the view, so the tool does not manage it and "
    "a deploy of a file for it is refused"
)
# The environment whose catalog is the reference of a baseline report (Part 2 (c)): its export
# wrote the snapshot that the release holds.
REFERENCE_ENV = "prod"


# ------------------------------------------------------------------ public data
class Quarantined(NamedTuple):
    """An object that export leaves unmanaged.

    object_key is an object key, with one exception: a database DDL trigger has no schema, so no
    object key. Its entry is DATABASE_TRIGGER:[name], which azsqlcd.toml does not accept.
    """

    object_key: str
    # ENCRYPTED | UNSUPPORTED | SET_OPTIONS | SIGNED | TRIGGER_ORDER | HEADER | SECRET_LITERAL | LINT |
    # PARSEONLY
    code: str
    reason: str  # for the operator; names and redacted engine messages only


@dataclass(frozen=True)
class ExportResult:
    files: dict[str, bytes]  # 'schema/<kind dir>/<schema>.<name>.sql' -> file bytes
    report_md: str  # onboarding/<env>/export.md: every header edit, every unmanaged object, the toml list
    unmanaged: tuple[Quarantined, ...]  # by object key; a module that azsqlcd.toml lists is not in it


@dataclass(frozen=True)
class Export:
    """What the export command writes: the modules and the table-class objects of one database."""

    files: dict[str, bytes]  # 'schema/<kind dir>/<file name>.sql' -> file bytes
    report_md: str  # onboarding/<env>/export.md: both parts, and one list for azsqlcd.toml
    unmanaged: tuple[Quarantined, ...]  # by object key
    rename_constraints_sql: str  # onboarding/<env>/rename-constraints.sql; '' when nothing to rename
    snapshot: dict[str, Any]  # onboarding/<env>/snapshot.json: the table captures of this database
    # (key of a history table, key of its temporal table): owned by the engine, no file, not in
    # unmanaged and not in the list for azsqlcd.toml; report_md has one line for each
    history_tables: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class BaselineItem:
    object_key: str  # the key of the file; for `only here` the key that the catalog names give
    status: str  # equal | differs | only here | missing here
    source_sha256: str | None = None  # equal: the checksum of the file. Else None
    note: str = ""


@dataclass(frozen=True)
class BaselineResult:
    items: tuple[BaselineItem, ...]  # by object key
    # 'onboarding/<env>/modules/<file name>' -> the live text of a module that differs from its file
    live_files: dict[str, bytes]
    report_md: str  # onboarding/<env>/baseline-diff.md
    overwrite_modules: tuple[str, ...]  # keys that differ: the plan lists each as OVERWRITE_MODULE
    unacknowledged: tuple[str, ...]  # those of them that overwrite-ack.toml of the release does not list
    report: Report | None  # the run of a write baseline; None for report_only
    # report_only with a table model: this database against the snapshot of the reference database
    table_items: tuple[tables.BaselineItem, ...] = ()
    rename_constraints_sql: str = ""  # onboarding/<env>/rename-constraints.sql; '' when nothing to rename


class DriftItem(NamedTuple):
    object_key: str
    property: str  # a property of the capture; `exists` for a missing or an unmanaged object
    stored_hash: str | None  # missing: the hash of the recorded capture. unmanaged: None
    live_hash: str | None
    cls: str  # managed | missing | unmanaged


@dataclass(frozen=True)
class DriftResult:
    items: tuple[DriftItem, ...]  # by object key, then property

    @property
    def has_drift(self) -> bool:
        """A managed object differs from its capture or is gone. An unmanaged object is not drift."""
        return any(item.cls != UNMANAGED for item in self.items)


# ------------------------------------------------------------------ helpers
class _Unfit(Exception):
    """A live module that cannot become an object file: the quarantine code and the reason."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _module_parts(key: str) -> tuple[str, str, str]:
    """(kind, schema, name) of a module key. ValueError for any other text."""
    kind, schema, name = names.parse_object_key(key)
    if schema is None or kind not in names.MODULE_KINDS:
        raise ValueError(f"not a module key: {key!r}")
    return kind, schema, name


def _is_object_key(text: str) -> bool:
    try:
        names.parse_object_key(text)
    except ValueError:
        return False
    return True


def _bound_state(session: Session, config: Config, env: str) -> State:
    """The recorded state of a database that is bound to this project and this environment."""
    recorded = state.read_state(session)
    meta = recorded.meta
    if (meta.project, meta.environment) != (config.project.name, env):
        raise refused(
            "FENCE_META_MISMATCH",
            f"this database is bound to project {meta.project!r}, environment {meta.environment!r}; "
            f"the command is for {config.project.name!r}, {env!r}",
            project=meta.project,
            environment=meta.environment,
        )
    return recorded


def _utf8(text: str | None) -> bytes | None:
    """File bytes of a live text. None: no text, or a lone surrogate (nvarchar holds one, a file cannot)."""
    try:
        return None if text is None else text.encode("utf-8")
    except UnicodeEncodeError:
        return None


def _secret_literal(data: bytes) -> bool:
    """A25: PASSWORD = <literal> or SECRET = <literal>.

    The rule of lint for a module file, and the same words inside a string or a comment: dynamic
    SQL holds its statements as strings, and nobody reviewed the text of a live module.
    """
    return lint.secret_in_text(data.decode("utf-8")) or bool(lint.secret_findings("module.sql", data))


def _module_file(key: str, definition: str) -> tuple[str, bytes, list[str]]:
    """(path, file bytes, header edits) of a live definition under its key. Raises _Unfit.

    The result is a file that modules.read_module accepts, so the plan and lint read it too.
    """
    kind, schema, name = _module_parts(key)
    try:
        text, edits = modules.rewrite_for_export(definition, schema, name)
    except ToolError as error:
        line = error.detail.get("line")
        raise _Unfit("HEADER", f"the header of the definition cannot be read (line {line})") from None
    data = _utf8(text)
    if data is None:
        raise _Unfit("UNSUPPORTED", "the definition holds a character that a UTF-8 file cannot hold")
    if _secret_literal(data):
        raise _Unfit(
            "SECRET_LITERAL",
            "the definition holds a PASSWORD = or SECRET = literal; a secret is never written to a file",
        )
    try:
        path = names.path_for(kind, schema, name)
    except ValueError:
        raise _Unfit("UNSUPPORTED", "the name cannot be a file name") from None
    try:
        modules.read_module(path, data)
    except ToolError as error:
        # the message of the reader can quote the text (A25): only the line is told
        line = error.detail.get("line")
        raise _Unfit(
            "HEADER", f"the tool cannot read the definition as a module file (line {line})"
        ) from None
    return path, data, edits


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(_cell(value) for value in row) + " |" for row in rows]
    return "\n".join(lines) + "\n"


def _toml_list(name: str, values: Iterable[str]) -> str:
    """`name = [...]` with one basic string in each line."""

    def basic(text: str) -> str:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return '"' + "".join(f"\\u{ord(c):04X}" if ord(c) < 32 or ord(c) == 127 else c for c in escaped) + '"'

    return f"{name} = [\n" + "".join(f"  {basic(value)},\n" for value in values) + "]\n"


# ------------------------------------------------------------------ export
class _Facts(NamedTuple):
    signed: set[str]  # keys of signed modules
    numbered: set[str]  # keys of numbered procedures
    indexed: set[str]  # keys of views with an index
    outside: list[Quarantined]  # objects that are no module file at all


def _export_facts(session: Session) -> _Facts:
    signed: set[str] = set()
    numbered: set[str] = set()
    indexed: set[str] = set()
    outside: list[Quarantined] = []
    for found in catalog.export_facts(session):
        if found.fact == catalog.INDEXED_VIEW:
            indexed.add(names.object_key("VIEW", found.schema, found.name))
        elif found.fact == catalog.DATABASE_TRIGGER:
            outside.append(
                Quarantined(
                    f"DATABASE_TRIGGER:{names.quote(found.name)}",
                    "UNSUPPORTED",
                    "a database DDL trigger: it belongs to no schema and has no object file",
                )
            )
        elif found.fact == catalog.CLR:
            key = names.object_key(catalog.CLR_TYPES[found.type], found.schema, found.name)
            outside.append(Quarantined(key, "UNSUPPORTED", "a CLR module: it has no definition text"))
        elif found.type in catalog.MODULE_TYPES:  # a signature of a table or a key is not about a module
            key = names.object_key(catalog.MODULE_TYPES[found.type], found.schema, found.name)
            (signed if found.fact == catalog.SIGNED else numbered).add(key)
    return _Facts(signed, numbered, indexed, outside)


def _legacy_options(capture: dict[str, Any]) -> list[str]:
    """The SET options that the module was stored with OFF. A deploy stores a module with both ON."""
    options = (("ANSI_NULLS", "uses_ansi_nulls"), ("QUOTED_IDENTIFIER", "uses_quoted_identifier"))
    return [option for option, flag in options if not capture[flag]]


def _ordered_events(capture: dict[str, Any]) -> list[str]:
    """The events for which a trigger has a first or last order (sp_settriggerorder, A13)."""
    return sorted(
        event for event, order in capture.get("events", {}).items() if order["is_first"] or order["is_last"]
    )


def _refuse_properties(key: str, capture: dict[str, Any], facts: _Facts) -> None:
    """Raises _Unfit for a module that a file and CREATE OR ALTER cannot carry (design (c) 3, A13)."""
    if capture["definition"] is None:
        raise _Unfit("ENCRYPTED", "the definition is encrypted (WITH ENCRYPTION); the tool cannot read it")
    if key in facts.numbered:
        raise _Unfit("UNSUPPORTED", "a numbered procedure (;2 and up): one file holds one procedure")
    if key in facts.indexed:
        raise _Unfit("UNSUPPORTED", INDEXED_VIEW)
    off = _legacy_options(capture)
    if off:
        raise _Unfit(
            "SET_OPTIONS",
            f"stored with {' and '.join(off)} OFF; a deploy stores a module with both ON",
        )
    if key in facts.signed:
        raise _Unfit("SIGNED", "the module is signed (sys.crypt_properties); a deploy drops the signature")
    ordered = _ordered_events(capture)
    if ordered:
        raise _Unfit(
            "TRIGGER_ORDER",
            f"the trigger has a first or last order for {', '.join(ordered)} (sp_settriggerorder); "
            "a module file cannot hold it",
        )


def _parse_failures(session: Session, texts: dict[str, str]) -> dict[str, str]:
    """key -> reason, for each text that the source does not parse under SET PARSEONLY ON.

    The check is the one of the planner (plan.parse_check, canary first). A text with the word
    PARSEONLY is never sent: the setting is read when a batch is parsed, so such a batch could
    switch the check off and run.
    """
    failures = {
        key: "the text holds the word PARSEONLY; it cannot be checked without the risk that it runs"
        for key, text in texts.items()
        if plan.names_parseonly(text)
    }
    checked = {key: text for key, text in texts.items() if key not in failures}
    if not checked:
        return failures
    # the session of the caller is used again after the export, so the setting is given back
    errors = plan.parse_check(session, checked, restore_setting=True)
    return failures | {
        key: f"the text does not parse on the source: {error.message}" for key, error in errors.items()
    }


type _Exported = dict[str, tuple[str, bytes, list[str]]]  # module key -> path, file bytes, header edits


def _modules_report(
    title: str, exported: _Exported, unmanaged: Sequence[Quarantined], skipped: Sequence[str]
) -> list[str]:
    edits = {path: found for path, _, found in exported.values()}
    edit_rows = [(path, edit) for path in sorted(edits) for edit in edits[path]]
    return [
        f"# azsqlcd export: {title}\n",
        f"- Module files: {len(edits)}\n- Objects that stay unmanaged: {len(unmanaged)}\n"
        f"- Modules left out because `[unmanaged] objects` lists them: {len(skipped)}\n",
        "## Header edits\n",
        "The text of each file is the definition that the catalog holds, with these edits in its header "
        "and with LF line ends.\n",
        _table(("File", "Edit"), edit_rows) if edit_rows else "None.\n",
        "## Unmanaged objects\n",
        "The tool never creates, alters, drops or refreshes these objects. Change path: a raw batch.\n",
        _table(("Object", "Code", "Reason"), unmanaged) if unmanaged else "None.\n",
    ]


def _toml_section(config: Config, unmanaged: Iterable[Quarantined], kept: str) -> list[str]:
    """The one [unmanaged] list of the report: every entry of the current list, in its spelling,
    and what stays unmanaged now. An export leaves a listed object out, so its entry must stay."""
    keys = {key.casefold(): key for key in config.unmanaged_objects}
    for found in unmanaged:
        if _is_object_key(found.object_key):
            keys.setdefault(found.object_key.casefold(), found.object_key)
    return [
        "## List for azsqlcd.toml\n",
        f"{kept} A database DDL trigger has no object key and is not in the list.\n",
        "```toml\n[unmanaged]\n" + _toml_list("objects", sorted(keys.values())) + "```\n",
    ]


def _lint_failures(exported: _Exported) -> dict[str, str]:
    """key -> reason, for each module file that lint refuses with an error.

    The files of an export must pass `lint` and `build`: a file with a SET NOEXEC, with a comment
    that reads as a directive, or in a cycle of views and functions would stop every release. The
    files are linted as one tree, again after each round: a module that leaves can end a cycle. A
    reason holds finding codes and lines, and for a cycle the object keys of it, never text (A25).
    """
    failures: dict[str, str] = {}
    by_path = {path: key for key, (path, _, _) in exported.items()}
    for _ in range(len(exported)):
        files = {path: exported[key][1] for path, key in by_path.items() if key not in failures}
        found: dict[str, list[str]] = {}
        for finding in lint.lint_repo(files):
            if finding.severity == "error" and finding.path in files:
                what = finding.message if finding.code == "ORD004" else f"line {finding.line}"
                found.setdefault(by_path[finding.path], []).append(f"{finding.code} ({what})")
        if not found:
            break
        failures |= {key: f"lint refuses the file: {'; '.join(codes)}" for key, codes in found.items()}
    return failures


def _read_modules(session: Session, config: Config) -> tuple[_Exported, list[Quarantined], list[str]]:
    """(the modules that become object files, the modules and other objects that stay unmanaged,
    the keys of the modules that [unmanaged] objects of azsqlcd.toml lists).

    A listed module is left out, as tables.export_tables leaves a listed table out: no file, no
    check of its text, no quarantine entry. Its entry stays in the list of the report.
    """
    captures = catalog.capture_modules(session, None)
    facts = _export_facts(session)
    unmanaged = list(facts.outside)
    listed = {key.casefold() for key in config.unmanaged_objects}
    skipped = sorted(key for key in captures if key.casefold() in listed)
    exported: dict[str, tuple[str, bytes, list[str]]] = {}  # key -> path, file bytes, header edits
    owner: dict[str, str] = {}  # path without case -> key: a file system holds one of two such paths
    for key in sorted(set(captures) - set(skipped)):
        try:
            _refuse_properties(key, captures[key], facts)
            path, data, edits = _module_file(key, captures[key]["definition"])
            if path.casefold() in owner:
                raise _Unfit("UNSUPPORTED", f"its file name is the file name of {owner[path.casefold()]}")
        except _Unfit as unfit:
            unmanaged.append(Quarantined(key, unfit.code, unfit.reason))
            continue
        owner[path.casefold()] = key
        exported[key] = (path, data, edits)
    texts = {key: data.decode("utf-8") for key, (_, data, _) in exported.items()}
    # a text with the word PARSEONLY has its own code below, and lint would name the same word
    linted = {key: found for key, found in exported.items() if not plan.names_parseonly(texts[key])}
    for key, reason in _lint_failures(linted).items():
        del exported[key], texts[key]
        unmanaged.append(Quarantined(key, "LINT", reason))
    for key, reason in _parse_failures(session, texts).items():
        del exported[key]
        unmanaged.append(Quarantined(key, "PARSEONLY", reason))
    unmanaged.sort()
    return exported, unmanaged, skipped


def export_modules(session: Session, config: Config) -> ExportResult:
    """Every module of the catalog outside schema azsqlcd, as object files (design (c) 1 to 3).

    The text of a file is the catalog definition after modules.rewrite_for_export, with LF line
    ends. A module that a file cannot carry is not written; it is in `unmanaged` with a code:
    ENCRYPTED, UNSUPPORTED (numbered procedure, indexed view, CLR module, database DDL trigger, a
    name or a text that a file cannot hold), SET_OPTIONS, SIGNED, TRIGGER_ORDER (A13), HEADER,
    SECRET_LITERAL (A25), LINT (the file has an error finding of lint: its codes and lines are
    the reason), PARSEONLY. One code for each object: the first that applies, in this order.
    Writes nothing to disk and changes nothing in the database.

    A module that config.unmanaged_objects lists is left out: it gets no file and is not in
    `unmanaged`. The list for azsqlcd.toml in the report holds every entry of the current list and
    each object of `unmanaged` that has an object key.

    Raises ToolError REFUSED: NO_VIEW_DEFINITION (of catalog.capture_modules: the export stops),
    PARSEONLY_CANARY.
    """
    exported, unmanaged, skipped = _read_modules(session, config)
    title = f"modules of project {_cell(config.project.name)}"
    parts = _modules_report(title, exported, unmanaged, skipped)
    parts += _toml_section(
        config,
        unmanaged,
        "Every entry of the current list is kept; an export never removes one. The modules that stay "
        "unmanaged now are added.",
    )
    return ExportResult(
        files={path: data for path, data, _ in exported.values()},
        report_md="\n".join(parts),
        unmanaged=tuple(unmanaged),
    )


def export(session: Session, config: Config) -> Export:
    """The export command: export_modules and tables.export_tables as one result (design (c) 1 to 4).

    files: the module files and the table-class files. unmanaged: the objects of both parts that
    stay unmanaged. report_md: the module part, the table part, then the list for [unmanaged]
    objects of azsqlcd.toml. It is the only such list of the report and holds both parts and every
    entry of the current list; an object that the current list names is left out of the export,
    module or table. rename_constraints_sql and snapshot come from the
    table part; the snapshot of the reference environment is what a baseline report of another
    environment compares with. Read-only; writes nothing to disk.

    The tables are read first. The module check sets the session to PARSEONLY and gives the
    setting back with a batch that is itself only parsed if that does not work; a catalog read
    after it would then give no row, and the export would say that the database has no tables.

    Raises ToolError REFUSED: the codes of export_modules, those of the table reader
    (NO_VIEW_DEFINITION), and SECRET_LITERAL when a table-class file would hold a PASSWORD = or
    SECRET = literal (A25): such a text is never written, and a table cannot be left out without
    the objects that need it, so the export stops and names the object.
    """
    table = tables.export_tables(session, config)
    exported, quarantined, skipped = _read_modules(session, config)
    secrets = sorted(path for path, data in table.files.items() if _secret_literal(data))
    if secrets:
        raise refused(
            "SECRET_LITERAL",
            f"{secrets[0]} would hold a PASSWORD = or SECRET = literal ({len(secrets)} file(s) in all); a "
            "secret is never written to a file. Remove the literal from the object, or list the object "
            "under [unmanaged] objects in azsqlcd.toml, then export again. Nothing was written",
            paths=secrets,
        )
    unmanaged = sorted(
        [*quarantined, *(Quarantined(u.object_key, u.code, u.reason) for u in table.unmanaged)]
    )
    parts = _modules_report(f"project {_cell(config.project.name)}", exported, quarantined, skipped)
    parts.append(table.report_md)
    parts += _toml_section(
        config,
        unmanaged,
        "This is the whole list after this export: every entry of the current list (an export never "
        "removes one), and the modules and the table-class objects that stay unmanaged now.",
    )
    return Export(
        files={path: data for path, data, _ in exported.values()} | table.files,
        report_md="\n".join(parts),
        unmanaged=tuple(unmanaged),
        rename_constraints_sql=table.rename_constraints_sql,
        snapshot=table.snapshot,
        history_tables=table.history_tables,
    )


# ------------------------------------------------------------------ baseline
@dataclass(frozen=True)
class _Compared:
    items: tuple[BaselineItem, ...]
    live_files: dict[str, bytes]
    captures: dict[str, dict[str, Any]]  # key of the file -> live capture, for `equal` and `differs`


def _ack_path(env: str) -> str:
    return f"onboarding/{env}/overwrite-ack.toml"


def _acknowledged(bundle: Bundle, env: str) -> frozenset[str]:
    """The module keys of onboarding/<env>/overwrite-ack.toml of the release. No file: none."""
    path = _ack_path(env)
    data = bundle.files.get(path)
    if data is None:
        return frozenset()
    try:
        doc = tomllib.loads(data.decode("utf-8"))
        listed = doc.get("modules")
        if set(doc) != {"modules"} or not isinstance(listed, list):
            raise ValueError
        for key in listed:
            if not isinstance(key, str):
                raise ValueError
            _module_parts(key)
    except ValueError:  # also UnicodeDecodeError and TOMLDecodeError
        raise refused(
            "BASELINE_ACK_INVALID",
            f"{path} must hold exactly one key, modules: a list of module keys, for example "
            '["PROCEDURE:[sales].[usp_x]"]',
            path=path,
        ) from None
    return frozenset(listed)


def _live_text(capture: dict[str, Any], catalog_key: str | None) -> tuple[str | None, bool]:
    """(the live text, it has the header that export writes). A15: only such a text can equal a file."""
    definition = capture["definition"]
    if definition is None or catalog_key is None:
        return definition, False
    _, schema, name = _module_parts(catalog_key)
    try:
        return modules.rewrite_for_export(definition, schema, name)[0], True
    except ToolError:
        return definition, False


def _not_as_deployed(capture: dict[str, Any]) -> str:
    """What the live module has that a deploy of its file would not give it; '' when nothing."""
    found = [f"stored with {option} OFF (a deploy stores it ON)" for option in _legacy_options(capture)]
    if capture.get("is_disabled"):
        found.append("the trigger is disabled here")
    ordered = _ordered_events(capture)
    if ordered:
        found.append(f"the trigger has a first or last order for {', '.join(ordered)}")
    return "; ".join(found)


def _compare(session: Session, bundle: Bundle, env: str) -> _Compared:
    """Each module file of the release against the live module of its name; then the modules with no file.

    The engine resolves the name of a file (catalog.capture_modules with keys), so a module that
    a deploy of the file would overwrite is never `missing here`.
    """
    files = plan.module_files(bundle.files)
    asked = catalog.capture_modules(session, list(files))
    every = catalog.capture_modules(session, None)
    indexed = {key.casefold() for key in catalog.indexed_views(session)}
    without_case = {key.casefold(): key for key in every}
    items: list[BaselineItem] = []
    live_files: dict[str, bytes] = {}
    captures: dict[str, dict[str, Any]] = {}
    named: set[str] = set()  # the catalog keys that a file names
    for key, module in files.items():
        capture = asked.get(key)
        if capture is None or capture["kind"] != module.kind:
            taken = (
                ""
                if capture is None
                else f"the name is taken by a {capture['kind']}; the plan refuses the create (NAME_COLLISION)"
            )
            items.append(BaselineItem(key, MISSING_HERE, note=taken))
            continue
        catalog_key = key if key in every else without_case.get(key.casefold())
        if catalog_key is not None:
            named.add(catalog_key)
        if key.casefold() in indexed:
            # no capture, so no row: the view stays unmanaged in this target
            items.append(BaselineItem(key, ONLY_HERE, note=f"{INDEXED_VIEW}. Its file is not recorded"))
            continue
        captures[key] = capture
        text, as_exported = _live_text(capture, catalog_key)
        data = _utf8(text)
        if as_exported and data is not None and modules.checksum(data) == module.checksum:
            other = _not_as_deployed(capture)
            if other:
                # the text is the text of the file and the module is not the module of a deploy:
                # recorded with no checksum, so the overwrite is listed and must be acknowledged
                items.append(BaselineItem(key, DIFFERS, note=f"the text is the text of the file; {other}"))
            else:
                items.append(BaselineItem(key, EQUAL, module.checksum))
            continue
        if data is None:
            note = "the live text cannot be read: it is encrypted, or it is not valid Unicode"
        elif _secret_literal(data):
            note = "the live text holds a PASSWORD = or SECRET = literal and is not written to a file"
        else:
            path = f"onboarding/{env}/modules/{module.path.rpartition('/')[2]}"
            live_files[path] = data
            note = f"live text: {path}"
        items.append(BaselineItem(key, DIFFERS, note=note))
    items += [BaselineItem(key, ONLY_HERE) for key in every if key not in named]
    return _Compared(tuple(sorted(items, key=lambda item: item.object_key)), live_files, captures)


@dataclass(frozen=True)
class _Tables:
    """The table part of a baseline. No table model: every field has its default."""

    modelled: bool = False
    compared: tables.TableBaseline | None = None  # report_only, when the release holds the snapshot
    recorded: int | None = None  # write mode: the objects that were read back and recorded
    # report_only: the constraints of the model that the engine named here, and their rename script
    engine_named: tuple[str, ...] = ()
    rename_sql: str = ""


def _snapshot_path() -> str:
    return f"onboarding/{REFERENCE_ENV}/snapshot.json"


def _engine_named(session: Session, hooks: tables.Hooks) -> tuple[tuple[str, ...], str]:
    """(one line for each constraint of the model that the engine named here, the rename script)."""
    found = hooks.engine_named(session, list(hooks.model))
    lines = tuple(
        f"{item.table_key} {item.kind} {names.quote(item.live_name)} -> {names.quote(item.fixed_name)}"
        for item in found
    )
    return lines, tables.rename_script(found)


def _compare_tables(session: Session, bundle: Bundle, hooks: tables.Hooks | None) -> _Tables:
    """Report only: the table-class objects of this database against the reference snapshot."""
    if hooks is None:
        return _Tables()
    data = bundle.files.get(_snapshot_path())
    if data is None:
        # nothing to compare with; the names that a baseline that writes refuses need no snapshot
        engine_named, rename_sql = _engine_named(session, hooks)
        return _Tables(modelled=True, engine_named=engine_named, rename_sql=rename_sql)
    try:
        snapshot = json.loads(data)
    except ValueError:  # also UnicodeDecodeError
        snapshot = None
    if not isinstance(snapshot, dict):
        raise refused("SNAPSHOT_INVALID", f"{_snapshot_path()} of the release is not a JSON object")
    compared = tables.compare_with_snapshot(session, snapshot, list(hooks.model))
    engine_named, rename_sql = _engine_named(session, hooks)
    return _Tables(True, compared, engine_named=engine_named, rename_sql=rename_sql)


def _engine_named_report(found: _Tables) -> list[str]:
    if not found.engine_named:
        return []
    return [
        "### Constraints of the release with a name that the engine made\n",
        f"{len(found.engine_named)} constraint(s) of the table files have another name in this database. "
        "A baseline that writes refuses until they have the names of the files (CONSTRAINT_NAMES). "
        "rename-constraints.sql gives them those names; it holds EXEC sys.sp_rename only. A human "
        "reviews and runs it.\n",
        "".join(f"- `{line}`\n" for line in found.engine_named),
    ]


def _table_report(found: _Tables) -> list[str]:
    """Object keys, capture property names and counts; never expression text (A25)."""
    return _table_part(found) + (_engine_named_report(found) if found.modelled else [])


def _table_part(found: _Tables) -> list[str]:
    if not found.modelled:
        return ["Modules only. Table-class objects are not compared.\n"]
    if found.recorded is not None:
        return [
            "## Table-class objects\n",
            f"{found.recorded} object(s) of the table model were read from the catalog. Each one equals "
            "the model of the release and is recorded.\n",
        ]
    if found.compared is None:
        return [
            "## Table-class objects\n",
            f"Not compared: the release holds no {_snapshot_path()}. That file is the catalog of the "
            f"reference database; `azsqlcd export --env {REFERENCE_ENV}` writes it. Only the modules and "
            "the constraint names are checked here. A baseline that writes still reads every object of "
            "the table model back against the files (READBACK_MISMATCH).\n",
        ]
    items = found.compared.items
    meanings = (
        (tables.EQUAL, "equal to the reference database"),
        (tables.DIFFERS, "differs from the reference database: a baseline that writes refuses"),
        (tables.ONLY_HERE, "no file: unmanaged in this target; reported, never dropped"),
        (tables.MISSING_HERE, "not in this database: a baseline that writes refuses"),
    )
    parts = [
        "## Table-class objects\n",
        f"This database against {_snapshot_path()} of the release, catalog against catalog. A baseline "
        "that writes reads each object of the table model back against the model of the release and "
        "refuses when one differs or is missing. Fix: refresh this database from the reference "
        "database, or align it by hand; the tool writes no alignment DDL.\n",
        _table(
            ("Status", "Objects", "Meaning"),
            [
                (status, str(sum(item.state == status for item in items)), meaning)
                for status, meaning in meanings
            ],
        ),
        _table(
            ("Object", "Status", "Properties that differ", "Warnings"),
            [
                (item.object_key, item.state, ", ".join(item.properties), "; ".join(item.warnings))
                for item in items
            ],
        )
        if items
        else "None.\n",
    ]
    if found.compared.rename_constraints_sql:
        parts.append(
            "Constraints that the engine named here have the shape of constraints of the reference "
            "database. rename-constraints.sql gives them the reference names; it holds EXEC sys.sp_rename "
            "only. A human reviews and runs it.\n"
        )
    return parts


def _baseline_report(
    env: str,
    target_id: str,
    compared: _Compared,
    acknowledged: Collection[str],
    baselined: bool,
    report: Report | None,
    table: _Tables,
) -> str:
    items = compared.items
    differing = [item.object_key for item in items if item.status == DIFFERS]
    mode = (
        "Report only. Nothing was written."
        if report is None
        else f"Recorded as run {report.run_id} (started {report.started_utc} UTC)."
    )
    parts = [
        f"# azsqlcd baseline: {_cell(env)} / {_cell(target_id)}\n",
        mode + (" This database has a baseline step already." if baselined else "") + "\n",
        "## Module counts\n" if table.modelled else "Modules only. Table-class objects are not compared.\n",
        _table(
            ("Status", "Modules", "Meaning"),
            [
                (status, str(sum(item.status == status for item in items)), meaning)
                for status, meaning in (
                    (EQUAL, "the live text is the text of the file; recorded with the checksum of the file"),
                    (DIFFERS, "recorded with no checksum; the first deploy overwrites the live text"),
                    (ONLY_HERE, "no file: unmanaged in this target; reported, never dropped"),
                    (MISSING_HERE, "no live module: the first deploy creates it"),
                )
            ],
        ),
        "## Modules\n",
        _table(("Object", "Status", "Note"), [(item.object_key, item.status, item.note) for item in items])
        if items
        else "None.\n",
    ]
    if differing:
        parts += [
            "## Modules that the first deploy overwrites\n",
            "The first deploy replaces the live text of each module below with the text of its file "
            f"(OVERWRITE_MODULE). Compare the files under onboarding/{env}/modules/ with the files under "
            f"schema/. A baseline that writes needs every key in {_ack_path(env)} of the release:\n",
            "```toml\n" + _toml_list("modules", differing) + "```\n",
            _table(
                ("Object", "Acknowledged in this release"),
                [(key, "yes" if key in acknowledged else "no") for key in differing],
            ),
        ]
    if table.modelled:
        parts += _table_report(table)
    return "\n".join(parts)


def baseline(
    session: Session,
    bundle: Bundle,
    config: Config,
    env: str,
    target_id: str,
    *,
    report_only: bool,
    confirm_database: str,
    tool_version: str,
    tool_digest: str,
    table_hooks: tables.Hooks | None = None,
) -> BaselineResult:
    """Compare an existing database with the files of the release, and record it.

    Each module file against the live module of its name: `equal` when the live definition, after
    the header rewrite of export, has the normal form of the file (A15); else `differs`. A live
    module with no file is `only here` (unmanaged, never dropped). A file with no live module is
    `missing here` (the first deploy creates it). The live text of each module that differs is in
    live_files, for the review of the overwrite. A module with the text of its file is still
    `differs`, with a note and no live file, when it is stored with ANSI_NULLS or
    QUOTED_IDENTIFIER OFF, or is a trigger that is disabled or has a first or last order: a
    deploy of the file would change it. A view with an index is `only here` although it has a
    file: it gets no row (ALTER VIEW drops the indexes of a view).

    report_only: fence, state and catalog reads only. No lock, no write.
    Write mode: session options, fence, lock, then one transaction: an object row with the live
    capture for each `equal` (source = checksum of the file) and each `differs` (source NULL: the
    plan lists OVERWRITE_MODULE), and a baseline step. The run row has the command baseline and
    does not move the recorded release. No object DDL is sent. The session is closed at the end.

    table_hooks (table_model = true) adds the table-class objects of the model of the release.
    report_only: tables.compare_with_snapshot against onboarding/prod/snapshot.json of the
    release, the catalog of the reference database (Part 2 (c) 6); table_items holds the result
    and rename_constraints_sql the renames. A release with no such file gets a module report that
    says so and reads no table. The report lists the constraints of the model that the engine
    named here; where the snapshot gives no rename for them, rename_constraints_sql gives them
    the names of the files. Write mode: a constraint of the model that the engine named
    refuses the baseline (CONSTRAINT_NAMES; the detail holds the rename script). Then every
    object of the model is read back against the model (Part 2 (c) 7); one that differs or is
    missing refuses the baseline (READBACK_MISMATCH, exit 22: nothing was executed). Each object
    gets a row with its capture and no source checksum.

    A second baseline is refused (ALREADY_BASELINED), with one exception: a table model, on a
    database whose state holds no table-class row. That is the switch to table_model = true
    (Part 2 (c) 8); the run then records the table-class objects only and leaves every module row
    as it is.

    Raises ToolError REFUSED before any batch: ENV_NOT_CONFIGURED, TARGET_NOT_CONFIGURED,
    BASELINE_ACK_INVALID. From the database, in both modes: the fence codes, CONFIRM_MISMATCH,
    STATE_MISSING and the other codes of read_state, FENCE_META_MISMATCH, NO_VIEW_DEFINITION,
    MODULE_INVALID; report_only also SNAPSHOT_INVALID. Write mode raises runner.RunError (a
    ToolError with the Report) and adds ALREADY_BASELINED, BASELINE_ACK_REQUIRED (a module differs
    and onboarding/<env>/overwrite-ack.toml of the release does not list its key),
    CONSTRAINT_NAMES, READBACK_MISMATCH and the codes of the runner frame (LOCK_NOT_GRANTED, STEP_UNRESOLVED,
    SESSION_OPTIONS, and those of a unit of work).
    """
    _, target = resolve_target(config, env, target_id)
    acknowledged = _acknowledged(bundle, env)

    def result(compared: _Compared, baselined: bool, report: Report | None, table: _Tables) -> BaselineResult:
        differing = tuple(item.object_key for item in compared.items if item.status == DIFFERS)
        return BaselineResult(
            items=compared.items,
            live_files=compared.live_files,
            report_md=_baseline_report(env, target_id, compared, acknowledged, baselined, report, table),
            overwrite_modules=differing,
            unacknowledged=tuple(key for key in differing if key not in acknowledged),
            report=report,
            table_items=table.compared.items if table.compared else (),
            # the names of the reference database where the snapshot gives them, else those of the files
            rename_constraints_sql=(table.compared.rename_constraints_sql if table.compared else "")
            or table.rename_sql,
        )

    if report_only:
        facts = catalog.fence_facts(session)
        plan.check_fence(facts, target.database)
        if confirm_database != facts.db_name:
            raise refused(
                "CONFIRM_MISMATCH",
                f"--confirm-database names {confirm_database!r}; the session is in database "
                f"{facts.db_name!r}. Nothing was changed",
                db_name=facts.db_name,
                confirmed=confirm_database,
            )
        recorded = _bound_state(session, config, env)
        baselined = any(step.kind == "baseline" for step in recorded.steps)
        compared = _compare(session, bundle, env)
        return result(compared, baselined, None, _compare_tables(session, bundle, table_hooks))

    done: list[tuple[_Compared, bool, _Tables]] = []

    def prepare(session: Session, recorded: State) -> StateChange:
        """Under the lock: compare, and refuse what must not be recorded. Changes nothing."""
        baselined = any(step.kind == "baseline" for step in recorded.steps)
        table_rows = any(
            names.parse_object_key(key)[0] in names.TABLE_CLASS_KINDS for key in recorded.objects
        )
        # Part 2 (c) 8: the modules are recorded, and the table model came later
        tables_only = baselined and table_hooks is not None and not table_rows
        if baselined and not tables_only:
            raise refused(
                "ALREADY_BASELINED",
                "this database has a baseline step: it was recorded before. An object that changed "
                "since is drift (azsqlcd drift, azsqlcd resolve --accept-drift); a module that the tool "
                "does not manage is adopted with azsqlcd resolve --adopt-module",
            )
        compared = _Compared((), {}, {}) if tables_only else _compare(session, bundle, env)
        differing = [item.object_key for item in compared.items if item.status == DIFFERS]
        unacknowledged = [key for key in differing if key not in acknowledged]
        if unacknowledged:
            raise refused(
                "BASELINE_ACK_REQUIRED",
                f"{unacknowledged[0]} differs from its file, and the first deploy overwrites it "
                f"({len(unacknowledged)} module(s) in all). Read the report of azsqlcd baseline "
                f"--report-only, then list each key under modules in {_ack_path(env)} by pull request. "
                "Nothing was written",
                objects=unacknowledged,
                path=_ack_path(env),
            )
        # a constraint that the engine named reads back as the constraint of the model, and would be
        # recorded under the name of the file: a name that this database does not have
        engine_named, rename_sql = _engine_named(session, table_hooks) if table_hooks else ((), "")
        if engine_named:
            raise refused(
                "CONSTRAINT_NAMES",
                f"{len(engine_named)} constraint(s) of the table files have a name that the engine "
                f"made in this database; first: {engine_named[0]}. Run the rename script (EXEC "
                "sys.sp_rename only; it is in rename_constraints_sql of this refusal, and azsqlcd "
                "baseline --report-only writes it as rename-constraints.sql), then baseline again. "
                "Nothing was written",
                constraints=list(engine_named),
                rename_constraints_sql=rename_sql,
            )
        # the read-back raises READBACK_MISMATCH for an object that differs from the model or is
        # missing; in this frame nothing was executed, so it leaves as a refusal
        read = table_hooks.read_back(session, bundle, list(table_hooks.model), None) if table_hooks else {}
        table_captures = {key: capture for key, capture in read.items() if capture is not None}
        table = _Tables(table_hooks is not None, recorded=len(table_captures) if table_hooks else None)
        done.append((compared, tables_only, table))
        recorded_modules = len(compared.captures)
        counts = f"{recorded_modules} module(s), {len(differing)} to overwrite"
        if table_hooks is not None:
            counts = f"{len(table_captures)} table-class object(s)" + ("" if tables_only else f", {counts}")

        def writes(run_id: int) -> list[str]:
            objects = [
                state.upsert_object(
                    item.object_key,
                    run_id=run_id,
                    capture=compared.captures[item.object_key],
                    source_sha256=item.source_sha256,
                )
                for item in compared.items
                if item.object_key in compared.captures
            ]
            # a table-class object has no source checksum: the model is its source
            objects += [
                state.upsert_object(key, run_id=run_id, capture=capture, source_sha256=None)
                for key, capture in table_captures.items()
            ]
            return [*objects, state.insert_step(run_id=run_id, kind="baseline", status="ok", note=counts)]

        return StateChange(
            note=f"baseline: {counts}", step="baseline", summary=f"baseline of {counts}", writes=writes
        )

    # as a resolve run, a baseline does not move the recorded release: it deploys nothing
    report = runner.state_run(
        "baseline",
        bundle,
        config,
        env,
        target_id,
        prepare,
        confirm_database=confirm_database,
        tool_version=tool_version,
        tool_digest=tool_digest,
        session=session,
        reconcile=True,
    )
    compared, tables_only, table = done[0]
    return result(compared, tables_only, report, table)


# ------------------------------------------------------------------ drift
def drift(
    session: Session,
    bundle: Bundle,
    config: Config,
    env: str,
    target_id: str,
    table_hooks: TableHooks | None = None,
) -> DriftResult:
    """The catalog against the recorded captures (design (f)). Read-only.

    One item for each property of a managed object that differs (cls managed), one for a managed
    object that is gone (cls missing), one for a live object with no managed row (cls unmanaged;
    reported, never drift). A managed view that has an index now has the item `has_index` (cls
    managed): ALTER VIEW would drop the index, and the runner refuses to send it. Modules are
    compared here; table-class objects by table_hooks.table_drift, and without table_hooks not at
    all. An item holds a key, a property name and hashes, never text (A25). The bundle is not
    read: nothing of a module drift comes from the files.

    Raises ToolError REFUSED: ENV_NOT_CONFIGURED, TARGET_NOT_CONFIGURED, the fence codes,
    FENCE_META_MISMATCH, STATE_MISSING and the other codes of read_state, NO_VIEW_DEFINITION.
    """
    _, target = resolve_target(config, env, target_id)
    plan.check_fence(catalog.fence_facts(session), target.database)
    recorded = _bound_state(session, config, env)
    managed = {key: row for key, row in recorded.objects.items() if row.status == "managed"}
    module_keys = [key for key in managed if names.parse_object_key(key)[0] in names.MODULE_KINDS]
    live = catalog.capture_modules(session, module_keys)
    differing = {
        key: catalog.capture_differences(managed[key].capture, live[key])
        for key in module_keys
        if key in live
    }
    if table_hooks is not None:
        differing |= table_hooks.table_drift(session, recorded, sorted(set(managed) - set(module_keys)))
    items = [
        DriftItem(key, difference.property, difference.stored_sha256, difference.live_sha256, MANAGED)
        for key, differences in differing.items()
        for difference in differences
    ]
    items += [
        DriftItem(key, EXISTS, managed[key].catalog_sha256, None, MISSING)
        for key in module_keys
        if key not in live
    ]
    # a managed view that has an index now: the capture does not hold the index, the catalog does
    indexed = {key.casefold() for key in catalog.indexed_views(session)}
    items += [
        DriftItem(
            key,
            HAS_INDEX,
            state.capture_sha256({HAS_INDEX: False}),
            state.capture_sha256({HAS_INDEX: True}),
            MANAGED,
        )
        for key in module_keys
        if key in live and key.casefold() in indexed
    ]
    kinds = names.MODULE_KINDS + (() if table_hooks is None else ("TABLE", "SEQUENCE", "SYNONYM"))
    managed_without_case = {key.casefold() for key in managed}
    # the history table of a system-versioned table is owned by the engine: it has no managed row
    # and is not an unmanaged object. Without table hooks no table is listed
    history = {} if table_hooks is None else catalog_tables.history_tables(session)
    owned_by_engine = {fold(key) for key in history}
    for found in catalog.list_user_objects(session):
        if found.kind is not None and found.kind in kinds:
            key = names.object_key(found.kind, found.schema, found.name)
            if key.casefold() not in managed_without_case and fold(key) not in owned_by_engine:
                items.append(DriftItem(key, EXISTS, None, None, UNMANAGED))
    return DriftResult(tuple(sorted(items, key=lambda item: (item.object_key, item.property))))


def export_drift(session: Session, key: str) -> tuple[str, bytes]:
    """(path, file bytes): the live text of one module as an object file, for a pull request.

    The header gets CREATE OR ALTER and the name of the key, as export writes it. After the merge
    the operator runs azsqlcd resolve --accept-drift for the key. Read-only.

    Raises ToolError REFUSED: MODULE_NOT_EXPORTABLE (not a module key; no live module with this
    name and kind; encrypted; a text that cannot be a module file), SECRET_LITERAL (A25),
    NO_VIEW_DEFINITION.
    """
    try:
        kind = _module_parts(key)[0]
    except ValueError:
        raise refused(
            "MODULE_NOT_EXPORTABLE",
            "the live text can be exported for a module key only, for example PROCEDURE:[sales].[usp_x]",
        ) from None
    capture = catalog.capture_modules(session, [key]).get(key)
    definition = capture["definition"] if capture is not None and capture["kind"] == kind else None
    if definition is None:
        raise refused(
            "MODULE_NOT_EXPORTABLE",
            f"{key}: the database has no module with this name and kind, or its definition is encrypted",
            object=key,
        )
    try:
        path, data, _ = _module_file(key, definition)
    except _Unfit as unfit:
        code = unfit.code if unfit.code == "SECRET_LITERAL" else "MODULE_NOT_EXPORTABLE"
        raise refused(code, f"{key}: {unfit.reason}", object=key, code=unfit.code) from None
    return path, data
