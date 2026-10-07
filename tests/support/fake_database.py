"""FakeDatabase: a small Azure SQL Database, as far as azsqlcd can see one. No driver, no network.

FakeSession answers from rules that a test writes. FakeDatabase answers from a state, and it keeps
that state with the knowledge of the tool itself: a DDL batch goes through parse.parse_statement
and replay.apply, a module is stored by lex.module_header, and every catalog query is answered
from the state. So what one part of the tool writes is what another part reads back, and a test
through cli.main closes the loop: gen, verify, build, deploy, drift, export, baseline.

    from support.fake_database import FakeDatabase

    script = state.setup_sql(config, "dev", "sales-dev")      # or the output of `azsqlcd setup-sql`
    db = FakeDatabase("sales", setup_sql=script)              # an empty database after that script
    code = cli.main(["deploy", ...], session_factory=db.session, token_provider=Tokens())
    assert code == 0 and db.unknown == []
    assert db.model == tables.head_model(bundle.files)        # the table-class files of the release
    assert db.module("VIEW:[sales].[vw_x]").sent == file_text
    assert db.rows("step")[-1]["status"] == "ok"

State (plain attributes; a test may read and set them)
    model      model.Model of the table-class objects. Schema dbo always exists.
    modules    folded (schema, name) -> Module (definition: the text as the engine keeps it,
               modules.stored_text of the batch; sent: the batch; flags of the session that sent it)
    tables     the four tables of schema azsqlcd, name -> list of row dicts; None before the
               setup script ran. state_tables holds their declarations, read from that script
    name, server, collation, engine_edition, updateability, case_sensitive, service_objective
    indexed_views    object keys of views that have an index (no DDL of the fake makes one)

Sessions
    db.session() opens a session on the shared state; pass db.session as session_factory. Each
    session has its own SET options, transaction and applock. BEGIN TRANSACTION takes a snapshot of
    model, modules and tables; ROLLBACK, an error with XACT_ABORT ON, and the end of a session
    in a transaction restore it. Identity values are not given back. Two sessions in a transaction
    at the same time are not isolated from each other: the tool never does that.

Failure injection (a batch under SET PARSEONLY ON only meets a rule with on_parse=True)
    fail_on(matcher, error, times=1, keeps_transaction=False, on_parse=False)
    kill_on(matcher, after=False)   the connection dies before the batch runs (after=True: after it)
    before(matcher, action)         action() is called once, directly before the first matching batch
                                    runs: what another client does at that moment (a second deploy)
    respond(matcher, result)        for what the fake does not model, for example a data batch
    hold_lock()                     another session takes the deploy lock; close it to give it back
    out_of_band(*batches)           a DBA changes the database behind the tool: not in `batches`,
                                    no rule applies. Any batch that the fake understands.
  A matcher is a substring, a compiled regex (search) or a callable(batch) -> bool.

Questions
    batches            every batch of every session, in order (out_of_band left out)
    ddl                the batches among them that changed the catalog: table DDL, module text,
                       DROP of a module, sp_refreshsqlmodule
    unknown            heads of the batches that the fake did not understand. Such a batch raises
                       AssertionError, which the tool reports as TOOL_DEFECT: assert unknown == []
    sent(matcher)      the matching batches
    rows(table)        a copy of the rows of one state table, each row a dict of every column
    module(key)        the Module of an object key, or None
    recorded()         state.read_state on a session of its own

What the fake engine checks, beyond replay.apply
    a module needs its schema; a name is taken once in a schema; ALTER and DROP need the module;
    a view and an inline function need the objects they name with two parts (error 208), other
    modules resolve late; a schema-bound module blocks DROP TABLE, DROP / ALTER COLUMN and
    sp_rename; sp_refreshsqlmodule and sys.dm_sql_referenced_entities fail on a column that is
    gone (errors 207 and 2020); DROP TABLE drops the triggers of the table; a write to a state
    table obeys the columns, NOT NULL, lengths, keys and CHECK lists that the setup script declares.
Limits. A module body is not parsed: a reference is a two-part name whose first part is a schema,
or a one-part name after FROM or JOIN that names an object of dbo; a used column is an identifier
of the text that is a column of a referenced table when the module is bound. Rows of user tables
do not exist: a data batch is unknown unless a respond rule answers it. Under SET PARSEONLY ON
only the lexer and the canary shape (SELECT FROM) decide: the fake does not judge T-SQL syntax.
The catalog rows of a table come from unit.test_catalog_tables.rows_from_model, so a capture holds
what that helper writes (for example max_length 0 for int), not what an engine would.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any

from azsqlcd import lex, names, parse, replay, state
from azsqlcd.catalog import APPLOCK_RESOURCE, INDEXED_VIEW, MODULE_TYPES
from azsqlcd.lex import LexError, Tok
from azsqlcd.model import (
    AlterColumn,
    Check,
    CreateIndex,
    CreateTable,
    DropColumn,
    DropSchema,
    DropTable,
    ForeignKey,
    Model,
    PrimaryKey,
    Rename,
    Schema,
    Sequence,
    Synonym,
    Table,
    Unique,
    fold,
)
from azsqlcd.modules import stored_text
from azsqlcd.session import ResultSets
from azsqlcd.sqlerrors import ErrorClass, SqlError, sql_error
from unit.test_catalog_tables import COLUMNS, rows_from_model

Matcher = str | re.Pattern[str] | Callable[[str], bool]
Result = ResultSets | Callable[[str], ResultSets]
type Name = tuple[str, str]  # (schema, name), case-folded
type Rows = list[tuple[Any, ...]]

_IDENT = ("word", "bident", "qident")
_LITERAL = ("string", "nstring")
_TAG = re.compile(r"\s*/\* azsqlcd:([a-z_.]+) \*/")
_BUILTIN_SCHEMAS = ("dbo", "sys", "guest", "INFORMATION_SCHEMA", "db_owner")
_MODULE_WORDS = {"VIEW": "VIEW", "PROCEDURE": "PROCEDURE", "PROC": "PROCEDURE", "FUNCTION": "FUNCTION"}
_MODULE_WORDS["TRIGGER"] = "TRIGGER"
_OPTION_BITS = {  # @@OPTIONS
    "IMPLICIT_TRANSACTIONS": 2,
    "ANSI_WARNINGS": 8,
    "ANSI_PADDING": 16,
    "ANSI_NULLS": 32,
    "ARITHABORT": 64,
    "QUOTED_IDENTIFIER": 256,
    "NOCOUNT": 512,
    "CONCAT_NULL_YIELDS_NULL": 4096,
    "NUMERIC_ROUNDABORT": 8192,
    "XACT_ABORT": 16384,
}
_SESSION_DEFAULTS: dict[str, Any] = dict.fromkeys(_OPTION_BITS, False) | {
    "ANSI_WARNINGS": True,
    "ANSI_PADDING": True,
    "ANSI_NULLS": True,
    "QUOTED_IDENTIFIER": True,
    "CONCAT_NULL_YIELDS_NULL": True,
    "LOCK_TIMEOUT": -1,
    "LANGUAGE": "us_english",
}


# ------------------------------------------------------------------ small helpers
def head(batch: str) -> str:
    """The start of a batch in one line: enough to find it, never the text of a module."""
    return " ".join(batch.split())[:70]


def _matches(matcher: Matcher, batch: str) -> bool:
    if isinstance(matcher, str):
        return matcher in batch
    if isinstance(matcher, re.Pattern):
        return matcher.search(batch) is not None
    return bool(matcher(batch))


def _word(tok: Tok) -> str:
    return tok.text.upper() if tok.kind in ("word", "var") else ""


def _op(tok: Tok, text: str) -> bool:
    return tok.kind == "op" and tok.text == text


def _split(toks: list[Tok], separator: str) -> list[list[Tok]]:
    """The tokens between each separator that is outside parentheses. Empty parts are dropped."""
    parts: list[list[Tok]] = [[]]
    depth = 0
    for tok in toks:
        depth += 1 if _op(tok, "(") else -1 if _op(tok, ")") else 0
        if depth == 0 and _op(tok, separator):
            parts.append([])
        else:
            parts[-1].append(tok)
    return [part for part in parts if part]


def _first(toks: list[Tok], *words: str) -> int:
    """Index of the first of the words outside parentheses; len(toks) when there is none."""
    depth = 0
    for index, tok in enumerate(toks):
        depth += 1 if _op(tok, "(") else -1 if _op(tok, ")") else 0
        if depth == 0 and _word(tok) in words:
            return index
    return len(toks)


def _parts(literal: str) -> tuple[str, ...]:
    """'[schema].[name]' -> its parts, case-folded, as the engine reads a name in a literal."""
    return tuple(fold(tok.value) for tok in lex.significant(lex.tokenize(literal)) if tok.kind in _IDENT)


def _calls(toks: list[Tok], function: str) -> list[tuple[int, str]]:
    """(index, first argument) of every call FUNCTION(N'...' in the tokens."""
    return [
        (index, toks[index + 2].value)
        for index in range(len(toks) - 2)
        if _word(toks[index]) == function and _op(toks[index + 1], "(") and toks[index + 2].kind in _LITERAL
    ]


def _object_names(toks: list[Tok]) -> list[Name]:
    """The two-part names of every OBJECT_ID(N'[s].[n]') in the tokens."""
    found = [_parts(literal) for _, literal in _calls(toks, "OBJECT_ID")]
    return [(parts[0], parts[1]) for parts in found if len(parts) == 2]


def _equal(a: object, b: object) -> bool:
    """Equality as a case-insensitive collation has it."""
    return a.casefold() == b.casefold() if isinstance(a, str) and isinstance(b, str) else a == b


def _engine(text: str, number: int | None = None) -> SqlError:
    return sql_error(text, number=number)


# ------------------------------------------------------------------ data
@dataclass(frozen=True)
class Module:
    """A view, procedure, function or trigger as the catalog holds it."""

    kind: str
    schema: str
    name: str
    type: str  # sys.objects.type: V P FN IF TF TR
    # what sys.sql_modules holds: the batch with the verb as the engine keeps it (live spike L7:
    # CREATE OR ALTER is stored as CREATE, ALTER as CREATE, every other byte as sent)
    definition: str
    uses_ansi_nulls: bool
    uses_quoted_identifier: bool
    is_schema_bound: bool
    # folded (schema, name) of each object the text names -> the columns of it that the text
    # names, as they were when the module was bound. None: the object did not exist then
    references: dict[Name, frozenset[str] | None]
    parent: tuple[str, str] | None = None  # trigger: (schema, table)
    events: tuple[str, ...] = ()  # trigger: INSERT, UPDATE, DELETE
    sent: str = ""  # the batch as it was sent

    @property
    def key(self) -> str:
        return names.object_key(self.kind, self.schema, self.name)


@dataclass
class _Rule:
    matcher: Matcher
    result: Result | None = None
    error: BaseException | None = None
    remaining: int | None = None  # None = no limit
    keeps_transaction: bool = False
    kills: bool = False
    after: bool = False
    on_parse: bool = False
    action: Callable[[], None] | None = None


@dataclass(frozen=True)
class _Snapshot:
    model: Model
    modules: dict[Name, Module]
    tables: dict[str, list[dict[str, Any]]] | None


# ------------------------------------------------------------------ the database
class FakeDatabase:
    def __init__(self, name: str = "sales", setup_sql: str | None = None) -> None:
        self.name = name
        self.server = "fake-server"
        self.collation = "SQL_Latin1_General_CP1_CI_AS"
        self.engine_edition = 5
        self.updateability = "READ_WRITE"
        self.case_sensitive = False
        self.service_objective = "GP_S_Gen5_2"
        self.model = Model()
        self.modules: dict[Name, Module] = {}
        self.tables: dict[str, list[dict[str, Any]]] | None = None
        self.state_tables: dict[str, Table] = {}  # schema azsqlcd, as the setup script declares it
        self.indexed_views: set[str] = set()
        self.batches: list[str] = []
        self.ddl: list[str] = []
        self.unknown: list[str] = []
        self.sessions: list[FakeDbSession] = []
        self.lock_holder: FakeDbSession | None = None
        self._rules: list[_Rule] = []
        self._identity: dict[str, int] = {}
        self._transactions = 1000
        self._clock = 0
        if setup_sql is not None:
            self.run_setup(setup_sql)

    # -------------------------------------------------------------- set up, sessions, rules
    def run_setup(self, script: str) -> None:
        """What is left when an administrator ran the output of `azsqlcd setup-sql` (state.setup_sql).

        Taken from the script, with the parser of the tool: the CREATE TABLE statements of schema
        azsqlcd, the unique index, and the INSERT of the meta row (once). The columns, lengths,
        keys and CHECK lists of these tables are then the rules for every state write. The
        guards, the users and the grants of the script are not run.
        """
        tables = self.tables = self.tables if self.tables is not None else {}
        reader = FakeDbSession(self, 97, recorded=False)
        for batch in lex.split_batches(script):
            toks = lex.significant(lex.tokenize(batch.text))
            words = [_word(tok) for tok in toks]
            create = next((i for i, w in enumerate(words) if w == "CREATE" and words[i + 1] != "USER"), None)
            insert = next((i for i, w in enumerate(words) if w == "INSERT" and words[i + 1] == "INTO"), None)
            if insert is not None:
                end = next(i for i, tok in enumerate(toks) if _op(tok, ";"))
                if not tables["meta"]:  # the script writes the meta row once
                    reader.execute(batch.text[toks[insert].pos : toks[end].pos])
            elif create is not None and words[create + 1] in ("TABLE", "UNIQUE"):
                try:
                    op = parse.parse_statement(batch.text[toks[create].pos :])
                except parse.ParseError as error:
                    raise AssertionError(f"the fake cannot read the setup script: {error}") from None
                if isinstance(op, CreateTable):
                    assert op.table.schema == state.SCHEMA, "the setup script makes tables in azsqlcd only"
                    self.state_tables[op.table.name] = op.table
                    tables.setdefault(op.table.name, [])
                elif isinstance(op, CreateIndex):
                    table = self.state_tables[op.table]
                    self.state_tables[op.table] = replace(table, indexes=(*table.indexes, op.index))
        assert set(self.state_tables) == set(state.TABLES) and tables["meta"], "not a setup script"

    def session(self) -> FakeDbSession:
        """A new session. Give this method as session_factory."""
        found = FakeDbSession(self, 51 + len(self.sessions))
        self.sessions.append(found)
        return found

    def respond(self, matcher: Matcher, result: Result) -> None:
        self._rules.append(_Rule(matcher, result=result))

    def fail_on(
        self,
        matcher: Matcher,
        error: BaseException,
        times: int = 1,
        *,
        keeps_transaction: bool = False,
        on_parse: bool = False,
    ) -> None:
        self._rules.append(
            _Rule(
                matcher,
                error=error,
                remaining=times,
                keeps_transaction=keeps_transaction,
                on_parse=on_parse,
            )
        )

    def kill_on(self, matcher: Matcher, *, after: bool = False) -> None:
        self._rules.append(_Rule(matcher, kills=True, after=after, remaining=1))

    def before(self, matcher: Matcher, action: Callable[[], None]) -> None:
        """Call action once, directly before the first matching batch runs. The batch then runs as
        it would with no rule. The action is the other client: it can open sessions of this database."""
        self._rules.append(_Rule(matcher, action=action, remaining=1))

    def rule_for(self, batch: str, parse_only: bool) -> _Rule | None:
        for rule in self._rules:
            if rule.remaining != 0 and rule.on_parse == parse_only and _matches(rule.matcher, batch):
                if rule.remaining is not None:
                    rule.remaining -= 1
                return rule
        return None

    def hold_lock(self) -> FakeDbSession:
        """Another session that holds the deploy lock, as a run that is live. close() ends it."""
        other = FakeDbSession(self, 90, recorded=False)
        assert self.lock_holder is None or self.lock_holder.closed, "the deploy lock is taken"
        self.lock_holder = other
        return other

    def out_of_band(self, *batches: str) -> None:
        """Run batches as somebody else would: nothing is recorded and no rule applies."""
        other = FakeDbSession(self, 99, recorded=False)
        try:
            for batch in batches:
                other.execute(batch)
        finally:
            other.close()

    # -------------------------------------------------------------- questions
    def sent(self, matcher: Matcher) -> list[str]:
        return [batch for batch in self.batches if _matches(matcher, batch)]

    def rows(self, table: str) -> list[dict[str, Any]]:
        """A copy of the rows of one table of schema azsqlcd."""
        assert self.tables is not None, "the database has no schema azsqlcd"
        return copy.deepcopy(self.tables[table])

    def module(self, key: str) -> Module | None:
        _, schema, name = names.parse_object_key(key)
        found = self.modules.get((fold(schema or ""), fold(name)))
        return found if found is not None and fold(found.key) == fold(key) else None

    def recorded(self) -> state.State:
        """state.read_state on a session of its own: what the tool would read now."""
        reader = FakeDbSession(self, 98, recorded=False)
        try:
            return state.read_state(reader)
        finally:
            reader.close()

    # -------------------------------------------------------------- clock and counters
    def now(self) -> str:
        """Server time, ISO 8601 with milliseconds. Each call is one millisecond later."""
        self._clock += 1
        ms = self._clock
        return f"2026-10-07T09:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"

    def next_transaction(self) -> int:
        self._transactions += 1
        return self._transactions

    def snapshot(self) -> _Snapshot:
        return _Snapshot(self.model, dict(self.modules), copy.deepcopy(self.tables))

    def restore(self, saved: _Snapshot) -> None:
        self.model, self.modules, self.tables = saved.model, dict(saved.modules), copy.deepcopy(saved.tables)

    # -------------------------------------------------------------- the catalog, from the state
    def schema_names(self) -> dict[str, str]:
        """Folded name -> name of every schema that an object can be created in."""
        found = {"dbo": "dbo"}
        return found | {fold(obj.name): obj.name for obj in self.model.values() if isinstance(obj, Schema)}

    def objects(self) -> dict[Name, tuple[str, str, str]]:
        """sys.objects: folded (schema, name) -> (schema, name, type) of everything outside azsqlcd."""
        found: dict[Name, tuple[str, str, str]] = {}

        def add(schema: str, name: str | None, code: str) -> None:
            if name is not None:
                found[fold(schema), fold(name)] = (schema, name, code)

        for obj in self.model.values():
            if isinstance(obj, Table):
                add(obj.schema, obj.name, "U")
                for constraint in obj.constraints:
                    code = {PrimaryKey: "PK", Unique: "UQ", ForeignKey: "F", Check: "C"}[type(constraint)]
                    add(obj.schema, constraint.name, code)
                for column in obj.columns:
                    if column.default is not None:
                        add(obj.schema, column.default.name, "D")
            elif isinstance(obj, Sequence):
                add(obj.schema, obj.name, "SO")
            elif isinstance(obj, Synonym):
                add(obj.schema, obj.name, "SN")
        for module in self.modules.values():
            add(module.schema, module.name, module.type)
        return found

    def table(self, name: Name) -> Table | None:
        found = self.model.get(names.object_key("TABLE", *name))
        return found if isinstance(found, Table) else None

    def _has_index(self, table: Table, name: str) -> bool:
        keys = [c.name for c in table.constraints if isinstance(c, PrimaryKey | Unique)]
        return any(fold(found) == fold(name) for found in (*keys, *(i.name for i in table.indexes)) if found)

    def users_of(self, name: Name, column: str | None = None, *, bound_only: bool = False) -> list[Module]:
        """The modules that name the object (and, with a column, use that column), in key order."""
        found = [
            module
            for module in self.modules.values()
            if name in module.references
            and (not bound_only or module.is_schema_bound)
            and (column is None or fold(column) in (module.references[name] or ()))
        ]
        return sorted(found, key=lambda module: module.key)

    def problems(self, module: Module) -> tuple[list[Name], list[str]]:
        """(objects that the module names and that do not exist, used columns that are gone)."""
        existing = self.objects()
        missing = sorted(name for name in module.references if name not in existing)
        gone: list[str] = []
        for name, columns in module.references.items():
            table = self.table(name)
            if table is not None and columns:
                gone += sorted(columns - {fold(column.name) for column in table.columns})
        return missing, gone

    # -------------------------------------------------------------- modules
    def bind(self, module: Module) -> Module:
        """The module with the references that its text has now. A view needs every object it names."""
        try:
            toks = lex.significant(lex.tokenize(module.definition))
        except LexError as error:
            raise AssertionError(f"the fake cannot lex a module: {head(module.definition)}") from error
        schemas, existing = self.schema_names(), self.objects()
        idents = {fold(tok.value) for tok in toks if tok.kind in _IDENT}
        own = (fold(module.schema), fold(module.name))
        named: set[Name] = set()
        index = 0
        while index < len(toks):
            if toks[index].kind not in _IDENT:
                index += 1
                continue
            start = index
            parts = [fold(toks[index].value)]
            index += 1
            while index + 1 < len(toks) and _op(toks[index], ".") and toks[index + 1].kind in _IDENT:
                parts.append(fold(toks[index + 1].value))
                index += 2
            if start and _op(toks[start - 1], "."):
                continue  # @x.method: never an object name
            if len(parts) > 1 and parts[0] in schemas:
                named.add((parts[0], parts[1]))
            elif len(parts) == 1 and start and _word(toks[start - 1]) in ("FROM", "JOIN"):
                if ("dbo", parts[0]) in existing:
                    named.add(("dbo", parts[0]))
        named.discard(own)
        references: dict[Name, frozenset[str] | None] = {}
        for name in sorted(named):
            table = self.table(name)
            columns = [fold(c.name) for c in table.columns] if table is not None else []
            references[name] = frozenset(set(columns) & idents) if name in existing else None
        missing = [name for name, columns in references.items() if columns is None]
        if missing and module.type in ("V", "IF"):  # no late name resolution for these
            raise _engine(f"Invalid object name '{missing[0][0]}.{missing[0][1]}'.")
        return replace(module, references=references)

    def store_module(self, header: lex.ModuleHeader, text: str, ansi_nulls: bool, quoted: bool) -> None:
        schema = self.schema_names().get(fold(header.schema or "dbo"))
        if schema is None:
            raise _engine(
                f'The specified schema name "{header.schema}" either does not exist or you do not have '
                "permission to use it.",
                2760,
            )
        own = (fold(schema), fold(header.name))
        old, taken = self.modules.get(own), self.objects().get(own)
        if old is None and header.verb == "ALTER":
            raise _engine(f"Invalid object name '{schema}.{header.name}'.")
        if (old is not None and header.verb == "CREATE") or (old is None and taken is not None):
            raise _engine(f"There is already an object named '{header.name}' in the database.")
        toks = lex.significant(lex.tokenize(text))
        first = next((i for i, tok in enumerate(toks) if tok.pos >= header.name_span[1]), len(toks))
        words = [_word(tok) for tok in toks]
        body = next(
            (
                i
                for i in range(first, len(toks))
                if words[i] == "AS" and toks[i - 1].kind != "var" and words[i - 1] not in ("EXECUTE", "EXEC")
            ),
            len(toks),
        )
        code = {"VIEW": "V", "PROCEDURE": "P", "TRIGGER": "TR", "FUNCTION": "FN"}[header.kind]
        if header.kind == "FUNCTION" and "RETURNS" in words[first:]:
            returns = toks[words.index("RETURNS", first) + 1]
            code = "TF" if returns.kind == "var" else "IF" if _word(returns) == "TABLE" else "FN"
        if old is not None and MODULE_TYPES[old.type] != header.kind:
            raise _engine(f"There is already an object named '{header.name}' in the database.")
        parent: tuple[str, str] | None = None
        events: tuple[str, ...] = ()
        if header.kind == "TRIGGER":
            on = [tok.value for tok in toks[first + 1 : first + 4] if tok.kind in _IDENT]
            table = self.table((fold(on[0]), fold(on[1]))) if len(on) == 2 else None
            if words[first] != "ON" or table is None:
                raise _engine(f'Cannot find the object "{".".join(on)}" because it does not exist.', 8197)
            parent = (table.schema, table.name)
            events = tuple(word for word in words[first:body] if word in ("INSERT", "UPDATE", "DELETE"))
        module = Module(
            header.kind,
            schema,
            header.name if old is None else old.name,
            code,
            stored_text(text),
            ansi_nulls,
            quoted,
            "SCHEMABINDING" in words[first:body],
            {},
            parent,
            events,
            text,
        )
        self.modules[own] = self.bind(module)

    def drop_module(self, kind: str, name: Name) -> None:
        module = self.modules.get(name)
        if module is None or module.kind != kind:
            raise _engine(
                f"Cannot drop the {kind.lower()} '{'.'.join(name)}', because it does not exist or you do "
                "not have permission.",
                3701,
            )
        bound = [other for other in self.users_of(name, bound_only=True) if other is not module]
        if bound:
            raise _engine(
                f"Cannot DROP {kind} '{'.'.join(name)}' because it is being referenced by object "
                f"'{bound[0].name}'.",
                3729,
            )
        del self.modules[name]

    def refresh_module(self, literal: str) -> None:
        parts = _parts(literal)
        module = self.modules.get((parts[0], parts[1])) if len(parts) == 2 else None
        if module is None:
            raise _engine(f"Could not find object '{literal}' or you do not have permission.", 15165)
        missing, gone = self.problems(module)
        if gone:
            raise _engine(f"Invalid column name '{gone[0]}'.", 207)
        if missing and module.type in ("V", "IF", "TF"):
            raise _engine(f"Invalid object name '{'.'.join(missing[0])}'.")
        self.modules[parts[0], parts[1]] = self.bind(module)

    # -------------------------------------------------------------- table DDL
    def apply_ddl(self, batch: str) -> None:
        """One statement of the closed grammar of the tool, with the checks of replay.apply."""
        try:
            op = parse.parse_statement(batch)
        except parse.ParseError as error:
            raise AssertionError(
                f"the fake cannot read a DDL batch ({error.code}, line {error.line}): {head(batch)}"
            ) from None
        self._refuse_bound(op)
        if isinstance(op, DropSchema) and any(fold(m.schema) == fold(op.name) for m in self.modules.values()):
            raise _engine(
                f"Cannot drop schema '{op.name}' because it is being referenced by an object.", 3729
            )
        try:
            model = replay.apply(self.model, op)
        except replay.ReplayError as error:
            message = f"fake engine: the statement was refused: {error.object_key}: {error.message}"
            raise SqlError(message) from None
        for obj in model.values():  # the model does not know modules; the engine has one namespace
            new = obj.key not in self.model and isinstance(obj, Table | Sequence | Synonym)
            if new and (fold(obj.schema), fold(obj.name)) in self.modules:
                raise _engine(f"There is already an object named '{obj.name}' in the database.")
        self.model = model
        if isinstance(op, DropTable):  # the engine drops the triggers of a table with the table
            gone = (fold(op.schema), fold(op.name))
            triggers = [
                n
                for n, m in self.modules.items()
                if m.parent and (fold(m.parent[0]), fold(m.parent[1])) == gone
            ]
            for name in triggers:
                del self.modules[name]

    def _refuse_bound(self, op: object) -> None:
        """A schema-bound module blocks what would change the object under it."""
        name: Name | None = None
        column: str | None = None
        if isinstance(op, DropTable):
            name = (fold(op.schema), fold(op.name))
        elif isinstance(op, DropColumn | AlterColumn):
            name, column = (fold(op.schema), fold(op.table)), op.column
        elif isinstance(op, Rename) and op.kind in ("table", "column", "object"):
            name = (fold(op.old[0]), fold(op.old[1]))
            column = op.old[2] if op.kind == "column" else None
        bound = self.users_of(name, column, bound_only=True) if name is not None else []
        if not bound:
            return
        if isinstance(op, Rename):
            raise _engine(
                f"Object '{'.'.join(op.old)}' cannot be renamed because the object participates in "
                "enforced dependencies.",
                15336,
            )
        if column is None:
            raise _engine(
                f"Cannot DROP TABLE '{'.'.join(name or ())}' because it is being referenced by object "
                f"'{bound[0].name}'.",
                3729,
            )
        raise _engine(f"The object '{bound[0].name}' is dependent on column '{column}'.", 5074)

    # -------------------------------------------------------------- catalog queries (by tag)
    def query(self, tag: str) -> Callable[[list[Tok]], Rows] | None:
        if tag.startswith("read_") and tag.removeprefix("read_") in COLUMNS:
            return lambda toks: self._q_read_model(tag.removeprefix("read_"), toks)
        return getattr(self, "_q_" + tag.replace(".", "_"), None)

    def _q_fence_facts(self, toks: list[Tok]) -> Rows:
        facts = (self.engine_edition, self.updateability, self.name, self.collation, int(self.case_sensitive))
        return [facts]

    def _q_read_model(self, query: str, toks: list[Tok]) -> Rows:
        """The eleven queries of catalog_tables.read_model, for everything or for the asked ids."""
        rows = rows_from_model(self.model)
        objects = set(_object_names(toks))
        types = {_parts(literal) for _, literal in _calls(toks, "TYPE_ID")}
        schemas = {fold(literal) for _, literal in _calls(toks, "SCHEMA_ID")}
        everything = not (objects or types or schemas)

        def named(row: dict[str, Any], asked: set[Any]) -> bool:
            return everything or (fold(row["schema_name"]), fold(row["name"])) in asked

        heads = [row for row in rows["tabulars"] if named(row, objects if row["kind"] == "TABLE" else types)]
        if query == "schemas":
            counts: dict[str, int] = {}
            for module in self.modules.values():
                counts[fold(module.schema)] = counts.get(fold(module.schema), 0) + 1
            owners = dict.fromkeys(_BUILTIN_SCHEMAS, "dbo")
            owners |= {o.name: o.owner or "dbo" for o in self.model.values() if isinstance(o, Schema)}
            found = [
                {"name": name, "owner_name": owner, "module_count": counts.get(fold(name), 0)}
                for name, owner in owners.items()
                if everything or fold(name) in schemas
            ]
        elif query == "tabulars":
            found = heads
        elif query in ("alias_types", "sequences", "synonyms"):
            found = [row for row in rows[query] if named(row, types if query == "alias_types" else objects)]
        else:  # the first column of a child row is the object_id of its table
            parents = {row["object_id"] for row in heads}
            found = [row for row in rows[query] if next(iter(row.values())) in parents]
        for row in found:
            assert tuple(row) == COLUMNS[query], f"a {query} row does not have the columns of the contract"
        return [tuple(row.values()) for row in found]

    def _module_row(self, requested: str | None, module: Module) -> tuple[Any, ...]:
        trigger = module.kind == "TRIGGER"
        return (
            requested,
            module.schema,
            module.name,
            module.type,
            module.definition,
            int(module.uses_ansi_nulls),
            int(module.uses_quoted_identifier),
            int(module.is_schema_bound),
            None,
            1,
            module.parent[0] if module.parent else None,
            module.parent[1] if module.parent else None,
            0 if trigger else None,
            ";".join(f"{event}:0:0" for event in module.events) if trigger else None,
        )

    def _requested(self, toks: list[Tok]) -> list[tuple[str, Name]]:
        """(asked key, name) of each (N'<key>', OBJECT_ID(N'[s].[n]')) of a VALUES list."""
        found: list[tuple[str, Name]] = []
        for index, literal in _calls(toks, "OBJECT_ID"):
            parts = _parts(literal)
            if index >= 2 and toks[index - 2].kind in _LITERAL and len(parts) == 2:
                found.append((toks[index - 2].value, (parts[0], parts[1])))
        return found

    def _q_capture_modules(self, toks: list[Tok]) -> Rows:
        if not any(_word(tok) == "VALUES" for tok in toks):
            return [self._module_row(None, module) for module in self.modules.values()]
        asked = self._requested(toks)
        return [self._module_row(key, self.modules[name]) for key, name in asked if name in self.modules]

    def _q_list_user_objects(self, toks: list[Tok]) -> Rows:
        return sorted(self.objects().values())

    def _q_object_exists(self, toks: list[Tok]) -> Rows:
        (name,) = _object_names(toks)
        codes = {tok.value for tok in toks if tok.kind in _LITERAL}
        found = self.objects().get(name)
        return [(int(found is not None and found[2] in codes),)]

    def _indexed(self) -> list[Module]:
        keys = {fold(key) for key in self.indexed_views}
        return [module for module in self.modules.values() if fold(module.key) in keys]

    def _q_has_index(self, toks: list[Tok]) -> Rows:
        (name,) = _object_names(toks)
        return [(int(any((fold(m.schema), fold(m.name)) == name for m in self._indexed())),)]

    def _q_indexed_views(self, toks: list[Tok]) -> Rows:
        return [(module.schema, module.name) for module in self._indexed()]

    def _q_export_facts(self, toks: list[Tok]) -> Rows:
        return [(INDEXED_VIEW, module.schema, module.name, "V") for module in self._indexed()]

    def _q_numbered_procedures(self, toks: list[Tok]) -> Rows:
        return []

    def _q_history_tables(self, toks: list[Tok]) -> Rows:
        """(history schema, history table, schema, table) of each system-versioned table of the model.
        The fake keeps no object for a history table: only its name, in the model of its table."""
        found = [
            (obj, getattr(obj, "temporal", None)) for obj in self.model.values() if isinstance(obj, Table)
        ]
        return sorted((t.history_schema, t.history_table, obj.schema, obj.name) for obj, t in found if t)

    def _q_dependants_of(self, toks: list[Tok]) -> Rows:
        existing = self.objects()
        found = {
            (module.schema, module.name, module.type, int(module.is_schema_bound))
            for name in _object_names(toks)
            if name in existing  # OBJECT_ID of a name that is gone is NULL
            for module in self.users_of(name)
        }
        return sorted(found)

    def _q_broken_references(self, toks: list[Tok]) -> Rows:
        literal = next(tok.value for tok in toks if tok.kind in _LITERAL)
        parts = _parts(literal)
        module = self.modules.get((parts[0], parts[1])) if len(parts) == 2 else None
        if module is None:
            return []
        missing, gone = self.problems(module)
        if gone:
            raise _engine(
                f'The dependencies reported for entity "{module.schema}.{module.name}" might not include '
                "references to all columns. This is either because the entity references an object that "
                "does not exist or because of an error in one or more statements in the entity."
            )
        existing = self.objects()
        rows: Rows = []
        for name in module.references:
            found = existing.get(name)
            schema, entity = (found[0], found[1]) if found else name
            # the last column: sys.objects.type of the referenced object, NULL for a name with no object
            code = found[2] if found else None
            rows.append((schema, entity, None, None if name in missing else 1, 0, 1, 0, code))
        return rows

    def _q_table_facts(self, toks: list[Tok]) -> Rows:
        return [(key, 0, 0) for key, name in self._requested(toks) if self.table(name) is not None]

    def _q_table_blockers(self, toks: list[Tok]) -> Rows:
        name = _object_names(toks)[0]
        table = self.table(name)
        if table is None:
            return []
        at = next((i for i, tok in enumerate(toks) if _word(tok) == "COLUMNPROPERTY"), None)
        column = None if at is None else next(t.value for t in toks[at + 5 :] if t.kind in _LITERAL)
        rows: set[tuple[Any, ...]] = set()
        tables = [obj for obj in self.model.values() if isinstance(obj, Table)]
        for other in tables:
            for key in (c for c in other.constraints if isinstance(c, ForeignKey)):
                refers = (fold(key.ref_schema), fold(key.ref_table)) == name
                if column is None:
                    hit = refers and other is not table
                else:
                    hit = (other is table and fold(column) in map(fold, key.columns)) or (
                        refers and fold(column) in map(fold, key.ref_columns)
                    )
                if hit:
                    rows.add(("FOREIGN KEY", other.schema, other.name, key.name))
        if column is not None:
            keyed = [c for c in table.constraints if isinstance(c, PrimaryKey | Unique)]
            for item in (*keyed, *table.indexes):
                used = [c.name for c in item.columns] + list(getattr(item, "included", ()))
                if fold(column) in map(fold, used):
                    rows.add(("INDEX", table.schema, table.name, item.name))
        for module in self.users_of(name, column, bound_only=True):
            rows.add(("MODULE", module.schema, module.name, module.type))
        return sorted(rows)

    def _q_names_present(self, toks: list[Tok]) -> Rows:
        """(n) of each row (n, <id expression>) of the VALUES list whose expression is not NULL."""
        existing, schemas = self.objects(), self.schema_names()
        types = {(fold(o.schema), fold(o.name)) for o in self.model.values() if o.kind == "TYPE" and o.schema}
        rows: Rows = []
        for index in range(len(toks) - 5):
            tok, function = toks[index + 1], _word(toks[index + 3])
            if not (_op(toks[index], "(") and tok.kind == "number" and _op(toks[index + 2], ",")):
                continue
            literals = [found.value for found in toks[index + 4 : index + 12] if found.kind in _LITERAL]
            if function == "SCHEMA_ID":  # the one function that takes a bare name
                present = fold(literals[0]) in schemas or fold(literals[0]) in map(fold, _BUILTIN_SCHEMAS)
            elif function == "OBJECT_ID":
                present = _parts(literals[0]) in existing
            elif function == "TYPE_ID":
                present = _parts(literals[0]) in types
            elif function == "INDEXPROPERTY":
                schema, name = _parts(literals[0])
                table = self.table((schema, name))
                present = table is not None and self._has_index(table, literals[1])
            else:
                raise AssertionError(f"names_present asks {function}, which the fake does not know")
            if present:
                rows.append((int(tok.text),))
        return rows

    def _q_sub_object(self, toks: list[Tok]) -> Rows:
        (name,) = set(_object_names(toks))
        index = [tok.value for tok in toks if tok.kind in _LITERAL][1]
        table = self.table(name)
        return [(int(table is not None and self._has_index(table, index)), 0)]

    # -------------------------------------------------------------- state queries (by tag)
    def _state(self, table: str) -> list[dict[str, Any]]:
        if self.tables is None:
            raise _engine(f"Invalid object name '{state.SCHEMA}.{table}'.")
        return self.tables[table]

    def _q_read_state_tables(self, toks: list[Tok]) -> Rows:
        return [(table, 1) for table in self.tables or ()]

    def _q_read_state_meta(self, toks: list[Tok]) -> Rows:
        return [(r["state_version"], r["project"], r["environment"]) for r in self._state("meta")]

    def _q_read_state_runs(self, toks: list[Tok]) -> Rows:
        runs = self._state("run")

        def top(found: list[dict[str, Any]]) -> int | None:
            best = max(found, key=lambda run: (run["release_seq"], run["run_id"]), default=None)
            return best["run_id"] if best else None

        ok = top([r for r in runs if r["status"] == "ok" and r["command"] in state.RELEASE_COMMANDS])
        committed = top([r for r in runs if r["command"] == "deploy" and r["segments_committed"] > 0])
        columns = (
            "run_id",
            "command",
            "status",
            "segments_committed",
            "release_seq",
            "git_sha",
            "started_utc",
        )
        return [
            tuple(run[column] for column in columns)
            for run in sorted(runs, key=lambda run: run["run_id"])
            if run["status"] in ("running", "unknown") or run["run_id"] in (ok, committed)
        ]

    def _q_read_state_steps(self, toks: list[Tok]) -> Rows:
        columns = ("step_id", "run_id", "kind", "migration_id", "file_sha256", "status", "note")
        steps = sorted(self._state("step"), key=lambda step: step["step_id"])
        return [tuple(step[column] for column in columns) for step in steps]

    def _q_read_state_objects(self, toks: list[Tok]) -> Rows:
        columns = (
            "object_key",
            "status",
            "source_sha256",
            "capture_format",
            "catalog_capture",
            "catalog_sha256",
        )
        found = sorted(self._state("object"), key=lambda row: row["object_key"])
        return [tuple(row[column] for column in columns) for row in found]

    def _run_column(self, toks: list[Tok], column: str) -> Rows:
        run_id = int([tok.text for tok in toks if tok.kind == "number"][-1])
        return [(run[column],) for run in self._state("run") if run["run_id"] == run_id]

    def _q_read_fence(self, toks: list[Tok]) -> Rows:
        return self._run_column(toks, "segments_committed")

    _q_read_fence_locking = _q_read_fence

    def _q_run_started(self, toks: list[Tok]) -> Rows:
        return self._run_column(toks, "started_utc")

    # -------------------------------------------------------------- state writes
    def check_row(self, table: str, row: dict[str, Any]) -> None:
        """The rules of the table as the setup script declares it: NOT NULL, lengths, keys, CHECK."""
        rows, declared = self._state(table), self.state_tables[table]
        where = f"table '{state.SCHEMA}.{table}'"

        def same(columns: Iterable[str], other: dict[str, Any], values: dict[str, Any]) -> bool:
            return all(values[c] is not None and _equal(other[c], values[c]) for c in columns)

        for column in declared.columns:
            value, length = row[column.name], column.type.length if column.type else None
            if value is None and column.nullable is False:
                raise _engine(f"Cannot insert the value NULL into column '{column.name}', {where}.", 515)
            if isinstance(value, str) and isinstance(length, int):
                if len(value.encode("utf-16-le", "surrogatepass")) // 2 > length:
                    raise _engine(f"String or binary data would be truncated in {where}.", 2628)
        unique = [
            [k.name for k in c.columns] for c in declared.constraints if isinstance(c, PrimaryKey | Unique)
        ]
        unique += [[k.name for k in index.columns] for index in declared.indexes if index.unique]
        for columns in unique:
            if any(other is not row and same(columns, other, row) for other in rows):
                raise _engine(
                    f"Cannot insert duplicate key row in object '{state.SCHEMA}.{table}' with unique "
                    f"index on {', '.join(columns)}. The duplicate key value is ({row[columns[0]]}).",
                    2601,
                )
        for constraint in declared.constraints:
            if isinstance(constraint, ForeignKey):
                wanted = {
                    ref: row[own] for own, ref in zip(constraint.columns, constraint.ref_columns, strict=True)
                }
                if not any(same(wanted, other, wanted) for other in self._state(constraint.ref_table)):
                    raise _engine(
                        f'The statement conflicted with the FOREIGN KEY constraint "{constraint.name}".', 547
                    )
            elif isinstance(constraint, Check):  # [column] = <value>, or [column] IN (<values>)
                toks = lex.significant(lex.tokenize(" ".join(constraint.expression.tokens)))
                column = next(tok.value for tok in toks if tok.kind == "bident")
                allowed = [tok.value for tok in toks if tok.kind in _LITERAL]
                allowed += [int(tok.text) for tok in toks if tok.kind == "number"]
                if row[column] not in allowed:
                    raise _engine(
                        f'The statement conflicted with the CHECK constraint "{constraint.name}". The '
                        f"conflict occurred in {where}, column '{column}'.",
                        547,
                    )


# ------------------------------------------------------------------ one session
class FakeDbSession:
    """azsqlcd.session.Session on a FakeDatabase."""

    def __init__(self, db: FakeDatabase, spid: int, *, recorded: bool = True) -> None:
        self.db = db
        self.spid = spid
        self.recorded = recorded  # False: out of band; no rule applies and nothing is listed
        self.closed = False
        self.batches: list[str] = []
        self.options = dict(_SESSION_DEFAULTS)
        self.parse_only = False
        self.trancount = 0
        self.transaction_id = 0
        self.rowcount = 0
        self.identity: int | None = None
        self._undo: _Snapshot | None = None

    # -------------------------------------------------------------- Session
    def execute(self, batch: str) -> ResultSets:
        db = self.db
        if self.closed:
            raise SqlError("fake: the session is closed", cls=ErrorClass.SESSION_LOST)
        self.batches.append(batch)
        rule = None
        if self.recorded:
            db.batches.append(batch)
            rule = db.rule_for(batch, self.parse_only)
        if rule is not None and rule.action is not None:
            rule.action()
        try:
            if rule is not None and rule.kills and not rule.after:
                self._die()
            if rule is not None and rule.error is not None:
                raise rule.error
            if rule is not None and rule.result is not None:
                answer = rule.result(batch) if callable(rule.result) else rule.result
            else:
                answer = self._run(batch)
            if rule is not None and rule.kills:
                self._die()
        except SqlError:
            keep = rule is not None and rule.keeps_transaction
            if not self.closed and self.trancount and self.options["XACT_ABORT"] and not keep:
                self._rollback()
            raise
        except AssertionError:
            db.unknown.append(head(batch))
            raise
        return [list(result_set) for result_set in answer]

    def close(self) -> None:
        """The end of the session: the engine rolls an open transaction back and frees the lock."""
        if self.closed:
            return
        self.closed = True
        if self.trancount:
            self._rollback()
        if self.db.lock_holder is self:
            self.db.lock_holder = None

    def _die(self) -> None:
        self.close()
        raise sql_error("fake: Communication link failure", sqlstate="08S01")

    # -------------------------------------------------------------- transactions
    def _begin(self) -> None:
        if self.trancount == 0:
            self._undo = self.db.snapshot()
            self.transaction_id = self.db.next_transaction()
        self.trancount += 1

    def _commit(self) -> None:
        if self.trancount == 0:
            raise _engine("The COMMIT TRANSACTION request has no corresponding BEGIN TRANSACTION.", 3902)
        self.trancount -= 1
        if self.trancount == 0:
            self._undo = None

    def _rollback(self) -> None:
        if self.trancount == 0:
            raise _engine("The ROLLBACK TRANSACTION request has no corresponding BEGIN TRANSACTION.", 3903)
        if self._undo is not None:
            self.db.restore(self._undo)
        self.trancount, self._undo = 0, None

    def _implicit(self) -> None:
        """SET IMPLICIT_TRANSACTIONS ON: a statement that reads or writes a table opens a transaction."""
        if self.options["IMPLICIT_TRANSACTIONS"] and self.trancount == 0:
            self._begin()

    def _lock_is_free(self) -> bool:
        holder = self.db.lock_holder
        return holder is None or holder is self or holder.closed

    # -------------------------------------------------------------- one batch
    def _run(self, batch: str) -> ResultSets:
        db = self.db
        try:
            toks = lex.significant(lex.tokenize(batch))
        except LexError as error:
            raise AssertionError(f"the fake cannot lex a batch ({error}): {head(batch)}") from None
        words = [_word(tok) for tok in toks]
        if self.parse_only:
            return self._parse(words)
        tag = _TAG.match(batch)
        if tag is not None and tag[1] == "lock":
            return [[(self._take_lock(toks),)]]
        query = db.query(tag[1]) if tag is not None else None
        if query is not None:
            self._implicit()
            return [query(toks)]
        try:
            header = lex.module_header(batch)
        except LexError:
            header = None
        changed = True
        if header is not None:
            self._implicit()
            db.store_module(header, batch, self.options["ANSI_NULLS"], self.options["QUOTED_IDENTIFIER"])
        elif words[:1] == ["DROP"] and words[1:2] and words[1] in _MODULE_WORDS:
            self._implicit()
            parts = [fold(tok.value) for tok in toks[2:] if tok.kind in _IDENT]
            assert len(parts) == 2, f"the fake drops a module by its two-part name: {head(batch)}"
            db.drop_module(_MODULE_WORDS[words[1]], (parts[0], parts[1]))
        elif words[:1] in (["CREATE"], ["ALTER"], ["DROP"]) or (
            words[:1] in (["EXEC"], ["EXECUTE"]) and any(fold(tok.value) == "sp_rename" for tok in toks[:4])
        ):
            self._implicit()
            db.apply_ddl(batch)
        else:
            changed = False
        if changed:
            if self.recorded:
                db.ddl.append(batch)
            return []
        results: ResultSets = []
        for statement in _split(toks, ";"):
            found = self._statement(statement, batch)
            if found is not None:
                results.append(found)
        return results

    def _parse(self, words: list[str]) -> ResultSets:
        """SET PARSEONLY ON: nothing runs. The one syntax rule here is the shape of the canary."""
        if words[:3] == ["SET", "PARSEONLY", "OFF"]:
            self.parse_only = False
        elif any(a == "SELECT" and b == "FROM" for a, b in zip(words, words[1:], strict=False)):
            raise _engine("Incorrect syntax near the keyword 'FROM'.", 156)
        return []

    def _take_lock(self, toks: list[Tok]) -> int:
        resource = next(tok.value for tok in toks if tok.kind in _LITERAL)
        assert resource == APPLOCK_RESOURCE, f"the fake knows one applock, not {resource!r}"
        if not self._lock_is_free():
            return -1  # the wait ended and the lock was not granted
        self.db.lock_holder = self
        return 0

    # -------------------------------------------------------------- statements of a state batch
    def _statement(self, toks: list[Tok], batch: str) -> Rows | None:
        db = self.db
        words = [_word(tok) for tok in toks]
        first = words[0]
        if first == "IF":
            at = _first(toks, "THROW", "ROLLBACK", "INSERT", "UPDATE")
            assert at < len(toks), f"the fake does not know this IF: {head(batch)}"
            return self._statement(toks[at:], batch) if self._condition(toks[1:at], batch) else None
        if first == "THROW":
            raise SqlError(toks[3].value, number=int(toks[1].text))
        if words[:2] in (["BEGIN", "TRANSACTION"], ["BEGIN", "TRAN"]):
            self._begin()
        elif first == "COMMIT":
            self._commit()
        elif first == "ROLLBACK":
            self._rollback()
        elif first == "SET":
            self._set(toks, words, batch)
        elif first == "SELECT":
            return [tuple(self._scalar(item, batch) for item in _split(toks[1:], ","))]
        elif first in ("INSERT", "UPDATE"):
            self._implicit()
            self._write(toks, words, batch)
        elif first in ("EXEC", "EXECUTE"):
            procedure = next((fold(tok.value) for tok in toks[1:4] if fold(tok.value).startswith("sp_")), "")
            literals = [tok.value for tok in toks if tok.kind in _LITERAL]
            if procedure == "sp_releaseapplock":
                if db.lock_holder is not self:
                    raise _engine(
                        "Cannot release the application lock because it is not currently held.", 1223
                    )
                db.lock_holder = None
            elif procedure == "sp_refreshsqlmodule":
                self._implicit()
                db.refresh_module(literals[0])
                if self.recorded:
                    db.ddl.append(batch)
            else:
                raise AssertionError(f"the fake does not know this procedure: {head(batch)}")
        else:
            raise AssertionError(f"the fake does not understand this batch: {head(batch)}")
        return None

    def _condition(self, toks: list[Tok], batch: str) -> bool:
        """<scalar> <> | = | > <literal>, joined by OR."""
        for part in _split_words(toks, "OR"):
            at = next(i for i, tok in enumerate(part) if tok.kind == "op" and tok.text in ("<>", "=", ">"))
            left, right = self._scalar(part[:at], batch), self._value(part[at + 1 :], None, batch)
            operator = part[at].text
            if (
                (operator == "<>" and not _equal(left, right))
                or (operator == "=" and _equal(left, right))
                or (operator == ">" and left > right)
            ):
                return True
        return False

    def _set(self, toks: list[Tok], words: list[str], batch: str) -> None:
        options = [word for word in words[1:-1] if word]
        value = toks[-1]
        if options == ["PARSEONLY"]:
            self.parse_only = words[-1] == "ON"
            return
        for option in options:
            assert option in self.options, f"the fake does not know SET {option}: {head(batch)}"
            if option == "LOCK_TIMEOUT":
                self.options[option] = int(value.text)
            elif option == "LANGUAGE":
                self.options[option] = value.value
            else:
                self.options[option] = words[-1] == "ON"

    def _scalar(self, item: list[Tok], batch: str) -> Any:
        """One item of a select list that reads the session or the database, not a table."""
        db = self.db
        words = [_word(tok) for tok in item]
        first = words[0] if words else ""
        literals = [tok.value for tok in item if tok.kind in _LITERAL]
        if first == "CAST":  # CAST(<inner> AS type)
            return self._scalar(item[2 : len(words) - 1 - words[::-1].index("AS")], batch)
        if first == "@@TRANCOUNT":
            return self.trancount
        if first == "XACT_STATE":
            return 1 if self.trancount else 0
        if first == "CURRENT_TRANSACTION_ID":  # outside a transaction every statement has its own
            return self.transaction_id if self.trancount else db.next_transaction()
        if first == "@@SPID":
            return self.spid
        if first == "@@ROWCOUNT":
            return self.rowcount
        if first == "@@LOCK_TIMEOUT":
            return self.options["LOCK_TIMEOUT"]
        if first == "@@LANGUAGE":
            return self.options["LANGUAGE"]
        if first == "@@OPTIONS":
            bits = sum(bit for option, bit in _OPTION_BITS.items() if self.options[option])
            return bits & int(item[2].text) if len(item) == 3 and _op(item[1], "&") else bits
        if first == "SESSIONPROPERTY":
            return int(self.options[literals[0]])
        if first == "APPLOCK_MODE":
            return "Exclusive" if db.lock_holder is self else "NoLock"
        if first == "APPLOCK_TEST":
            return int(self._lock_is_free())
        if first == "SCOPE_IDENTITY":
            return self.identity
        if first == "DB_NAME":
            return db.name
        if first == "SERVERPROPERTY":
            return {"EngineEdition": db.engine_edition, "ServerName": db.server}[literals[0]]
        if first == "DATABASEPROPERTYEX":
            facts = {"Updateability": db.updateability, "Collation": db.collation}
            return (facts | {"ServiceObjective": db.service_objective})[literals[0]]
        raise AssertionError(
            f"the fake cannot evaluate {' '.join(t.text for t in item)[:40]!r}: {head(batch)}"
        )

    def _value(self, toks: list[Tok], row: dict[str, Any] | None, batch: str) -> Any:
        """A value of an INSERT, of a SET clause or of a comparison."""
        words = [_word(tok) for tok in toks]
        tok = toks[0]
        if len(toks) == 1 and tok.kind in _LITERAL:
            return tok.value
        if len(toks) == 1 and tok.kind == "number":
            return int(tok.text)
        if words == ["NULL"]:
            return None
        if words[0] == "SYSUTCDATETIME":
            return self.db.now()
        if words[0] == "COALESCE":
            return "fake-principal"
        if words[0] == "CONVERT":  # CONVERT(datetime2(3), N'<ISO time>', 126)
            return next(found.value for found in toks if found.kind in _LITERAL)
        if row is not None and tok.kind == "bident" and len(toks) == 3 and _op(toks[1], "+"):
            return row[tok.value] + int(toks[2].text)
        return self._scalar(toks, batch)

    def _write(self, toks: list[Tok], words: list[str], batch: str) -> None:
        """INSERT INTO [azsqlcd].[t] (...) VALUES (...), or UPDATE [azsqlcd].[t] SET ... WHERE [c] = v."""
        db = self.db
        at = 2 if words[0] == "INSERT" else 1
        if not (toks[at].kind == "bident" and toks[at].value == state.SCHEMA and _op(toks[at + 1], ".")):
            raise AssertionError(f"the fake holds no rows of user tables: {head(batch)}")
        table = toks[at + 2].value
        rows = db._state(table)
        declared = {column.name: column for column in db.state_tables[table].columns}

        def known(column: str) -> str:
            if column not in declared:
                raise _engine(f"Invalid column name '{column}'.", 207)
            return column

        if words[0] == "INSERT":
            values_at = _first(toks, "VALUES")
            columns = [known(tok.value) for tok in toks[at + 3 : values_at] if tok.kind == "bident"]
            values = _split(toks[values_at + 2 : -1], ",")
            assert len(columns) == len(values), f"columns and values differ in number: {head(batch)}"
            row: dict[str, Any] = dict.fromkeys(declared)
            for column, value in zip(columns, values, strict=True):
                row[column] = self._value(value, None, batch)
            for column in declared.values():
                if column.identity is not None:  # a value is used up, whatever the transaction does
                    db._identity[table] = db._identity.get(table, column.identity.seed - 1) + 1
                    row[column.name] = self.identity = db._identity[table]
            db.check_row(table, row)
            rows.append(row)
            self.rowcount = 1
            return
        set_at, where_at = _first(toks, "SET"), _first(toks, "WHERE")
        where = toks[where_at + 1 :]
        assert where and where[0].kind == "bident" and _op(where[1], "="), f"unknown WHERE: {head(batch)}"
        wanted = self._value(where[2:], None, batch)
        hit = [row for row in rows if _equal(row[known(where[0].value)], wanted)]
        for row in hit:
            for assignment in _split(toks[set_at + 1 : where_at], ","):
                row[known(assignment[0].value)] = self._value(assignment[2:], row, batch)
            db.check_row(table, row)
        self.rowcount = len(hit)


def _split_words(toks: list[Tok], word: str) -> list[list[Tok]]:
    parts: list[list[Tok]] = [[]]
    for tok in toks:
        if _word(tok) == word:
            parts.append([])
        else:
            parts[-1].append(tok)
    return parts
