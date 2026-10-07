"""Typed model of table-class objects, the operations a migration may hold, and canonical JSON.

Rules that every user of this module relies on:
  * Names are kept as written and compared case-folded (the tool refuses case-sensitive catalog
    collations). `==` and `hash` of every class here use the canonical form, never the spelling.
    Use `fold()` or the lookup methods when you match a name yourself.
  * An Expression is a run of tokens. It is compared token by token and never parsed.
  * Everything is immutable. Model.add / remove / replace return a new Model.
  * The canonical form holds every dataclass field (it walks `dataclasses.fields`), so a new
    field takes part in equality without further code. A field named in `_sparse` is left out
    while it holds its default: that is how a later field is added without a change of any hash.

Public API:
    fold, is_reserved, builtin_shape, RESERVED_WORDS, BUILTIN_TYPES, INDEX_OPTION_VALUES, FK_ACTIONS,
    Expression, TypeRef, Identity, DefaultConstraint, Computed, Column, KeyColumn,
    PrimaryKey, Unique, ForeignKey, Check, Constraint, Index, Options,
    Schema, AliasType, TableType, Sequence, Synonym, Temporal, Table, UnversionedTable,
    ModelObject, Model, GENERATED_KINDS, RETENTION_UNITS, mask_spelling,
    ABSENT_IN_SELF, ABSENT_IN_OTHER (the two suffixes that Model.diff_paths uses),
    CreateSchema, DropSchema, CreateType, DropType, CreateSequence, AlterSequence, DropSequence,
    CreateSynonym, DropSynonym, CreateTable, DropTable, AddColumn, AlterColumn, DropColumn,
    AddConstraint, DropConstraint, CreateIndex, DropIndex, Rename, RenameKind,
    SetSystemVersioning, MaskColumn, UnmaskColumn, AlterColumnProperty, RebuildTable, Operation,
    COLUMN_PROPERTIES, TABLE_COMPRESSIONS, COLUMNSTORE_OPTION_VALUES

Wider table coverage: Identity.not_for_replication, Check.not_for_replication,
ForeignKey.not_for_replication, Column.rowguidcol, Column.sparse, Table.compression (the
DATA_COMPRESSION of a heap), Index.columnstore and the index option XML_COMPRESSION. Each of these
fields is written to the canonical form only when it is not the default, so an object that does
not use the feature keeps its JSON and its hash.

System-versioned temporal tables: Column.generated / Column.hidden mark the two period columns and
Table.temporal holds the period and the NAME of the history table. The history table is not an
object of the model: the engine owns its structure. These fields are written to the canonical
form only when they are not the default, so an object that does not use them keeps its JSON.

Dynamic data masking: Column.masked holds the masking function as the engine stores it in
sys.masked_columns.masking_function, for example default(), email(), random(1, 12),
partial(1, "XXXX", 0). That is the text between the quotes of the statement, a single quote still
written twice (the engine stores it so). It is compared as text, letter case included, and it is
written to the canonical form only when the column has a mask.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, fields
from functools import cached_property
from typing import Any, ClassVar, Literal, cast

from azsqlcd import names
from azsqlcd.lex import TRIVIA, LexError, tokenize

# T-SQL reserved keywords. A bare reserved word is never an identifier; [select] is one.
RESERVED_WORDS = frozenset(
    """ADD ALL ALTER AND ANY AS ASC AUTHORIZATION BACKUP BEGIN BETWEEN BREAK BROWSE BULK BY CASCADE CASE
CHECK CHECKPOINT CLOSE CLUSTERED COALESCE COLLATE COLUMN COMMIT COMPUTE CONSTRAINT CONTAINS CONTAINSTABLE
CONTINUE CONVERT CREATE CROSS CURRENT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP CURRENT_USER CURSOR
DATABASE DBCC DEALLOCATE DECLARE DEFAULT DELETE DENY DESC DISTINCT DISTRIBUTED DOUBLE DROP ELSE END ERRLVL
ESCAPE EXCEPT EXEC EXECUTE EXISTS EXIT EXTERNAL FETCH FILE FILLFACTOR FOR FOREIGN FREETEXT FREETEXTTABLE
FROM FULL FUNCTION GOTO GRANT GROUP HAVING HOLDLOCK IDENTITY IDENTITY_INSERT IDENTITYCOL IF IN INDEX INNER
INSERT INTERSECT INTO IS JOIN KEY KILL LEFT LIKE LINENO MERGE NATIONAL NOCHECK NONCLUSTERED NOT NULL
NULLIF OF OFF OFFSETS ON OPEN OPENDATASOURCE OPENQUERY OPENROWSET OPENXML OPTION OR ORDER OUTER OVER
PERCENT PIVOT PLAN PRIMARY PRINT PROC PROCEDURE PUBLIC RAISERROR READ READTEXT RECONFIGURE REFERENCES
REPLICATION RESTORE RESTRICT RETURN REVERT REVOKE RIGHT ROLLBACK ROWCOUNT ROWGUIDCOL RULE SAVE SCHEMA
SELECT SESSION_USER SET SETUSER SHUTDOWN SOME STATISTICS SYSTEM_USER TABLE TABLESAMPLE TEXTSIZE THEN TO
TOP TRAN TRANSACTION TRIGGER TRUNCATE TRY_CONVERT TSEQUAL UNION UNIQUE UNPIVOT UPDATE UPDATETEXT USE USER
VALUES VARYING VIEW WAITFOR WHEN WHERE WHILE WITH WRITETEXT""".split()
)

# Built-in data type (catalog name, lower case) -> which arguments the normal form requires.
#   none             no argument
#   length           (n)
#   length_or_max    (n) or (max)
#   precision_scale  (p, s), both
#   scale            (s): fractional seconds, as sys.columns.scale holds it
BUILTIN_TYPES: Mapping[str, str] = {
    "bigint": "none",
    "int": "none",
    "smallint": "none",
    "tinyint": "none",
    "bit": "none",
    "money": "none",
    "smallmoney": "none",
    "decimal": "precision_scale",
    "numeric": "precision_scale",
    "float": "none",
    "real": "none",
    "date": "none",
    "datetime": "none",
    "smalldatetime": "none",
    "datetime2": "scale",
    "time": "scale",
    "datetimeoffset": "scale",
    "char": "length",
    "nchar": "length",
    "binary": "length",
    "varchar": "length_or_max",
    "nvarchar": "length_or_max",
    "varbinary": "length_or_max",
    "text": "none",
    "ntext": "none",
    "image": "none",
    "uniqueidentifier": "none",
    "xml": "none",
    "sql_variant": "none",
    "hierarchyid": "none",
    "geography": "none",
    "geometry": "none",
    "sysname": "none",
    "timestamp": "none",
    "json": "none",
    "vector": "length",
}
# shape -> (length, precision, scale) must be present
_SHAPE_ARGS = {
    "none": (False, False, False),
    "length": (True, False, False),
    "length_or_max": (True, False, False),
    "precision_scale": (False, True, True),
    "scale": (False, False, True),
}

# Index options that are part of the model (closed list) -> allowed values; None = an integer.
INDEX_OPTION_VALUES: Mapping[str, tuple[str, ...] | None] = {
    "ALLOW_PAGE_LOCKS": ("ON", "OFF"),
    "ALLOW_ROW_LOCKS": ("ON", "OFF"),
    "DATA_COMPRESSION": ("NONE", "ROW", "PAGE"),
    "FILLFACTOR": None,
    "IGNORE_DUP_KEY": ("ON", "OFF"),
    "OPTIMIZE_FOR_SEQUENTIAL_KEY": ("ON", "OFF"),
    "PAD_INDEX": ("ON", "OFF"),
    "STATISTICS_NORECOMPUTE": ("ON", "OFF"),
    "XML_COMPRESSION": ("ON", "OFF"),
}
# Options of a columnstore index that are part of the model (closed list).
COLUMNSTORE_OPTION_VALUES: Mapping[str, tuple[str, ...] | None] = {
    "COMPRESSION_DELAY": None,  # minutes
    "DATA_COMPRESSION": ("COLUMNSTORE", "COLUMNSTORE_ARCHIVE"),
}
# DATA_COMPRESSION of a heap (CREATE TABLE ... WITH, ALTER TABLE ... REBUILD WITH).
TABLE_COMPRESSIONS = ("ROW", "PAGE")
# ALTER TABLE ... ALTER COLUMN c ADD | DROP <property>. (MASKED has its own operations.)
COLUMN_PROPERTIES = ("ROWGUIDCOL", "SPARSE", "NOT FOR REPLICATION")
FK_ACTIONS = ("NO ACTION", "CASCADE", "SET NULL", "SET DEFAULT")
GENERATED_KINDS = ("ROW_START", "ROW_END")  # Column.generated
RETENTION_UNITS = ("DAYS", "WEEKS", "MONTHS", "YEARS")  # Temporal.retention[1]

ABSENT_IN_OTHER = "(absent in other)"
ABSENT_IN_SELF = "(absent in self)"

Options = tuple[tuple[str, str], ...]


_MASK_CALL = re.compile(r"([A-Za-z_]+)[ \t]*\((.*)\)", re.DOTALL)
_MASK_INTEGER = re.compile(r"-?[0-9]+")
_MASK_DECIMAL = re.compile(r"(-?[0-9]+)(?:\.([0-9]*))?")


def mask_spelling(function: str, scale: int | None = None) -> str:
    """A masking function in the spelling of sys.masked_columns.masking_function.

    The engine stores the name in lower case, ', ' between the arguments, no other space outside
    the double-quoted argument and whole numbers without leading zeros: Partial(1,"xX",0) is stored
    as partial(1, "xX", 0). On a decimal or numeric column random() gets the scale of the column:
    give scale and random(1, 12.5) becomes random(1.00, 12.50). A text that is not name(arguments)
    comes back as it is: the engine decides about it.
    """
    match = _MASK_CALL.fullmatch(function)
    if match is None:
        return function
    name = match[1].lower()
    arguments: list[str] = []
    current, quoted = "", False
    for char in match[2]:
        if char == '"':
            quoted = not quoted
        if char == "," and not quoted:
            arguments.append(current)
            current = ""
        else:
            current += char
    if quoted:
        return function
    arguments.append(current)
    out: list[str] = []
    for argument in arguments:
        argument = argument.strip(" \t")
        if _MASK_INTEGER.fullmatch(argument):
            argument = str(int(argument))
        decimal = _MASK_DECIMAL.fullmatch(argument)
        if name == "random" and scale and decimal is not None and len(decimal[2] or "") <= scale:
            argument = f"{int(decimal[1])}.{(decimal[2] or '').ljust(scale, '0')}"
            if decimal[1].startswith("-") and int(decimal[1]) == 0:
                argument = "-" + argument
        out.append(argument)
    if out == [""]:
        return f"{name}()"
    return f"{name}({', '.join(out)})"


def _check_mask(function: str, where: str = "") -> None:
    if not isinstance(function, str) or not function.strip():
        raise ValueError(f"{where}the masking function cannot be empty")
    if "'" in function.replace("''", ""):
        raise ValueError(f"{where}a single quote in a masking function is written twice, as in the SQL text")


def fold(name: str) -> str:
    """Comparison form of an identifier or an object key. The catalog collation is case-insensitive."""
    return name.casefold()


def is_reserved(word: str) -> bool:
    """True for a bare word that is a T-SQL keyword. Keywords are ASCII: 'ſelect' is a name."""
    return word.isascii() and word.upper() in RESERVED_WORDS


def builtin_shape(name: str) -> str | None:
    """The argument shape of a built-in data type (see BUILTIN_TYPES), or None for any other name."""
    return BUILTIN_TYPES.get(name.lower()) if name.isascii() else None


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canon(value: object, expressions: bool, exact: bool = False) -> Any:
    """JSON-ready comparison form: every field, strings case-folded, unordered tuples sorted."""
    if isinstance(value, Expression):
        if not expressions:
            return {"class": "Expression"}
        return {"class": "Expression", "tokens": list(value.comparison)}
    if isinstance(value, _Node):
        out: dict[str, Any] = {"class": type(value).__name__}
        for f in fields(cast(Any, value)):
            if f.name in value._sparse and getattr(value, f.name) == f.default:
                continue
            item = _canon(getattr(value, f.name), expressions, f.name in value._exact)
            out[f.name] = sorted(item, key=_dumps) if f.name in value._unordered else item
        return out
    if isinstance(value, tuple):
        return [_canon(v, expressions, exact) for v in value]
    if isinstance(value, str):
        return value if exact else value.casefold()
    if value is None or isinstance(value, bool | int):
        return value
    raise TypeError(f"not a model value: {value!r}")


class _Node:
    """Base of every model and operation dataclass. Equality and hash use the canonical form."""

    _unordered: ClassVar[frozenset[str]] = frozenset()  # tuple fields whose order has no meaning
    _exact: ClassVar[frozenset[str]] = frozenset()  # fields that hold upper-case keywords, not names
    _sparse: ClassVar[frozenset[str]] = frozenset()  # fields left out of the form at their default

    def __eq__(self, other: object) -> bool:
        return type(other) is type(self) and _canon(self, True) == _canon(other, True)

    def __hash__(self) -> int:
        return hash(_dumps(_canon(self, True)))


# ------------------------------------------------------------------ expressions
def _is_bare(text: str) -> bool:
    try:
        toks = tokenize(text)
    except LexError:
        return False
    return len(toks) == 1 and toks[0].kind == "word"


def _comparison_token(text: str) -> str:
    toks = tokenize(text)
    if len(toks) != 1 or toks[0].kind in TRIVIA:
        raise ValueError(f"not one significant token: {text!r}")
    tok = toks[0]
    if tok.kind in ("string", "nstring"):
        return text
    if tok.kind == "word" and is_reserved(text):
        return text.casefold()
    if tok.kind in ("word", "bident", "qident"):
        if not tok.value:
            raise ValueError("an identifier cannot be empty")
        folded = tok.value.casefold()
        # [Status] and Status are one identifier; [select] is not the keyword select
        return folded if _is_bare(folded) and not is_reserved(folded) else names.quote(folded)
    return text.casefold()


@dataclass(frozen=True, eq=False)
class Expression(_Node):
    """DEFAULT, CHECK, computed-column or filter expression: the significant tokens as written.

    Every token between the introducing keyword and the next clause is kept, outer parentheses
    included. Two expressions are equal when their comparison forms are equal.
    """

    tokens: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.tokens:
            raise ValueError("an expression has at least one token")
        _ = self.comparison  # fail now on a text that is not one token

    @classmethod
    def from_sql(cls, text: str) -> Expression:
        """Tokenize expression text (for example from the catalog). Comments and white space go."""
        return cls(tuple(t.text for t in tokenize(text) if t.kind not in TRIVIA))

    @cached_property
    def comparison(self) -> tuple[str, ...]:
        """Keywords and identifiers case-folded, a bracketed identifier equal to its bare form,
        string literals exact. Each item is again one token, so the form is stable."""
        return tuple(_comparison_token(t) for t in self.tokens)


# ------------------------------------------------------------------ columns
@dataclass(frozen=True, eq=False)
class TypeRef(_Node):
    """A data type as declared. schema is set for an alias type and None for a built-in type."""

    name: str
    schema: str | None = None
    length: int | Literal["max"] | None = None
    precision: int | None = None
    scale: int | None = None

    def __post_init__(self) -> None:
        given = (self.length is not None, self.precision is not None, self.scale is not None)
        if self.schema is not None:
            if any(given):
                raise ValueError(f"alias type {self.name!r} takes no arguments")
            return
        shape = builtin_shape(self.name)
        if shape is None:
            raise ValueError(f"unknown built-in data type {self.name!r}")
        if given != _SHAPE_ARGS[shape] or (isinstance(self.length, str) and shape != "length_or_max"):
            raise ValueError(f"data type {self.name!r} needs the arguments of shape {shape!r}")
        if isinstance(self.length, str) and self.length != "max":
            raise ValueError(f"length is an integer or 'max', not {self.length!r}")


@dataclass(frozen=True, eq=False)
class Identity(_Node):
    _sparse: ClassVar[frozenset[str]] = frozenset({"not_for_replication"})

    seed: int
    increment: int
    not_for_replication: bool = False


@dataclass(frozen=True, eq=False)
class DefaultConstraint(_Node):
    name: str | None  # None only inside a table type, where a constraint cannot be named
    expression: Expression


@dataclass(frozen=True, eq=False)
class Computed(_Node):
    expression: Expression
    persisted: bool = False


@dataclass(frozen=True, eq=False)
class Column(_Node):
    _exact: ClassVar[frozenset[str]] = frozenset({"generated", "masked"})
    _sparse: ClassVar[frozenset[str]] = frozenset({"generated", "hidden", "masked", "rowguidcol", "sparse"})

    name: str
    type: TypeRef | None  # None for a computed column
    nullable: bool | None  # None only for a computed column that does not say NOT NULL
    identity: Identity | None = None
    default: DefaultConstraint | None = None
    computed: Computed | None = None
    collation: str | None = None  # None = not declared (the database default applies)
    generated: str | None = None  # GENERATED ALWAYS AS ROW START / END: one of GENERATED_KINDS
    hidden: bool = False  # HIDDEN; only on a generated column
    # MASKED WITH (FUNCTION = '...'): the text between the quotes as written, a ' still doubled.
    # That is the text of sys.masked_columns.masking_function: the engine keeps '' as two characters.
    masked: str | None = None
    rowguidcol: bool = False
    sparse: bool = False

    def __post_init__(self) -> None:
        if self.masked is not None:
            _check_mask(self.masked, f"column {self.name!r}: ")
            if self.computed is not None:
                raise ValueError(f"computed column {self.name!r} cannot be masked")
            if self.generated is not None:
                raise ValueError(f"generated column {self.name!r} cannot be masked")
        if self.generated is not None:
            if self.generated not in GENERATED_KINDS:
                raise ValueError(f"column {self.name!r}: generated is one of {GENERATED_KINDS}")
            if self.computed is not None or self.identity is not None:
                raise ValueError(f"generated column {self.name!r} cannot be computed or IDENTITY")
        elif self.hidden:
            raise ValueError(f"column {self.name!r}: HIDDEN goes with GENERATED ALWAYS and nothing else")
        if (self.type is None) == (self.computed is None):
            raise ValueError(f"column {self.name!r} has either a data type or a computed expression")
        if self.computed is None and self.nullable is None:
            raise ValueError(f"column {self.name!r} needs explicit nullability")
        if self.computed is not None and (self.identity or self.default or self.collation):
            raise ValueError(f"computed column {self.name!r} cannot have IDENTITY, DEFAULT or COLLATE")
        if (self.computed is not None or self.generated is not None) and (self.rowguidcol or self.sparse):
            raise ValueError(f"computed or generated column {self.name!r} cannot have ROWGUIDCOL or SPARSE")
        if self.rowguidcol and (
            self.type is None or self.type.schema is not None or fold(self.type.name) != "uniqueidentifier"
        ):
            raise ValueError(f"column {self.name!r}: ROWGUIDCOL goes with the data type uniqueidentifier")
        if self.sparse and (self.nullable is not True or self.identity is not None or self.rowguidcol):
            raise ValueError(
                f"column {self.name!r}: a SPARSE column is NULL and has no IDENTITY and no ROWGUIDCOL"
            )


# ------------------------------------------------------------------ constraints and indexes
def _check_options(
    options: Options, values: Mapping[str, tuple[str, ...] | None] = INDEX_OPTION_VALUES
) -> None:
    seen: set[str] = set()
    for name, value in options:
        if name not in values or name in seen:
            raise ValueError(f"unknown or repeated index option {name!r}")
        seen.add(name)
        allowed = values[name]
        number = value.isascii() and value.isdigit()
        if (allowed is None and not number) or (allowed is not None and value not in allowed):
            raise ValueError(f"index option {name} cannot be {value!r}")


@dataclass(frozen=True, eq=False)
class KeyColumn(_Node):
    name: str
    descending: bool = False


@dataclass(frozen=True, eq=False)
class PrimaryKey(_Node):
    _unordered: ClassVar[frozenset[str]] = frozenset({"options"})
    _exact: ClassVar[frozenset[str]] = frozenset({"options"})

    name: str | None  # None only inside a table type
    clustered: bool
    columns: tuple[KeyColumn, ...]
    options: Options = ()  # declared options from INDEX_OPTION_VALUES, sorted by name

    def __post_init__(self) -> None:
        _check_options(self.options)


@dataclass(frozen=True, eq=False)
class Unique(_Node):
    _unordered: ClassVar[frozenset[str]] = frozenset({"options"})
    _exact: ClassVar[frozenset[str]] = frozenset({"options"})

    name: str | None  # None only inside a table type
    clustered: bool
    columns: tuple[KeyColumn, ...]
    options: Options = ()

    def __post_init__(self) -> None:
        _check_options(self.options)


@dataclass(frozen=True, eq=False)
class ForeignKey(_Node):
    _exact: ClassVar[frozenset[str]] = frozenset({"on_delete", "on_update"})
    _sparse: ClassVar[frozenset[str]] = frozenset({"not_for_replication"})

    name: str
    columns: tuple[str, ...]
    ref_schema: str
    ref_table: str
    ref_columns: tuple[str, ...]
    on_delete: str = "NO ACTION"  # one of FK_ACTIONS
    on_update: str = "NO ACTION"
    not_for_replication: bool = False

    def __post_init__(self) -> None:
        if self.on_delete not in FK_ACTIONS or self.on_update not in FK_ACTIONS:
            raise ValueError(f"foreign key {self.name!r}: the action is one of {FK_ACTIONS}")
        if len(self.columns) != len(self.ref_columns) or not self.columns:
            raise ValueError(f"foreign key {self.name!r}: column lists differ in length or are empty")


@dataclass(frozen=True, eq=False)
class Check(_Node):
    _sparse: ClassVar[frozenset[str]] = frozenset({"not_for_replication"})

    name: str | None  # None only inside a table type
    expression: Expression
    not_for_replication: bool = False


Constraint = PrimaryKey | Unique | ForeignKey | Check


@dataclass(frozen=True, eq=False)
class Index(_Node):
    """A rowstore index or, with columnstore, a columnstore index. Included columns and options
    have no order.

    A columnstore index is not unique. columns holds its ORDER columns (most have none);
    included holds the column list of a nonclustered one. A clustered one holds every column of
    the table, so it has no list and no filter. Its options come from COLUMNSTORE_OPTION_VALUES."""

    _unordered: ClassVar[frozenset[str]] = frozenset({"included", "options"})
    _exact: ClassVar[frozenset[str]] = frozenset({"options"})
    _sparse: ClassVar[frozenset[str]] = frozenset({"columnstore"})

    name: str
    unique: bool
    clustered: bool
    columns: tuple[KeyColumn, ...]
    included: tuple[str, ...] = ()
    filter: Expression | None = None
    options: Options = ()  # declared options from INDEX_OPTION_VALUES, sorted by name
    columnstore: bool = False

    def __post_init__(self) -> None:
        if not self.columnstore:
            _check_options(self.options)
            return
        _check_options(self.options, COLUMNSTORE_OPTION_VALUES)
        where = f"columnstore index {self.name!r}"
        if self.unique or any(c.descending for c in self.columns):
            raise ValueError(f"{where} cannot be UNIQUE and its ORDER columns cannot be DESC")
        if self.clustered and (self.included or self.filter is not None):
            raise ValueError(f"{where} is clustered: it has no column list and no filter")
        if not self.clustered and not self.included:
            raise ValueError(f"{where} is nonclustered: it needs a column list")


# ------------------------------------------------------------------ objects
@dataclass(frozen=True, eq=False)
class Schema(_Node):
    kind: ClassVar[str] = "SCHEMA"

    name: str
    owner: str | None = None  # AUTHORIZATION; None = not declared

    @property
    def key(self) -> str:
        return names.object_key(self.kind, None, self.name)


@dataclass(frozen=True, eq=False)
class _SchemaObject(_Node):
    kind: ClassVar[str]

    schema: str
    name: str

    @property
    def key(self) -> str:
        return names.object_key(self.kind, self.schema, self.name)


@dataclass(frozen=True, eq=False)
class AliasType(_SchemaObject):
    kind: ClassVar[str] = "TYPE"

    base: TypeRef
    nullable: bool


@dataclass(frozen=True, eq=False)
class Sequence(_SchemaObject):
    kind: ClassVar[str] = "SEQUENCE"

    type: TypeRef
    start: int
    increment: int
    minvalue: int
    maxvalue: int
    cycle: bool
    cached: bool  # False = NO CACHE
    cache_size: int | None = None  # None with cached = the engine default size


@dataclass(frozen=True, eq=False)
class Synonym(_SchemaObject):
    kind: ClassVar[str] = "SYNONYM"

    target_schema: str
    target_name: str


def _duplicate(found: Iterable[str | None]) -> str | None:
    seen: set[str] = set()
    for name in found:
        if name is None:
            continue
        if fold(name) in seen:
            return name
        seen.add(fold(name))
    return None


@dataclass(frozen=True, eq=False)
class _Tabular(_SchemaObject):
    """Columns in declared order; constraints and indexes have no order."""

    _unordered: ClassVar[frozenset[str]] = frozenset({"constraints", "indexes"})

    columns: tuple[Column, ...]
    constraints: tuple[Constraint, ...] = ()
    indexes: tuple[Index, ...] = ()

    def __post_init__(self) -> None:
        where = f"{self.kind} {names.qualified(self.schema, self.name)}"
        if not self.columns:
            raise ValueError(f"{where} has no column")
        keys = [c.name for c in self.constraints if isinstance(c, PrimaryKey | Unique)]
        defaults = [c.default.name for c in self.columns if c.default is not None]
        # three engine namespaces: columns; schema-scoped constraint objects; indexes of the table
        for what, found in (
            ("columns", [c.name for c in self.columns]),
            ("constraints", [c.name for c in self.constraints] + defaults),
            ("indexes", [i.name for i in self.indexes] + keys),
        ):
            name = _duplicate(found)
            if name is not None:
                raise ValueError(f"{where} has two {what} named {names.quote(name)}")
        if sum(c.rowguidcol for c in self.columns) > 1:
            raise ValueError(f"{where} has more than one ROWGUIDCOL column")
        if sum(i.columnstore for i in self.indexes) > 1:
            raise ValueError(f"{where} has more than one columnstore index")

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if fold(c.name) == fold(name)), None)

    def constraint(self, name: str) -> Constraint | None:
        return next((c for c in self.constraints if c.name is not None and fold(c.name) == fold(name)), None)

    def index(self, name: str) -> Index | None:
        return next((i for i in self.indexes if fold(i.name) == fold(name)), None)

    def clustered_item(self) -> PrimaryKey | Unique | Index | None:
        """The clustered key or index (rowstore or columnstore); None for a heap."""
        keys = [c for c in self.constraints if isinstance(c, PrimaryKey | Unique) and c.clustered]
        return next(iter([*keys, *(i for i in self.indexes if i.clustered)]), None)


@dataclass(frozen=True, eq=False)
class Temporal(_Node):
    """SYSTEM_VERSIONING = ON: the period columns and the name of the history table.

    The history table is not an object of the model; the engine owns its structure.
    retention None = INFINITE, else (n, unit) with the unit one of RETENTION_UNITS.
    """

    _exact: ClassVar[frozenset[str]] = frozenset({"retention"})

    period_start: str
    period_end: str
    history_schema: str
    history_table: str
    retention: tuple[int, str] | None = None

    def __post_init__(self) -> None:
        _check_retention(self.retention)


_MAX_RETENTION = 2147483647


def _check_retention(retention: tuple[int, str] | None) -> None:
    if retention is None:
        return
    ok = isinstance(retention, tuple) and len(retention) == 2
    # sys.tables.history_retention_period is an int: the engine takes no larger number
    whole = ok and type(retention[0]) is int and 1 <= retention[0] <= _MAX_RETENTION
    if not whole or retention[1] not in RETENTION_UNITS:
        raise ValueError(
            f"retention is None (INFINITE) or (1 <= n <= {_MAX_RETENTION}, one of {RETENTION_UNITS}), "
            f"not {retention!r}"
        )


@dataclass(frozen=True, eq=False)
class Table(_Tabular):
    kind: ClassVar[str] = "TABLE"
    _exact: ClassVar[frozenset[str]] = frozenset({"compression"})
    _sparse: ClassVar[frozenset[str]] = frozenset({"temporal", "compression"})
    _period_needs_versioning: ClassVar[bool] = True

    temporal: Temporal | None = None  # None = not system-versioned
    # DATA_COMPRESSION of the heap: ROW or PAGE; None = not compressed. A table with a clustered
    # key or index states its compression there, as an option.
    compression: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        where = f"TABLE {names.qualified(self.schema, self.name)}"
        unnamed = [c for c in self.constraints if c.name is None]
        if unnamed or any(c.default is not None and c.default.name is None for c in self.columns):
            raise ValueError(f"{where} has an unnamed constraint")
        if self.compression is not None:
            if self.compression not in TABLE_COMPRESSIONS:
                raise ValueError(f"{where}: DATA_COMPRESSION of a table is ROW or PAGE")
            clustered = self.clustered_item()
            if clustered is not None:
                raise ValueError(
                    f"{where}: DATA_COMPRESSION of the table is the compression of a heap; state it on "
                    f"the clustered key or index {names.quote(clustered.name or '')}"
                )
        generated = [c for c in self.columns if c.generated is not None]
        if self.temporal is None:
            if generated and self._period_needs_versioning:
                raise ValueError(
                    f"{where}: column {names.quote(generated[0].name)} is GENERATED ALWAYS, "
                    "but the table is not system-versioned"
                )
            return
        t = self.temporal
        if fold(t.period_start) == fold(t.period_end):
            raise ValueError(f"{where}: the period needs two different columns")
        for name, kind in ((t.period_start, "ROW_START"), (t.period_end, "ROW_END")):
            col = self.column(name)
            if col is None or col.generated != kind:
                raise ValueError(
                    f"{where}: period column {names.quote(name)} must be GENERATED ALWAYS AS {kind}"
                )
            if col.type is None or col.type.schema is not None or col.type.name.lower() != "datetime2":
                raise ValueError(f"{where}: period column {names.quote(name)} must be datetime2")
            if col.nullable is not False:
                raise ValueError(f"{where}: period column {names.quote(name)} must be NOT NULL")
        start, end = self.column(t.period_start), self.column(t.period_end)
        if start is not None and end is not None and start.type != end.type:
            # the engine refuses two precisions (error 13513)
            raise ValueError(f"{where}: the two period columns must have one datetime2 scale")
        if len(generated) != 2:
            raise ValueError(f"{where}: only the two period columns can be GENERATED ALWAYS")
        if not any(isinstance(c, PrimaryKey) for c in self.constraints):
            raise ValueError(f"{where}: a system-versioned table needs a PRIMARY KEY")


@dataclass(frozen=True, eq=False)
class UnversionedTable(Table):
    """A table that still has its period columns after SYSTEM_VERSIONING = OFF.

    Only a replay holds one, between the statement that switches versioning off and the DROP TABLE
    (or the statement that switches it on again). A table file cannot describe it, so it is never
    equal to a Table: a history that ends in this state does not match any declared model.
    """

    _period_needs_versioning: ClassVar[bool] = False

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.temporal is not None:
            raise ValueError(f"TABLE {names.qualified(self.schema, self.name)}: versioning is on")


@dataclass(frozen=True, eq=False)
class TableType(_Tabular):
    """CREATE TYPE ... AS TABLE. Its constraints cannot be named and it has no foreign key."""

    kind: ClassVar[str] = "TYPE"

    def __post_init__(self) -> None:
        super().__post_init__()
        if any(isinstance(c, ForeignKey) for c in self.constraints):
            raise ValueError(f"TYPE {names.qualified(self.schema, self.name)} cannot have a foreign key")
        kind = f"TYPE {names.qualified(self.schema, self.name)}"
        if any(c.sparse for c in self.columns):
            raise ValueError(f"{kind} cannot have a SPARSE column")
        not_for_replication = [c for c in self.constraints if isinstance(c, Check) and c.not_for_replication]
        if not_for_replication or any(c.identity and c.identity.not_for_replication for c in self.columns):
            raise ValueError(f"{kind} cannot have NOT FOR REPLICATION")
        if any(i.columnstore for i in self.indexes):
            raise ValueError(f"{kind} cannot have a columnstore index")
        if any(c.masked is not None for c in self.columns):
            where = f"TYPE {names.qualified(self.schema, self.name)}"
            raise ValueError(f"{where}: this version has no masked column in a table type")


ModelObject = Schema | AliasType | TableType | Sequence | Synonym | Table

_MODEL_CLASSES: dict[str, type] = {
    cls.__name__: cls
    for cls in (
        Expression,
        TypeRef,
        Identity,
        DefaultConstraint,
        Computed,
        Column,
        KeyColumn,
        PrimaryKey,
        Unique,
        ForeignKey,
        Check,
        Index,
        Schema,
        AliasType,
        Sequence,
        Synonym,
        Temporal,
        Table,
        UnversionedTable,
        TableType,
    )
}


def _revive(value: Any) -> Any:
    if isinstance(value, dict):
        cls = _MODEL_CLASSES.get(str(value.get("class")))
        if cls is None:
            raise ValueError(f"not a model class: {value.get('class')!r}")
        try:
            return cls(**{k: _revive(v) for k, v in value.items() if k != "class"})
        except TypeError as e:
            raise ValueError(f"{cls.__name__}: {e}") from e
    if isinstance(value, list):
        return tuple(_revive(v) for v in value)
    return value


def _labels(items: list[Any]) -> dict[str, Any] | None:
    """Items by name ([{name: ...}] or [[name, value]]); None when they have no unique names."""
    out: dict[str, Any] = {}
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            label, value = item["name"], item
        elif isinstance(item, list) and len(item) == 2 and isinstance(item[0], str):
            label, value = item[0], item[1]
        else:
            return None
        if label in out:
            return None
        out[label] = value
    return out


def _diff(a: Any, b: Any, path: str) -> list[str]:
    if a == b:
        return []
    if isinstance(a, dict) and isinstance(b, dict):
        if a["class"] != b["class"]:
            return [f"{path}.class" if path else "class"]
        if a["class"] == "Expression":
            return [path]  # never a token index: the path must not describe expression text
        keys = [k for k in dict.fromkeys([*a, *b]) if k != "class"]  # a sparse field can be in one only
        return [p for k in keys for p in _diff(a.get(k), b.get(k), f"{path}.{k}" if path else k)]
    if isinstance(a, list) and isinstance(b, list):
        la, lb = _labels(a), _labels(b)
        if la is None or lb is None:
            if len(a) != len(b):
                return [f"{path} (count)"]
            return [p for i, (x, y) in enumerate(zip(a, b, strict=True)) for p in _diff(x, y, f"{path}[{i}]")]
        out: list[str] = []
        for label in sorted(la.keys() | lb.keys()):
            here = f"{path}[{label}]"
            if label not in lb:
                out.append(f"{here} {ABSENT_IN_OTHER}")
            elif label not in la:
                out.append(f"{here} {ABSENT_IN_SELF}")
            else:
                out.extend(_diff(la[label], lb[label], here))
        return out or [f"{path} (order)"]  # the same items in another order
    return [path]


class Model(Mapping[str, ModelObject]):
    """Object key (names.object_key) -> object. Lookup ignores case; iteration is in key order."""

    __slots__ = ("_objects",)

    def __init__(self, objects: Iterable[ModelObject] = ()) -> None:
        found: dict[str, ModelObject] = {}
        for obj in objects:
            if fold(obj.key) in found:
                raise ValueError(f"object exists: {obj.key}")
            found[fold(obj.key)] = obj
        self._objects: dict[str, ModelObject] = dict(sorted(found.items()))

    def __getitem__(self, key: str) -> ModelObject:
        try:
            return self._objects[fold(key)]
        except KeyError:
            raise KeyError(key) from None

    def __iter__(self) -> Iterator[str]:
        return (obj.key for obj in self._objects.values())

    def __len__(self) -> int:
        return len(self._objects)

    def __repr__(self) -> str:
        return f"Model({list(self)})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Model) and self.to_canonical_json() == other.to_canonical_json()

    def __hash__(self) -> int:
        return hash(self.to_canonical_json())

    def add(self, obj: ModelObject) -> Model:
        """A new model that also holds obj. ValueError when its key exists."""
        return Model([*self._objects.values(), obj])

    def remove(self, key: str) -> Model:
        """A new model without the object. KeyError when the key is absent."""
        if key not in self:
            raise KeyError(key)
        return Model(obj for k, obj in self._objects.items() if k != fold(key))

    def replace(self, obj: ModelObject) -> Model:
        """A new model where obj takes the place of the object with the same key. KeyError when absent."""
        if obj.key not in self:
            raise KeyError(obj.key)
        return Model(obj if k == fold(obj.key) else old for k, old in self._objects.items())

    def to_canonical_json(self) -> str:
        """Sorted, compact JSON of the comparison form. Equal models give equal text."""
        return _dumps({k: _canon(obj, True) for k, obj in self._objects.items()})

    @classmethod
    def from_canonical_json(cls, text: str) -> Model:
        """Read to_canonical_json() back. The result is equal to the original model, but names
        and expressions come back in comparison form: compare and store it, do not emit from it."""
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("canonical model JSON is an object of object keys")
        objects = [_revive(value) for value in data.values()]
        if not all(isinstance(obj, Schema | _SchemaObject) for obj in objects):
            raise ValueError("canonical model JSON holds something that is not a schema object")
        model = cls(objects)
        if list(model._objects) != sorted(data):
            raise ValueError("canonical model JSON: a key does not match its object")
        return model

    def diff_paths(self, other: Model, *, expressions: bool = True) -> list[tuple[str, str]]:
        """(object key, property path) for every difference, in key order. Empty when equal.

        A path names properties and case-folded names only, never expression text, for example
        'columns[status].type.length' or 'constraints[pk_order] (absent in other)'. With
        expressions=False the presence of an expression is compared and its text is not.
        """
        out: list[tuple[str, str]] = []
        for k in sorted(self._objects.keys() | other._objects.keys()):
            mine, theirs = self._objects.get(k), other._objects.get(k)
            if mine is None or theirs is None:
                present = mine if mine is not None else theirs
                assert present is not None
                out.append((present.key, ABSENT_IN_SELF if mine is None else ABSENT_IN_OTHER))
                continue
            paths = _diff(_canon(mine, expressions), _canon(theirs, expressions), "")
            out.extend((mine.key, p) for p in paths)
        return out


# ------------------------------------------------------------------ operations (one per statement)
@dataclass(frozen=True, eq=False)
class CreateSchema(_Node):
    schema: Schema


@dataclass(frozen=True, eq=False)
class DropSchema(_Node):
    name: str


@dataclass(frozen=True, eq=False)
class CreateType(_Node):
    type: AliasType | TableType


@dataclass(frozen=True, eq=False)
class DropType(_Node):
    schema: str
    name: str


@dataclass(frozen=True, eq=False)
class CreateSequence(_Node):
    sequence: Sequence


@dataclass(frozen=True, eq=False)
class AlterSequence(_Node):
    """None = the clause is not written and the property does not change."""

    schema: str
    name: str
    restart: bool = False  # RESTART is written
    restart_with: int | None = None  # RESTART WITH n
    increment: int | None = None
    minvalue: int | None = None
    maxvalue: int | None = None
    cycle: bool | None = None
    cached: bool | None = None  # CACHE = True, NO CACHE = False
    cache_size: int | None = None  # CACHE n


@dataclass(frozen=True, eq=False)
class DropSequence(_Node):
    schema: str
    name: str


@dataclass(frozen=True, eq=False)
class CreateSynonym(_Node):
    synonym: Synonym


@dataclass(frozen=True, eq=False)
class DropSynonym(_Node):
    schema: str
    name: str


@dataclass(frozen=True, eq=False)
class CreateTable(_Node):
    table: Table


@dataclass(frozen=True, eq=False)
class DropTable(_Node):
    schema: str
    name: str


@dataclass(frozen=True, eq=False)
class AddColumn(_Node):
    schema: str
    table: str
    column: Column
    with_values: bool = False  # DEFAULT ... WITH VALUES: existing rows get the default


@dataclass(frozen=True, eq=False)
class AlterColumn(_Node):
    """ALTER COLUMN states the whole of type, collation and nullability; nothing else changes."""

    _unordered: ClassVar[frozenset[str]] = frozenset({"exec_options"})
    _exact: ClassVar[frozenset[str]] = frozenset({"exec_options"})

    schema: str
    table: str
    column: str
    type: TypeRef
    nullable: bool
    collation: str | None = None  # None = not written (the database default applies)
    exec_options: Options = ()  # ONLINE only; not part of the model


@dataclass(frozen=True, eq=False)
class AlterColumnProperty(_Node):
    """ALTER TABLE ... ALTER COLUMN c ADD | DROP ROWGUIDCOL | SPARSE | NOT FOR REPLICATION."""

    _exact: ClassVar[frozenset[str]] = frozenset({"property"})

    schema: str
    table: str
    column: str
    add: bool  # ADD = True, DROP = False
    property: str  # one of COLUMN_PROPERTIES

    def __post_init__(self) -> None:
        if self.property not in COLUMN_PROPERTIES:
            raise ValueError(f"not a column property: {self.property!r}")


@dataclass(frozen=True, eq=False)
class RebuildTable(_Node):
    """ALTER TABLE ... REBUILD WITH (DATA_COMPRESSION = NONE | ROW | PAGE)."""

    _unordered: ClassVar[frozenset[str]] = frozenset({"exec_options"})
    _exact: ClassVar[frozenset[str]] = frozenset({"compression", "exec_options"})

    schema: str
    table: str
    compression: str  # NONE | ROW | PAGE: always written
    exec_options: Options = ()  # ONLINE, MAXDOP, SORT_IN_TEMPDB; not part of the model

    def __post_init__(self) -> None:
        if self.compression not in ("NONE", *TABLE_COMPRESSIONS):
            raise ValueError(f"DATA_COMPRESSION of a table cannot be {self.compression!r}")


@dataclass(frozen=True, eq=False)
class DropColumn(_Node):
    schema: str
    table: str
    column: str


@dataclass(frozen=True, eq=False)
class AddConstraint(_Node):
    _unordered: ClassVar[frozenset[str]] = frozenset({"exec_options"})
    _exact: ClassVar[frozenset[str]] = frozenset({"exec_options"})

    schema: str
    table: str
    constraint: Constraint | DefaultConstraint
    for_column: str | None = None  # set when (and only when) the constraint is a DefaultConstraint
    with_check: bool | None = None  # WITH CHECK = True, WITH NOCHECK = False, not written = None
    exec_options: Options = ()  # PRIMARY KEY and UNIQUE only; not part of the model

    def __post_init__(self) -> None:
        if isinstance(self.constraint, DefaultConstraint) != (self.for_column is not None):
            raise ValueError("for_column goes with a DefaultConstraint and with nothing else")


@dataclass(frozen=True, eq=False)
class DropConstraint(_Node):
    schema: str
    table: str
    name: str


@dataclass(frozen=True, eq=False)
class CreateIndex(_Node):
    _unordered: ClassVar[frozenset[str]] = frozenset({"exec_options"})
    _exact: ClassVar[frozenset[str]] = frozenset({"exec_options"})

    schema: str
    table: str
    index: Index
    exec_options: Options = ()  # ONLINE, RESUMABLE, ...; not part of the model


@dataclass(frozen=True, eq=False)
class DropIndex(_Node):
    schema: str
    table: str
    name: str


RenameKind = Literal["table", "column", "index", "constraint", "object"]
_RENAME_PARTS = {"table": 2, "constraint": 2, "object": 2, "column": 3, "index": 3}


@dataclass(frozen=True, eq=False)
class Rename(_Node):
    """EXEC sys.sp_rename.

    old holds the parts of the old name: (schema, table) for a table, (schema, constraint) for a
    constraint, (schema, table, name) for a column or an index. The parser returns kind 'object'
    for N'OBJECT': the statement text cannot say whether the name is a table or a constraint.
    """

    _exact: ClassVar[frozenset[str]] = frozenset({"kind"})

    kind: RenameKind
    old: tuple[str, ...]
    new_name: str

    def __post_init__(self) -> None:
        if _RENAME_PARTS.get(self.kind) != len(self.old):
            raise ValueError(f"rename of kind {self.kind!r} cannot have the old name {self.old!r}")


@dataclass(frozen=True, eq=False)
class SetSystemVersioning(_Node):
    """ALTER TABLE ... SET (SYSTEM_VERSIONING = ON (...) | OFF).

    The tool generates only on = False (before DROP TABLE of a temporal table). on = True is for a
    hand-written migration: it names the history table, and DATA_CONSISTENCY_CHECK is not kept.
    """

    _exact: ClassVar[frozenset[str]] = frozenset({"retention"})

    schema: str
    table: str
    on: bool
    history_schema: str | None = None
    history_table: str | None = None
    retention: tuple[int, str] | None = None

    def __post_init__(self) -> None:
        _check_retention(self.retention)
        named = (self.history_schema is not None, self.history_table is not None)
        if named != (self.on, self.on) or (not self.on and self.retention is not None):
            raise ValueError("SYSTEM_VERSIONING = ON names the history table; OFF names nothing")


@dataclass(frozen=True, eq=False)
class MaskColumn(_Node):
    """ALTER TABLE ... ALTER COLUMN c ADD MASKED WITH (FUNCTION = '...').

    It sets the mask of a column that has none and replaces the mask of a column that has one.
    """

    _exact: ClassVar[frozenset[str]] = frozenset({"function"})

    schema: str
    table: str
    column: str
    function: str  # the text between the quotes as written (Column.masked)

    def __post_init__(self) -> None:
        _check_mask(self.function)


@dataclass(frozen=True, eq=False)
class UnmaskColumn(_Node):
    """ALTER TABLE ... ALTER COLUMN c DROP MASKED: every reader sees the real values after it."""

    schema: str
    table: str
    column: str


Operation = (
    CreateSchema
    | DropSchema
    | CreateType
    | DropType
    | CreateSequence
    | AlterSequence
    | DropSequence
    | CreateSynonym
    | DropSynonym
    | CreateTable
    | DropTable
    | AddColumn
    | AlterColumn
    | AlterColumnProperty
    | RebuildTable
    | DropColumn
    | AddConstraint
    | DropConstraint
    | CreateIndex
    | DropIndex
    | Rename
    | SetSystemVersioning
    | MaskColumn
    | UnmaskColumn
)
