"""`gen` and the model proof of `verify`: git access, diff, replay.

generate() writes a migration from the difference between two models: the table-class files at the
merge base and those of the working tree. It is not trusted. prove() replays the new migrations of
a pull request, generated or hand-written, on the base model and requires the head model
(design (b), proof steps 5 and 6). verify() is the whole pull-request check: lint, the rules of a
change, the model validation and the proof.

Everything here works on file maps (path -> bytes, the shape of release.read_tree). Only
generate(), resum(), verify() and the withdraw-and-replace proof read git, through release.git.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from azsqlcd import chain, config, diff, emit, lex, lint, modules, names, release, replay
from azsqlcd.errors import ToolError, refused
from azsqlcd.lint import ERROR, WARNING, Finding
from azsqlcd.model import (
    ABSENT_IN_OTHER,
    ABSENT_IN_SELF,
    AddColumn,
    AddConstraint,
    AlterColumn,
    Check,
    Column,
    Constraint,
    CreateSequence,
    CreateSynonym,
    CreateTable,
    CreateType,
    DefaultConstraint,
    DropColumn,
    DropTable,
    Expression,
    Model,
    ModelObject,
    Operation,
    Rename,
    Schema,
    Table,
    TableType,
    fold,
)
from azsqlcd.parse import ParseError, parse_object_file, parse_statement

# GenResult.reason when no migration is written
NO_CHANGE = "no change"
ONLY_MODULES = "only modules changed"

# Every finding code that this module gives, with its severity. A table-class file or a model batch
# that does not parse keeps the code of the parser (SYNTAX, UNSUPPORTED, NF001 to NF006, all
# errors). A finding made from a ToolError keeps the reason code of that error and is an error.
CODES: dict[str, str] = {
    "NF000": ERROR,  # A14: the file says something that the model does not hold
    "MDL001": ERROR,  # the head model does not fit together
    "MODEL_INVALID": ERROR,  # table-class files do not parse, so there is no model and no proof
    "PRF000": WARNING,  # no proof: table_model is false, or this pull request sets it to true
    "PRF001": ERROR,  # the new migrations do not give the head model
    "PRF002": ERROR,  # a table-class file changes and the pull request adds no migration
    "PRF003": ERROR,  # allow SET_NOT_NULL is missing; or an allow line of the proof matches nothing
    "PRF004": ERROR,  # the pull request that sets table_model = true adds a migration
    "PRF005": ERROR,  # the pull request sets table_model from true to false
    "PRF006": ERROR,  # a rename of the new migrations that plan could not follow
    "PRF007": ERROR,  # a statement that a schema-bound module blocks, with no unbind line for it
    "RAW001": ERROR,  # a raw batch for an object that [unmanaged] objects does not list
    "RAW002": ERROR,  # the text of a raw batch names a managed table-class object or module
    "WDR001": ERROR,  # withdraw-and-replace together with a change of a table-class file
    "WDR002": ERROR,  # the replacement does not give the model of the withdrawn migration
    "WDR003": ERROR,  # the replacement has no allow REPLACEMENT_EDGE <withdrawn id>
    "WDR004": ERROR,  # withdrawn with no replacement, and the table-class files still hold its model
    "WDR005": ERROR,  # a replacement of a migration that an earlier pull request withdrew
    "TMB003": ERROR,  # a new tombstone for a function that a table-class file still uses
}

# Allow codes of diff.classify that depend on the model before the statement, not on its text.
# SET_NOT_NULL is written by _set_not_null; LONG_LOCK is read by lint.py with the tables that the
# migration created.
_MODEL_ONLY_CODES = frozenset({"SET_NOT_NULL", "LONG_LOCK"})
# the roots that release.read_tree reads, next to release.CONFIG_PATH
_ROOTS = ("schema", "migrations", "onboarding")
_IDENT = ("word", "bident", "qident")
_NAME = re.compile(r"[A-Za-z0-9_]+")
_MIGRATION_FILE = re.compile(r"([0-9]{4,})__([A-Za-z0-9_]+)\.sql")
_DIFFERENCE_LINE = re.compile(r"line (\d+): ")
_COMMIT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")  # a full object name: no ref can stand in for it
# One line of migrations.sum, read without the rules between lines: after a merge of main the
# numbers of the file do not rise, and that is what resum() repairs.
_SUM_LINE = re.compile(
    r"(?P<file>\S+) sha256:[0-9a-f]{64} (?P<mode>tx|nontx)(?P<withdrawn> withdrawn)?"
    r"(?: replaces=(?P<replaces>\S+))?"
)
_ORD002_HINT = (
    "Split the migration: the function is deployed above the statement that names it, so every object "
    "that the function text names must exist before that statement. Create the object in an earlier "
    "migration, or hand-write the order."
)
_WDR004_HINT = (
    "Add the replacement in this pull request (a new line with replaces={file}), or change the "
    "table-class files to the model without the withdrawn migration"
)
_ERROR_PATHS = {
    "CHAIN_INVALID": chain.SUM_PATH,
    "CONFIG_INVALID": release.CONFIG_PATH,
    "TOMBSTONE_INVALID": chain.TOMBSTONES_PATH,
}


# ------------------------------------------------------------------ data
@dataclass(frozen=True)
class GenResult:
    """What generate() found. Nothing is on disk until write_result()."""

    reason: str  # '' when a migration is written; else NO_CHANGE or ONLY_MODULES
    file: str | None = None  # the migration id, 'NNNN__name.sql'
    text: str | None = None  # the text of migrations/<file>
    sum_text: str | None = None  # the new text of migrations/migrations.sum
    # What the author must read before the migration is kept: a DROP + ADD that can be a rename,
    # with the exact --rename value (gen infers no rename).
    hints: tuple[str, ...] = ()


@dataclass(frozen=True)
class Renumbered:
    old: str  # the migration id in the working tree
    new: str  # the migration id with its new number
    text: str  # the file text with the new id in its header


@dataclass(frozen=True)
class ResumResult:
    """What resum() found. Nothing is on disk until write_resum()."""

    renames: tuple[Renumbered, ...]
    sum_text: str  # the new text of migrations/migrations.sum
    # migration ids whose chain line is dropped: new against the base revision, and the file is gone
    dropped: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Step:
    """One model batch of a migration, parsed."""

    batch: chain.MigrationBatch
    line: int  # file line of the statement
    op: Operation


class _NoProof(Exception):
    """The model before a withdrawn migration cannot be found. The message says why."""


# ------------------------------------------------------------------ files
def read_working_tree(root: release.StrPath) -> dict[str, bytes]:
    """The files that release.read_tree reads at a revision, read from the working tree.

    azsqlcd.toml, schema/**, migrations/** and onboarding/**: path with forward slashes -> bytes.
    A symbolic link is refused (TREE_INVALID), as a release refuses it: azsqlcd.toml, one of the
    three folders, or any entry below them.
    """
    top = Path(root)
    files: dict[str, bytes] = {}
    for name in (release.CONFIG_PATH, *_ROOTS):
        # os.walk reads through a root that is a link, and is_file() through a link to a file; git
        # holds the link itself, so a release has no file there
        if (top / name).is_symlink():
            raise refused("TREE_INVALID", f"{name} is a symbolic link", path=name)
    if (top / release.CONFIG_PATH).is_file():
        files[release.CONFIG_PATH] = (top / release.CONFIG_PATH).read_bytes()
    for name in _ROOTS:
        for folder, folders, found in os.walk(top / name):
            for entry in sorted((*folders, *found)):
                path = Path(folder, entry)
                relative = path.relative_to(top).as_posix()
                if path.is_symlink():
                    raise refused("TREE_INVALID", f"{relative} is a symbolic link", path=relative)
                if entry in found:
                    files[relative] = path.read_bytes()
    return dict(sorted(files.items()))


def _kind(path: str) -> str | None:
    """The object kind of an object file path; None for any other path."""
    try:
        return names.key_for_path(path)[0]
    except ValueError:
        return None


def _text(path: str, data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise refused("FILE_INVALID", f"{path}: not valid UTF-8 at byte {e.start}", path=path) from None


def _chain(files: Mapping[str, bytes]) -> chain.Chain:
    data = files.get(chain.SUM_PATH)
    return chain.Chain() if data is None else chain.parse_sum(_text(chain.SUM_PATH, data))


def _config(files: Mapping[str, bytes]) -> config.Config:
    data = files.get(release.CONFIG_PATH)
    if data is None:
        raise refused("CONFIG_INVALID", f"{release.CONFIG_PATH} does not exist")
    return config.load_config(_text(release.CONFIG_PATH, data))


def _base_table_model(base_files: Mapping[str, bytes]) -> bool | None:
    """table_model at the base revision; None when that revision has no azsqlcd.toml.

    A base config that this version cannot read counts as true, the strict side, unless its text
    says table_model = false: so a newer tool never reads an old config as "no model".
    """
    data = base_files.get(release.CONFIG_PATH)
    if data is None:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    try:
        return config.load_config(text).project.table_model
    except ToolError:
        pass
    try:
        project = tomllib.loads(text).get("project")
    except tomllib.TOMLDecodeError:
        return True
    return not (isinstance(project, dict) and project.get("table_model") is False)


def _read_migration(files: Mapping[str, bytes], file: str) -> chain.Migration:
    path = f"migrations/{file}"
    data = files.get(path)
    if data is None:
        raise refused("MIGRATION_INVALID", f"{path} does not exist", file=file, line=1)
    return chain.parse_migration(_text(path, data), file)


def _module_files(files: Mapping[str, bytes], kinds: Collection[str]) -> list[modules.ModuleFile]:
    return [modules.read_module(path, files[path]) for path in sorted(files) if _kind(path) in kinds]


def _object_path(key: str) -> str:
    """The object file of a key; migrations.sum for a name that no file can have."""
    try:
        return names.path_for(*names.parse_object_key(key))
    except ValueError:
        return chain.SUM_PATH


def _error_finding(e: ToolError) -> Finding:
    """A refusal of a layer below as a finding, in the shape that lint.py gives the same error."""
    detail = e.detail
    path = detail.get("path") or (f"migrations/{detail['file']}" if "file" in detail else None)
    return Finding(
        e.reason_code, ERROR, path or _ERROR_PATHS.get(e.reason_code, "."), detail.get("line") or 1, e.message
    )


# ------------------------------------------------------------------ model of one revision
def load_model(files: Mapping[str, bytes]) -> Model:
    """The model of every table-class object file of one revision (schemas, types, sequences,
    tables, synonyms).

    Every file is read before anything is refused: ToolError MODEL_INVALID carries all errors in
    detail['errors'] (path, line, code, message); detail['path'] and detail['line'] are the first.
    """
    objects: dict[str, tuple[str, ModelObject]] = {}  # case-folded key -> (path, object)
    errors: list[dict[str, str | int]] = []
    for path in sorted(files):
        if _kind(path) not in names.TABLE_CLASS_KINDS:
            continue
        try:
            (obj,) = parse_object_file(files[path].decode("utf-8"), path)
        except UnicodeDecodeError as e:
            message = f"not valid UTF-8 at byte {e.start}"
            errors.append({"path": path, "line": 1, "code": "FILE_INVALID", "message": message})
            continue
        except ParseError as e:
            errors.append({"path": path, "line": e.line, "code": e.code, "message": e.message})
            continue
        first = objects.setdefault(fold(obj.key), (path, obj))[0]
        if first != path:  # two files whose names differ in letter case only
            message = f"{obj.key} is also defined in {first}"
            errors.append({"path": path, "line": 1, "code": "MDL001", "message": message})
    if errors:
        e0 = errors[0]
        raise refused(
            "MODEL_INVALID",
            f"{len(errors)} table-class file(s) cannot be read, so there is no model. "
            f"First: {e0['path']} line {e0['line']}: {e0['code']}: {e0['message']}",
            path=e0["path"],
            line=e0["line"],
            errors=errors,
        )
    return Model(obj for _, obj in objects.values())


def table_file_check(path: str, text: str) -> list[Finding]:
    """The hook of lint.lint_repo for one table-class file: the parser rules, then NF000 (A14).

    A file that does not parse is one finding with the code of the parser. A file that parses is
    compared, token by token, with the canonical text of what the parser kept: each difference is
    NF000.
    """
    try:
        differences = emit.token_roundtrip_differences(text, path)
    except ParseError as e:
        return [Finding(e.code, ERROR, path, e.line, e.message)]
    except ValueError as e:  # the parser kept a value that has no canonical spelling
        return [Finding("NF000", ERROR, path, 1, f"the file has no canonical form: {e}")]
    out: list[Finding] = []
    for difference in differences:
        m = _DIFFERENCE_LINE.match(difference)
        out.append(Finding("NF000", ERROR, path, int(m[1]) if m else 1, difference))
    return out


def _missing_schemas(model: Model) -> list[tuple[str, Finding]]:
    """(case-folded object key, MDL001) for each object in a schema that has no schema file; the
    message names the file to add (CU-04).

    The rule of replay: dbo needs no file. The other schemas of the engine have no file at all;
    the replay says so. One finding for each object, on its file.
    """
    out: list[tuple[str, Finding]] = []
    for obj in model.values():
        if isinstance(obj, Schema):
            continue
        wanted = [(obj.schema, "")]
        if isinstance(obj, Table) and obj.temporal is not None:
            history = names.qualified(obj.temporal.history_schema, obj.temporal.history_table)
            wanted.append((obj.temporal.history_schema, f" of its history table {history}"))
        for schema, of in wanted:
            if fold(schema) in replay.ENGINE_SCHEMAS or names.object_key("SCHEMA", None, schema) in model:
                continue
            try:
                file = names.path_for("SCHEMA", None, schema)
            except ValueError:
                file = "its schema file"
            message = (
                f"{obj.key}: the schema {names.quote(schema)}{of} has no schema file. Add {file} with the "
                f"text 'CREATE SCHEMA {names.quote(schema)};'"
            )
            out.append((fold(obj.key), Finding("MDL001", ERROR, _object_path(obj.key), 1, message)))
            break
    return out


def validate_model(model: Model) -> list[Finding]:
    """Ladder rung 3: the model of one revision fits together (MDL001).

    The statements that create the whole model on an empty database must pass the replay
    preconditions: the schema of each object has a file, names are free, the columns of keys,
    indexes and foreign keys exist, a foreign key has a target with a matching key and equal
    types. One finding for each object, on its file: what follows the first is often an echo.
    """
    try:
        ops = [parse_statement(statement) for statement in emit.emit_create_script(model)]
    except ValueError as e:  # also ParseError: the model holds a value that has no SQL spelling
        return [Finding("MDL001", ERROR, "schema", 1, f"the model cannot be written as SQL: {e}")]
    state = Model()
    # the schema with no file first: its finding names the file to add, and the replay error of the
    # same object ("the schema does not exist") is then an echo
    found: dict[str, Finding] = dict(_missing_schemas(model))
    for op in ops:
        try:
            state = replay.apply(state, op)
        except replay.ReplayError as e:
            message = f"{e.object_key}: {e.message}"
            found.setdefault(
                fold(e.object_key), Finding("MDL001", ERROR, _object_path(e.object_key), 1, message)
            )
    return list(found.values())


# ------------------------------------------------------------------ replay of migration files
def _new_entries(base: chain.Chain, head: chain.Chain) -> list[chain.ChainEntry]:
    """The chain lines that head adds, in effective order."""
    merged = {entry.file for entry in base.entries}
    return [entry for entry in chain.effective_order(head) if entry.file not in merged]


def _changes_model(entry: chain.ChainEntry) -> bool:
    """False for a replacement and for a line that is added as withdrawn: the object files do not
    move with them. A replacement is proven against the migration that it replaces."""
    return entry.replaces is None and not entry.withdrawn


def _steps(path: str, migration: chain.Migration) -> tuple[list[_Step], list[Finding]]:
    """The model batches of a migration as operations. Data and raw batches are not modelled."""
    steps: list[_Step] = []
    findings: list[Finding] = []
    for batch in migration.batches:
        if batch.kind != "model":
            continue
        try:
            op = parse_statement(batch.text)
        except ParseError as e:
            findings.append(Finding(e.code, ERROR, path, batch.first_line - 1 + e.line, e.message))
            continue
        first = lex.significant(lex.tokenize(batch.text))[0]
        steps.append(_Step(batch, batch.first_line - 1 + first.line, op))
    return steps, findings


def _same_name(a: str, b: str) -> bool:
    """Two object texts of an allow line name one object: dotted names are compared part by part
    without quoting and without case, as lint.py does; any other text must be equal."""

    def parts(text: str) -> list[str] | None:
        try:
            toks = lex.significant(lex.tokenize(text))
        except lex.LexError:
            return None
        is_name = len(toks) % 2 == 1 and all(
            t.kind in _IDENT if i % 2 == 0 else (t.kind == "op" and t.text == ".") for i, t in enumerate(toks)
        )
        return [fold(t.value) for t in toks[::2]] if is_name else None

    pa, pb = parts(a), parts(b)
    return a == b if pa is None or pb is None else pa == pb


def _set_not_null(op: Operation, state: Model) -> str | None:
    """The object of the allow SET_NOT_NULL line that the operation needs, or None.

    Only the model can decide this code: the statement does not say what the column was. state is
    the model that the operation is applied to (ADD NULL, data, then ALTER COLUMN NOT NULL).
    """
    if isinstance(op, AlterColumn) and "SET_NOT_NULL" in diff.classify(op, state):
        return f"{names.qualified(op.schema, op.table)}.{names.quote(op.column)}"
    return None


def _not_null_findings(step: _Step, state: Model, path: str) -> list[Finding]:
    """PRF003 for one model batch: allow SET_NOT_NULL is missing, or it is there for nothing.

    lint.py cannot match this code to a statement and never reports it; the proof does both ways.
    """
    wanted = _set_not_null(step.op, state)
    matched = False
    out: list[Finding] = []
    for d in step.batch.directives:
        if not isinstance(d, chain.Allow) or d.code != "SET_NOT_NULL":
            continue
        if wanted is not None and _same_name(d.object, wanted):
            matched = True
        else:
            why = "the statement of its batch does not change that column from NULL to NOT NULL"
            out.append(Finding("PRF003", ERROR, path, d.line, f"allow SET_NOT_NULL {d.object}: {why}"))
    if wanted is not None and not matched:
        message = (
            f"SET_NOT_NULL {wanted} has no allow line: the column allows NULL before this statement, so "
            "the statement fails when a row holds NULL. Write above the statement: "
            f"{lex.DIRECTIVE_PREFIX}allow SET_NOT_NULL {wanted} reason: <why no row holds NULL>"
        )
        out.append(Finding("PRF003", ERROR, path, step.line, message))
    return out


def _stray_allows(path: str, migration: chain.Migration, entry: chain.ChainEntry) -> list[Finding]:
    """PRF003 for the allow lines of the proof that no statement can match: SET_NOT_NULL outside a
    model batch, REPLACEMENT_EDGE in a migration that does not replace that id."""
    out: list[Finding] = []
    for batch in migration.batches:
        for d in batch.directives:
            if not isinstance(d, chain.Allow):
                continue
            if d.code == "SET_NOT_NULL" and batch.kind != "model":
                why = "only ALTER COLUMN in a model batch sets NOT NULL"
            elif d.code == "REPLACEMENT_EDGE" and not _replaces(entry, d):
                why = "the chain line of this migration has no replaces= with that migration id"
            else:
                continue
            out.append(Finding("PRF003", ERROR, path, d.line, f"allow {d.code} {d.object}: {why}"))
    return out


def _replaces(entry: chain.ChainEntry, allow: chain.Allow) -> bool:
    # the id has two spellings: the file name in the chain, the name without .sql in the header
    wanted = (entry.replaces or "").removesuffix(".sql")
    return entry.replaces is not None and allow.object.removesuffix(".sql") == wanted


def _managed_keys(head_files: Mapping[str, bytes], head: Model) -> set[str]:
    """Keys of what the repository manages and a statement can name: the table-class objects of
    the head model and the modules that have a file. A module file that cannot be read is left
    to lint."""
    keys = {obj.key for obj in head.values()}
    for path in head_files:
        if _kind(path) in names.MODULE_KINDS:
            try:
                keys.add(modules.read_module(path, head_files[path]).key)
            except ToolError:
                continue
    return keys


def _raw_findings(
    new: Sequence[tuple[chain.ChainEntry, chain.Migration]], unmanaged: Collection[str], managed: set[str]
) -> list[Finding]:
    """RAW001 and RAW002 for the raw batches of the new migrations.

    RAW001 reads the directive: the object that the batch is for is unmanaged. RAW002 reads the
    text: a raw batch is not replayed and not read back, so it names no managed object. The
    token scan of modules.scan_references: strings and comments do not count, a one-part name is
    an object of schema dbo.
    """
    out: list[Finding] = []
    for entry, migration in new:
        path = f"migrations/{entry.file}"
        for batch in migration.batches:
            if batch.kind != "raw":
                continue
            own = fold(batch.raw_object or "")
            if own not in unmanaged:
                message = (
                    f"a raw batch for {batch.raw_object}, which [unmanaged] objects of "
                    f"{release.CONFIG_PATH} does not list. A raw batch is for an unmanaged object only: "
                    "the tool does not model it, read it back or look for drift on it"
                )
                out.append(Finding("RAW001", ERROR, path, batch.first_line, message))
            try:
                named = sorted(key for key in _named(batch.text, managed) if fold(key) != own)
                first = lex.significant(lex.tokenize(batch.text))[0]
            except lex.LexError:
                continue  # lint reports text that does not lex
            if named:
                message = (
                    f"the raw batch for {batch.raw_object} names {', '.join(named[:3])}"
                    f"{' ...' if len(named) > 3 else ''}, which the repository manages. A raw batch is "
                    "not replayed and not read back, so what it does to that object has no proof. Change "
                    "a table-class object in a model batch and a module in its file; a raw batch names "
                    "unmanaged objects only (a name of one part is read as an object of schema dbo)"
                )
                out.append(Finding("RAW002", ERROR, path, batch.first_line - 1 + first.line, message))
    return out


def _replay(
    state: Model, steps: Sequence[_Step], path: str, code: str, *, check_allows: bool = False
) -> tuple[Model | None, list[Finding]]:
    """(the model after the steps, findings). The model is None when a statement is blocked; the
    finding for it has the given code. check_allows: the SET_NOT_NULL lines of a new migration."""
    findings: list[Finding] = []
    for step in steps:
        if check_allows:
            findings += _not_null_findings(step, state, path)
        try:
            state = replay.apply(state, step.op)
        except replay.ReplayError as e:
            message = f"the model does not accept this statement: {e.object_key}: {e.message}"
            return None, [*findings, Finding(code, ERROR, path, step.line, message)]
    return state, findings


def _new_migrations(
    base_files: Mapping[str, bytes], head_files: Mapping[str, bytes]
) -> list[tuple[chain.ChainEntry, chain.Migration]]:
    """The migrations that the head revision adds to the chain, in effective order."""
    new = _new_entries(_chain(base_files), _chain(head_files))
    return [(entry, _read_migration(head_files, entry.file)) for entry in new]


def _replay_new(
    new: Sequence[tuple[chain.ChainEntry, chain.Migration]], state: Model
) -> tuple[Model | None, list[Finding]]:
    """Replay the new migrations of a change, with the allow lines that only the model can check.

    (the model after them, findings). The model is None when a batch does not parse or a
    statement is blocked: then nothing can be said about the result. A replacement is read and
    not replayed here: it is proven against the migration that it replaces.
    """
    findings: list[Finding] = []
    done: Model | None = state
    for entry, migration in new:
        path = f"migrations/{entry.file}"
        steps, unparsed = _steps(path, migration)
        findings += [*unparsed, *_stray_allows(path, migration, entry)]
        if not _changes_model(entry):
            continue
        if unparsed or done is None:
            done = None
            continue
        done, found = _replay(done, steps, path, "PRF001", check_allows=True)
        findings += found
    return done, findings


def _rename_findings(new: Sequence[tuple[chain.ChainEntry, chain.Migration]], head: Model) -> list[Finding]:
    """PRF006: plan must know every table that the statements of a release change, and it reads
    them with the head model (diff.touched_objects). A rename that it cannot follow there would be
    the refusal RENAME_NOT_RESOLVED of a migration that is merged and can no longer change."""
    kept = [(entry, _steps("", migration)[0]) for entry, migration in new if _changes_model(entry)]
    try:
        diff.touched_objects([step.op for _, steps in kept for step in steps], head)
    except ValueError as e:
        renames = (
            (f"migrations/{entry.file}", step.line)
            for entry, steps in kept
            for step in steps
            if isinstance(step.op, Rename)
        )
        path, line = next(renames, (chain.SUM_PATH, 1))
        message = (
            f"{e}. The replay accepts these statements, and plan could not say which tables they "
            "change. Do not rename a constraint and drop its table in one pull request: drop the "
            "table without the rename"
        )
        return [Finding("PRF006", ERROR, path, line, message)]
    return []


# ------------------------------------------------------------------ gen: the text of a migration
def _added_expressions(op: Operation) -> list[Expression]:
    """Every DEFAULT, CHECK and computed expression that the statement adds."""

    def of(columns: Sequence[Column], constraints: Sequence[Constraint]) -> list[Expression]:
        found = [c.default.expression for c in columns if c.default is not None]
        found += [c.computed.expression for c in columns if c.computed is not None]
        return found + [c.expression for c in constraints if isinstance(c, Check)]

    if isinstance(op, CreateTable):
        return of(op.table.columns, op.table.constraints)
    if isinstance(op, CreateType) and isinstance(op.type, TableType):
        return of(op.type.columns, op.type.constraints)
    if isinstance(op, AddColumn):
        return of((op.column,), ())
    if isinstance(op, AddConstraint) and isinstance(op.constraint, Check | DefaultConstraint):
        return [op.constraint.expression]
    return []


def _created_key(op: Operation) -> str | None:
    """The key of the table-class object that the statement creates; a schema is not an object
    that a function text can name."""
    match op:
        case CreateTable():
            return op.table.key
        case CreateType():
            return op.type.key
        case CreateSequence():
            return op.sequence.key
        case CreateSynonym():
            return op.synonym.key
        case _:
            return None


def _named(text: str, keys: Collection[str]) -> set[str]:
    """The keys that a piece of SQL names: the token scan of modules.scan_references."""
    probe = modules.ModuleFile(
        key="",
        kind="",
        schema="",
        name="",
        path="",
        text=text,
        checksum="",
        schema_bound=False,
        after=(),
        ignore_dep=(),
    )
    return modules.scan_references(probe, keys)


def _blocked(op: Operation) -> tuple[str, str | None] | None:
    """(key of the table, column) of a statement that a schema-bound module can block; the column
    is None when any module that names the table blocks it (design (b) step 4). A rename lists the
    table under the name that the base modules use."""
    match op:
        case AlterColumn() | DropColumn():
            return names.object_key("TABLE", op.schema, op.table), op.column
        case DropTable():
            return names.object_key("TABLE", op.schema, op.name), None
        case Rename() if op.kind == "table":
            return names.object_key("TABLE", op.old[0], op.old[1]), None
        case Rename() if op.kind == "column":
            return names.object_key("TABLE", op.old[0], op.old[1]), op.old[2]
        case _:
            return None


def _bound_blockers(
    base_files: Mapping[str, bytes], tables: Mapping[str, Collection[str] | None]
) -> list[tuple[modules.ModuleFile, str]]:
    """The schema-bound modules of the base revision that block a statement on one of the tables,
    and the schema-bound modules on top of those; dependants first. With each module: what it
    names ('' for a module on top).

    tables: key of a table -> the columns that the statements change; None when the statement
    changes the table itself. A module blocks a column that it names: '*' is not allowed in a
    schema-bound module, so it names every column that it is bound to. The token scan does not
    know which table a column name belongs to; a column of the same name in another table of
    the module counts.
    """
    if not tables:
        return []
    # read_module decides what is schema-bound; a file without the word cannot be (N5-07: 65 of
    # 4,500 files hold it, and reading all of them was half of the time of gen)
    worded = {path: data for path, data in base_files.items() if b"schemabinding" in data.lower()}
    bound = [m for m in _module_files(worded, names.MODULE_KINDS) if m.schema_bound]
    edges = modules.build_edges(bound, ())
    columns = {fold(key): wanted for key, wanted in tables.items()}
    why: dict[str, str] = {}
    for m in bound:
        named: list[str] = []
        for key in sorted(modules.scan_references(m, tables.keys())):
            wanted = columns[fold(key)]
            _, schema, name = names.parse_object_key(key)
            table = names.qualified(schema or "", name)
            if wanted is None:
                named.append(table)
                continue
            try:
                idents = {fold(t.value) for t in lex.significant(lex.tokenize(m.text)) if t.kind in _IDENT}
            except lex.LexError:
                idents = {fold(column) for column in wanted}  # text that the scan read: the strict side
            hit = sorted(names.quote(column) for column in wanted if fold(column) in idents)
            if hit:
                named.append(f"{table} ({', '.join(hit)})")
        if named:
            why[m.key] = ", ".join(named)
    needed = set(why)
    while True:
        # a schema-bound module on a module that is unbound blocks the ALTER of that module
        more = {key for key, uses in edges.items() if uses & needed} - needed
        if not more:
            break
        needed |= more
    by_key = {m.key: m for m in bound}
    return [(by_key[key], why.get(key, "")) for key in modules.drop_order(needed, edges)]


def _unbind_line(module: modules.ModuleFile) -> str:
    return f"{lex.DIRECTIVE_PREFIX}unbind {names.qualified(module.schema, module.name)}"


def _unbind_lines(base_files: Mapping[str, bytes], tables: Collection[str]) -> list[str]:
    """`unbind` lines for the schema-bound modules of the base revision that name one of the
    tables, and for the schema-bound modules on top of those; dependants first."""
    return [_unbind_line(m) for m, _ in _bound_blockers(base_files, dict.fromkeys(tables))]


def _written_name(text: str) -> str:
    """'[schema].[name]' as a directive or the tool writes it, without quotes and letter case."""
    try:
        return "".join(fold(token.value) for token in lex.significant(lex.tokenize(text)))
    except lex.LexError:
        return fold(text)


def _unbind_findings(
    base_files: Mapping[str, bytes], new: Sequence[tuple[chain.ChainEntry, chain.Migration]]
) -> list[Finding]:
    """PRF007: a new migration has a statement that a schema-bound module of the base revision
    blocks, and no `unbind` line for that module (live T3).

    gen writes the line; a hand-written migration (gen refused the change) needs it too. Without
    it the plan refuses the release with TABLE_BLOCKER, and by then the migration is merged and
    can only be withdrawn and replaced. So this is an error.
    """
    out: list[Finding] = []
    for entry, migration in new:
        if entry.withdrawn:
            continue
        path = f"migrations/{entry.file}"
        tables: dict[str, set[str] | None] = {}
        first = 0  # line of the first statement that a module can block
        for step in _steps(path, migration)[0]:
            found = _blocked(step.op)
            if found is None:
                continue
            first = first or step.line
            key, column = found
            columns = tables.setdefault(key, set())
            if column is None:
                tables[key] = None
            elif columns is not None:
                columns.add(column)
        written = {
            _written_name(directive.object_key)
            for batch in migration.batches
            for directive in batch.directives
            if isinstance(directive, chain.Unbind)
        }
        for module, named in _bound_blockers(base_files, tables):
            line = _unbind_line(module)
            if _written_name(line.removeprefix(f"{lex.DIRECTIVE_PREFIX}unbind ")) in written:
                continue
            what = (
                f"names {named}, which this migration alters, renames or drops"
                if named
                else "is bound to a module that this migration must unbind"
            )
            message = (
                f"{names.qualified(module.schema, module.name)} is schema-bound and {what}, and the "
                f"migration has no line '{line}'. The plan refuses the merged release (TABLE_BLOCKER), "
                "and a merged migration cannot change. Write the line above the first statement, then "
                "run `azsqlcd gen --resum`"
            )
            out.append(Finding("PRF007", ERROR, path, first, message))
    return out


def _rename_candidates(
    start: Model, head: Model, specs: Sequence[diff.RenameSpec]
) -> list[tuple[str, str, str]]:
    """(key of the table at the start, what the models show, the --rename value) of each change
    that can be one rename and that no --rename value declares (CU-03).

    A column: one column of a table is gone and one is new, with the same data type. A table: one
    table is gone and one of the same schema is new, with the same columns. gen infers no rename:
    the author says whether the rows stay.
    """
    declared = {(spec.kind, tuple(fold(part) for part in spec.old)) for spec in specs}
    out: list[tuple[str, str, str]] = []
    gone = [obj for key, obj in start.items() if isinstance(obj, Table) and key not in head]
    new = [obj for key, obj in head.items() if isinstance(obj, Table) and key not in start]

    def shape(table: Table) -> list[tuple[str, object, bool]]:
        return [(fold(c.name), c.type, c.computed is None) for c in table.columns]

    for old in gone:
        same = [t for t in new if fold(t.schema) == fold(old.schema) and shape(t) == shape(old)]
        if len(same) != 1 or ("table", (fold(old.schema), fold(old.name))) in declared:
            continue
        if sum(fold(t.schema) == fold(old.schema) and shape(t) == shape(old) for t in gone) != 1:
            continue  # two tables of one shape are gone: which one became the new one?
        was, now = names.qualified(old.schema, old.name), names.qualified(same[0].schema, same[0].name)
        what = f"table {was} is gone and table {now} with the same columns is new"
        out.append((old.key, what, f'--rename "table:{was}={names.quote(same[0].name)}"'))
    for key, old in start.items():
        now_table = head.get(key)
        if not isinstance(old, Table) or not isinstance(now_table, Table):
            continue
        before = {fold(c.name): c for c in old.columns}
        after = {fold(c.name): c for c in now_table.columns}
        lost = [c for name, c in before.items() if name not in after]
        added = [c for name, c in after.items() if name not in before]
        if len(lost) != 1 or len(added) != 1:
            continue
        a, b = lost[0], added[0]
        if a.type != b.type or (a.computed is None) != (b.computed is None):
            continue
        if ("column", (fold(old.schema), fold(old.name), fold(a.name))) in declared:
            continue
        table = names.qualified(old.schema, old.name)
        what = (
            f"column {names.quote(a.name)} of {table} is gone and column {names.quote(b.name)} with the "
            "same data type is new"
        )
        out.append((old.key, what, f'--rename "column:{table}.{names.quote(a.name)}={names.quote(b.name)}"'))
    return out


def _gen_refused(
    refusals: Sequence[diff.GenRefusal], renames: Sequence[tuple[str, str, str]] = ()
) -> ToolError:
    """renames: _rename_candidates(). ORD001 for a table with a candidate is most often a column
    that was renamed in the file: its hint and the message get the exact --rename value."""
    used: list[str] = []
    told: list[diff.GenRefusal] = []
    for r in refusals:
        mine = [(what, value) for key, what, value in renames if fold(key) == fold(r.object_key)]
        if r.code == "ORD001" and mine:
            used += [value for _, value in mine if value not in used]
            said = "; ".join(f"{what}: run gen again with {value}" for what, value in mine)
            r = replace(r, hint=f"{r.hint} If this is a rename ({said}). gen infers no rename.")
        told.append(r)
    refusals = told
    listed = "; ".join(f"{r.code} {r.object_key}: {r.message}" for r in refusals)
    if used:
        listed += f". If a column was renamed, say so: run gen again with {' '.join(used)}"
    return refused(
        "GEN_REFUSED",
        f"gen does not write this change; the hint of each refusal says what to do. {listed}",
        refusals=[
            {"code": r.code, "object": r.object_key, "message": r.message, "hint": r.hint} for r in refusals
        ],
    )


def _draft(
    base_files: Mapping[str, bytes],
    head_files: Mapping[str, bytes],
    start: Model,
    head: Model,
    specs: Sequence[diff.RenameSpec] = (),
) -> str:
    """The batches of the migration that changes start into head, with their directive lines.

    '' when the models are equal. Raises ToolError GEN_REFUSED or GEN_INVALID.
    """
    try:
        ops = diff.diff(start, head, specs)
    except diff.GenRefused as e:
        raise _gen_refused(e.refusals, _rename_candidates(start, head, specs)) from None
    if not ops:
        return ""
    functions = {m.key: m for m in _module_files(head_files, ("FUNCTION",))}
    refusals: list[diff.GenRefusal] = []
    batches: list[str] = []
    state = start
    created: set[str] = set()  # '[schema].[table]' of each CREATE TABLE so far, as lint.py reads it
    deployed: set[str] = set()
    blocked: set[str] = set()  # keys of the tables that a schema-bound module can block
    for index, op in enumerate(ops):
        try:
            statement = emit.emit_operation(op)
        except ValueError as e:
            raise refused("GEN_INVALID", f"statement {index + 1} has no SQL spelling: {e}") from None
        lines: list[str] = []

        # design (b) step 3: a function that a new DEFAULT, CHECK or computed column names
        scanned = (_named(" ".join(e.tokens), functions.keys()) for e in _added_expressions(op))
        for key in sorted(set().union(*scanned) - deployed):
            function = functions[key]
            # the function is deployed above this statement, so "later" starts at this statement
            later = [k for k in map(_created_key, ops[index:]) if k is not None]
            for missing in sorted(modules.scan_references(function, later)):
                message = f"the function names {missing}, which this migration creates later"
                refusals.append(diff.GenRefusal("ORD002", key, message, _ORD002_HINT))
            lines.append(
                f"{lex.DIRECTIVE_PREFIX}deploy-module {names.qualified(function.schema, function.name)}"
            )
            deployed.add(key)

        # design (b) step 5. The codes and objects are those that lint.py asks for, read from the
        # statement as lint.py reads it; SET_NOT_NULL is the one code that only the model decides.
        facts = lint.classify_batch(chain.MigrationBatch("model", statement, 1, ()), "tx", created)
        # LFP-03: the closed loop fails here and not in the pull request. A statement that lint
        # refuses would fail lint, verify and build as a file that the author did not write.
        refusing = [f for f in facts.findings if f.severity == ERROR]
        if refusing:
            raise refused(
                "GEN_INVALID",
                f"statement {index + 1} ({facts.statement or 'no statement class'}) fails lint with "
                f"{refusing[0].code}: {refusing[0].message}. gen writes no migration that lint refuses. "
                "Write the table-class file in a form whose statement passes (an expression: put it in "
                "parentheses); when no form passes, this is a defect of the tool. The statement: "
                f"{statement}",
                statement_class=facts.statement,
                code=refusing[0].code,
                findings=[{"code": f.code, "message": f.message} for f in refusing],
            )
        wanted = [(item.code, item.object) for item in facts.needs_allow]
        not_null = _set_not_null(op, state)
        if not_null is not None:
            wanted.append(("SET_NOT_NULL", not_null))
        # The codes that the statement alone decides (DROP_TABLE, TEMPORAL_OFF, UNMASK, ...) come
        # from diff.classify too. lint.py must read each of them from the statement: a code that
        # it does not read would leave a destructive statement with no allow line, and verify,
        # which asks lint.py, would pass it.
        unread = set(diff.classify(op, state)) - {code for code, _ in wanted} - _MODEL_ONLY_CODES
        if unread:
            raise refused(
                "GEN_INVALID",
                f"statement {index + 1} needs allow {', '.join(sorted(unread))} and lint does not read "
                f"that from its text; this is a defect of the tool, hand-write the migration: {statement}",
            )
        lines += [f"{lex.DIRECTIVE_PREFIX}allow {code} {obj} reason: TODO" for code, obj in wanted]
        created.update(facts.creates_tables)

        # design (b) step 4: the tables whose statements a schema-bound module blocks. Renames come
        # first, and the rename of a table lists it under the name that the base modules use.
        table = _blocked(op)
        if table is not None:
            blocked.add(table[0])

        try:
            state = replay.apply(state, op)
        except replay.ReplayError as e:
            raise refused("GEN_INVALID", f"statement {index + 1} does not replay: {e}") from None
        batches.append("\n".join([*lines, statement, "GO"]))
    if refusals:
        raise _gen_refused(refusals)
    return "\n".join([*_unbind_lines(base_files, blocked), *batches]) + "\n"


def _start_model(
    base_files: Mapping[str, bytes], head_files: Mapping[str, bytes], root: release.StrPath, base: str
) -> Model:
    """The model that a new migration starts from: the base revision, then the migrations that the
    working tree has already added to the chain. So a second `gen` on one branch writes only what
    the first did not. When the branch withdraws a merged migration with no replacement, the start
    is the model without it, as in the proof (WDR004); base is the revision whose first-parent
    history holds that migration."""
    base_chain, head_chain = _chain(base_files), _chain(head_files)
    start = load_model(base_files)
    if _withdrawn_alone(base_files, base_chain, head_chain):
        try:
            start = _model_without(root, base, base_files, base_chain, head_chain)
        except _NoProof as e:
            raise refused(
                "GEN_INVALID",
                f"the branch withdraws a merged migration with no replacement, and the model without it "
                f"cannot be found: {e}. Add the replacement (a new line with replaces=) or hand-write "
                "the change",
            ) from None
    done, findings = _replay_new(_new_migrations(base_files, head_files), start)
    blocking = [f for f in findings if f.severity == ERROR and f.code != "PRF003"]
    if blocking:
        first = blocking[0]
        raise refused(
            "GEN_INVALID",
            f"{first.path} line {first.line} is a new migration of this branch and does not replay on "
            f"the base revision: {first.message}. Correct it or remove it (file and chain line), then run "
            "gen again",
            path=first.path,
            line=first.line,
        )
    assert done is not None  # no model means a batch that does not parse or a blocked statement
    return done


def _commit(root: release.StrPath, revision: str) -> str:
    # --end-of-options: a revision that starts with '-' is never read as an option
    try:
        found = release.git(["rev-parse", "--verify", "--end-of-options", revision + "^{commit}"], root)
    except ToolError as e:
        # git says only "Needed a single revision": the reader must see which one
        raise refused(
            "GIT_FAILED", f"{revision!r} is not a commit of this repository: {e.message}", revision=revision
        ) from None
    return found.decode().strip()


def _exact_commit(root: release.StrPath, revision: str) -> str:
    """The commit of a full commit id, or of one exact ref that is not a tag.

    Refused (MAIN_REF_INVALID) for any other text. git reads a short name such as origin/main as
    refs/tags/origin/main before refs/remotes/origin/main, and anyone who can push a tag can make
    one; the same holds for a short commit id and for a revision expression on a short name.
    """
    if _COMMIT_ID.fullmatch(revision):
        return _commit(root, revision)
    if not revision.startswith("refs/") or revision.startswith("refs/tags/"):
        raise refused(
            "MAIN_REF_INVALID",
            f"{revision!r} is not a full commit id and not a full ref name that is not a tag. Give the "
            f"commit id (git rev-parse) or a ref such as {release.MAIN_REF}",
            ref=revision,
        )
    try:
        exact = release.git(["show-ref", "--verify", "--hash", revision], root).decode().strip()
    except ToolError as e:
        raise refused(
            "GIT_FAILED", f"the ref {revision!r} is not in this repository: {e.message}", revision=revision
        ) from None
    return _commit(root, exact)


def _merge_base(root: release.StrPath, base_ref: str) -> str:
    return release.git(["merge-base", _commit(root, base_ref), "HEAD"], root).decode().strip()


def _number(file: str) -> int:
    m = _MIGRATION_FILE.fullmatch(file)
    return int(m[1]) if m else 0


def generate(root: release.StrPath, base_ref: str, name: str, renames: Sequence[str] = ()) -> GenResult:
    """Design (b), generation: the migration that brings the base model to the working tree.

    base_ref is a git revision, for example origin/main; the base model is the model at the merge
    base of base_ref and HEAD. name is the part of the file name after the number. renames are
    --rename values, KIND:[schema].[table].[old]=[new]. Nothing is written: see write_result().
    GenResult.hints names the --rename value for a DROP + ADD that can be one rename; the caller
    prints it.

    Raises ToolError REFUSED: GEN_REFUSED (detail['refusals']: code, object, message, hint of every
    change that the generator does not write: the codes of diff.REFUSALS, TEMPORAL_CHANGE among
    them, and ORD002), RENAME_INVALID (a --rename value
    that cannot be read), GEN_INVALID (the name; a new migration of the branch that does not
    replay), and the refusals of the layers below (GIT_FAILED, MODEL_INVALID, CHAIN_INVALID, ...).
    """
    if not _NAME.fullmatch(name) or len(name) > 180:
        raise refused(
            "GEN_INVALID", "the migration name holds letters, digits and '_' only, at most 180 of them"
        )
    try:
        specs = [diff.parse_rename_spec(text) for text in renames]
    except ValueError as e:
        raise refused("RENAME_INVALID", str(e)) from None
    merge_base = _merge_base(root, base_ref)
    base_files = release.read_tree(root, merge_base)
    head_files = read_working_tree(root)
    head = load_model(head_files)
    missing = [finding for _, finding in _missing_schemas(head)]
    if missing:
        # CU-04: the diff would say ORDER_BLOCKED "hand-write the migration"; the correction is a file
        raise refused(
            "MODEL_INVALID",
            f"{len(missing)} object(s) are in a schema that has no schema file, so gen writes nothing. "
            f"First: {missing[0].message}, then run gen again",
            path=missing[0].path,
            line=1,
            errors=[{"path": f.path, "line": f.line, "code": f.code, "message": f.message} for f in missing],
        )
    for entry in _new_entries(_chain(base_files), _chain(head_files)):
        if f"migrations/{entry.file}" not in head_files:
            # CU-01: the author deleted the file of a migration of this branch to write it again
            raise refused(
                "MIGRATION_INVALID",
                f"migrations/{entry.file} does not exist, and {chain.SUM_PATH} lists it. The migration "
                "is new on this branch: run `azsqlcd gen --resum`, which drops the chain line of a new "
                "migration whose file is gone, then run gen again. Or put the file back",
                file=entry.file,
                line=1,
            )
    start = _start_model(base_files, head_files, root, merge_base)
    body = _draft(base_files, head_files, start, head, specs)
    if not body:
        others = (p for p in base_files.keys() | head_files.keys() if p.startswith("schema/"))
        changed = any(
            base_files.get(p) != head_files.get(p) for p in others if _kind(p) not in names.TABLE_CLASS_KINDS
        )
        return GenResult(ONLY_MODULES if changed else NO_CHANGE)
    head_chain = _chain(head_files)
    on_disk = [p.rpartition("/")[2] for p in head_files if p.startswith("migrations/")]
    listed = [entry.file for c in (_chain(base_files), head_chain) for entry in c.entries]
    stem = f"{max(map(_number, [*on_disk, *listed]), default=0) + 1:04d}__{name}"
    text = f"{lex.DIRECTIVE_PREFIX}migration {stem}\n{lex.DIRECTIVE_PREFIX}mode tx\n{body}"
    file = f"{stem}.sql"
    chain.parse_migration(text, file)  # the generator reads its own output before it returns it
    entry = chain.ChainEntry(file, chain.file_sha256(text.encode("utf-8")), "tx")
    sum_text = chain.format_sum(replace(head_chain, entries=(*head_chain.entries, entry)))
    hints = tuple(
        f"{what}: the migration drops it, and its rows are lost. If this is a rename: delete "
        f"migrations/{file}, run `azsqlcd gen --resum`, then run gen again with {value}"
        for _, what, value in _rename_candidates(start, head, specs)
    )
    return GenResult("", file, text, sum_text, hints)


def write_result(root: release.StrPath, result: GenResult) -> list[str]:
    """Write the migration file and migrations/migrations.sum. Returns the paths written; [] when
    the result holds no migration. An existing migration file is never replaced (GEN_INVALID)."""
    if result.file is None or result.text is None or result.sum_text is None:
        return []
    folder = Path(root, "migrations")
    folder.mkdir(parents=True, exist_ok=True)
    path = f"migrations/{result.file}"
    try:
        with open(folder / result.file, "xb") as out:
            out.write(result.text.encode("utf-8"))
    except FileExistsError:
        raise refused("GEN_INVALID", f"{path} exists already", path=path) from None
    Path(root, chain.SUM_PATH).write_bytes(result.sum_text.encode("utf-8"))
    return [path, chain.SUM_PATH]


# ------------------------------------------------------------------ gen --resum
def _written_sum(files: Mapping[str, bytes]) -> tuple[bool, list[chain.ChainEntry]]:
    """(baseline, entries) of migrations.sum as the working tree holds it; the sha256 is not kept."""
    data = files.get(chain.SUM_PATH)
    if data is None:
        return False, []
    baseline = False
    entries: dict[str, chain.ChainEntry] = {}
    lines = _text(chain.SUM_PATH, data).removeprefix(lex.BOM).splitlines()
    for n, line in enumerate(lines[1:], start=2):
        m = _SUM_LINE.fullmatch(line)
        if line == "baseline":
            baseline = True
        elif m is None:
            raise refused(
                "CHAIN_INVALID",
                f"{chain.SUM_PATH} line {n}: not a chain line. Resolve the merge first: keep the lines "
                "of both sides, in any order",
                line=n,
            )
        else:
            entries.setdefault(
                m["file"],
                chain.ChainEntry(m["file"], "", m["mode"], m["withdrawn"] is not None, m["replaces"]),
            )
    return baseline, list(entries.values())


def resum(root: release.StrPath, base_ref: str) -> ResumResult:
    """`gen --resum`, after a merge of main or a hand edit: the chain of the working tree again.

    The lines of base_ref (the revision itself, not a merge base: the merge need not be concluded)
    stay as they are, first; a 'withdrawn' that the working tree adds to one of them is kept.
    Every other migration is new: those of migrations.sum in their order, then migration files
    that have no line. The line of a new migration whose file is gone is dropped
    (ResumResult.dropped; the caller says so): delete the file, run gen --resum, run gen again.
    Each new one gets the next number above the highest number at base_ref
    (the file is renamed and its header id changed when the number differs) and the sha256 of its
    text. Nothing is written: see write_resum(). Raises ToolError REFUSED: CHAIN_INVALID (a line
    of migrations.sum that cannot be read), MIGRATION_INVALID, GEN_INVALID (the working tree does
    not hold a migration of base_ref, or a new line does not name a migration file).
    """
    base_files = release.read_tree(root, base_ref)
    head_files = read_working_tree(root)
    base_chain = _chain(base_files)
    merged = {entry.file for entry in base_chain.entries}
    absent = sorted(file for file in merged if f"migrations/{file}" not in head_files)
    if absent:
        raise refused(
            "GEN_INVALID",
            f"the working tree does not hold migrations/{absent[0]} of {base_ref}: merge {base_ref} "
            "into the branch first",
            path=f"migrations/{absent[0]}",
        )
    baseline, written = _written_sum(head_files)
    withdrawn = {entry.file for entry in written if entry.withdrawn}
    entries = [
        replace(entry, withdrawn=entry.withdrawn or entry.file in withdrawn) for entry in base_chain.entries
    ]
    new = [entry for entry in written if entry.file not in merged]
    known = merged | {entry.file for entry in new}
    # CU-01: a migration that only this branch added and whose file the author deleted. Its line
    # goes, so gen can write the migration again. A merged line never goes (GEN_INVALID above).
    dropped = tuple(entry.file for entry in new if f"migrations/{entry.file}" not in head_files)
    new = [entry for entry in new if entry.file not in dropped]
    on_disk = sorted(
        p.rpartition("/")[2] for p in head_files if p.startswith("migrations/") and p.endswith(".sql")
    )
    new += [chain.ChainEntry(file, "", "tx") for file in on_disk if file not in known]

    number = max(map(_number, merged), default=0)
    renames: list[Renumbered] = []
    moved: dict[str, str] = {}
    for entry in new:
        path = f"migrations/{entry.file}"
        m = _MIGRATION_FILE.fullmatch(entry.file)
        if m is None:
            raise refused("GEN_INVALID", f"{path} is not a migration file name", path=path)
        number += 1
        file = f"{number:04d}__{m[2]}.sql"
        text = _text(path, head_files[path])
        if file != entry.file:
            old_stem, new_stem = entry.file.removesuffix(".sql"), file.removesuffix(".sql")
            # chain.parse_migration reads a file that starts with a byte order mark; keep the mark
            header = (
                rf"(?m)^({lex.BOM}?){re.escape(lex.DIRECTIVE_PREFIX)}migration {re.escape(old_stem)}"
                r"(?=[ \t]*\r?$)"
            )
            text = re.sub(header, rf"\g<1>{lex.DIRECTIVE_PREFIX}migration {new_stem}", text, count=1)
            renames.append(Renumbered(entry.file, file, text))
            moved[entry.file] = file
        mode = chain.parse_migration(text, file).mode
        replaces = moved.get(entry.replaces, entry.replaces) if entry.replaces is not None else None
        entries.append(
            chain.ChainEntry(file, chain.file_sha256(text.encode("utf-8")), mode, entry.withdrawn, replaces)
        )
    sum_text = chain.format_sum(chain.Chain(base_chain.baseline or baseline, tuple(entries)))
    chain.parse_sum(sum_text)  # the rules between the lines, for example what replaces= names
    return ResumResult(tuple(renames), sum_text, dropped)


def write_resum(root: release.StrPath, result: ResumResult) -> list[str]:
    """Apply resum(): rename the migration files and write migrations/migrations.sum. Returns the
    paths written."""
    folder = Path(root, "migrations")
    folder.mkdir(parents=True, exist_ok=True)
    for renumbered in result.renames:  # every old name first: a new name can be an old name
        (folder / renumbered.old).unlink()
    for renumbered in result.renames:
        (folder / renumbered.new).write_bytes(renumbered.text.encode("utf-8"))
    Path(root, chain.SUM_PATH).write_bytes(result.sum_text.encode("utf-8"))
    return [*(f"migrations/{renumbered.new}" for renumbered in result.renames), chain.SUM_PATH]


# ------------------------------------------------------------------ the proof
def _model_before(root: release.StrPath, main_ref: str, withdrawn: str) -> tuple[Model, list[_Step]]:
    """A31: (the model that the withdrawn migration ran on, its statements).

    The model of the parent of the first-parent commit of main that added the file, then the
    other migrations that the same commit added to the chain before it.
    """
    path = f"migrations/{withdrawn}"
    # -m: a merge commit is compared with its first parent, also by a git older than 2.31
    log = ["log", "-m", "--first-parent", "--diff-filter=A", "--format=%H", _exact_commit(root, main_ref)]
    added = release.git([*log, "--", path], root).decode().split()
    if not added:
        raise _NoProof(f"no first-parent commit of {main_ref} adds {path}")
    commit = added[-1]
    parents = release.git(["rev-list", "--parents", "-n", "1", commit], root).decode().split()[1:]
    before = release.read_tree(root, parents[0]) if parents else {}
    at = release.read_tree(root, commit)
    state = load_model(before)
    for entry in _new_entries(_chain(before), _chain(at)):
        steps, unparsed = _steps(f"migrations/{entry.file}", _read_migration(at, entry.file))
        if unparsed:
            raise _NoProof(f"migrations/{entry.file} line {unparsed[0].line}: {unparsed[0].message}")
        if entry.file == withdrawn:
            return state, steps
        if _changes_model(entry):
            done, found = _replay(state, steps, f"migrations/{entry.file}", "WDR002")
            if done is None:
                raise _NoProof(f"migrations/{entry.file} line {found[0].line}: {found[0].message}")
            state = done
    raise _NoProof(f"commit {commit[:12]} adds {path} and no chain line for it")


def _withdrawn_now(base: chain.Chain, head: chain.Chain) -> list[chain.ChainEntry]:
    """The lines of the base chain that the head chain marks withdrawn and the base chain does not."""
    live = {entry.file for entry in base.entries if not entry.withdrawn}
    return [entry for entry in head.entries if entry.withdrawn and entry.file in live]


def _alone(head: chain.Chain) -> set[str]:
    """The withdrawn lines of the head chain that no line replaces."""
    replaced = {entry.replaces for entry in head.entries}
    return {entry.file for entry in head.entries if entry.withdrawn and entry.file not in replaced}


def _withdrawn_alone(
    base_files: Mapping[str, bytes], base: chain.Chain, head: chain.Chain
) -> list[tuple[int, chain.ChainEntry]]:
    """(line of migrations.sum, entry) of each merged migration that this change withdraws with no
    replacement and that has a model batch. A migration of data and raw batches only does not
    change the model, so the proof has nothing to say about it."""
    alone = _alone(head)
    lines = {entry.file: n for n, entry in enumerate(head.entries, start=3 if head.baseline else 2)}
    return [
        (lines[entry.file], entry)
        for entry in _withdrawn_now(base, head)
        if entry.file in alone
        and any(batch.kind == "model" for batch in _read_migration(base_files, entry.file).batches)
    ]


def _model_without(
    root: release.StrPath | None,
    main_ref: str,
    base_files: Mapping[str, bytes],
    base: chain.Chain,
    head: chain.Chain,
) -> Model:
    """The model that the base chain gives without its withdrawn lines that nothing replaces.

    A database that has not run such a line never runs it. The model before the first of them
    comes from the history (as for a replacement, A31); the later lines of the base chain are
    replayed on it. A line that is withdrawn and replaced is replayed as it was merged: its
    replacement is proven to give the same model. Raises _NoProof when a later line does not
    replay without the withdrawn ones, and ToolError when git or a file cannot be read.
    """
    alone = _alone(head)
    first = next(
        i
        for i, entry in enumerate(base.entries)
        if entry.file in alone and _steps("", _read_migration(base_files, entry.file)) != ([], [])
    )
    if root is None:
        raise _NoProof(
            f"the model before {base.entries[first].file} comes from the git history; no repository was given"
        )
    state, _ = _model_before(root, main_ref, base.entries[first].file)
    for entry in base.entries[first + 1 :]:
        if entry.file in alone or entry.replaces is not None:
            continue
        path = f"migrations/{entry.file}"
        steps, unparsed = _steps(path, _read_migration(base_files, entry.file))
        done, found = (None, unparsed) if unparsed else _replay(state, steps, path, "WDR004")
        if done is None:
            raise _NoProof(
                f"{path} line {found[0].line} does not replay without the withdrawn migration: "
                f"{found[0].message}"
            )
        state = done
    return state


def _new_tombstones(base_files: Mapping[str, bytes], head_files: Mapping[str, bytes]) -> list[str]:
    """Object keys of the tombstones that the head revision adds. A tombstone file that cannot be
    read: lint reports the head file, and the base file counts as empty."""

    def keys(files: Mapping[str, bytes]) -> list[str]:
        data = files.get(chain.TOMBSTONES_PATH)
        if data is None:
            return []
        return [t.object_key for t in chain.parse_tombstones(_text(chain.TOMBSTONES_PATH, data))]

    try:
        head = keys(head_files)
    except ToolError:
        return []
    try:
        base = {fold(key) for key in keys(base_files)}
    except ToolError:
        base = set()
    return [key for key in head if fold(key) not in base]


def _line_of_name(data: bytes | None, schema: str, name: str) -> int:
    """The line of the first [schema].[name] in a file; 1 when the file does not show it."""
    try:
        toks = lex.significant(lex.tokenize((data or b"").decode("utf-8")))
    except (UnicodeDecodeError, lex.LexError):
        return 1
    for a, dot, b in zip(toks, toks[1:], toks[2:], strict=False):
        if a.kind in _IDENT and b.kind in _IDENT and dot.kind == "op" and dot.text == ".":
            if fold(a.value) == fold(schema) and fold(b.value) == fold(name):
                return a.line
    return 1


def _tombstone_findings(
    base_files: Mapping[str, bytes], head_files: Mapping[str, bytes], head: Model
) -> list[Finding]:
    """TMB003: a new tombstone for a function that a CHECK, a DEFAULT or a computed column of a
    table-class file still uses (CU-10).

    The engine refuses DROP FUNCTION while such an expression uses the function, so the deploy of
    the release would be the first to say so. lint gives DRP002 for the modules that still use a
    tombstoned module; it does not read the table-class files.
    """
    functions = [key for key in _new_tombstones(base_files, head_files) if key.startswith("FUNCTION:")]
    out: list[Finding] = []
    if not functions:
        return out
    for obj in head.values():
        if not isinstance(obj, Table | TableType):
            continue
        uses: list[tuple[str, Expression]] = []
        for column in obj.columns:
            if column.default is not None:
                named = f" {names.quote(column.default.name)}" if column.default.name else ""
                uses.append(
                    (f"DEFAULT{named} of column {names.quote(column.name)}", column.default.expression)
                )
            if column.computed is not None:
                uses.append((f"computed column {names.quote(column.name)}", column.computed.expression))
        for constraint in obj.constraints:
            if isinstance(constraint, Check):
                named = f" {names.quote(constraint.name)}" if constraint.name else ""
                uses.append((f"CHECK{named}", constraint.expression))
        found: dict[str, list[str]] = {}  # function key -> what uses it
        for what, expression in uses:
            for key in sorted(_named(" ".join(expression.tokens), functions)):
                found.setdefault(key, []).append(what)
        path = _object_path(obj.key)
        for key, users in found.items():
            _, schema, name = names.parse_object_key(key)
            message = (
                f"{key} has a new tombstone, and {', '.join(users[:3])}{' ...' if len(users) > 3 else ''} "
                f"of {obj.key} still uses the function. The engine refuses DROP FUNCTION while a CHECK, a "
                "DEFAULT or a computed column uses it, so the deploy of this release would fail. Take the "
                "use out of the table-class file (with its migration), or keep the function file and "
                "remove the tombstone"
            )
            line = _line_of_name(head_files.get(path), schema or "", name)
            out.append(Finding("TMB003", ERROR, path, line, message))
    return out


def _replacement_findings(
    root: release.StrPath | None,
    main_ref: str,
    entry: chain.ChainEntry,
    migration: chain.Migration,
    changed: Sequence[str],
) -> list[Finding]:
    """Design (b) proof step 6 for one replacement: WDR001, WDR002, WDR003."""
    assert entry.replaces is not None
    path = f"migrations/{entry.file}"
    out: list[Finding] = []
    if changed:
        message = (
            f"a pull request that replaces a withdrawn migration changes no table-class file; this one "
            f"changes {', '.join(changed[:3])}{' ...' if len(changed) > 3 else ''}. The replacement must "
            "give the model that the withdrawn migration gave"
        )
        out.append(Finding("WDR001", ERROR, path, 1, message))
    allows = (d for b in migration.batches for d in b.directives if isinstance(d, chain.Allow))
    if not any(d.code == "REPLACEMENT_EDGE" and _replaces(entry, d) for d in allows):
        message = (
            f"this migration replaces {entry.replaces}: a database that ran it and a database that runs "
            f"this file must end equal, also in their rows. Write above a statement: "
            f"{lex.DIRECTIVE_PREFIX}allow REPLACEMENT_EDGE {entry.replaces} reason: <why>"
        )
        out.append(Finding("WDR003", ERROR, path, 1, message))
    steps, unparsed = _steps(path, migration)
    if unparsed:
        return out  # _replay_new reports the batches that do not parse
    if root is None:
        message = f"the model before {entry.replaces} comes from the git history; no repository was given"
        return [*out, Finding("WDR002", ERROR, path, 1, message)]
    try:
        before, old_steps = _model_before(root, main_ref, entry.replaces)
    except (ToolError, _NoProof) as e:
        message = f"the model before {entry.replaces} cannot be read, so nothing is proven: {e}"
        return [*out, Finding("WDR002", ERROR, path, 1, message)]
    old, found = _replay(before, old_steps, f"migrations/{entry.replaces}", "WDR002")
    new, more = _replay(before, steps, path, "WDR002", check_allows=True)
    out += [*found, *more]
    if old is not None and new is not None:
        for key, prop in old.diff_paths(new):
            what = {
                ABSENT_IN_SELF: f"only this migration leaves it; {entry.replaces} does not",
                ABSENT_IN_OTHER: f"only {entry.replaces} leaves it; this migration does not",
            }.get(prop, f"{prop} differs from what {entry.replaces} gives")
            out.append(Finding("WDR002", ERROR, path, 1, f"{key}: {what}"))
    return out


def _prove(
    base_files: Mapping[str, bytes],
    head_files: Mapping[str, bytes],
    cfg: config.Config,
    head: Model,
    root: release.StrPath | None,
    main_ref: str,
) -> list[Finding]:
    base_chain, head_chain = _chain(base_files), _chain(head_files)
    new = _new_migrations(base_files, head_files)
    out: list[Finding] = []

    unmanaged = {fold(key) for key in cfg.unmanaged_objects}
    # the managed keys need every module file of the revision: read them for a raw batch only (N5-03)
    has_raw = any(batch.kind == "raw" for _, migration in new for batch in migration.batches)
    out += _raw_findings(new, unmanaged, _managed_keys(head_files, head) if has_raw else set())
    out += _unbind_findings(base_files, new)
    out += _tombstone_findings(base_files, head_files, head)

    # a base revision whose config cannot be read gets the strict path
    if _base_table_model(base_files) is False and cfg.project.table_model:
        # design (c) 8: the table files arrive with the switch and `baseline` records them
        message = (
            "this pull request sets table_model = true: the table-class files are taken as they are, "
            "there is no base model to prove them against. Run baseline on every target after the merge"
        )
        out.append(Finding("PRF000", WARNING, release.CONFIG_PATH, 1, message))
        for entry, _ in new:
            message = (
                "the pull request that sets table_model = true adds no migration: nothing can prove it. "
                "Add the migration in a pull request of its own"
            )
            out.append(Finding("PRF004", ERROR, f"migrations/{entry.file}", 1, message))
        return out

    base = load_model(base_files)
    withdrawn = _withdrawn_alone(base_files, base_chain, head_chain)
    start: Model | None = base
    if withdrawn:
        try:
            start = _model_without(root, main_ref, base_files, base_chain, head_chain)
        except (ToolError, _NoProof) as e:
            start = None
            for line, entry in withdrawn:
                message = (
                    f"{entry.file} is withdrawn and no line of this pull request replaces it. That the "
                    f"table-class files hold the model without it cannot be proven: {e}. "
                    + _WDR004_HINT.format(file=entry.file)
                )
                out.append(Finding("WDR004", ERROR, chain.SUM_PATH, line, message))
    done: Model | None = None
    if start is not None:
        done, found = _replay_new(new, start)
        out += found
    if done is not None:
        out += _rename_findings(new, head)
    differences = done.diff_paths(head) if done is not None else []
    if differences and withdrawn:
        line, entry = withdrawn[0]
        also = "".join(f", {other.file}" for _, other in withdrawn[1:])
        for key, prop in differences:
            what = {
                ABSENT_IN_SELF: "the object file exists, and the chain without the withdrawn lines does "
                "not create the object",
                ABSENT_IN_OTHER: "the chain without the withdrawn lines leaves the object, and it has no "
                "object file",
            }.get(prop, f"{prop}: the object file holds another value than the chain without them gives")
            message = (
                f"{entry.file}{also}: withdrawn, and no line of this pull request replaces it. A database "
                f"that has not run a withdrawn migration never runs it, so the table-class files must hold "
                f"the model without it. {key}: {what}. " + _WDR004_HINT.format(file=entry.file)
            )
            out.append(Finding("WDR004", ERROR, chain.SUM_PATH, line, message))
    elif differences and not any(_changes_model(entry) for entry, _ in new):
        try:
            draft = "it writes:\n" + _draft(base_files, head_files, base, head)
        except ToolError as e:
            draft = f"it refuses, so hand-write the migration: {e}"
        message = (
            f"{len(differences)} difference(s) between the table-class files of the base revision and "
            f"of this revision (first: {differences[0][0]}), and the pull request adds no migration. Run "
            f"`azsqlcd gen --base <ref> --name <name>`; {draft}"
        )
        out.append(Finding("PRF002", ERROR, chain.SUM_PATH, 1, message))
    else:
        for key, prop in differences:
            what = {
                ABSENT_IN_SELF: "the object file exists and no migration creates the object",
                ABSENT_IN_OTHER: "the migrations leave the object and it has no object file",
            }.get(prop, f"{prop}: the migrations give another value than the object file")
            out.append(Finding("PRF001", ERROR, _object_path(key), 1, f"{key}: {what}"))

    replacements = [(entry, migration) for entry, migration in new if entry.replaces is not None]
    now = {entry.file for entry in _withdrawn_now(base_chain, head_chain)} | {entry.file for entry, _ in new}
    for entry, _ in replacements:
        if entry.replaces not in now:
            message = (
                f"{entry.replaces} was withdrawn by an earlier pull request. A replacement is added by the "
                "pull request that withdraws the migration: a database that skipped the withdrawn "
                "migration can have applied later migrations since, and a replacement stands before "
                "those in the apply order. Add this change as a migration of its own, without replaces="
            )
            out.append(Finding("WDR005", ERROR, f"migrations/{entry.file}", 1, message))
    if replacements:
        paths = sorted(base_files.keys() | head_files.keys())
        changed = [
            p for p in paths if _kind(p) in names.TABLE_CLASS_KINDS and base_files.get(p) != head_files.get(p)
        ]
        for entry, migration in replacements:
            out += _replacement_findings(root, main_ref, entry, migration, changed)
    return out


def prove(
    base_files: Mapping[str, bytes],
    head_files: Mapping[str, bytes],
    *,
    root: release.StrPath | None = None,
    main_ref: str = release.MAIN_REF,
) -> list[Finding]:
    """Design (b), proof steps 5 and 6, for a change from base_files to head_files.

    Both arguments: path -> file bytes. The new migrations of the chain, in effective order, are
    replayed on the base model; data and raw batches are skipped; the result must be the head
    model. PRF001 names the object and the property of each difference; PRF002 holds what `gen`
    would write when no migration was added; PRF003 is an allow line that only the model can
    check; RAW001 a raw batch for a managed object and RAW002 a raw batch whose text names one;
    WDR001 to WDR003 the withdraw-and-replace
    rules, which read the first-parent history of main_ref in the repository root (A31).
    main_ref is a full commit id or a full ref name that is not a tag (the default is
    release.MAIN_REF); any other text is MAIN_REF_INVALID and nothing of that history is proven.

    PRF007: a new migration alters, renames or drops a column or a table that a schema-bound
    module of the base revision names, and has no `unbind` line for it (plan would refuse the
    merged release with TABLE_BLOCKER). TMB003: a new tombstone for a function that a CHECK, a
    DEFAULT or a computed column of a table-class file still uses.

    WDR004: a merged migration with a model batch is withdrawn and no new line replaces it. A
    database that has not run it never runs it, so the new migrations are replayed on the model
    without it (from the same history) and the table-class files must hold that model; when the
    history cannot show it, nothing is proven and the finding says so. WDR005: a replacement
    names a line that this change does not withdraw. PRF006: the replay accepts the new
    migrations, and plan could not follow one of their renames (diff.touched_objects).

    With table_model = false in the head config there is no proof: one warning, PRF000. When the
    base config says true, the change is PRF005 and the proof runs all the same. Raises
    ToolError when the config, a chain, a new migration file or the table-class files of a
    revision cannot be read; lint reports the same.
    """
    cfg = _config(head_files)
    out: list[Finding] = []
    if not cfg.project.table_model:
        if _base_table_model(base_files) is not True:
            return [_no_proof()]
        out.append(_switched_off())
    return out + _prove(base_files, head_files, cfg, load_model(head_files), root, main_ref)


def _no_proof() -> Finding:
    message = (
        "table_model = false: no model proof. A migration is proven by the first deploy only; lint is "
        "not a safety proof"
    )
    return Finding("PRF000", WARNING, release.CONFIG_PATH, 1, message)


def _switched_off() -> Finding:
    message = (
        "this pull request sets table_model from true to false. That takes away the model proof, the "
        "normal form of the table-class files and the rule for raw batches, for this pull request "
        "first. A pull request cannot do that; this one was checked as with table_model = true"
    )
    return Finding("PRF005", ERROR, release.CONFIG_PATH, 1, message)


def verify(root: release.StrPath, base_sha: str) -> list[Finding]:
    """The pull-request check: every finding of the working tree against the base revision.

    lint.lint_repo (with table_file_check when table_model = true), lint.lint_change, and with
    table_model = true validate_model and the proof. No repeats; sorted by path, line and code.
    A pull request that sets table_model from true to false is PRF005 and is checked as with true.
    base_sha is a full commit id or a full ref name that is not a tag (refs/remotes/origin/main);
    a short name is MAIN_REF_INVALID: git would read a tag of that name first.
    A refusal of a layer below (a base revision that git cannot read, a chain that does not
    parse) is a finding with the reason code of the refusal, so this function raises for a tool
    defect only. The check fails when a finding has severity 'error'.
    """
    out: list[Finding] = []

    def done() -> list[Finding]:
        return sorted(set(out), key=lambda f: (f.path, f.line, f.code, f.message))

    try:
        head_files = read_working_tree(root)
    except ToolError as e:
        return [_error_finding(e)]
    try:
        cfg = _config(head_files)
    except ToolError:
        cfg = None  # lint reports CONFIG_INVALID
    table_model = cfg is not None and cfg.project.table_model
    base_files: dict[str, bytes] | None = None
    try:
        # one exact commit for the base tree and for the history of the withdraw rules
        base_sha = _exact_commit(root, base_sha)
        base_files = release.read_tree(root, base_sha)
    except ToolError as e:
        out.append(_error_finding(e))
    if base_files is not None and cfg is not None and not table_model and _base_table_model(base_files):
        out.append(_switched_off())  # LB-06: the checks of the model stay on for this pull request
        table_model = True
    # one read of the head revision for both rule sets (N5-04)
    parsed = lint.parse_revision(head_files)
    out += lint.lint_repo(
        head_files, table_file_check=table_file_check if table_model else None, parsed=parsed
    )
    if base_files is None:
        return done()
    try:
        out += lint.lint_change(base_files, head_files, parsed=parsed)
    except ToolError as e:
        out.append(_error_finding(e))
        return done()
    if cfg is not None and not table_model:
        out.append(_no_proof())
    elif cfg is not None:
        try:
            head = load_model(head_files)
            out += validate_model(head)
            out += _prove(base_files, head_files, cfg, head, root, base_sha)
        except ToolError as e:
            out.append(_error_finding(e))
    return done()
