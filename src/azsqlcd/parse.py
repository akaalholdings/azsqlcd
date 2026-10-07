"""DDL parser for table-class objects: object files and migration model batches.

Recursive descent over lex tokens. Rules that hold everywhere:
  * Closed lists. A statement, column option, index option or table option that is not in the
    grammar is an error. Nothing is skipped and there is no recovery.
  * A statement must consume every token of its text.
  * Expressions (DEFAULT, CHECK, computed column, index filter) are captured as token runs and
    never parsed. A parenthesised expression is a balanced run. An unparenthesised DEFAULT or
    computed expression has a closed shape: operands (literal, name, function call, NEXT VALUE
    FOR, parenthesised run) joined by arithmetic operators. Anything else needs parentheses.
  * The normal form (NF001 to NF006) applies to object files and to migration statements alike:
    the model has no place for an implicit nullability, name, length, clustering or history table.
  * System-versioned temporal tables: the two GENERATED ALWAYS AS ROW START / END columns, PERIOD
    FOR SYSTEM_TIME and WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [schema].[table])) are read
    in CREATE TABLE, all three or none. ALTER TABLE ... SET (SYSTEM_VERSIONING = ...) is read in
    a migration. Every other temporal statement is UNSUPPORTED.
  * Dynamic data masking: MASKED WITH (FUNCTION = '...') directly after the data type (and COLLATE)
    of a column, the one place where the engine reads it. In a migration: ALTER COLUMN [c] ADD
    MASKED WITH (FUNCTION = '...') and ALTER COLUMN [c] DROP MASKED. The function is kept as text.
    NF004 asks for the spelling in which the engine stores it ('default()', not 'DEFAULT( )').

Public API: ParseError, parse_object_file, parse_statement, TYPE_SYNONYMS, EXECUTION_OPTIONS.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal, NoReturn

from azsqlcd import names
from azsqlcd.lex import BOM, LexError, Tok, significant, split_batches, tokenize
from azsqlcd.model import (
    COLUMNSTORE_OPTION_VALUES,
    FK_ACTIONS,
    INDEX_OPTION_VALUES,
    RESERVED_WORDS,
    AddColumn,
    AddConstraint,
    AliasType,
    AlterColumn,
    AlterColumnProperty,
    AlterSequence,
    Check,
    Column,
    Computed,
    Constraint,
    CreateIndex,
    CreateSchema,
    CreateSequence,
    CreateSynonym,
    CreateTable,
    CreateType,
    DefaultConstraint,
    DropColumn,
    DropConstraint,
    DropIndex,
    DropSchema,
    DropSequence,
    DropSynonym,
    DropTable,
    DropType,
    Expression,
    ForeignKey,
    Identity,
    Index,
    KeyColumn,
    MaskColumn,
    ModelObject,
    Operation,
    Options,
    PrimaryKey,
    RebuildTable,
    Rename,
    Schema,
    Sequence,
    SetSystemVersioning,
    Synonym,
    Table,
    TableType,
    Temporal,
    TypeRef,
    Unique,
    UnmaskColumn,
    builtin_shape,
    fold,
    is_reserved,
    mask_spelling,
)


class ParseError(ValueError):
    """Text outside the grammar. code is SYNTAX, UNSUPPORTED or one of NF001 to NF006.

    line and column are 1-based positions in the text that was passed in. column is 0 when the
    lexer gave no position inside the line (unterminated string, comment or identifier).
    """

    def __init__(self, code: str, message: str, line: int, column: int) -> None:
        super().__init__(f"{code} at {line}:{column}: {message}")
        self.code = code
        self.message = message
        self.line = line
        self.column = column


# NF004, the one table: synonym (upper case, words joined by one space) -> the name to write.
TYPE_SYNONYMS: Mapping[str, str] = {
    "INTEGER": "int",
    "DEC": "decimal",
    "DOUBLE PRECISION": "float",
    "ROWVERSION": "timestamp",
    "CHARACTER": "char",
    "CHARACTER VARYING": "varchar",
    "CHAR VARYING": "varchar",
    "BINARY VARYING": "varbinary",
    "NATIONAL CHARACTER": "nchar",
    "NATIONAL CHAR": "nchar",
    "NATIONAL CHARACTER VARYING": "nvarchar",
    "NATIONAL CHAR VARYING": "nvarchar",
    "NATIONAL TEXT": "ntext",
}

# Options of a migration statement that say how the engine runs it. They are kept on the
# operation (exec_options) and are never part of the model. ONLINE = ON may also carry
# (WAIT_AT_LOW_PRIORITY (...)), which is kept under the name WAIT_AT_LOW_PRIORITY.
EXECUTION_OPTIONS = frozenset({"ONLINE", "RESUMABLE", "MAX_DURATION", "MAXDOP", "SORT_IN_TEMPDB"})

_REFUSED_INDEX_OPTIONS = {
    "DROP_EXISTING": "DROP_EXISTING (drop the index, then create it)",
    "STATISTICS_INCREMENTAL": "partitioned tables (STATISTICS_INCREMENTAL)",
}
_REFUSED_TABLE_OPTIONS = {
    "LEDGER": "ledger tables",
    "MEMORY_OPTIMIZED": "memory-optimized tables",
    "DURABILITY": "memory-optimized tables (DURABILITY)",
    "XML_COMPRESSION": "the table option XML_COMPRESSION (state it on the clustered index or key)",
}
_REFUSED_COLUMN_OPTIONS = {
    "COLUMN_SET": "column sets (COLUMN_SET FOR ALL_SPARSE_COLUMNS)",
    "ENCRYPTED": "Always Encrypted (ENCRYPTED WITH)",
    "FILESTREAM": "FILESTREAM",
    "HIDDEN": "temporal tables (HIDDEN on a column that is not GENERATED ALWAYS AS ROW START or END)",
}
# first word after CREATE
_REFUSED_CREATE = {
    "EXTERNAL": "external tables and other external objects",
    "PARTITION": "partition functions and partition schemes",
    "XML": "XML indexes",
    "SELECTIVE": "XML indexes",
    "SPATIAL": "spatial indexes",
    "JSON": "JSON indexes",
    "VECTOR": "vector indexes",
    "FULLTEXT": "full-text indexes",
}
# DATA_COMPRESSION of a heap: CREATE TABLE ... WITH ( ... ) and ALTER TABLE ... REBUILD WITH ( ... )
_TABLE_OPTION_VALUES: Mapping[str, tuple[str, ...] | None] = {"DATA_COMPRESSION": ("NONE", "ROW", "PAGE")}
_REBUILD_EXECUTION = frozenset({"ONLINE", "MAXDOP", "SORT_IN_TEMPDB"})
_COLUMNSTORE_EXECUTION = frozenset({"ONLINE", "MAXDOP"})
_NOT_FOR_REPLICATION = (
    "NOT FOR REPLICATION goes directly after IDENTITY, directly after the word CHECK, or at the end of "
    "a FOREIGN KEY"
)
_CONSTRAINT_KINDS = {
    "PRIMARY": "PRIMARY KEY",
    "UNIQUE": "UNIQUE",
    "FOREIGN": "FOREIGN KEY",
    "REFERENCES": "FOREIGN KEY",
    "CHECK": "CHECK",
}
_BINARY_OPERATORS = frozenset("+-*/%&|^")
# reserved words that are a complete operand of an unparenthesised expression
_NILADIC = frozenset({"NULL", "CURRENT_TIMESTAMP", "CURRENT_USER", "SESSION_USER", "SYSTEM_USER", "USER"})
# words that end an index filter; a clause that is out of order then fails as a trailing token
_FILTER_STOPS = frozenset({"WITH", "ON", "FILESTREAM_ON", "INCLUDE", "WHERE"})
_RENAME_KINDS: dict[str, Literal["column", "index", "object"]] = {
    "COLUMN": "column",
    "INDEX": "index",
    "OBJECT": "object",
}
_SEQUENCE_REQUIRED = ("type", "start", "increment", "minvalue", "maxvalue", "cycle", "cached")
_SEQUENCE_CLAUSES = {
    "type": "AS <type>",
    "start": "START WITH",
    "increment": "INCREMENT BY",
    "minvalue": "MINVALUE",
    "maxvalue": "MAXVALUE",
    "cycle": "CYCLE or NO CYCLE",
    "cached": "CACHE or NO CACHE",
}
# kind of an object file -> (the operation its first batch must be, the field that holds the object)
_FILE_STATEMENT: dict[str, tuple[type, str]] = {
    "SCHEMA": (CreateSchema, "schema"),
    "TYPE": (CreateType, "type"),
    "SEQUENCE": (CreateSequence, "sequence"),
    "TABLE": (CreateTable, "table"),
    "SYNONYM": (CreateSynonym, "synonym"),
}
_BREAK = re.compile(r"\r\n|\r|\n")
# HISTORY_RETENTION_PERIOD: the unit as written -> the unit of the model (always the plural)
_RETENTION_UNITS = {
    "DAY": "DAYS",
    "DAYS": "DAYS",
    "WEEK": "WEEKS",
    "WEEKS": "WEEKS",
    "MONTH": "MONTHS",
    "MONTHS": "MONTHS",
    "YEAR": "YEARS",
    "YEARS": "YEARS",
}
_Retention = tuple[int, str] | None


def _show(tok: Tok | None) -> str:
    """How an error names the token found. The text of a string literal is never repeated."""
    if tok is None:
        return "end of text"
    if tok.kind in ("string", "nstring"):
        return "a string literal"
    return repr(tok.text)


def _line_col(text: str, pos: int) -> tuple[int, int]:
    before = text[:pos]
    start = max(before.rfind("\n"), before.rfind("\r")) + 1
    if start == 0 and text.startswith(BOM):
        start = 1
    return 1 + len(_BREAK.findall(before)), pos - start + 1


def _lex_error(e: LexError, line0: int) -> ParseError:
    return ParseError("SYNTAX", str(e).removeprefix(f"line {e.line}: "), line0 + e.line, 0)


@dataclass(frozen=True)
class _Period:
    start: str
    end: str
    tok: Tok | None  # PERIOD


@dataclass(frozen=True)
class _Versioning:
    history_schema: str
    history_table: str
    retention: _Retention
    tok: Tok | None  # SYSTEM_VERSIONING


class _Parser:
    def __init__(self, text: str, line0: int, migration: bool) -> None:
        self.text = text
        self.line0 = line0  # lines of the file before this batch
        self.migration = migration  # a migration statement may hold execution options
        self.toks = significant(tokenize(text))
        self.i = 0
        self.generated_at: Tok | None = None  # the first GENERATED ALWAYS AS ROW of the statement

    # -------------------------------------------------------------- errors
    def error(self, code: str, message: str, tok: Tok | None) -> ParseError:
        """tok = None points at the end of the text."""
        line, column = _line_col(self.text, tok.pos if tok is not None else len(self.text.rstrip()))
        return ParseError(code, message, self.line0 + line, column)

    def fail(self, expected: str) -> NoReturn:
        tok = self.peek()
        raise self.error("SYNTAX", f"expected {expected}, found {_show(tok)}", tok)

    def unsupported(self, feature: str, tok: Tok | None) -> NoReturn:
        raise self.error("UNSUPPORTED", f"not supported in this version: {feature}", tok)

    # -------------------------------------------------------------- tokens
    def peek(self, k: int = 0) -> Tok | None:
        j = self.i + k
        return self.toks[j] if 0 <= j < len(self.toks) else None

    def kw(self, k: int = 0) -> str:
        """The upper-case text of a bare word, or '' for any other token."""
        tok = self.peek(k)
        return tok.text.upper() if tok is not None and tok.kind == "word" and tok.text.isascii() else ""

    def is_op(self, text: str, k: int = 0) -> bool:
        tok = self.peek(k)
        return tok is not None and tok.kind == "op" and tok.text == text

    def accept(self, *words: str) -> bool:
        if all(self.kw(k) == word for k, word in enumerate(words)):
            self.i += len(words)
            return True
        return False

    def expect(self, *words: str) -> None:
        if not self.accept(*words):
            self.fail(" ".join(words))

    def accept_op(self, text: str) -> bool:
        if self.is_op(text):
            self.i += 1
            return True
        return False

    def op(self, text: str) -> None:
        if not self.accept_op(text):
            self.fail(repr(text))

    def ident(self, what: str) -> str:
        tok = self.peek()
        quoted = tok is not None and tok.kind in ("bident", "qident")
        bare = tok is not None and tok.kind == "word" and not is_reserved(tok.text)
        if tok is None or not (quoted or bare):
            self.fail(what)
        if not tok.value:
            raise self.error("SYNTAX", "an identifier cannot be empty", tok)
        self.i += 1
        return tok.value

    def dotted(self, what: str) -> list[str]:
        parts = [self.ident(what)]
        while self.accept_op("."):
            parts.append(self.ident(what))
        return parts

    def qualified(self, what: str) -> tuple[str, str]:
        tok = self.peek()
        parts = self.dotted(what)
        if len(parts) == 1:
            found = names.quote(parts[0])
            raise self.error("SYNTAX", f"expected {what} with two parts [schema].[name], found {found}", tok)
        if len(parts) > 2:
            self.unsupported("three-part names (write [schema].[name])", tok)
        return parts[0], parts[1]

    def integer(self, what: str, signed: bool = False) -> int:
        start = self.i
        negative = signed and self.accept_op("-")
        tok = self.peek()
        if tok is None or tok.kind != "number" or not (tok.text.isascii() and tok.text.isdigit()):
            self.i = start
            self.fail(what)
        self.i += 1
        return -int(tok.text) if negative else int(tok.text)

    def paren_list[T](self, item: Callable[[], T]) -> tuple[T, ...]:
        self.op("(")
        out = [item()]
        while self.accept_op(","):
            out.append(item())
        if not self.accept_op(")"):
            self.fail("',' or ')'")
        return tuple(out)

    def no_if_exists(self) -> None:
        if self.kw() == "IF":
            self.unsupported("IF EXISTS (a migration states one outcome)", self.peek())

    # -------------------------------------------------------------- expressions (never parsed)
    def balanced(self) -> None:
        """Consume '(' ... ')' with every nested parenthesis."""
        self.op("(")
        depth = 1
        while depth:
            tok = self.peek()
            if tok is None:
                self.fail("')' to close the expression")
            if tok.kind == "op" and tok.text == "(":
                depth += 1
            elif tok.kind == "op" and tok.text == ")":
                depth -= 1
            self.i += 1

    def captured(self, start: int) -> Expression:
        try:
            return Expression(tuple(t.text for t in self.toks[start : self.i]))
        except ValueError as e:
            raise self.error("SYNTAX", str(e), self.toks[start]) from e

    def parenthesised(self) -> Expression:
        start = self.i
        self.balanced()
        return self.captured(start)

    def operand(self) -> None:
        while self.is_op("-") or self.is_op("+") or self.is_op("~"):
            self.i += 1
        tok = self.peek()
        word = self.kw()
        if self.is_op("("):
            self.balanced()
        elif tok is not None and tok.kind in ("number", "string", "nstring"):
            self.i += 1
        elif word == "NEXT" and self.kw(1) == "VALUE" and self.kw(2) == "FOR":
            self.i += 3
            self.dotted("a sequence name")
        elif word in RESERVED_WORDS and not self.is_op("(", 1):
            if word not in _NILADIC:
                self.fail(
                    "an expression (put anything but a value, a function call or arithmetic in parentheses)"
                )
            self.i += 1
        elif tok is not None and tok.kind in ("word", "bident", "qident"):
            # a name with optional schema, then optional call arguments
            self.i += 1
            while (
                self.is_op(".")
                and (after := self.peek(1)) is not None
                and after.kind in ("word", "bident", "qident")
            ):
                self.i += 2
            if self.is_op("("):
                self.balanced()
        else:
            self.fail("an expression")
        if self.accept("COLLATE"):
            self.ident("a collation name")

    def expression(self) -> Expression:
        """DEFAULT or computed expression: operand { arithmetic-operator operand }."""
        start = self.i
        while True:
            self.operand()
            tok = self.peek()
            if tok is None or tok.kind != "op" or tok.text not in _BINARY_OPERATORS:
                return self.captured(start)
            self.i += 1

    def filter(self) -> Expression:
        """An index filter runs to WITH, ON, a ',' ')' ';' outside parentheses, or the end.

        This is the one place where a word the parser does not know stays inside an expression.
        """
        start, depth = self.i, 0
        while (tok := self.peek()) is not None:
            if tok.kind == "op" and tok.text == "(":
                depth += 1
            elif tok.kind == "op" and tok.text == ")":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and (self.is_op(",") or self.is_op(";") or self.kw() in _FILTER_STOPS):
                break
            self.i += 1
        if depth:
            self.fail("')' to close the filter")
        if self.i == start:
            self.fail("a filter predicate")
        return self.captured(start)

    # -------------------------------------------------------------- data types
    def type_argument(self) -> int | Literal["max"]:
        if self.accept("MAX"):
            return "max"
        return self.integer("a number or MAX")

    def data_type(self) -> TypeRef:
        tok = self.peek()
        words: list[str] = []
        while len(words) < 3 and self.kw(len(words)):
            words.append(self.kw(len(words)))
        for n in range(len(words), 0, -1):
            self.refuse_synonym(" ".join(words[:n]), tok)
        parts = self.dotted("a data type")
        if len(parts) > 2:
            self.unsupported("three-part names (write [schema].[type])", tok)
        if len(parts) == 2:
            if self.is_op("("):
                raise self.error("SYNTAX", "an alias type takes no arguments", self.peek())
            return TypeRef(parts[1], parts[0])
        name = parts[0]
        self.refuse_synonym(name.upper(), tok)
        shape = builtin_shape(name)
        if shape is None:
            expected = "a built-in data type or an alias type with two parts [schema].[type]"
            raise self.error("SYNTAX", f"expected {expected}, found {_show(tok)}", tok)
        args: tuple[int | Literal["max"], ...] = ()
        if self.is_op("("):
            if fold(name) == "float":
                raise self.error("NF004", "float(n) is a type synonym: write float or real", tok)
            if fold(name) == "xml":
                self.unsupported("typed xml (XML schema collections)", tok)
            if shape == "none":
                raise self.error("SYNTAX", f"data type {name} takes no arguments", self.peek())
            args = self.paren_list(self.type_argument)
        numbers = [a for a in args if isinstance(a, int)]
        if not args:
            if shape != "none":
                what = {"precision_scale": "precision and scale", "scale": "scale"}.get(shape, "length")
                raise self.error("NF003", f"data type {name} needs an explicit {what}", tok)
            return TypeRef(name)
        if shape == "length_or_max" and len(args) == 1:
            return TypeRef(name, length=args[0])
        if shape == "length" and len(numbers) == 1 == len(args):
            return TypeRef(name, length=numbers[0])
        if shape == "scale" and len(numbers) == 1 == len(args):
            return TypeRef(name, scale=numbers[0])
        if shape == "precision_scale" and len(numbers) == len(args) <= 2:
            if len(numbers) == 1:
                raise self.error("NF003", f"data type {name} needs an explicit scale: {name}(p, s)", tok)
            return TypeRef(name, precision=numbers[0], scale=numbers[1])
        raise self.error("SYNTAX", f"data type {name} cannot take these arguments", tok)

    def refuse_synonym(self, words: str, tok: Tok | None) -> None:
        if words in TYPE_SYNONYMS:
            message = f"{words.lower()} is a type synonym: write {TYPE_SYNONYMS[words]}"
            raise self.error("NF004", message, tok)

    # -------------------------------------------------------------- shared clauses
    def key_column(self) -> KeyColumn:
        name = self.ident("a column name")
        descending = self.accept("DESC")
        if not descending:
            self.accept("ASC")
        return KeyColumn(name, descending)

    def clustering(self, what: str) -> bool:
        if self.accept("CLUSTERED"):
            clustered = True
        elif self.accept("NONCLUSTERED"):
            clustered = False
        else:
            raise self.error("NF005", f"{what} needs explicit CLUSTERED or NONCLUSTERED", self.peek())
        self.refuse_index_kind()
        return clustered

    def refuse_index_kind(self) -> None:
        if self.kw() == "HASH":
            self.unsupported("memory-optimized tables (HASH indexes)", self.peek())

    def option_value(self, key: str, allowed: tuple[str, ...] | None) -> str:
        if allowed is None:
            return str(self.integer(f"a number for {key}"))
        word = self.kw()
        if word not in allowed:
            self.fail(f"{' or '.join(allowed)} for {key}")
        self.i += 1
        return word

    def low_priority(self) -> str:
        self.op("(")
        self.expect("WAIT_AT_LOW_PRIORITY")
        self.op("(")
        self.expect("MAX_DURATION")
        self.op("=")
        minutes = self.integer("a number of minutes")
        self.accept("MINUTES")
        self.op(",")
        self.expect("ABORT_AFTER_WAIT")
        self.op("=")
        action = self.option_value("ABORT_AFTER_WAIT", ("NONE", "SELF", "BLOCKERS"))
        self.op(")")
        self.op(")")
        return f"MAX_DURATION = {minutes} MINUTES, ABORT_AFTER_WAIT = {action}"

    def with_options(
        self,
        model: bool,
        execution: frozenset[str],
        *,
        low_priority: bool = True,
        values: Mapping[str, tuple[str, ...] | None] = INDEX_OPTION_VALUES,
        refused: Mapping[str, str] = _REFUSED_INDEX_OPTIONS,
        statement: str = "ALTER COLUMN",
    ) -> tuple[Options, Options]:
        """WITH ( ... ) -> (model options, execution options), each sorted by name.

        low_priority=False: ONLINE = ON takes no (WAIT_AT_LOW_PRIORITY (...)) in this statement.
        """
        if not (self.kw() == "WITH" and self.is_op("(", 1)):
            return (), ()
        opening = self.peek()
        self.i += 2
        found: dict[str, str] = {}
        executed: dict[str, str] = {}
        while True:
            tok = self.peek()
            key = self.kw()
            if key in refused:
                self.unsupported(refused[key], tok)
            if key in found or key in executed:
                raise self.error("SYNTAX", f"option {key} is written twice", tok)
            if model and key in values:
                self.i += 1
                self.op("=")
                found[key] = self.option_value(key, values[key])
                if key == "COMPRESSION_DELAY":
                    self.accept("MINUTES")
                if self.kw() == "ON" and self.kw(1) == "PARTITIONS":
                    self.unsupported("partitioned tables (ON PARTITIONS)", self.peek())
            elif key in EXECUTION_OPTIONS:
                if key not in execution:
                    here = ", ".join(sorted(execution)) or "none"
                    message = f"{key} is an execution option and is not valid here (valid here: {here})"
                    raise self.error("SYNTAX", message, tok)
                self.i += 1
                self.op("=")
                if key in ("MAX_DURATION", "MAXDOP"):
                    executed[key] = str(self.integer(f"a number for {key}"))
                    if key == "MAX_DURATION":
                        self.accept("MINUTES")
                else:
                    executed[key] = self.option_value(key, ("ON", "OFF"))
                    if key == "ONLINE" and executed[key] == "ON" and self.is_op("("):
                        if not low_priority:
                            message = (
                                f"WAIT_AT_LOW_PRIORITY is not valid on {statement}: the engine takes "
                                "WITH (ONLINE = ON) or WITH (ONLINE = OFF) there and nothing more"
                            )
                            raise self.error("SYNTAX", message, self.peek())
                        executed["WAIT_AT_LOW_PRIORITY"] = self.low_priority()
            else:
                known = sorted((set(values) if model else set()) | execution)
                self.fail(f"a known option ({', '.join(known)})")
            if self.accept_op(","):
                continue
            if self.accept_op(")"):
                # The engine reads these and refuses them when the statement runs, so the PARSEONLY
                # check of a plan passes them.
                resumable = executed.get("RESUMABLE") == "ON"
                problem = None
                if resumable and executed.get("ONLINE") != "ON":
                    problem = "RESUMABLE = ON needs ONLINE = ON"
                elif resumable and executed.get("SORT_IN_TEMPDB") == "ON":
                    problem = "SORT_IN_TEMPDB = ON is not valid with RESUMABLE = ON"
                elif "MAX_DURATION" in executed and not resumable:
                    problem = "MAX_DURATION needs RESUMABLE = ON"
                if problem is not None:
                    raise self.error("SYNTAX", problem, opening)
                return tuple(sorted(found.items())), tuple(sorted(executed.items()))
            self.fail("',' or ')'")

    def filegroup(self) -> None:
        tok = self.peek()
        name = "PRIMARY" if self.accept("PRIMARY") else self.ident("a filegroup")
        if self.is_op("("):
            self.unsupported("partition schemes (ON scheme(column))", tok)
        if fold(name) not in ("primary", "default"):
            feature = f"filegroup {names.quote(name)} (Azure SQL Database has [PRIMARY] only)"
            self.unsupported(feature, tok)

    def storage(self) -> None:
        """ON [PRIMARY] is read and not modelled: Azure SQL Database has no other filegroup."""
        if self.kw() == "FILESTREAM_ON":
            self.unsupported("FILESTREAM", self.peek())
        if self.accept("ON"):
            self.filegroup()

    # -------------------------------------------------------------- constraints and indexes
    def constraint_name(self, in_type: bool) -> str:
        tok = self.peek()
        self.expect("CONSTRAINT")
        if in_type:
            raise self.error("SYNTAX", "a constraint in a table type cannot be named", tok)
        return self.ident("a constraint name")

    def constraint(
        self, name: str | None, in_type: bool, column: str | None, execution: frozenset[str] = frozenset()
    ) -> tuple[Constraint, Options]:
        """PRIMARY KEY, UNIQUE, FOREIGN KEY or CHECK. column is set for a column-level constraint."""
        tok = self.peek()
        word = self.kw()
        kind = _CONSTRAINT_KINDS.get(word)
        if kind is None or (word == "REFERENCES" and column is None):
            self.fail("PRIMARY KEY, UNIQUE, FOREIGN KEY or CHECK")
        if in_type and kind == "FOREIGN KEY":
            raise self.error("SYNTAX", "a table type cannot have a FOREIGN KEY", tok)
        if name is None and not in_type:
            message = f"the {kind} constraint needs a name: write CONSTRAINT [name] {kind}"
            raise self.error("NF002", message, tok)
        if word == "CHECK":
            self.i += 1
            at = self.peek()
            not_for_replication = self.accept("NOT", "FOR", "REPLICATION")
            if not_for_replication and in_type:
                raise self.error("SYNTAX", "a table type cannot have NOT FOR REPLICATION", at)
            return Check(name, self.parenthesised(), not_for_replication), ()
        if kind == "FOREIGN KEY":
            assert name is not None  # a table type has no foreign key
            return self.foreign_key(name, column), ()
        self.i += 1
        if word == "PRIMARY":
            self.expect("KEY")
        clustered = self.clustering(f"{kind} constraint")
        if self.is_op("("):
            columns = self.paren_list(self.key_column)
        elif column is not None:
            columns = (KeyColumn(column),)
        else:
            self.fail("'(' and the key columns")
        options, exec_options = self.with_options(True, execution)
        self.storage()
        cls = PrimaryKey if word == "PRIMARY" else Unique
        return cls(name, clustered, columns, options), exec_options

    def foreign_key(self, name: str, column: str | None) -> ForeignKey:
        tok = self.peek()
        if self.accept("FOREIGN"):
            self.expect("KEY")
        if self.is_op("(") or column is None:
            columns = self.paren_list(lambda: self.ident("a column name"))
        else:
            columns = (column,)
        self.expect("REFERENCES")
        ref_schema, ref_table = self.qualified("the referenced table")
        if not self.is_op("("):
            self.fail("'(' and the referenced columns (they are always written)")
        ref_columns = self.paren_list(lambda: self.ident("a referenced column"))
        if len(columns) != len(ref_columns):
            counts = f"{len(columns)} columns and {len(ref_columns)} referenced columns"
            raise self.error("SYNTAX", f"foreign key {names.quote(name)} has {counts}", tok)
        actions: dict[str, str] = {}
        while self.kw() == "ON" and self.kw(1) in ("DELETE", "UPDATE"):
            on, event = self.peek(), self.kw(1)
            self.i += 2
            if event in actions:
                raise self.error("SYNTAX", f"ON {event} is written twice", on)
            action = next((a for a in FK_ACTIONS if self.accept(*a.split())), None)
            if action is None:
                self.fail(" | ".join(FK_ACTIONS))
            actions[event] = action
        not_for_replication = self.accept("NOT", "FOR", "REPLICATION")
        delete, update = actions.get("DELETE", "NO ACTION"), actions.get("UPDATE", "NO ACTION")
        return ForeignKey(
            name, columns, ref_schema, ref_table, ref_columns, delete, update, not_for_replication
        )

    def index_tail(
        self, name: str, unique: bool, clustered: bool, column: str | None, execution: frozenset[str]
    ) -> tuple[Index, Options]:
        """( key columns ) [INCLUDE] [WHERE] [WITH] [ON]. column is set for a column-level index."""
        if self.is_op("("):
            columns = self.paren_list(self.key_column)
        elif column is not None:
            columns = (KeyColumn(column),)
        else:
            self.fail("'(' and the index key columns")
        included: tuple[str, ...] = ()
        if self.accept("INCLUDE"):
            included = self.paren_list(lambda: self.ident("an included column"))
        where = self.filter() if self.accept("WHERE") else None
        options, exec_options = self.with_options(True, execution)
        self.storage()
        return Index(name, unique, clustered, columns, included, where, options), exec_options

    def columnstore_tail(
        self, name: str, clustered: bool, at: Tok | None, execution: frozenset[str]
    ) -> tuple[Index, Options]:
        """[( columns )] [ORDER ( columns )] [WHERE] [WITH] [ON]: the column list goes with
        NONCLUSTERED only, and so does the filter. at is the token that an error points at."""
        included: tuple[str, ...] = ()
        if clustered:
            if self.is_op("("):
                message = "a clustered columnstore index has no column list: it holds every column"
                raise self.error("SYNTAX", message, self.peek())
        elif self.is_op("("):
            included = self.paren_list(lambda: self.ident("a column name"))
        else:
            self.fail("'(' and the columns of the nonclustered columnstore index")
        order: tuple[KeyColumn, ...] = ()
        if self.accept("ORDER"):
            order = tuple(KeyColumn(n) for n in self.paren_list(lambda: self.ident("a column name")))
        where: Expression | None = None
        if self.kw() == "WHERE":
            if clustered:
                raise self.error("SYNTAX", "a clustered columnstore index has no filter", self.peek())
            self.i += 1
            where = self.filter()
        options, exec_options = self.with_options(
            True,
            execution & _COLUMNSTORE_EXECUTION,
            low_priority=False,
            values=COLUMNSTORE_OPTION_VALUES,
            statement="a columnstore index",
        )
        self.storage()
        try:
            return Index(name, False, clustered, order, included, where, options, True), exec_options
        except ValueError as e:
            raise self.error("SYNTAX", str(e), at) from e

    def inline_index(self, column: str | None) -> Index:
        self.expect("INDEX")
        at = self.peek()
        name = self.ident("an index name")
        unique_at = self.peek()
        unique = self.accept("UNIQUE")
        clustered = self.clustering(f"index {names.quote(name)}")
        if self.accept("COLUMNSTORE"):
            if unique:
                raise self.error("SYNTAX", "a columnstore index cannot be UNIQUE", unique_at)
            if column is not None:
                message = "a columnstore index is written as an element of the table, not on a column"
                raise self.error("SYNTAX", message, at)
            return self.columnstore_tail(name, clustered, at, frozenset())[0]
        return self.index_tail(name, unique, clustered, column, frozenset())[0]

    # -------------------------------------------------------------- columns and table bodies
    def column(self, in_type: bool, alter: bool) -> tuple[Column, list[Constraint], list[Index], bool]:
        """One column definition -> (column, its column-level constraints and indexes, WITH VALUES)."""
        name_tok = self.peek()
        name = self.ident("a column name")
        type_: TypeRef | None = None
        computed: Computed | None = None
        collation: str | None = None
        nullable: bool | None = None
        if self.accept("AS"):
            expression = self.expression()
            computed = Computed(expression, self.accept("PERSISTED"))
            if computed.persisted and self.accept("NOT", "NULL"):
                nullable = False
        else:
            type_ = self.data_type()
            if self.accept("COLLATE"):
                collation = self.ident("a collation name")
        rowguidcol = False
        # SPARSE has its place before MASKED in the grammar of the engine; it is also read later
        sparse = computed is None and self.accept("SPARSE")
        masked: str | None = None
        if computed is None and self.kw() == "MASKED":
            if in_type:
                self.unsupported("dynamic data masking (MASKED) in a table type", self.peek())
            masked = self.masking_function(type_)
        identity: Identity | None = None
        default: DefaultConstraint | None = None
        with_values = False
        generated: str | None = None
        generated_tok: Tok | None = None
        hidden = False
        constraints: list[Constraint] = []
        indexes: list[Index] = []
        while True:
            tok = self.peek()
            word = self.kw()
            cname: str | None = None
            if word == "CONSTRAINT":
                cname = self.constraint_name(in_type)
                tok, word = self.peek(), self.kw()
                if word != "DEFAULT" and word not in _CONSTRAINT_KINDS:
                    self.fail("DEFAULT, PRIMARY KEY, UNIQUE, FOREIGN KEY, REFERENCES or CHECK")
            if word in _CONSTRAINT_KINDS:
                constraints.append(self.constraint(cname, in_type, name)[0])
            elif word == "INDEX":
                indexes.append(self.inline_index(name))
            elif word == "MASKED":
                message = (
                    "a computed column cannot be masked"
                    if computed is not None
                    else "MASKED WITH (FUNCTION = '...') must directly follow the data type (after COLLATE; "
                    "before IDENTITY, NULL or NOT NULL and DEFAULT)"
                )
                raise self.error("SYNTAX", message, tok)
            elif computed is not None:
                break
            elif word == "NULL" or (word == "NOT" and self.kw(1) == "NULL"):
                if nullable is not None:
                    raise self.error("SYNTAX", "NULL or NOT NULL is written twice", tok)
                nullable = word == "NULL"
                self.i += 1 if nullable else 2
            elif word == "IDENTITY":
                if identity is not None:
                    raise self.error("SYNTAX", "IDENTITY is written twice", tok)
                self.i += 1
                identity = Identity(1, 1)
                if self.accept_op("("):
                    seed = self.integer("the identity seed", signed=True)
                    self.op(",")
                    identity = Identity(seed, self.integer("the identity increment", signed=True))
                    self.op(")")
                if self.accept("NOT", "FOR", "REPLICATION"):
                    identity = replace(identity, not_for_replication=True)
            elif word in ("ROWGUIDCOL", "SPARSE"):
                if (word == "ROWGUIDCOL" and rowguidcol) or (word == "SPARSE" and sparse):
                    raise self.error("SYNTAX", f"{word} is written twice", tok)
                self.i += 1
                rowguidcol, sparse = rowguidcol or word == "ROWGUIDCOL", sparse or word == "SPARSE"
            elif word == "DEFAULT":
                if default is not None:
                    raise self.error("SYNTAX", "DEFAULT is written twice", tok)
                if cname is None and not in_type:
                    message = (
                        f"the DEFAULT of column {names.quote(name)} needs a name: CONSTRAINT [name] DEFAULT"
                    )
                    raise self.error("NF002", message, tok)
                self.i += 1
                default = DefaultConstraint(cname, self.expression())
                if self.kw() == "WITH" and self.kw(1) == "VALUES":
                    if not alter:
                        raise self.error(
                            "SYNTAX", "WITH VALUES is valid only in ALTER TABLE ... ADD", self.peek()
                        )
                    self.i += 2
                    with_values = True
            elif word == "NOT" and self.kw(1) == "FOR":
                raise self.error("SYNTAX", _NOT_FOR_REPLICATION, tok)
            elif word == "GENERATED":
                if self.kw(3) != "ROW":
                    self.unsupported("ledger tables (GENERATED ALWAYS)", tok)
                if in_type or alter:
                    where = "in a table type" if in_type else "added by ALTER TABLE"
                    self.unsupported(f"temporal tables (GENERATED ALWAYS) {where}", tok)
                if generated is not None:
                    raise self.error("SYNTAX", "GENERATED ALWAYS is written twice", tok)
                if masked is not None:
                    raise self.error("SYNTAX", "a period column (GENERATED ALWAYS) cannot be masked", tok)
                self.expect("GENERATED", "ALWAYS", "AS", "ROW")
                if self.kw() not in ("START", "END"):
                    self.fail("START or END after GENERATED ALWAYS AS ROW")
                generated, generated_tok = f"ROW_{self.kw()}", tok
                self.generated_at = self.generated_at or tok
                self.i += 1
                hidden = self.accept("HIDDEN")
            elif word in _REFUSED_COLUMN_OPTIONS:
                self.unsupported(_REFUSED_COLUMN_OPTIONS[word], tok)
            elif word == "COLLATE":
                raise self.error("SYNTAX", "COLLATE must directly follow the data type", tok)
            else:
                break
        end = self.peek()
        if end is not None and not (end.kind == "op" and end.text in (",", ")", ";")):
            self.fail(
                "a column option or the end of the statement" if alter else "a column option, ',' or ')'"
            )
        if computed is None and nullable is None:
            message = f"column {names.quote(name)} needs explicit NULL or NOT NULL"
            raise self.error("NF001", message, name_tok)
        if generated is not None:
            self.period_column(name, type_, nullable, identity, generated_tok)
        try:
            column = Column(
                name,
                type_,
                nullable,
                identity,
                default,
                computed,
                collation,
                generated,
                hidden,
                masked,
                rowguidcol,
                sparse,
            )
        except ValueError as e:
            raise self.error("SYNTAX", str(e), name_tok) from e
        return column, constraints, indexes, with_values

    def masking_function(self, type_: TypeRef | None) -> str:
        """MASKED WITH (FUNCTION = '<text>'): the text, in the spelling of the engine (NF004).

        type_ is the data type of the column when the statement gives it: random() on a decimal or
        numeric column is stored with the scale of the column.
        """
        self.expect("MASKED")
        if self.kw() != "WITH":
            self.fail("WITH after MASKED: MASKED WITH (FUNCTION = '...')")
        self.i += 1
        self.op("(")
        self.expect("FUNCTION")
        self.op("=")
        tok = self.peek()
        if tok is None or tok.kind not in ("string", "nstring"):
            self.fail("the masking function as a string literal, for example 'default()'")
        self.i += 1
        self.op(")")
        # the text between the quotes as written: the engine stores a doubled quote as two characters
        text = tok.text[tok.text.index("'") + 1 : -1]
        if not text.strip():
            raise self.error("SYNTAX", "the masking function cannot be empty", tok)
        decimal = type_ is not None and type_.schema is None and type_.name.lower() in ("decimal", "numeric")
        stored = mask_spelling(text, type_.scale if decimal and type_ is not None else None)
        if stored != text:
            message = f"the engine stores this masking function as '{stored}': write that text"
            raise self.error("NF004", message, tok)
        return text

    def period_column(
        self,
        name: str,
        type_: TypeRef | None,
        nullable: bool | None,
        identity: Identity | None,
        tok: Tok | None,
    ) -> None:
        """What the engine requires of a GENERATED ALWAYS AS ROW START / END column."""
        shown = names.quote(name)
        if type_ is None or type_.schema is not None or fold(type_.name) != "datetime2":
            raise self.error("SYNTAX", f"period column {shown} must have the data type datetime2", tok)
        if nullable is not False:
            raise self.error("SYNTAX", f"period column {shown} must be NOT NULL", tok)
        if identity is not None:
            raise self.error("SYNTAX", f"period column {shown} cannot be IDENTITY", tok)

    def body(self, in_type: bool) -> tuple[list[Column], list[Constraint], list[Index], _Period | None]:
        """( column | constraint | index | PERIOD FOR SYSTEM_TIME , ... ) -> the parts of a table body."""
        columns: list[Column] = []
        constraints: list[Constraint] = []
        indexes: list[Index] = []
        period: _Period | None = None
        self.op("(")
        while True:
            word = self.kw()
            if word == "PERIOD" and self.kw(1) == "FOR":  # a column may be called Period
                tok = self.peek()
                if in_type:
                    self.unsupported("temporal tables (PERIOD FOR SYSTEM_TIME) in a table type", tok)
                if period is not None:
                    raise self.error("SYNTAX", "PERIOD FOR SYSTEM_TIME is written twice", tok)
                self.i += 2
                self.expect("SYSTEM_TIME")
                found = self.paren_list(lambda: self.ident("a period column"))
                if len(found) != 2:
                    message = "PERIOD FOR SYSTEM_TIME names two columns: (row start, row end)"
                    raise self.error("SYNTAX", message, tok)
                period = _Period(found[0], found[1], tok)
            elif word == "CONSTRAINT" or (word in _CONSTRAINT_KINDS and word != "REFERENCES"):
                cname = self.constraint_name(in_type) if word == "CONSTRAINT" else None
                constraints.append(self.constraint(cname, in_type, None)[0])
            elif word == "INDEX":
                indexes.append(self.inline_index(None))
            else:
                column, more, inline, _ = self.column(in_type, alter=False)
                columns.append(column)
                constraints += more
                indexes += inline
            if self.accept_op(","):
                continue
            if self.accept_op(")"):
                break
            self.fail("',' or ')'")
        return columns, constraints, indexes, period

    # -------------------------------------------------------------- CREATE
    def create(self) -> Operation:
        tok = self.peek()
        word = self.kw()
        if word in ("UNIQUE", "CLUSTERED", "NONCLUSTERED", "COLUMNSTORE", "INDEX"):
            return self.create_index()
        if word in _REFUSED_CREATE or (word == "PRIMARY" and self.kw(1) == "XML"):
            self.unsupported(_REFUSED_CREATE.get(word, "XML indexes"), tok)
        creators: dict[str, Callable[[], Operation]] = {
            "TABLE": self.create_table,
            "SCHEMA": self.create_schema,
            "TYPE": self.create_type,
            "SEQUENCE": self.create_sequence,
            "SYNONYM": self.create_synonym,
        }
        if word not in creators:
            self.fail("TABLE, INDEX, SCHEMA, TYPE, SEQUENCE or SYNONYM after CREATE")
        self.i += 1
        return creators[word]()

    def create_table(self) -> CreateTable:
        tok = self.peek()
        schema, name = self.qualified("the table name")
        columns, constraints, indexes, period = self.body(in_type=False)
        versioning: _Versioning | None = None
        compression: str | None = None
        seen: set[str] = set()
        while True:
            here = self.peek()
            word = self.kw()
            if word == "AS" and self.kw(1) in ("NODE", "EDGE"):
                self.unsupported("graph tables (AS NODE, AS EDGE)", here)
            if word == "WITH" and self.is_op("(", 1) and word not in seen:
                seen.add(word)
                self.i += 2
                versioning, compression = self.table_options()
                continue
            if word not in ("ON", "TEXTIMAGE_ON", "FILESTREAM_ON") or word in seen:
                break
            seen.add(word)
            if word == "TEXTIMAGE_ON":
                self.i += 1
                self.filegroup()
            else:
                self.storage()
        temporal = self.temporal(columns, period, versioning)
        try:
            table = Table(
                schema, name, tuple(columns), tuple(constraints), tuple(indexes), temporal, compression
            )
        except ValueError as e:
            raise self.error("SYNTAX", str(e), tok) from e
        return CreateTable(table)

    def temporal(
        self, columns: list[Column], period: _Period | None, versioning: _Versioning | None
    ) -> Temporal | None:
        """A system-versioned table has all three: period columns, PERIOD and SYSTEM_VERSIONING = ON."""
        if period is None and versioning is None:
            # the Table refuses a GENERATED ALWAYS column here; say it as a feature, at the column
            if any(c.generated is not None for c in columns):
                feature = "temporal tables (GENERATED ALWAYS) without PERIOD FOR SYSTEM_TIME"
                self.unsupported(feature, self.generated_at)
            return None
        if versioning is None:
            assert period is not None
            feature = (
                "temporal tables (PERIOD FOR SYSTEM_TIME) without WITH (SYSTEM_VERSIONING = ON "
                "(HISTORY_TABLE = [schema].[table]))"
            )
            self.unsupported(feature, period.tok)
        if period is None:
            message = "SYSTEM_VERSIONING = ON needs PERIOD FOR SYSTEM_TIME (row start, row end) in the table"
            raise self.error("SYNTAX", message, versioning.tok)
        return Temporal(
            period.start,
            period.end,
            versioning.history_schema,
            versioning.history_table,
            versioning.retention,
        )

    def table_options(self) -> tuple[_Versioning | None, str | None]:
        """The options of CREATE TABLE after 'WITH (' -> (versioning, compression of the heap).

        DATA_COMPRESSION = NONE | ROW | PAGE and SYSTEM_VERSIONING = ON (...), each at most once.
        DATA_COMPRESSION = NONE is the same as no option: the result holds None.
        """
        versioning: _Versioning | None = None
        compression: str | None = None
        seen: set[str] = set()
        while True:
            tok = self.peek()
            key = self.kw()
            if key in seen:
                raise self.error("SYNTAX", f"option {key} is written twice", tok)
            if key == "SYSTEM_VERSIONING":
                on, history, retention = self.system_versioning(in_alter=False)
                assert on and history is not None  # CREATE TABLE takes no OFF
                versioning = _Versioning(history[0], history[1], retention, tok)
            elif key == "DATA_COMPRESSION":
                self.i += 1
                self.op("=")
                value = self.option_value(key, _TABLE_OPTION_VALUES[key])
                if self.kw() == "ON" and self.kw(1) == "PARTITIONS":
                    self.unsupported("partitioned tables (ON PARTITIONS)", self.peek())
                compression = None if value == "NONE" else value
            else:
                feature = _REFUSED_TABLE_OPTIONS.get(key)
                if feature is not None:
                    self.unsupported(feature, tok)
                self.fail("a table option of the model (DATA_COMPRESSION or SYSTEM_VERSIONING = ON)")
            seen.add(key)
            if self.accept_op(","):
                continue
            self.op(")")
            return versioning, compression

    def retention(self) -> _Retention:
        if self.accept("INFINITE"):
            return None
        tok = self.peek()
        number = self.integer("INFINITE or a number of DAYS, WEEKS, MONTHS or YEARS")
        unit = _RETENTION_UNITS.get(self.kw())
        if unit is None:
            self.fail("DAY, DAYS, WEEK, WEEKS, MONTH, MONTHS, YEAR or YEARS")
        if number < 1:
            raise self.error("SYNTAX", "HISTORY_RETENTION_PERIOD is INFINITE or at least 1", tok)
        self.i += 1
        return number, unit

    def system_versioning(self, in_alter: bool) -> tuple[bool, tuple[str, str] | None, _Retention]:
        """SYSTEM_VERSIONING = OFF | ON ( HISTORY_TABLE = [s].[t] [, ...] ) -> (on, history table, retention).

        DATA_CONSISTENCY_CHECK says how the engine runs the statement. It is read in ALTER TABLE
        and not kept; a table file has no place for it.
        """
        tok = self.peek()
        self.expect("SYSTEM_VERSIONING")
        self.op("=")
        if self.kw() == "OFF":
            if not in_alter:
                message = "SYSTEM_VERSIONING = OFF is not a table option: leave the WITH clause out"
                raise self.error("SYNTAX", message, self.peek())
            self.i += 1
            return False, None, None
        self.expect("ON")
        nf006 = (
            "SYSTEM_VERSIONING = ON needs the history table by name: ON (HISTORY_TABLE = [schema].[table]). "
            "Without it the engine makes a name from the object id of the table"
        )
        if not self.accept_op("("):
            raise self.error("NF006", nf006, tok)
        history: tuple[str, str] | None = None
        retention: _Retention = None
        seen: set[str] = set()
        while True:
            here = self.peek()
            key = self.kw()
            if key in seen:
                raise self.error("SYNTAX", f"{key} is written twice", here)
            seen.add(key)
            if key == "HISTORY_TABLE":
                self.i += 1
                self.op("=")
                at = self.peek()
                parts = self.dotted("the history table name")
                if len(parts) == 1:
                    found = names.quote(parts[0])
                    message = f"HISTORY_TABLE needs a name with two parts [schema].[table], found {found}"
                    raise self.error("NF006", message, at)
                if len(parts) > 2:
                    self.unsupported("three-part names (write [schema].[table])", at)
                history = (parts[0], parts[1])
            elif key == "HISTORY_RETENTION_PERIOD":
                self.i += 1
                self.op("=")
                retention = self.retention()
            elif key == "DATA_CONSISTENCY_CHECK":
                if not in_alter:
                    feature = (
                        "DATA_CONSISTENCY_CHECK in CREATE TABLE (it is not a property of the table; write it "
                        "in a hand-written ALTER TABLE ... SET (SYSTEM_VERSIONING = ON (...)))"
                    )
                    self.unsupported(feature, here)
                self.i += 1
                self.op("=")
                if not (self.accept("ON") or self.accept("OFF")):
                    self.fail("ON or OFF for DATA_CONSISTENCY_CHECK")
            else:
                more = ", DATA_CONSISTENCY_CHECK" if in_alter else ""
                self.fail(f"HISTORY_TABLE{more} or HISTORY_RETENTION_PERIOD")
            if self.accept_op(","):
                continue
            if self.accept_op(")"):
                break
            self.fail("',' or ')'")
        if history is None:
            raise self.error("NF006", nf006, tok)
        return True, history, retention

    def create_index(self) -> CreateIndex:
        unique_at = self.peek()
        unique = self.accept("UNIQUE")
        at = self.peek()
        clustered: bool | None = None
        if self.kw() in ("CLUSTERED", "NONCLUSTERED"):
            clustered = self.clustering("CREATE INDEX")
        self.refuse_index_kind()
        columnstore = self.accept("COLUMNSTORE")
        self.expect("INDEX")
        if clustered is None:
            raise self.error("NF005", "CREATE INDEX needs explicit CLUSTERED or NONCLUSTERED", at)
        if columnstore and unique:
            raise self.error("SYNTAX", "a columnstore index cannot be UNIQUE", unique_at)
        name = self.ident("an index name")
        self.expect("ON")
        schema, table = self.qualified("the table name")
        execution = EXECUTION_OPTIONS if self.migration else frozenset()
        if columnstore:
            index, exec_options = self.columnstore_tail(name, clustered, at, execution)
            return CreateIndex(schema, table, index, exec_options)
        index, exec_options = self.index_tail(name, unique, clustered, None, execution)
        return CreateIndex(schema, table, index, exec_options)

    def create_schema(self) -> CreateSchema:
        name = self.ident("a schema name")
        owner = self.ident("the owner") if self.accept("AUTHORIZATION") else None
        return CreateSchema(Schema(name, owner))

    def create_synonym(self) -> CreateSynonym:
        schema, name = self.qualified("the synonym name")
        self.expect("FOR")
        target_schema, target_name = self.qualified("the synonym target")
        return CreateSynonym(Synonym(schema, name, target_schema, target_name))

    def create_type(self) -> CreateType:
        tok = self.peek()
        schema, name = self.qualified("the type name")
        if self.accept("FROM"):
            base_tok = self.peek()
            base = self.data_type()
            if base.schema is not None:
                raise self.error("SYNTAX", "an alias type is built on a built-in data type", base_tok)
            if self.accept("NULL"):
                return CreateType(AliasType(schema, name, base, True))
            if self.accept("NOT", "NULL"):
                return CreateType(AliasType(schema, name, base, False))
            message = f"alias type {names.quote(name)} needs explicit NULL or NOT NULL"
            raise self.error("NF001", message, tok)
        if self.kw() == "EXTERNAL":
            self.unsupported("CLR types (EXTERNAL NAME)", self.peek())
        if not self.accept("AS", "TABLE"):
            self.fail("FROM <data type> or AS TABLE")
        columns, constraints, indexes, _ = self.body(in_type=True)
        try:
            table_type = TableType(schema, name, tuple(columns), tuple(constraints), tuple(indexes))
        except ValueError as e:
            raise self.error("SYNTAX", str(e), tok) from e
        if self.kw() == "WITH" and self.is_op("(", 1):
            self.i += 2
            if self.kw() == "MEMORY_OPTIMIZED":
                self.unsupported("memory-optimized table types", self.peek())
            self.fail("the end of the statement (a table type takes no option in this version)")
        return CreateType(table_type)

    def sequence_clauses(self, create: bool) -> dict[str, Any]:
        """Clauses in any order, each at most once -> field name: value."""
        seen: dict[str, Any] = {}

        def put(key: str, value: Any, tok: Tok | None) -> None:
            if key in seen:
                raise self.error("SYNTAX", f"{_SEQUENCE_CLAUSES.get(key, 'RESTART')} is written twice", tok)
            seen[key] = value

        while True:
            tok = self.peek()
            if self.kw() == "NO" and self.kw(1) in ("MINVALUE", "MAXVALUE"):
                bound = self.kw(1)
                message = f"NO {bound} leaves the value to the engine: write {bound} <n>"
                raise self.error("SYNTAX", message, tok)
            if create and self.accept("AS"):
                put("type", self.data_type(), tok)
            elif create and self.accept("START", "WITH"):
                put("start", self.integer("a whole number", signed=True), tok)
            elif not create and self.accept("RESTART"):
                put("restart", True, tok)
                if self.accept("WITH"):
                    put("restart_with", self.integer("a whole number", signed=True), tok)
            elif self.accept("INCREMENT", "BY"):
                put("increment", self.integer("a whole number", signed=True), tok)
            elif self.accept("MINVALUE"):
                put("minvalue", self.integer("a whole number", signed=True), tok)
            elif self.accept("MAXVALUE"):
                put("maxvalue", self.integer("a whole number", signed=True), tok)
            elif self.accept("NO", "CYCLE"):
                put("cycle", False, tok)
            elif self.accept("CYCLE"):
                put("cycle", True, tok)
            elif self.accept("NO", "CACHE"):
                put("cached", False, tok)
            elif self.accept("CACHE"):
                put("cached", True, tok)
                size = self.peek()
                if size is not None and size.kind == "number":
                    put("cache_size", self.integer("a cache size"), tok)
            else:
                return seen

    def create_sequence(self) -> CreateSequence:
        schema, name = self.qualified("the sequence name")
        clauses = self.sequence_clauses(create=True)
        missing = [_SEQUENCE_CLAUSES[key] for key in _SEQUENCE_REQUIRED if key not in clauses]
        if missing:
            self.fail(f"every property of the sequence (missing: {', '.join(missing)})")
        return CreateSequence(Sequence(schema, name, **clauses))

    # -------------------------------------------------------------- ALTER
    def alter(self) -> Operation:
        if self.accept("TABLE"):
            return self.alter_table()
        if self.accept("SEQUENCE"):
            schema, name = self.qualified("the sequence name")
            clauses = self.sequence_clauses(create=False)
            if not clauses:
                self.fail("RESTART, INCREMENT BY, MINVALUE, MAXVALUE, CYCLE, NO CYCLE, CACHE or NO CACHE")
            return AlterSequence(schema, name, **clauses)
        self.fail("TABLE or SEQUENCE after ALTER")

    def alter_table(self) -> Operation:
        schema, table = self.qualified("the table name")
        with_check: bool | None = None
        if self.kw() == "WITH" and self.kw(1) in ("CHECK", "NOCHECK"):
            with_check = self.kw(1) == "CHECK"
            self.i += 2
        if self.kw() in ("CHECK", "NOCHECK"):
            self.unsupported("CHECK CONSTRAINT and NOCHECK CONSTRAINT (enable or disable)", self.peek())
        if self.accept("ADD"):
            operation = self.alter_add(schema, table, with_check)
        elif with_check is not None:
            self.fail("ADD CONSTRAINT after WITH CHECK or WITH NOCHECK")
        elif self.accept("ALTER"):
            self.expect("COLUMN")
            operation = self.alter_column(schema, table)
        elif self.accept("DROP"):
            self.no_if_exists()
            if self.accept("COLUMN"):
                self.no_if_exists()
                operation = DropColumn(schema, table, self.ident("a column name"))
            elif self.accept("CONSTRAINT"):
                self.no_if_exists()
                operation = DropConstraint(schema, table, self.ident("a constraint name"))
            elif self.kw() == "PERIOD":
                self.unsupported("temporal tables (DROP PERIOD FOR SYSTEM_TIME)", self.peek())
            else:
                self.fail("COLUMN or CONSTRAINT after DROP")
        elif self.accept("SET"):
            self.op("(")
            if self.kw() != "SYSTEM_VERSIONING":
                self.fail("SYSTEM_VERSIONING (the one ALTER TABLE ... SET option of this version)")
            on, history, retention = self.system_versioning(in_alter=True)
            self.op(")")
            history_schema, history_table = history if history is not None else (None, None)
            operation = SetSystemVersioning(schema, table, on, history_schema, history_table, retention)
        elif self.kw() == "REBUILD":
            operation = self.rebuild(schema, table)
        else:
            self.fail("ADD, ALTER COLUMN, DROP, REBUILD or SET (SYSTEM_VERSIONING = ...)")
        if self.is_op(","):
            raise self.error("SYNTAX", "one action per statement: write another ALTER TABLE", self.peek())
        return operation

    def alter_add(self, schema: str, table: str, with_check: bool | None) -> Operation:
        tok = self.peek()
        word = self.kw()
        only = "WITH CHECK and WITH NOCHECK go with ADD of a FOREIGN KEY or CHECK constraint only"
        if word == "PERIOD" and self.kw(1) == "FOR":
            self.unsupported("temporal tables (ADD PERIOD FOR SYSTEM_TIME to an existing table)", tok)
        if word == "CONSTRAINT" or word == "DEFAULT" or (word in _CONSTRAINT_KINDS and word != "REFERENCES"):
            name = self.constraint_name(False) if word == "CONSTRAINT" else None
            constraint: Constraint | DefaultConstraint
            for_column: str | None = None
            exec_options: Options = ()
            if self.kw() == "DEFAULT":
                if name is None:
                    message = "the DEFAULT constraint needs a name: write CONSTRAINT [name] DEFAULT"
                    raise self.error("NF002", message, tok)
                self.i += 1
                expression = self.expression()
                self.expect("FOR")
                constraint, for_column = DefaultConstraint(name, expression), self.ident("a column name")
            else:
                if self.kw() not in _CONSTRAINT_KINDS or self.kw() == "REFERENCES":
                    self.fail("DEFAULT, PRIMARY KEY, UNIQUE, FOREIGN KEY or CHECK")
                execution = EXECUTION_OPTIONS if self.migration else frozenset()
                constraint, exec_options = self.constraint(name, False, None, execution)
            if with_check is not None and not isinstance(constraint, ForeignKey | Check):
                raise self.error("SYNTAX", only, tok)
            return AddConstraint(schema, table, constraint, for_column, with_check, exec_options)
        if with_check is not None:
            raise self.error("SYNTAX", only, tok)
        column, constraints, indexes, with_values = self.column(False, alter=True)
        if constraints or indexes:
            message = "one action per statement: add the column, then its constraint or index"
            raise self.error("SYNTAX", message, tok)
        return AddColumn(schema, table, column, with_values)

    def rebuild(self, schema: str, table: str) -> RebuildTable:
        """REBUILD WITH ( DATA_COMPRESSION = ... [, execution options] ). A rebuild that states no
        compression is index maintenance: it changes nothing that the model holds."""
        tok = self.peek()
        self.expect("REBUILD")
        if self.kw() == "PARTITION":
            self.unsupported("partitioned tables (REBUILD PARTITION)", self.peek())
        execution = _REBUILD_EXECUTION if self.migration else frozenset()
        options, exec_options = self.with_options(
            True, execution, values=_TABLE_OPTION_VALUES, refused=_REFUSED_TABLE_OPTIONS, statement="REBUILD"
        )
        if not options:
            message = (
                "REBUILD needs WITH (DATA_COMPRESSION = NONE | ROW | PAGE): a rebuild that changes no "
                "compression is maintenance and not a model statement"
            )
            raise self.error("SYNTAX", message, tok)
        return RebuildTable(schema, table, dict(options)["DATA_COMPRESSION"], exec_options)

    def alter_column(
        self, schema: str, table: str
    ) -> AlterColumn | AlterColumnProperty | MaskColumn | UnmaskColumn:
        column = self.ident("a column name")
        tok = self.peek()
        if self.accept("ADD", "MASKED"):
            self.i -= 1
            return MaskColumn(schema, table, column, self.masking_function(None))
        if self.accept("DROP", "MASKED"):
            return UnmaskColumn(schema, table, column)
        if self.kw() in ("ADD", "DROP"):
            add = self.kw() == "ADD"
            if self.kw(1) in ("ROWGUIDCOL", "SPARSE"):
                self.i += 2
                return AlterColumnProperty(schema, table, column, add, self.kw(-1))
            if self.kw(1) == "NOT" and self.kw(2) == "FOR":
                self.i += 1
                self.expect("NOT", "FOR", "REPLICATION")
                return AlterColumnProperty(schema, table, column, add, "NOT FOR REPLICATION")
            what = self.kw(1)
            hints = {
                "HIDDEN": " (temporal tables: the HIDDEN flag of a period column)",
            }
            hint = hints.get(what, "")
            self.unsupported(f"ALTER COLUMN {self.kw()} {what}{hint}", tok)
        type_ = self.data_type()
        collation = self.ident("a collation name") if self.accept("COLLATE") else None
        if self.kw() == "MASKED":
            feature = (
                "MASKED WITH inside ALTER COLUMN with a data type (write ALTER COLUMN, then ALTER COLUMN "
                "... ADD MASKED WITH (FUNCTION = '...') as its own statement)"
            )
            self.unsupported(feature, self.peek())
        if self.accept("NULL"):
            nullable = True
        elif self.accept("NOT", "NULL"):
            nullable = False
        elif self.peek() is None or self.is_op(";") or self.kw() == "WITH":
            message = f"ALTER COLUMN {names.quote(column)} needs explicit NULL or NOT NULL"
            raise self.error("NF001", message, self.peek())
        else:
            self.fail("NULL or NOT NULL (ALTER COLUMN changes type, collation and nullability only)")
        _, exec_options = self.with_options(False, frozenset({"ONLINE"}), low_priority=False)
        return AlterColumn(schema, table, column, type_, nullable, collation, exec_options)

    # -------------------------------------------------------------- DROP and sp_rename
    def drop(self) -> Operation:
        word = self.kw()
        if word not in ("TABLE", "INDEX", "SEQUENCE", "SCHEMA", "TYPE", "SYNONYM"):
            self.fail("TABLE, INDEX, SEQUENCE, SCHEMA, TYPE or SYNONYM after DROP")
        self.i += 1
        self.no_if_exists()
        if word == "SCHEMA":
            return DropSchema(self.ident("a schema name"))
        if word == "INDEX":
            name = self.ident("an index name")
            self.expect("ON")
            schema, table = self.qualified("the table name")
            return DropIndex(schema, table, name)
        schema, name = self.qualified(f"the {word.lower()} name")
        if word == "TABLE":
            return DropTable(schema, name)
        if word == "SEQUENCE":
            return DropSequence(schema, name)
        if word == "TYPE":
            return DropType(schema, name)
        return DropSynonym(schema, name)

    def name_parts(self, tok: Tok) -> list[str]:
        """The parts of a qualified name that is written inside a string literal."""
        bad = self.error("SYNTAX", "the first argument of sp_rename is not a qualified name", tok)
        try:
            toks = significant(tokenize(tok.value))
        except LexError:
            raise bad from None
        parts: list[str] = []
        for n, part in enumerate(toks):
            if n % 2 == 0 and part.kind in ("word", "bident", "qident") and part.value:
                parts.append(part.value)
            elif not (n % 2 == 1 and part.kind == "op" and part.text == "."):
                raise bad
        if len(toks) % 2 == 0:
            raise bad
        return parts

    def sp_rename(self) -> Rename:
        tok = self.peek()
        procedure = self.dotted("sys.sp_rename")
        if [fold(p) for p in procedure] != ["sys", "sp_rename"]:
            raise self.error("SYNTAX", f"expected sys.sp_rename, found {'.'.join(procedure)}", tok)
        args: list[Tok] = []
        for n in range(3):
            if n:
                self.op(",")
            arg = self.peek()
            if arg is None or arg.kind != "nstring":
                self.fail("an N'...' literal (sp_rename takes three)")
            args.append(arg)
            self.i += 1
        kind = _RENAME_KINDS.get(args[2].value.upper())
        if kind is None:
            if args[2].value.upper() in ("USERDATATYPE", "STATISTICS", "DATABASE"):
                self.unsupported(f"sp_rename of {args[2].value.upper()}", args[2])
            raise self.error(
                "SYNTAX", "expected N'COLUMN', N'INDEX' or N'OBJECT' as the third argument", args[2]
            )
        old = self.name_parts(args[0])
        need = 2 if kind == "object" else 3
        if len(old) > need:
            self.unsupported("three-part names (the old name starts with the schema)", args[0])
        if len(old) < need:
            shape = "[schema].[name]" if need == 2 else "[schema].[table].[name]"
            raise self.error("SYNTAX", f"expected the old name as {shape}", args[0])
        new_name = args[1].value
        if not new_name or (new_name.startswith("[") and new_name.endswith("]")):
            message = "sp_rename takes the new name as it is written: one name, without brackets"
            raise self.error("SYNTAX", message, args[1])
        return Rename(kind, tuple(old), new_name)

    # -------------------------------------------------------------- one statement
    def statement(self) -> Operation:
        if self.accept("CREATE"):
            operation = self.create()
        elif self.accept("ALTER"):
            operation = self.alter()
        elif self.accept("DROP"):
            operation = self.drop()
        elif self.accept("EXEC") or self.accept("EXECUTE"):
            operation = self.sp_rename()
        else:
            self.fail("CREATE, ALTER, DROP or EXEC sys.sp_rename")
        self.accept_op(";")
        if self.peek() is not None:
            self.fail("the end of the statement")
        return operation


def parse_statement(text: str) -> Operation:
    """One migration model batch -> its operation.

    The closed grammar: CREATE / DROP TABLE; ALTER TABLE with one of ADD column, ALTER COLUMN,
    DROP COLUMN, ADD CONSTRAINT, DROP CONSTRAINT, SET (SYSTEM_VERSIONING = OFF | ON (HISTORY_TABLE =
    [schema].[table] [, DATA_CONSISTENCY_CHECK = ON | OFF] [, HISTORY_RETENTION_PERIOD = ...]));
    CREATE / DROP INDEX; CREATE / ALTER / DROP
    SEQUENCE; CREATE / DROP SCHEMA, TYPE, SYNONYM; EXEC sys.sp_rename with three N'' literals.
    Raises ParseError for anything else.
    """
    try:
        return _Parser(text, 0, migration=True).statement()
    except LexError as e:
        raise _lex_error(e, 0) from e


def parse_object_file(text: str, path: str) -> list[ModelObject]:
    """An object file of a table-class kind -> the one object it defines (a list of one).

    path is relative to the repository root with forward slashes ('schema/tables/sales.Order.sql')
    and must equal names.path_for() of the object. A table file is CREATE TABLE, then GO, then
    the CREATE INDEX statements of that table; they come back in Table.indexes. Every other file
    is one statement. Raises ParseError; raises ValueError when path is not a table-class path.
    """
    kind, _ = names.key_for_path(path)
    if kind not in names.TABLE_CLASS_KINDS:
        raise ValueError(f"not a table-class object file: {path!r}")
    try:
        batches = split_batches(text)
    except LexError as e:
        raise _lex_error(e, 0) from e
    if not batches:
        raise ParseError("SYNTAX", f"expected CREATE {kind}, found end of text", 1, 1)
    statement, field = _FILE_STATEMENT[kind]
    obj: ModelObject | None = None
    for batch in batches:
        parser = _Parser(batch.text, batch.first_line - 1, migration=False)
        first = parser.peek()
        if batch.repeat != 1:
            raise parser.error("SYNTAX", "GO with a count is not allowed", first)
        operation = parser.statement()
        if obj is None:
            if not isinstance(operation, statement):
                message = f"a file under schema/{names.KIND_DIRS[kind]}/ starts with CREATE {kind}"
                raise parser.error("SYNTAX", message, first)
            obj = getattr(operation, field)
            assert obj is not None
            try:
                expected = names.path_for(kind, None if isinstance(obj, Schema) else obj.schema, obj.name)
            except ValueError as e:
                raise parser.error("SYNTAX", str(e), first) from e
            if expected != path:
                raise parser.error("SYNTAX", f"{obj.key} belongs in the file {expected}", first)
        elif isinstance(obj, Table) and isinstance(operation, CreateIndex):
            if (fold(operation.schema), fold(operation.table)) != (fold(obj.schema), fold(obj.name)):
                on = names.qualified(operation.schema, operation.table)
                message = f"index {names.quote(operation.index.name)} is on {on}; this file holds {obj.key}"
                raise parser.error("SYNTAX", message, first)
            try:
                obj = replace(obj, indexes=(*obj.indexes, operation.index))
            except ValueError as e:
                raise parser.error("SYNTAX", str(e), first) from e
        else:
            message = (
                "one object per file: a table file holds CREATE TABLE, then GO, then the CREATE INDEX "
                "statements of that table; every other file holds one statement"
            )
            raise parser.error("SYNTAX", message, first)
    assert obj is not None
    return [obj]
