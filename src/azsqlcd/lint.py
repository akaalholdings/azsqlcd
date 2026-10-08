"""Lint: findings with codes, the batch classifier and the allow lines.

classify_batch reads one migration batch with the lexer only; there is no SQL parser here. It
matches unquoted keywords, so a word inside a string, a comment or a quoted identifier is never a
finding. lint_repo holds the rules that need one revision, lint_change the rules that need the
base revision too. `verify` reports both lists.

A statement that needs an allow line and has none is a finding whose code is the allow code
(DROP_TABLE, ...). The one exception is LONG_LOCK, whose finding is LCK001.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass

from azsqlcd import chain, config, lex, modules, names
from azsqlcd.errors import ToolError, refused
from azsqlcd.lex import Tok

ERROR = "error"
WARNING = "warning"

# The closed list of codes that an allow line can carry (design (h), A10, A11).
ALLOW_CODES = frozenset(
    {
        "DROP_TABLE",
        "DROP_COLUMN",
        "ALTER_COLUMN_LOSSY",
        "SET_NOT_NULL",
        "DROP_SEQUENCE",
        "DROP_TYPE",
        "DROP_SCHEMA",
        "RENAME",
        "TEMPORAL_OFF",
        "UNMASK",
        "LONG_LOCK",
        "DATA_NO_WHERE",
        "TRUNCATE",
        "DYNAMIC_SQL",
        "EXEC_PROC",
        "RAW",
        "OLD_MODULE",
        "REPLACEMENT_EDGE",
    }
)
# Allow codes that the lexer cannot match to a statement. SET_NOT_NULL and REPLACEMENT_EDGE belong
# to the model proof; OLD_MODULE is matched by lint_change and stops matching after the merge. An
# allow line with one of these codes is never ALLOW_UNUSED.
DEFERRED_ALLOW_CODES = frozenset({"SET_NOT_NULL", "OLD_MODULE", "REPLACEMENT_EDGE"})
# The object of the DYNAMIC_SQL item: dynamic SQL names no object, the allow line covers the batch.
BATCH_OBJECT = "batch"

# Every code that this module gives to a finding, with its severity. A finding that comes from a
# ToolError of chain, modules or config keeps the reason code of that error.
CODES: dict[str, str] = {
    # a statement that needs an allow line and has none
    "DROP_TABLE": ERROR,
    "DROP_COLUMN": ERROR,
    "ALTER_COLUMN_LOSSY": ERROR,
    "DROP_SEQUENCE": ERROR,
    "DROP_TYPE": ERROR,
    "DROP_SCHEMA": ERROR,
    "RENAME": ERROR,
    "TEMPORAL_OFF": ERROR,
    "UNMASK": ERROR,
    "LCK001": ERROR,  # LONG_LOCK
    "DATA_NO_WHERE": ERROR,
    "TRUNCATE": ERROR,
    "DYNAMIC_SQL": ERROR,
    "EXEC_PROC": ERROR,
    "RAW": ERROR,
    # allow lines
    "ALLOW_UNUSED": ERROR,
    "ALLOW_REASON": ERROR,
    "ALLOW_FORMAT": ERROR,
    # migration batches
    "FORBIDDEN_TOKEN": ERROR,
    "THREE_PART_NAME": ERROR,
    "SECURITY_STATEMENT": ERROR,
    "DATA000": ERROR,
    "DATA_DDL": ERROR,
    "DATA_FORBIDDEN": ERROR,
    "MODEL_STATEMENT": ERROR,
    "STATEMENT_UNREADABLE": ERROR,
    "DROP_INDEX": WARNING,
    "DROP_CONSTRAINT": WARNING,
    "NTX004": WARNING,
    "NNL001": WARNING,
    "NTX005": ERROR,
    "NTX006": ERROR,
    # files of one revision
    "SECRET_LITERAL": ERROR,
    "UNKNOWN_PATH": ERROR,
    "FILE_INVALID": ERROR,
    "MODULE_INVALID": ERROR,
    "MIGRATION_INVALID": ERROR,
    "CHAIN_INVALID": ERROR,
    "TOMBSTONE_INVALID": ERROR,
    "CONFIG_INVALID": ERROR,
    "CHN002": ERROR,
    "CHN003": ERROR,
    "CHN004": ERROR,
    "CHN005": ERROR,
    "ORD004": ERROR,
    "CYCLE_BROKEN": WARNING,
    "EXA001": WARNING,
    "MODULE_DUPLICATE": ERROR,
    "DIR001": ERROR,
    "DIR002": WARNING,
    "TMB002": ERROR,
    # a change against the base revision
    "CHN001": ERROR,
    "CHN006": ERROR,
    "KND001": ERROR,
    "TMB001": ERROR,
    "MODULE_CASE": WARNING,
    "NTX003": ERROR,
    "DAT001": ERROR,
    "REN001": WARNING,
    "DRP002": WARNING,
    "DRP003": WARNING,
}

_CONFIG_PATH = "azsqlcd.toml"
_DATA_OFF = (
    "data batches are switched off; this version manages structural changes; set data_batches = true "
    "in azsqlcd.toml to use them"
)
_IDENT = ("word", "bident", "qident")
_EXEC = ("EXEC", "EXECUTE")
_TRAN = ("TRAN", "TRANSACTION")
_MODEL_HINT = (
    "a model batch is one statement of: CREATE or DROP TABLE, ALTER TABLE ADD / ALTER COLUMN / DROP / "
    "REBUILD WITH (DATA_COMPRESSION = ...) / SET (SYSTEM_VERSIONING = ...), CREATE or DROP INDEX, "
    "CREATE, ALTER or DROP SEQUENCE, CREATE or DROP SCHEMA, TYPE, SYNONYM, EXEC sys.sp_rename. DML goes "
    "in a '-- azsqlcd:data' batch, other DDL in a '-- azsqlcd:raw' batch"
)

# word -> the next words that make the pair forbidden; () = the word alone is forbidden
type _Phrases = dict[str, tuple[str, ...]]
_FORBIDDEN: _Phrases = {
    "BEGIN": (*_TRAN, "DISTRIBUTED"),
    "SAVE": _TRAN,
    "COMMIT": (),
    "ROLLBACK": (),
    "RAISERROR": (),
    "RETURN": (),
    "GOTO": (),
}
# SET options that no migration batch can name, with any value. One SET statement can hold a list
# of options (SET NOCOUNT, XACT_ABORT OFF), so every option of the list is read.
#   NOEXEC, PARSEONLY: later batches of the session would not run
#   XACT_ABORT: an error would no longer end the transaction of the tool
#   IMPLICIT_TRANSACTIONS, ANSI_DEFAULTS (which sets it): the writes of the tool after its COMMIT
#       would open a transaction that nothing commits
#   ROWCOUNT, FMTONLY: the writes and reads of the tool itself would touch fewer rows, or none
_SET_FORBIDDEN = frozenset(
    {"NOEXEC", "PARSEONLY", "XACT_ABORT", "IMPLICIT_TRANSACTIONS", "ANSI_DEFAULTS", "ROWCOUNT", "FMTONLY"}
)
# In a module the engine restores a SET option when the module returns. An implicit transaction
# that the module opened stays open in the session of its caller, so these are refused unless OFF.
_SET_OPENS_TRANSACTION = frozenset({"IMPLICIT_TRANSACTIONS", "ANSI_DEFAULTS"})
_SECURITY: _Phrases = {
    "GRANT": (),
    "DENY": (),
    "REVOKE": (),
    "REVERT": (),
    "EXECUTE": ("AS",),
    "EXEC": ("AS",),
    "ALTER": ("ROLE", "AUTHORIZATION"),
    "CREATE": ("USER",),
}
# Reserved words that start a statement. None of them can stand outside parentheses inside an
# UPDATE or DELETE statement (END and ELSE: outside CASE), so each one ends the search for WHERE.
_STATEMENT_WORDS = frozenset(
    "SELECT INSERT UPDATE DELETE MERGE DECLARE IF ELSE WHILE BEGIN END EXEC EXECUTE PRINT TRUNCATE FETCH "
    "OPEN CLOSE DEALLOCATE BREAK CONTINUE WAITFOR RETURN GOTO COMMIT ROLLBACK CREATE ALTER DROP GRANT DENY "
    "REVOKE USE".split()
)
# The words that can start a data batch. A batch that starts with any other name runs a procedure:
# T-SQL needs no EXECUTE for a call that is the first statement of a batch.
_DATA_STARTERS = _STATEMENT_WORDS | frozenset(
    "WITH SET THROW SAVE RAISERROR DBCC ENABLE DISABLE REVERT KILL CHECKPOINT BULK BACKUP RESTORE SETUSER "
    "SHUTDOWN RECONFIGURE READTEXT WRITETEXT UPDATETEXT".split()
)
# Reserved words that cannot stand outside parentheses in one statement of a model batch. SET,
# UPDATE and DELETE are in the foreign key actions (ON DELETE SET NULL) and are read apart.
_MODEL_STRAY = frozenset(
    "SELECT INSERT MERGE TRUNCATE DECLARE WHILE BEGIN PRINT DBCC EXEC EXECUTE CREATE ALTER DROP WAITFOR "
    "THROW KILL OPEN CLOSE FETCH DEALLOCATE BACKUP RESTORE CHECKPOINT BULK".split()
)
# The closed rule of a model batch. Outside parentheses, after the first word, a statement of the
# closed list holds only: these words, a name where the grammar takes a name, a type after a column
# name, numbers, strings and the operators of _MODEL_OPS. Any other token has no place in the first
# statement, so it would start a second one (T-SQL needs no ';' between statements).
_MODEL_WORDS = frozenset(
    "ADD ALTER DROP TABLE INDEX SEQUENCE SCHEMA TYPE SYNONYM UNIQUE CLUSTERED NONCLUSTERED COLUMNSTORE "
    "COLUMN CONSTRAINT IF EXISTS ON WITH CHECK NOCHECK NULL NOT DEFAULT VALUES PRIMARY KEY FOREIGN "
    "REFERENCES CASCADE NO ACTION SET DELETE UPDATE FOR REPLICATION IDENTITY COLLATE SPARSE PERSISTED "
    "ROWGUIDCOL FILESTREAM COLUMN_SET MASKED ENCRYPTED GENERATED ALWAYS AS ROW START END HIDDEN NEXT "
    "VALUE INCLUDE WHERE AND OR IS IN LIKE BETWEEN ORDER ASC DESC AUTHORIZATION FROM INCREMENT BY "
    "MINVALUE MAXVALUE CYCLE CACHE RESTART TEXTIMAGE_ON FILESTREAM_ON PRECISION VARYING FILLFACTOR".split()
)
# The words of the list after which a name can start.
_MODEL_NAME_AFTER = frozenset(
    "ADD DROP TABLE INDEX SEQUENCE SCHEMA TYPE SYNONYM COLUMN CONSTRAINT EXISTS ON REFERENCES FOR COLLATE AS "
    "FROM AUTHORIZATION DEFAULT WHERE AND OR NOT TEXTIMAGE_ON FILESTREAM_ON".split()
)
# The reserved words of the list. In the place of a name they are the keyword (ADD CONSTRAINT,
# ON DELETE); any other word there is a name (ADD [Value] and ADD Value are one column).
_MODEL_RESERVED = frozenset(
    "ADD ALTER AND AS AUTHORIZATION BETWEEN BY CASCADE CHECK CLUSTERED COLLATE COLUMN CONSTRAINT DEFAULT "
    "DELETE DROP EXISTS FOR FOREIGN FROM IF IN INDEX IS KEY LIKE NOCHECK NONCLUSTERED NOT NULL ON OR ORDER "
    "PRIMARY REFERENCES SCHEMA SET TABLE UNIQUE UPDATE VALUES WHERE WITH".split()
)
# '(' and ')' are read apart; ';' ends the statement.
_MODEL_OPS = frozenset(", . = < > <= >= <> != !< !> + - * / % & | ^ ~".split())
# Statements of the list with no parentheses outside a name. A query in parentheses is a statement
# of its own, so '(' after the end of one of these has no place.
_MODEL_NO_GROUP = frozenset(
    {
        "DROP TABLE",
        "DROP SEQUENCE",
        "DROP TYPE",
        "DROP SCHEMA",
        "DROP SYNONYM",
        "CREATE SCHEMA",
        "CREATE SYNONYM",
        "ALTER SEQUENCE",
        "EXEC sp_rename",
    }
)
# ALTER TABLE ... SET (SYSTEM_VERSIONING = ...) is read whole (system_versioning): the closed rule
# does not look inside parentheses, and these are the only forms of SET that the list holds.
_VERSIONING_HINT = (
    "the closed list holds SET (SYSTEM_VERSIONING = OFF) and SET (SYSTEM_VERSIONING = ON (HISTORY_TABLE = "
    "[schema].[name], DATA_CONSISTENCY_CHECK = ON | OFF, HISTORY_RETENTION_PERIOD = INFINITE | <n> DAYS | "
    "WEEKS | MONTHS | YEARS)); any other table option goes in a '-- azsqlcd:raw' batch"
)
_REBUILD_HINT = (
    "the closed list holds REBUILD WITH (DATA_COMPRESSION = NONE | ROW | PAGE [, ONLINE = ON | OFF] "
    "[, MAXDOP = <n>] [, SORT_IN_TEMPDB = ON | OFF]), each option once; any other rebuild goes in a "
    "'-- azsqlcd:raw' batch"
)
# ALTER COLUMN [c] ADD | DROP <property>: the properties that are read whole, as their words.
_COLUMN_PROPERTIES = (("ROWGUIDCOL",), ("SPARSE",), ("PERSISTED",), ("NOT", "FOR", "REPLICATION"))
# The first word of an element of ALTER TABLE ... ADD that is no column.
_NOT_A_COLUMN = frozenset("CONSTRAINT PRIMARY UNIQUE FOREIGN CHECK DEFAULT INDEX".split())
# Types whose values the engine writes itself: a NOT NULL column of the type needs no DEFAULT.
_ENGINE_FILLED_TYPES = ("ROWVERSION", "TIMESTAMP")
_MASKING_HINT = (
    "the closed list holds ALTER COLUMN [c] ADD MASKED WITH (FUNCTION = '...') and ALTER COLUMN [c] DROP "
    "MASKED, each as the whole statement"
)
_RETENTION_UNITS = frozenset("DAY DAYS WEEK WEEKS MONTH MONTHS YEAR YEARS".split())
_INTO_OWNERS = ("SELECT", "INSERT", "MERGE", "OUTPUT", "FETCH")
_DML = ("INSERT", "UPDATE", "DELETE", "MERGE")
# A name directly after one of these words is an object, never a method call on a column.
_OBJECT_WORDS = ("INTO", "INSERT", "TABLE", "FROM", "JOIN", "UPDATE", "DELETE", "MERGE", "REFERENCES")
# The directives that a module file can hold. Any other name does nothing there: a typing mistake.
_MODULE_DIRECTIVES = ("after", "ignore-dep")
_TODO = re.compile(r"todo\b", re.IGNORECASE)
# A line comment that looks like a directive: '--', any white space, 'azsqlcd', ':'. It is one only
# when it starts with lex.DIRECTIVE_PREFIX exactly.
_NEAR_DIRECTIVE = re.compile(r"--[ \t]*azsqlcd[ \t]*:", re.IGNORECASE)
_RESUM_HINT = "If the migration is not merged yet, run azsqlcd gen --resum; a merged migration never changes"
# NF000 (gen.table_file_check): the two messages that one clause in another place gives
_NF000_MISSING = re.compile(r"line (\d+): the canonical form has (.+); the file does not")
_NF000_EXTRA = re.compile(r"line (\d+): the file has (.+); the canonical form does not")
_CANONICAL_ORDER = (
    "The canonical order of a column is: [name] type, COLLATE, SPARSE, MASKED WITH (...), "
    "IDENTITY(seed, increment) [NOT FOR REPLICATION], ROWGUIDCOL, GENERATED ALWAYS AS ROW START | END "
    "[HIDDEN], NULL | NOT NULL, CONSTRAINT [name] DEFAULT expression"
)
# secret_findings: the words before `PASSWORD = <literal>` that make it a comparison, and the words
# that end the search for them. A statement needs no ';', so every word that starts a statement or
# a clause that gives a value ends it.
_COMPARES = frozenset("WHERE ON IF HAVING WHEN WHILE".split())
_GIVES_VALUE = _STATEMENT_WORDS | frozenset("SET WITH BY VALUES ADD THEN FROM OPEN BACKUP RESTORE".split())
_LINE = re.compile(r"line (\d+): ")
# For a file that is not SQL, or SQL that cannot be lexed: the same rule on the raw text.
_SECRET = re.compile(r"(?<![\w@#$])(password|secret)\s*=\s*(?:N?'|0x[0-9a-f])", re.IGNORECASE)
# For live text that nobody reviewed (secret_in_text): any name that holds one of the words, a
# variable too, with a type or '(' before the literal; a connection string key; a passphrase call.
_SECRET_LIVE = re.compile(
    r"(?:password|passwd|pwd|secret|passphrase)\w*[\]\"]?"  # @rmtpassword, [password], "Secret"
    r"(?:\s+(?:AS\s+)?\[?[a-z_]+\]?(?:\s*\([^)]*\))?)?"  # DECLARE @password varchar(9) = ...
    r"\s*=\s*\(?\s*(?:N?'|0x[0-9a-f])"
    r"|(?:password|pwd)=[^\s;'\"@]"  # 'Server=x;Pwd=hunter2'
    r"|(?:EN|DE)CRYPTBYPASSPHRASE\s*\(\s*N?'",
    re.IGNORECASE,
)
# What a migration, a module or a table-class file cannot hold anywhere: C0 without TAB, LF and CR;
# DEL; C1; the Unicode line and paragraph separators. A driver can end the text at NUL, a comment
# can end at a character that only one of the two readers takes for a line break, and a review
# page shows none of them.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u2028\u2029]")


# ------------------------------------------------------------------ data
@dataclass(frozen=True)
class Finding:
    code: str
    severity: str  # error | warning
    path: str  # relative to the repository root, forward slashes
    line: int  # 1-based; 1 when the finding is about the whole file
    message: str


@dataclass(frozen=True)
class Item:
    """A destructive or locking statement. It needs an allow line with this code and this object."""

    code: str  # an allow code
    object: str  # '[sales].[Order].[Stat]'; a variable, an object key or BATCH_OBJECT as written
    line: int  # line in the migration file


@dataclass(frozen=True)
class BatchFacts:
    statement: str  # model batch: the statement class, for example 'ALTER TABLE'; else ''
    needs_allow: tuple[Item, ...]
    findings: tuple[Finding, ...]  # errors and warnings that no allow line removes
    creates_tables: tuple[str, ...]  # '[schema].[name]' of each table that the batch creates


type TableFileCheck = Callable[[str, str], list[Finding]]


def _finding(code: str, path: str, line: int, message: str) -> Finding:
    return Finding(code, CODES[code], path, line, message)


def _sorted(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (f.path, f.line, f.code, f.message))


# ------------------------------------------------------------------ names
def _is_op(t: Tok, text: str) -> bool:
    return t.kind == "op" and t.text == text


def _word(t: Tok) -> str:
    """Upper-case text of an unquoted word, else ''. A quoted identifier is never a keyword."""
    return t.text.upper() if t.kind == "word" else ""


def _dotted(sig: list[Tok], i: int) -> tuple[tuple[str, ...], int]:
    """The dotted name that starts at sig[i]: (decoded parts, index after it). ((), i) when none."""
    if not 0 <= i < len(sig) or sig[i].kind not in _IDENT:
        return (), i
    parts = [sig[i].value]
    i += 1
    while i + 1 < len(sig) and _is_op(sig[i], ".") and sig[i + 1].kind in _IDENT:
        parts.append(sig[i + 1].value)
        i += 2
    return tuple(parts), i


def _show(parts: tuple[str, ...]) -> str:
    return ".".join(names.quote(p) if p else "[]" for p in parts)


def _name_parts(text: str) -> tuple[str, ...] | None:
    """The decoded parts when the text is one dotted name ([s].[t], s.t.c, "s".t); else None."""
    try:
        sig = lex.significant(lex.tokenize(text))
    except lex.LexError:
        return None
    parts, end = _dotted(sig, 0)
    return parts if parts and end == len(sig) else None


def _same(a: str, b: str) -> bool:
    """Two object texts name one object. A dotted name is compared part by part without quoting and
    without case (the catalog collation is case-insensitive); any other text must be equal."""
    pa, pb = _name_parts(a), _name_parts(b)
    if pa is None or pb is None:
        return a == b
    return [p.casefold() for p in pa] == [p.casefold() for p in pb]


def _mention(sig: list[Tok], schema: str, name: str) -> int | None:
    """Line of the first name in the tokens that is [schema].[name], or None.

    The matching rule of modules.scan_references: the first two parts of a dotted name; a one-part
    name is an object of schema dbo (A17); a name after '.' is a member, not an object.
    """
    want = (schema.casefold(), name.casefold())
    i = 0
    while i < len(sig):
        parts, end = _dotted(sig, i)
        if not parts:
            i += 1
            continue
        two = parts[:2] if len(parts) > 1 else ("dbo", *parts)
        if tuple(p.casefold() for p in two) == want and not (i and _is_op(sig[i - 1], ".")):
            return sig[i].line
        i = end
    return None


def _set_options(sig: list[Tok], i: int) -> tuple[list[str], str]:
    """The options of the SET statement at sig[i], and the word after them ('ON', 'OFF', else '').

    SET NOCOUNT, XACT_ABORT OFF gives (['NOCOUNT', 'XACT_ABORT'], 'OFF'). An assignment (UPDATE t SET
    c = 1, SET @v = 1) is not a SET statement: ([], '').
    """
    options: list[str] = []
    j = i + 1
    while j < len(sig) and sig[j].kind == "word":
        options.append(sig[j].text.upper())
        j += 1
        if j < len(sig) and _is_op(sig[j], ","):
            j += 1
        else:
            break
    if j < len(sig) and (_is_op(sig[j], "=") or _is_op(sig[j], ".")):
        return [], ""
    return options, _word(sig[j]) if j < len(sig) else ""


# ------------------------------------------------------------------ batch classifier
class _Classifier:
    """The significant tokens of one batch and what the rules find in them."""

    def __init__(self, batch: chain.MigrationBatch, path: str) -> None:
        toks = lex.tokenize(batch.text)
        self.sig = lex.significant(toks)
        if not self.sig:
            raise ValueError("a batch with no statement cannot be classified")
        self.line_starts: set[int] = set()  # offset of the first token of each line, comments included
        line_start = True
        for t in toks:
            if t.kind == "nl":
                line_start = True
            elif t.kind != "ws":
                if line_start:
                    self.line_starts.add(t.pos)
                line_start = False
        self.path = path
        self.offset = batch.first_line - 1
        self.depth: list[int] = []  # parentheses around each token; '(' and ')' have the depth outside
        depth = 0
        for t in self.sig:
            depth -= _is_op(t, ")")
            self.depth.append(depth)
            depth += _is_op(t, "(")
        self.items: list[Item] = []
        self.findings: list[Finding] = []
        # one finding per token and severity: the first rule that reads it wins. A warning never
        # takes the place of an error on its token (LFP-02).
        self.reported: set[tuple[int, str]] = set()
        self.outside = False  # a rule that reads a statement whole found it outside the closed list
        self.property_verb = -1  # index of ADD or DROP in ALTER COLUMN c ADD | DROP <property>

    # -------------------------------------------------------------- token access
    def word(self, i: int) -> str:
        """Upper-case text of the unquoted word at i. '' for any other token and outside the batch."""
        return self.sig[i].text.upper() if 0 <= i < len(self.sig) and self.sig[i].kind == "word" else ""

    def kind(self, i: int) -> str:
        return self.sig[i].kind if 0 <= i < len(self.sig) else ""

    def op(self, i: int, text: str) -> bool:
        return 0 <= i < len(self.sig) and _is_op(self.sig[i], text)

    def line(self, i: int) -> int:
        return self.offset + self.sig[min(i, len(self.sig) - 1)].line

    # -------------------------------------------------------------- results
    def report(self, code: str, i: int, message: str) -> bool:
        """Add the finding. False: the token has a finding of this severity already."""
        key = (i, CODES[code])
        if key in self.reported:
            return False
        self.reported.add(key)
        self.findings.append(_finding(code, self.path, self.line(i), message))
        return True

    def unreadable(self, i: int) -> None:
        self.report(
            "STATEMENT_UNREADABLE",
            i,
            "the lexer cannot read the object of this statement, so no allow line can match it; "
            "write the name as [schema].[name]",
        )

    def item(self, code: str, obj: str, i: int) -> None:
        if not any(it.code == code and _same(it.object, obj) for it in self.items):
            self.items.append(Item(code, obj, self.line(i)))

    # -------------------------------------------------------------- every batch
    def phrases(self, table: _Phrases, code: str, why: str) -> None:
        for i in range(len(self.sig)):
            after = table.get(self.word(i))
            if after is None or (after and self.word(i + 1) not in after):
                continue
            phrase = f"{self.word(i)} {self.word(i + 1)}" if after else self.word(i)
            self.report(code, i, f"{phrase} {why}")

    def forbidden(self) -> None:
        why = (
            "is not allowed in a migration batch: the tool owns the transaction and a batch runs to "
            "its end (to stop with an error, use THROW)"
        )
        self.phrases(_FORBIDDEN, "FORBIDDEN_TOKEN", why)
        for i in range(len(self.sig)):
            # ON DELETE SET NULL, [Noexec] int: SET is the action of a foreign key, not a statement
            if self.word(i) == "SET" and not self.fk_action(i - 1):
                for option in _set_options(self.sig, i)[0]:
                    if option in _SET_FORBIDDEN:
                        message = (
                            f"SET {option} is not allowed in a migration batch: the tool sets the "
                            "options of its session, and the option would stay for every later batch "
                            "and for the writes of the tool itself"
                        )
                        self.report("FORBIDDEN_TOKEN", i, message)
            # OPTION (USE HINT (...)) is inside parentheses; the USE statement never is
            if self.word(i) == "USE" and self.depth[i] == 0:
                self.report("FORBIDDEN_TOKEN", i, "USE is not allowed: a migration runs in one database")
            if self.op(i, ":") and self.sig[i].pos in self.line_starts:
                message = "a line that starts with ':' is a sqlcmd directive; the tool does not run sqlcmd"
                self.report("FORBIDDEN_TOKEN", i, message)
        dropped = self.drop_list_names()
        i = 0
        while i < len(self.sig):
            parts, end = _dotted(self.sig, i)
            if not parts or self.op(i - 1, "."):
                i += 1
                continue
            # [alias].[column].method(...) and [schema].[function](...): the last part is called.
            # After INTO, FROM, TABLE, ... the name is an object and '(' starts a column list:
            # INSERT INTO [other].[dbo].[t] ([a]) has three parts. A name that DROP INDEX or DROP
            # STATISTICS holds is an object too.
            is_object = (
                self.word(i - 1) in _OBJECT_WORDS
                or (self.word(i - 1) == "ON" and self.word(i - 3) == "INDEX")
                or i in dropped
            )
            named = len(parts) - (self.op(end, "(") and not is_object)
            # DROP INDEX [schema].[table].[index]: three parts and no more, all of this database
            if (named >= 3 and not dropped.get(i)) or (self.op(end, ".") and self.op(end + 1, ".")):
                self.report(
                    "THREE_PART_NAME",
                    i,
                    f"{_show(parts)}: a name with three or more parts. Azure SQL Database has no "
                    "cross-database names; for a column, use a table alias",
                )
            i = end

    def drop_list_names(self) -> dict[int, bool]:
        """Index of each name that a DROP INDEX or DROP STATISTICS statement holds -> the name is
        [schema].[table].[index] or [schema].[table].[statistics]: three parts and nothing after it.

        DROP INDEX [IF EXISTS] <element>, ...: an element is a name alone (the old form) or
        [index] ON <object> [WITH (...)], and the object is in the result too. DROP STATISTICS has
        the first form only. The statement can stand anywhere in the batch: behind a condition, in a
        block, after another statement. The list ends at the first token after an element that is no
        comma, so a comma list of a later statement is not a part of it.
        """
        found: dict[int, bool] = {}
        for k in range(len(self.sig)):
            if self.word(k) != "DROP" or self.word(k + 1) not in ("INDEX", "STATISTICS") or self.depth[k]:
                continue
            # No statement starts after a comma: ALTER TABLE [s].[t] ADD [c] int NULL, DROP INDEX [ix]
            if self.op(k - 1, ","):
                continue
            # ALTER TABLE [s].[t] DROP INDEX [ix]: the action of ALTER TABLE, its name has one part
            j = k - 1
            while self.op(j - 1, "."):
                j -= 2
            if self.kind(j) in _IDENT and (self.word(j - 2), self.word(j - 1)) == ("ALTER", "TABLE"):
                continue
            j = k + 2
            if self.word(j) == "IF" and self.word(j + 1) == "EXISTS":
                j += 2
            while True:
                parts, end = _dotted(self.sig, j)
                if not parts:
                    break
                new_form = self.word(end) == "ON"
                found[j] = (
                    len(parts) == 3 and not new_form and not self.op(end, "(") and not self.op(end, ".")
                )
                if new_form:
                    at = end + 1
                    target, end = _dotted(self.sig, at)
                    if not target:
                        break
                    found[at] = False
                    options = self.close(end + 1) if self.word(end) == "WITH" else None
                    if options is not None:
                        end = options + 1
                if not self.op(end, ","):
                    break
                j = end + 1
        return found

    def online_build(self) -> None:
        """NTX004: an online build with no low-priority wait queues behind every open transaction.
        Each ONLINE = ON is read with its own statement: a batch can hold more than one."""
        for i in range(len(self.sig)):
            if self.word(i) == "ONLINE" and self.op(i + 1, "=") and self.word(i + 2) == "ON":
                # ONLINE = ON (WAIT_AT_LOW_PRIORITY (...))
                if self.op(i + 3, "(") and self.word(i + 4) == "WAIT_AT_LOW_PRIORITY":
                    continue
                if self.takes_no_wait(i):
                    continue
                self.report(
                    "NTX004",
                    i,
                    "ONLINE = ON without WAIT_AT_LOW_PRIORITY: the build waits for its lock in the "
                    "normal queue and every later statement on the table waits behind it",
                )
                return

    def takes_no_wait(self, i: int) -> bool:
        """The option at i is one of ALTER TABLE ... ALTER COLUMN ... WITH (ONLINE = ON) or of CREATE
        [CLUSTERED | NONCLUSTERED] COLUMNSTORE INDEX. The engine has no low-priority wait there, so
        the rule must not ask for it. On the way back, outside parentheses, that ALTER or CREATE
        comes before any other word that starts a statement."""
        for k in range(i - 1, -1, -1):
            if self.depth[k] or not (self.op(k, ";") or self.word(k) in _STATEMENT_WORDS):
                continue
            # ALTER COLUMN [c] DROP SPARSE WITH (ONLINE = ON): DROP is no statement here
            if (self.word(k - 3), self.word(k - 2), self.word(k)) == ("ALTER", "COLUMN", "DROP"):
                continue
            if self.word(k) == "ALTER":
                return self.word(k + 1) == "COLUMN"
            # an index can have the name COLUMNSTORE: CREATE INDEX COLUMNSTORE ON ...
            c = k + 1 + (self.word(k + 1) in ("CLUSTERED", "NONCLUSTERED"))
            return (self.word(k), self.word(c), self.word(c + 1)) == ("CREATE", "COLUMNSTORE", "INDEX")
        return False

    def resumable(self) -> None:
        """NTX006: the engine refuses RESUMABLE = ON inside an explicit transaction, and every batch
        of a tx migration runs in the transaction of the tool."""
        for i in range(len(self.sig)):
            if self.word(i) == "RESUMABLE" and self.op(i + 1, "=") and self.word(i + 2) == "ON":
                self.report(
                    "NTX006",
                    i,
                    "RESUMABLE = ON in a tx migration: the engine refuses a resumable operation "
                    "inside a transaction. Use a nontx migration, or remove the option",
                )
                return

    # -------------------------------------------------------------- data batch
    def implicit_call(self) -> None:
        """A procedure call with no EXEC. T-SQL runs a name that is the first statement of a batch
        as a procedure, and the directive lines above it are comments. A11 needs EXEC_PROC for it."""
        i = 0
        while self.op(i, ";") or (self.kind(i) == "word" and self.op(i + 1, ":")):  # ';' or a label
            i += 1 if self.op(i, ";") else 2
        procedure, _ = _dotted(self.sig, i)
        if not procedure:
            return
        w = self.word(i)
        if len(procedure) == 1 and w in _DATA_STARTERS:
            # ENABLE and DISABLE are statements only before TRIGGER; alone they are names
            if w not in ("ENABLE", "DISABLE") or self.word(i + 1) == "TRIGGER":
                return
        if procedure[-1].casefold() not in ("sp_executesql", "sp_rename"):  # these have their own rule
            self.item("EXEC_PROC", _show(procedure), i)

    def data(self) -> None:
        self.implicit_call()
        for i, t in enumerate(self.sig):
            w = self.word(i)
            name = t.value.casefold() if t.kind in _IDENT else ""
            if name == "sp_rename" or w in ("CREATE", "ALTER", "DROP"):
                self.report("DATA_DDL", i, f"{t.text} in a data batch; a data batch is DML only")
            elif name == "sp_executesql":
                self.item("DYNAMIC_SQL", BATCH_OBJECT, i)
            elif w in _EXEC:
                self.execute(i)
            elif w == "TRUNCATE":
                table, _ = _dotted(self.sig, i + 1 + (self.word(i + 1) == "TABLE"))
                if table:
                    self.item("TRUNCATE", _show(table), i)
                else:
                    self.unreadable(i)
            elif w == "DBCC" or (w in ("ENABLE", "DISABLE") and self.word(i + 1) == "TRIGGER"):
                phrase = w if w == "DBCC" else f"{w} TRIGGER"
                self.report("DATA_FORBIDDEN", i, f"{phrase} is not allowed in a data batch")
            elif w == "INTO":
                self.select_into(i)
            elif w in ("UPDATE", "DELETE") and self.starts_statement(i) and not self.has_where(i):
                self.no_where(i)

    def execute(self, i: int) -> None:
        j = i + 1
        if self.word(j) == "AS":
            return  # EXECUTE AS: SECURITY_STATEMENT
        if self.kind(j) == "var" and self.op(j + 1, "="):  # EXEC @result = procedure
            j += 2
        procedure, _ = _dotted(self.sig, j)
        if self.op(j, "(") or self.kind(j) == "var":  # EXEC ('...'), EXEC @name_of_a_procedure
            self.item("DYNAMIC_SQL", BATCH_OBJECT, i)
        elif not procedure:
            self.unreadable(i)
        elif procedure[-1].casefold() not in ("sp_executesql", "sp_rename"):  # these have their own rule
            self.item("EXEC_PROC", _show(procedure), i)

    def select_into(self, i: int) -> None:
        """SELECT ... INTO a table that is not temporary creates a table.

        INSERT INTO, MERGE INTO, OUTPUT ... INTO and FETCH ... INTO are other statements: the
        nearest of these words before INTO, at the depth of INTO, says which one it is.
        """
        for j in range(i - 1, -1, -1):
            if self.depth[j] < self.depth[i]:
                return
            if self.depth[j] == self.depth[i] and self.word(j) in _INTO_OWNERS:
                if self.word(j) == "OUTPUT" and not self.output_clause(j):
                    continue  # a column alias: OUTPUT is not a reserved word
                target, _ = _dotted(self.sig, i + 1)
                if self.word(j) == "SELECT" and target and not target[-1].startswith("#"):
                    self.report(
                        "DATA_FORBIDDEN",
                        i,
                        f"SELECT ... INTO {_show(target)} creates a table; only a temporary table "
                        "is allowed in a data batch",
                    )
                return

    def output_clause(self, j: int) -> bool:
        """OUTPUT at j is the clause of INSERT, UPDATE, DELETE or MERGE: on the way back, at its
        depth, one of these words comes before any SELECT. SELECT [a] AS OUTPUT INTO ... has none."""
        for k in range(j - 1, -1, -1):
            if self.depth[k] < self.depth[j]:
                return False
            if self.depth[k] == self.depth[j]:
                if self.word(k) == "SELECT":
                    return False
                if self.word(k) in _DML:
                    return True
        return False

    def fk_action(self, i: int) -> bool:
        """DELETE or UPDATE at i is the action of a foreign key: ON DELETE | ON UPDATE and then
        CASCADE, NO ACTION, SET NULL or SET DEFAULT. ON alone is not enough: SET NOCOUNT ON also
        ends with ON, and the next line can be a DELETE statement."""
        if self.word(i - 1) != "ON":
            return False
        nxt, after = self.word(i + 1), self.word(i + 2)
        return (
            nxt == "CASCADE"
            or (nxt == "NO" and after == "ACTION")
            or (nxt == "SET" and after in ("NULL", "DEFAULT"))
        )

    def starts_statement(self, i: int) -> bool:
        """UPDATE or DELETE at i is a statement. It is not: ON DELETE / ON UPDATE with its action
        (foreign key), THEN UPDATE / THEN DELETE (MERGE), FOR UPDATE (cursor), a permission (GRANT
        SELECT, UPDATE ON), UPDATE(column), UPDATE STATISTICS."""
        return (
            not self.fk_action(i)
            and self.word(i - 1) not in ("THEN", "FOR", "GRANT", "DENY", "REVOKE")
            and not self.op(i - 1, ",")
            and not self.op(i + 1, "(")
            and self.word(i + 1) != "STATISTICS"
        )

    def has_where(self, i: int) -> bool:
        """The UPDATE or DELETE statement at i has a WHERE of its own, not one of a subquery."""
        case = 0
        for j in range(i + 1, len(self.sig)):
            if self.depth[j] > self.depth[i]:
                continue
            if self.depth[j] < self.depth[i] or self.op(j, ";"):
                return False
            w = self.word(j)
            if w == "WHERE":
                return True
            if w == "CASE":
                case += 1
            elif case and w == "END":
                case -= 1
            elif w in _STATEMENT_WORDS and not (case and w == "ELSE"):
                return False
        return False

    def no_where(self, i: int) -> None:
        j = i + 1
        if self.word(j) == "TOP" and self.op(j + 1, "("):  # TOP (n) [PERCENT]
            j += 2
            while j < len(self.sig) and not (self.op(j, ")") and self.depth[j] == self.depth[i]):
                j += 1
            j += 1
            j += self.word(j) == "PERCENT"
        j += self.word(j) == "FROM"
        target, _ = _dotted(self.sig, j)
        if self.kind(j) == "var":  # a table variable
            self.item("DATA_NO_WHERE", self.sig[j].text, i)
        elif target:
            self.item("DATA_NO_WHERE", _show(target), i)
        else:
            self.unreadable(i)

    # -------------------------------------------------------------- model batch
    def model(self, mode: str, created: Collection[str]) -> tuple[str, tuple[str, ...]]:
        """(statement class, tables created). The class comes from the leading tokens."""
        w0, w1 = self.word(0), self.word(1)
        statement = ""
        creates: tuple[str, ...] = ()
        action = -1
        if w0 == "CREATE":
            j = 1 + (w1 == "UNIQUE")
            j += self.word(j) in ("CLUSTERED", "NONCLUSTERED")
            j += self.word(j) == "COLUMNSTORE"
            if self.word(j) == "INDEX":
                statement = "CREATE INDEX"
                _, j = _dotted(self.sig, j + 1)
                self.long_lock(_dotted(self.sig, j + 1)[0] if self.word(j) == "ON" else (), mode, created)
            elif j == 1 and w1 in ("TABLE", "SEQUENCE", "SCHEMA", "TYPE", "SYNONYM"):
                statement = f"CREATE {w1}"
                table, _ = _dotted(self.sig, 2)
                if w1 == "TABLE" and table:
                    creates = (_show(table),)
                elif w1 == "TABLE":
                    self.unreadable(0)
        elif w0 == "ALTER" and w1 == "SEQUENCE":
            statement = "ALTER SEQUENCE"
        elif w0 == "ALTER" and w1 == "TABLE":
            statement = "ALTER TABLE"
            action = self.alter_table(mode, created)
        elif w0 == "DROP" and w1 in ("TABLE", "SEQUENCE", "TYPE", "SCHEMA", "INDEX", "SYNONYM"):
            statement = f"DROP {w1}"
            self.drop(w1)
        elif w0 in _EXEC:
            procedure, j = _dotted(self.sig, 1)
            if tuple(p.casefold() for p in procedure) in (("sp_rename",), ("sys", "sp_rename")):
                statement = "EXEC sp_rename"

                # the old name: the first argument, a literal (the closed grammar has no other form)
                old = _name_parts(self.sig[j].value) if self.kind(j) in ("string", "nstring") else None
                if old:
                    self.item("RENAME", _show(old), 0)
                else:
                    self.unreadable(0)
        if not statement:
            self.report("MODEL_STATEMENT", 0, f"{self.sig[0].text} ...: {_MODEL_HINT}")
            return "", ()
        if not self.outside:
            self.one_statement(action, statement)
        return statement, creates

    def long_lock(self, table: tuple[str, ...], mode: str, created: Collection[str]) -> None:
        if not table:
            self.unreadable(0)
        elif mode == "tx" and not any(_same(_show(table), other) for other in created):
            self.item("LONG_LOCK", _show(table), 0)

    def alter_table(self, mode: str, created: Collection[str]) -> int:
        """ALTER TABLE: ADD, ALTER COLUMN, DROP, REBUILD or SET. Returns the index of that action word."""
        table, j = _dotted(self.sig, 2)
        if not table:
            self.unreadable(0)
            return -1
        nocheck = False
        if self.word(j) == "WITH" and self.word(j + 1) in ("CHECK", "NOCHECK"):
            nocheck = self.word(j + 1) == "NOCHECK"
            j += 2
        action = self.word(j)
        if action == "ADD":
            # the engine reads every row for a key; for CHECK and FOREIGN KEY unless WITH NOCHECK
            locking = (
                ("PRIMARY", "UNIQUE") if nocheck else ("PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "REFERENCES")
            )
            if any(self.word(k) in locking for k in range(j + 1, len(self.sig))):
                self.long_lock(table, mode, created)
            if not any(_same(_show(table), other) for other in created):
                self.not_null_without_default(j)
        elif action == "ALTER" and self.word(j + 1) == "COLUMN":
            if self.kind(j + 2) not in _IDENT:
                self.unreadable(j)
            elif self.word(j + 3) in ("ADD", "DROP") and self.word(j + 4) == "MASKED":
                self.masking(table, j)
            elif self.word(j + 3) in ("ADD", "DROP") and self.column_property(j + 4):
                self.property(table, j, mode, created)
            else:
                self.item("ALTER_COLUMN_LOSSY", _show((*table, self.sig[j + 2].value)), 0)
        elif action == "DROP":
            self.drop_list(table, j + 1)
        elif action == "REBUILD":
            self.rebuild(table, j, mode, created)
        elif action == "SET":
            self.system_versioning(table, j)
        else:
            self.report(
                "MODEL_STATEMENT",
                j,
                f"ALTER TABLE ... {self.sig[min(j, len(self.sig) - 1)].text}: {_MODEL_HINT}",
            )
        return j

    def close(self, j: int) -> int | None:
        """Index of the ')' that closes the '(' at j; None when j is no '(' or nothing closes it."""
        if not self.op(j, "("):
            return None
        for k in range(j + 1, len(self.sig)):
            if self.op(k, ")") and self.depth[k] == self.depth[j]:
                return k
        return None

    def stray(self, j: int) -> None:
        self.report(
            "MODEL_STATEMENT",
            j,
            f"{self.sig[j].text} has no place in the statement before it, so it starts a second "
            "statement; a model batch is one statement, put GO between statements. If it is a "
            "name, write it in brackets",
        )

    def ends_at(self, k: int) -> None:
        """The statement ends at token k: nothing has a place after it but one ';'."""
        if k + 1 < len(self.sig) and not (self.op(k + 1, ";") and k + 2 == len(self.sig)):
            self.stray(k + 1)

    def system_versioning(self, table: tuple[str, ...], j: int) -> None:
        """ALTER TABLE t SET (SYSTEM_VERSIONING = OFF | ON [(option = value, ...)]) with SET at j.

        OFF needs allow TEMPORAL_OFF: the engine stops writing history, and the history table
        becomes a table of its own that nothing manages."""
        end = self.close(j + 1)
        switch = self.word(j + 4)
        ok = end is not None and self.word(j + 2) == "SYSTEM_VERSIONING" and self.op(j + 3, "=")
        if ok and switch == "ON" and end != j + 5:
            inner = self.close(j + 5)
            ok = inner is not None and inner + 1 == end and self.versioning_options(j + 6, inner)
        elif ok:
            ok = switch in ("ON", "OFF") and end == j + 5
        if not ok or end is None:
            self.outside = True
            self.report("MODEL_STATEMENT", j, f"ALTER TABLE ... SET: {_VERSIONING_HINT}")
            return
        if switch == "OFF":
            self.item("TEMPORAL_OFF", _show(table), 0)
        self.ends_at(end)

    def versioning_options(self, j: int, end: int) -> bool:
        """The tokens from j up to the ')' at end are the option list of SYSTEM_VERSIONING = ON."""
        while True:
            option = self.word(j)
            if not self.op(j + 1, "="):
                return False
            j += 2
            if option == "HISTORY_TABLE":
                name, j = _dotted(self.sig, j)
                if not name:
                    return False
            elif option == "DATA_CONSISTENCY_CHECK" and self.word(j) in ("ON", "OFF"):
                j += 1
            elif option == "HISTORY_RETENTION_PERIOD" and self.word(j) == "INFINITE":
                j += 1
            elif (
                option == "HISTORY_RETENTION_PERIOD"
                and self.kind(j) == "number"
                and self.word(j + 1) in _RETENTION_UNITS
            ):
                j += 2
            else:
                return False
            if j == end:
                return True
            if not self.op(j, ","):
                return False
            j += 1

    def masking(self, table: tuple[str, ...], j: int) -> None:
        """ALTER COLUMN c ADD MASKED WITH (FUNCTION = '...') | DROP MASKED, with ALTER at j.

        Neither changes a stored value, so there is no ALTER_COLUMN_LOSSY. DROP MASKED needs allow
        UNMASK: every reader of the column sees the real values after it."""
        if self.word(j + 3) == "DROP":
            self.property_verb = j + 3
            self.item("UNMASK", _show((*table, self.sig[j + 2].value)), 0)
            self.ends_at(j + 4)
        elif (
            self.word(j + 5) == "WITH"
            and self.close(j + 6) == j + 10
            and self.word(j + 7) == "FUNCTION"
            and self.op(j + 8, "=")
            and self.kind(j + 9) in ("string", "nstring")
        ):
            self.ends_at(j + 10)
        else:
            self.outside = True
            self.report("MODEL_STATEMENT", j, f"ALTER COLUMN ... ADD MASKED: {_MASKING_HINT}")

    def column_property(self, k: int) -> tuple[str, ...]:
        """The property of _COLUMN_PROPERTIES whose words start at k; () when none does."""
        for words in _COLUMN_PROPERTIES:
            if all(self.word(k + n) == w for n, w in enumerate(words)):
                return words
        return ()

    def property(self, table: tuple[str, ...], j: int, mode: str, created: Collection[str]) -> None:
        """ALTER COLUMN c ADD | DROP ROWGUIDCOL | SPARSE | PERSISTED | NOT FOR REPLICATION, with ALTER
        at j, as the whole statement.

        ROWGUIDCOL and NOT FOR REPLICATION change no stored value: no allow line. SPARSE writes every
        row again under the lock of the statement: LONG_LOCK. PERSISTED keeps ALTER_COLUMN_LOSSY."""
        words = self.column_property(j + 4)
        self.property_verb = j + 3
        if words == ("SPARSE",):
            self.long_lock(table, mode, created)
        elif words == ("PERSISTED",):
            self.item("ALTER_COLUMN_LOSSY", _show((*table, self.sig[j + 2].value)), 0)
        self.ends_at(j + 3 + len(words))

    def rebuild(self, table: tuple[str, ...], j: int, mode: str, created: Collection[str]) -> None:
        """ALTER TABLE t REBUILD WITH (DATA_COMPRESSION = NONE | ROW | PAGE, ...) with REBUILD at j,
        as the whole statement. The engine writes every row again: LONG_LOCK in a tx migration."""
        end = self.close(j + 2) if self.word(j + 1) == "WITH" else None
        if end is None or not self.rebuild_options(j + 3, end):
            self.outside = True
            self.report("MODEL_STATEMENT", j, f"ALTER TABLE ... REBUILD: {_REBUILD_HINT}")
            return
        self.long_lock(table, mode, created)
        self.ends_at(end)

    def rebuild_options(self, j: int, end: int) -> bool:
        """The tokens from j up to the ')' at end are the option list of the table rebuild."""
        seen: set[str] = set()
        while True:
            option, value = self.word(j), self.word(j + 2)
            if option in seen or not self.op(j + 1, "="):
                return False
            seen.add(option)
            if option == "DATA_COMPRESSION" and value in ("NONE", "ROW", "PAGE"):
                j += 3
            elif option in ("ONLINE", "SORT_IN_TEMPDB") and value in ("ON", "OFF"):
                j += 3
                if option == "ONLINE" and value == "ON" and self.op(j, "("):
                    inner = self.close(j)  # ONLINE = ON (WAIT_AT_LOW_PRIORITY (...))
                    if inner is None or self.word(j + 1) != "WAIT_AT_LOW_PRIORITY":
                        return False
                    j = inner + 1
            elif option == "MAXDOP" and self.kind(j + 2) == "number":
                j += 3
            else:
                return False
            if j == end:
                return "DATA_COMPRESSION" in seen
            if not self.op(j, ","):
                return False
            j += 1

    def not_null_without_default(self, j: int) -> None:
        """NNL001: ADD at j adds a NOT NULL column with no DEFAULT to a table that the migration did
        not create. The engine refuses the statement when the table has a row, so it passes in an
        empty database and fails in the first database with data.

        No finding for a column that the engine fills: IDENTITY, a computed column, rowversion."""
        start = j + 1
        for k in range(j + 1, len(self.sig) + 1):
            if k < len(self.sig) and not (self.depth[k] == 0 and (self.op(k, ",") or self.op(k, ";"))):
                continue
            element = [i for i in range(start, k) if self.depth[i] == 0]
            start = k + 1
            if len(element) < 2 or self.kind(element[0]) not in _IDENT:
                continue
            first, second = element[0], element[1]
            if self.word(first) in _NOT_A_COLUMN or (self.word(first), self.word(second)) == (
                "PERIOD",
                "FOR",
            ):
                continue
            words = [self.word(i) for i in element[1:]]
            filled = (
                words[0] == "AS"
                or "DEFAULT" in words
                or "IDENTITY" in words
                or (self.kind(second) in _IDENT and self.sig[second].value.upper() in _ENGINE_FILLED_TYPES)
            )
            if not filled and any(a == "NOT" and b == "NULL" for a, b in zip(words, words[1:], strict=False)):
                self.report(
                    "NNL001",
                    first,
                    f"ADD {names.quote(self.sig[first].value)} NOT NULL with no DEFAULT: the statement "
                    "fails on a table that has rows; add a DEFAULT, or add the column NULL, fill it and "
                    "alter it",
                )

    def drop_list(self, table: tuple[str, ...], j: int) -> None:
        """ALTER TABLE t DROP <element>, ...; CONSTRAINT is the default. An element is
        [COLUMN | CONSTRAINT] [IF EXISTS] name, a constraint name with WITH (...) after it, or
        PERIOD FOR SYSTEM_TIME. After an element only ',', one ';' or the end has a place: what the
        list does not read cannot ask for its allow line (LFP-01)."""
        what = "CONSTRAINT"
        while True:
            if (self.word(j), self.word(j + 1), self.word(j + 2)) == ("PERIOD", "FOR", "SYSTEM_TIME"):
                self.report(
                    "DROP_CONSTRAINT",
                    j,
                    "DROP PERIOD FOR SYSTEM_TIME: the two period columns become columns like any other",
                )
                after = j + 3
                what = ""  # the grammar gives the next name no kind: it must say COLUMN or CONSTRAINT
            else:
                if self.word(j) in ("COLUMN", "CONSTRAINT"):
                    what = self.word(j)
                    j += 1
                if self.word(j) == "IF" and self.word(j + 1) == "EXISTS":
                    j += 2
                if self.kind(j) not in _IDENT or not what:
                    self.unreadable(j)
                    return
                after = j + 1
                if what == "COLUMN":
                    self.item("DROP_COLUMN", _show((*table, self.sig[j].value)), j)
                else:
                    name = names.quote(self.sig[j].value)
                    self.report(
                        "DROP_CONSTRAINT", j, f"DROP CONSTRAINT {name}: the rule is no longer enforced"
                    )
                    # DROP CONSTRAINT [pk] WITH (ONLINE = ON, MAXDOP = 1): the options of a clustered key
                    options = self.close(j + 2) if self.word(j + 1) == "WITH" else None
                    if options is not None:
                        after = options + 1
            if not self.op(after, ","):
                self.ends_at(after - 1)
                return
            j = after + 1

    def drop(self, kind: str) -> None:
        """DROP TABLE | SEQUENCE | TYPE | SCHEMA | INDEX | SYNONYM [IF EXISTS] name, ..."""
        if kind == "INDEX":
            self.report("DROP_INDEX", 0, "DROP INDEX: check that no query needs the index")
        if kind in ("INDEX", "SYNONYM"):
            return
        j = 4 if self.word(2) == "IF" and self.word(3) == "EXISTS" else 2
        while True:
            name, end = _dotted(self.sig, j)
            if not name:
                self.unreadable(j)
                return
            self.item(f"DROP_{kind}", _show(name), j)
            if not self.op(end, ","):
                return
            j = end + 1

    def one_statement(self, action: int, statement: str) -> None:
        """The lexer's proof of 'one statement'. Two layers:

        the words that start a statement (nothing follows a ';', and no such word stands outside
        parentheses after the first token), and the closed rule (no_place): every token outside
        parentheses has a place in a statement of the closed list. After the end of the first
        statement nothing has a place but one ';'.
        """
        keywords = {action, self.property_verb}  # ALTER COLUMN c DROP MASKED | SPARSE | ...
        for j in range(1, len(self.sig)):
            w = self.word(j)
            second = self.op(j - 1, ";") or (
                self.depth[j] == 0
                and j not in keywords
                and (
                    w in _MODEL_STRAY
                    or (w in ("UPDATE", "DELETE") and not self.fk_action(j))  # ON DELETE CASCADE
                    or (w == "SET" and not self.fk_action(j - 1))  # ON DELETE SET NULL
                    or (w == "IF" and self.word(j + 1) != "EXISTS")  # DROP COLUMN IF EXISTS
                )
            )
            if second:
                # THROW, KILL and others are not reserved words, so the word can be a name
                name = "" if self.op(j - 1, ";") else ". If it is a name, write it in brackets"
                self.report(
                    "MODEL_STATEMENT",
                    j,
                    f"{self.sig[j].text} starts a second statement; a model batch is one statement, "
                    f"put GO between statements{name}",
                )
                # the closed rule is skipped only when an error stands on the token: this one, or
                # the error of another rule (COMMIT is FORBIDDEN_TOKEN). A warning is not enough.
                if (j, ERROR) in self.reported:
                    return
        j = self.no_place(action, statement)
        if j is not None:
            self.stray(j)

    def no_place(self, action: int, statement: str) -> int | None:
        """Index of the first token outside parentheses that a statement of the closed list has no
        place for; None when every token has one.

        A walk with one state: '' (only a word of _MODEL_WORDS can follow), 'name' (a name can
        start), 'type' (the type of the column that was named), 'dot' (the next part of a name).
        A statement that is complete leaves the state '' (or 'type' with no type, which the engine
        refuses), and no word of _MODEL_WORDS starts a statement that the first layer lets pass.
        '(' has a place after a word or a name only (a type, a column list, an option list); in a
        DROP only after WITH; in a statement of _MODEL_NO_GROUP nowhere.
        """
        rename = statement == "EXEC sp_rename"
        act = self.word(action) if action >= 0 else ""
        with_only = statement == "DROP INDEX" or act == "DROP"
        # the names that are columns with a type after them: ADD c int, ALTER COLUMN c int
        typed = {"ADD": ("ADD", ","), "ALTER": ("COLUMN",)}.get(act, ())
        state, opener = ("name", "EXEC") if rename else ("", "")
        for j in range(1, len(self.sig)):
            if self.depth[j] > 0 or (j == action and act == "REBUILD"):
                continue
            t = self.sig[j]
            if t.kind == "op":
                if t.text == ";" and j == len(self.sig) - 1:
                    continue
                if t.text == ")":
                    state = ""
                elif t.text == ".":
                    state = "dot"
                elif t.text in _MODEL_OPS:
                    state, opener = "name", t.text
                elif t.text in lex.CURRENCY:
                    state = ""  # a money literal: $5 is the sign and a number, $ alone is 0
                elif t.text != "(":
                    return j
                elif (
                    statement in _MODEL_NO_GROUP
                    or not (self.kind(j - 1) in _IDENT or self.sig[j - 1].text in _MODEL_OPS - {"."})
                    or (with_only and self.word(j - 1) != "WITH")
                ):
                    return j
            elif t.kind in ("number", "string", "nstring"):
                state = ""
            elif t.kind == "var":
                # EXEC sys.sp_rename @objname = N'...'; DEFAULT @@SPID: a system function is a value
                if not (rename or t.text.startswith("@@")):
                    return j
                state = ""
            else:
                w = self.word(j)  # '' for a quoted identifier: never a keyword
                if state == "dot":
                    state = ""
                elif w in (_MODEL_RESERVED if state == "name" else _MODEL_WORDS):
                    state, opener = ("name" if w in _MODEL_NAME_AFTER else ""), w
                    if w == "DEFAULT" and self.word(j - 1) == "SET":
                        state = ""  # ON DELETE SET DEFAULT is the whole action: no expression follows
                elif state == "name":
                    state = "type" if opener in typed else ""
                elif state == "type":
                    # national character varying(20): NATIONAL is the first word of a type
                    state = "type" if w == "NATIONAL" else ""
                else:
                    return j
        return None


def classify_batch(
    batch: chain.MigrationBatch, mode: str, created_tables: Collection[str] = (), *, path: str = ""
) -> BatchFacts:
    """Classify one migration batch with the lexer only.

    mode is the mode of the migration, 'tx' or 'nontx'. created_tables holds the tables that
    earlier batches of the same migration created (BatchFacts.creates_tables of those batches): a
    key or a constraint on such a table takes no long lock. path is only copied into the findings.
    """
    if mode not in ("tx", "nontx"):
        raise ValueError(f"unknown migration mode {mode!r}")
    if batch.kind not in ("model", "data", "raw"):
        raise ValueError(f"unknown batch kind {batch.kind!r}")
    c = _Classifier(batch, path)
    c.forbidden()
    statement = ""
    creates: tuple[str, ...] = ()
    if batch.kind == "raw":
        if batch.raw_object is None:
            raise ValueError("a raw batch without its object")
        c.item("RAW", batch.raw_object, 0)
    else:
        why = "is not allowed outside a raw batch; users, roles and permissions are not managed"
        c.phrases(_SECURITY, "SECURITY_STATEMENT", why)
        if batch.kind == "data":
            c.data()
        else:
            statement, creates = c.model(mode, created_tables)
    if mode == "nontx":
        c.online_build()
    else:
        c.resumable()
    return BatchFacts(statement, tuple(c.items), tuple(c.findings), creates)


# ------------------------------------------------------------------ allow lines
def _allow_findings(path: str, batch: chain.MigrationBatch, facts: BatchFacts) -> list[Finding]:
    """Each item needs an allow line of its batch with the same code and object; each allow line
    needs an item and a reason."""
    allows = [d for d in batch.directives if isinstance(d, chain.Allow)]
    out: list[Finding] = []
    used: set[int] = set()
    for item in facts.needs_allow:
        matching = [n for n, a in enumerate(allows) if a.code == item.code and _same(a.object, item.object)]
        used.update(matching)
        if not matching:
            hint = "; or use a nontx migration with ONLINE = ON" if item.code == "LONG_LOCK" else ""
            out.append(
                _finding(
                    "LCK001" if item.code == "LONG_LOCK" else item.code,
                    path,
                    item.line,
                    f"{item.code} {item.object} has no allow line. Write above the statement: "
                    f"-- azsqlcd:allow {item.code} {item.object} reason: <why this is safe>{hint}",
                )
            )
    for n, allow in enumerate(allows):
        if _TODO.match(allow.reason):
            out.append(_finding("ALLOW_REASON", path, allow.line, "TODO is not a reason; write the reason"))
        if n not in used and allow.code not in DEFERRED_ALLOW_CODES:
            known = "" if allow.code in ALLOW_CODES else f" ({allow.code} is not an allow code)"
            out.append(
                _finding(
                    "ALLOW_UNUSED",
                    path,
                    allow.line,
                    f"allow {allow.code} {allow.object} matches no statement of its batch{known}",
                )
            )
    return out


# ------------------------------------------------------------------ reading one revision
@dataclass(frozen=True)
class _Parsed:
    """What parse_revision read of one revision. lint_repo and lint_change take it, so that a
    caller of both (verify) reads each migration and each module file once."""

    chain: chain.Chain | None  # None: migrations.sum cannot be read
    tombstones: tuple[chain.Tombstone, ...] | None  # None: the file cannot be read
    migrations: dict[str, chain.Migration]  # migration id -> the file; only the files that parse
    modules: tuple[modules.ModuleFile, ...]  # only the files that read
    findings: tuple[Finding, ...]  # one for each file that cannot be read


def _utf8(path: str, data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise refused("FILE_INVALID", f"{path}: not valid UTF-8 at byte {e.start}", path=path) from None


def _sql_text(path: str, data: bytes) -> str:
    """The text of a migration, a module or a table-class file. Refused (FILE_INVALID, with the
    line): not UTF-8, or a control character anywhere in the text (the rule of _CONTROL)."""
    text = _utf8(path, data)
    m = _CONTROL.search(text)
    if m:
        before = text[: m.start()]
        line = 1 + before.count("\n") + len(re.findall(r"\r(?!\n)", before))
        raise refused(
            "FILE_INVALID",
            f"{path} line {line}: control character U+{ord(m[0]):04X}. A driver can end the text at NUL, "
            "and a comment can end at a character that is a line break for one reader only; remove it",
            path=path,
            line=line,
        )
    return text


def _read_sum(files: Mapping[str, bytes]) -> chain.Chain:
    data = files.get(chain.SUM_PATH)
    return chain.Chain() if data is None else chain.parse_sum(_utf8(chain.SUM_PATH, data))


def _read_tombstones(files: Mapping[str, bytes]) -> tuple[chain.Tombstone, ...]:
    data = files.get(chain.TOMBSTONES_PATH)
    return () if data is None else tuple(chain.parse_tombstones(_utf8(chain.TOMBSTONES_PATH, data)))


def _read_migration(path: str, data: bytes) -> chain.Migration:
    return chain.parse_migration(_sql_text(path, data), path.rpartition("/")[2])


def _read_module(path: str, data: bytes) -> modules.ModuleFile:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        pass  # read_module reports it as MODULE_INVALID with the line
    else:
        _sql_text(path, data)
    return modules.read_module(path, data)


def _kind(path: str) -> str | None:
    """The object kind of an object file path; None for any other path."""
    try:
        return names.key_for_path(path)[0]
    except ValueError:
        return None


def _is_migration(path: str) -> bool:
    return path.startswith("migrations/") and path.count("/") == 1 and path.endswith(".sql")


def parse_revision(files: Mapping[str, bytes]) -> _Parsed:
    """Read the chain, the tombstones, the migrations and the module files of one revision. Give
    the result to lint_repo and to lint_change of the same files (argument `parsed`)."""
    return _parse(files)


def _parse(files: Mapping[str, bytes]) -> _Parsed:
    findings: list[Finding] = []

    def read[T](path: str, reader: Callable[..., T], *args: object) -> T | None:
        try:
            return reader(*args)
        except ToolError as e:
            line = e.detail.get("line") or 1
            findings.append(Finding(e.reason_code, ERROR, path, line, e.message))
            return None

    migrations: dict[str, chain.Migration] = {}
    mods: list[modules.ModuleFile] = []
    for path in sorted(files):
        if _is_migration(path):
            migration = read(path, _read_migration, path, files[path])
            if migration is not None:
                migrations[migration.file] = migration
        elif _kind(path) in names.MODULE_KINDS:
            module = read(path, _read_module, path, files[path])
            if module is not None:
                mods.append(module)
    return _Parsed(
        chain=read(chain.SUM_PATH, _read_sum, files),
        tombstones=read(chain.TOMBSTONES_PATH, _read_tombstones, files),
        migrations=migrations,
        modules=tuple(mods),
        findings=tuple(findings),
    )


# ------------------------------------------------------------------ rules of one revision
def secret_in_text(text: str) -> bool:
    """A literal that looks like a credential, anywhere in the text: the rule for live text that
    nobody reviewed (a module of a database, before it becomes a file).

    It is wider than the rule for repository files (secret_findings). It also finds a variable or a
    named argument (DECLARE @password varchar(9) = 'x', @rmtpassword = 'x'), a quoted name
    ([password] = 'x'), PASSWORD = ('x'), a name that holds password, passwd, pwd, secret or
    passphrase, Pwd=x or Password=x inside a connection string, and a literal passphrase of
    ENCRYPTBYPASSPHRASE or DECRYPTBYPASSPHRASE. It reads text, so it also finds the words inside a
    string or a comment. A hit is a reason to look, not a proof.
    """
    return _SECRET.search(text) is not None or _SECRET_LIVE.search(text) is not None


def _is_comparison(sig: list[Tok], i: int) -> bool:
    """`name = <literal>` at sig[i] is a comparison of a query: on the way back, WHERE, ON, IF,
    HAVING, WHEN or WHILE comes before any word that starts a statement or gives a value (SET,
    WITH, BY, ...). Parentheses of a condition are left ((a = 1 OR Password = 'x')); the
    parentheses of a call or of an option list are not (f(PASSWORD = 'x'), WITH (PASSWORD = 'x')).
    When the words do not decide, it is no comparison: the finding stays."""
    depth = 0
    for j in range(i - 1, -1, -1):
        t = sig[j]
        if _is_op(t, ")"):
            depth += 1
        elif _is_op(t, "("):
            if depth:
                depth -= 1
            elif j and (
                sig[j - 1].kind in _IDENT and _word(sig[j - 1]) not in (*_COMPARES, "AND", "OR", "NOT")
            ):
                return False  # a call or an option list
        elif depth or t.kind != "word":
            if not depth and _is_op(t, ";"):
                return False
        elif t.text.upper() in _COMPARES:
            return True
        elif t.text.upper() in _GIVES_VALUE:
            return False
    return False


def secret_findings(path: str, data: bytes) -> list[Finding]:
    """SECRET_LITERAL findings of one file: PASSWORD = <literal> and SECRET = <literal> (A25).

    A .sql file is read as tokens, so a word in a string or a comment and a variable
    (@password = ...) are not findings; nor is the empty literal, nor a comparison in a WHERE, ON,
    IF, HAVING or WHEN (_is_comparison). Any other file, and SQL that cannot be lexed, is read as
    text (the rule of secret_in_text).
    """
    text = data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    lines: list[int] | None = None
    if path.endswith(".sql"):
        try:
            sig = lex.significant(lex.tokenize(text))
        except lex.LexError:
            sig = None
        if sig is not None:
            lines = [
                t.line
                for i, (t, equals, value) in enumerate(zip(sig, sig[1:], sig[2:], strict=False))
                if t.kind == "word"
                and t.text.upper() in ("PASSWORD", "SECRET")
                and _is_op(equals, "=")
                # '' is no secret: UPDATE ... SET Password = '' clears a column
                and ((value.kind in ("string", "nstring") and value.value) or value.text[:2].lower() == "0x")
                and not _is_comparison(sig, i)
            ]
    if lines is None:
        lines = [1 + text.count("\n", 0, m.start()) for m in _SECRET.finditer(text)]
    message = "a PASSWORD = or SECRET = literal; a secret is never written in the repository"
    return [_finding("SECRET_LITERAL", path, line, message) for line in lines]


def _path_findings(files: Mapping[str, bytes], table_file_check: TableFileCheck | None) -> list[Finding]:
    """Unknown paths under schema/ and migrations/; the table-class files."""
    out: list[Finding] = []
    for path in sorted(files):
        root = path.split("/", 1)[0]
        kind = _kind(path)
        known = path in (chain.SUM_PATH, chain.TOMBSTONES_PATH) or _is_migration(path) or kind is not None
        if root in ("schema", "migrations") and not known:
            out.append(
                _finding(
                    "UNKNOWN_PATH",
                    path,
                    1,
                    "not a file of the layout: schema/<kind directory>/<schema>.<name>.sql, "
                    f"{chain.TOMBSTONES_PATH}, {chain.SUM_PATH}, migrations/<NNNN__name>.sql",
                )
            )
        if kind in names.TABLE_CLASS_KINDS:
            try:
                text = _sql_text(path, files[path]).removeprefix(lex.BOM)
                lex.tokenize(text)
            except ToolError as e:
                out.append(_finding("FILE_INVALID", path, e.detail.get("line") or 1, e.message))
            except lex.LexError as e:
                out.append(_finding("FILE_INVALID", path, e.line or 1, f"{path}: {e}"))
            else:
                if table_file_check is not None:
                    out += _moved_clauses(table_file_check(path, text))
    return out


def _moved_clauses(found: list[Finding]) -> list[Finding]:
    """NF000 for a clause that stands in another place than in the canonical form.

    The token comparison reports it twice on one line: 'the canonical form has X; the file does
    not' and 'the file has X; the canonical form does not'. Those two are one finding that says
    the canonical order. Every other finding is returned as it is.
    """
    extra: dict[tuple[str, str, str], Finding] = {}
    for f in found:
        m = _NF000_EXTRA.fullmatch(f.message) if f.code == "NF000" else None
        if m:
            extra[(f.path, m[1], m[2])] = f
    out: list[Finding] = []
    merged: set[int] = set()  # id() of each 'the file has X' finding that became a part of another
    for f in found:
        m = _NF000_MISSING.fullmatch(f.message) if f.code == "NF000" else None
        twin = extra.pop((f.path, m[1], m[2]), None) if m else None
        if m and twin is not None:
            merged.add(id(twin))
            message = (
                f"line {m[1]}: {m[2]} is in another place than in the canonical form. {_CANONICAL_ORDER}"
            )
            out.append(Finding("NF000", f.severity, f.path, f.line, message))
        else:
            out.append(f)
    return [f for f in out if id(f) not in merged]


def _chain_findings(files: Mapping[str, bytes], parsed: _Parsed) -> list[Finding]:
    """Every file of migrations/ is in the chain and the reverse; sha256 and mode are equal."""
    if parsed.chain is None:
        return []
    out: list[Finding] = []
    listed = {entry.file for entry in parsed.chain.entries}
    for path in sorted(files):
        if _is_migration(path) and path.rpartition("/")[2] not in listed:
            message = f"this migration has no line in {chain.SUM_PATH}. {_RESUM_HINT}"
            out.append(_finding("CHN002", path, 1, message))
    for n, entry in enumerate(parsed.chain.entries):
        line = n + 2 + parsed.chain.baseline
        data = files.get(f"migrations/{entry.file}")
        if data is None:
            out.append(_finding("CHN003", chain.SUM_PATH, line, f"migrations/{entry.file} does not exist"))
            continue
        if chain.file_sha256(data) != entry.sha256:
            message = f"the sha256 of this line is not the sha256 of migrations/{entry.file}. {_RESUM_HINT}"
            out.append(_finding("CHN004", chain.SUM_PATH, line, message))
        migration = parsed.migrations.get(entry.file)
        if migration is not None and migration.mode != entry.mode:
            message = f"this line says {entry.mode}; migrations/{entry.file} says mode {migration.mode}"
            out.append(_finding("CHN005", chain.SUM_PATH, line, message))
    return out


def _data_line(batch: chain.MigrationBatch) -> int:
    """The file line of the '-- azsqlcd:data' line of a data batch: the first line of the batch."""
    line = next((d.line for d in lex.directives(batch.text) if d.name == "data"), 1)
    return batch.first_line - 1 + line


def _migration_findings(path: str, migration: chain.Migration, data_batches: bool) -> list[Finding]:
    """data_batches: [project] data_batches of azsqlcd.toml. When it is false, a data batch is one
    DATA000 finding and no other rule reads the batch."""
    out: list[Finding] = []
    if migration.mode == "nontx" and migration.expected_minutes is None:
        out.append(
            _finding(
                "NTX005",
                path,
                1,
                "a nontx migration needs expected-minutes: the job timeout is computed from it, and a "
                "timeout must not end a build. Write '-- azsqlcd:mode nontx expected-minutes: <N>'",
            )
        )
    created: set[str] = set()
    for batch in migration.batches:
        for t in lex.tokens(batch.text):
            if (
                t.kind == "comment"
                and _NEAR_DIRECTIVE.match(t.text)
                and not t.text.startswith(lex.DIRECTIVE_PREFIX)
            ):
                written = t.text[: t.text.index(":") + 1]
                message = (
                    f"'{written}' makes this line a plain comment, and it does nothing: a directive "
                    f"starts with '{lex.DIRECTIVE_PREFIX}' exactly (two hyphens, one space, lower case) "
                    "in column 0"
                )
                out.append(_finding("ALLOW_FORMAT", path, batch.first_line - 1 + t.line, message))
        if batch.kind == "data" and not data_batches:
            out.append(_finding("DATA000", path, _data_line(batch), _DATA_OFF))
            continue
        facts = classify_batch(batch, migration.mode, created, path=path)
        out += facts.findings
        out += _allow_findings(path, batch, facts)
        created.update(facts.creates_tables)
    return out


def _module_text_findings(m: modules.ModuleFile, taken: Collection[tuple[str, str]]) -> list[Finding]:
    """What one module file holds that a deploy cannot carry, and its directives.

    taken: (schema, name), without case, of every module file of the revision.
    """
    out: list[Finding] = []
    sig = lex.significant(lex.tokens(m.text))
    for i, t in enumerate(sig):
        w = _word(t)
        if w == "PARSEONLY":
            # the planner sends every module under SET PARSEONLY ON and refuses a text with this word:
            # the setting is read when a batch is parsed, so the text could switch the check off
            message = (
                "the word PARSEONLY in a module file: SET PARSEONLY would switch off the syntax check "
                "of the plan, and the plan refuses the file. For a name, write [PARSEONLY]"
            )
            out.append(_finding("FORBIDDEN_TOKEN", m.path, t.line, message))
        elif w == "SET":
            # one SET can hold a list of options: SET NOCOUNT, NOEXEC ON
            options, value = _set_options(sig, i)
            if "NOEXEC" in options:
                message = (
                    "SET NOEXEC in a module file: a session that runs the module would compile every "
                    "later batch and run none of them"
                )
                out.append(_finding("FORBIDDEN_TOKEN", m.path, t.line, message))
            opens = sorted(_SET_OPENS_TRANSACTION.intersection(options))
            if opens and value != "OFF":
                message = (
                    f"SET {opens[0]} in a module file, not OFF: a write of the module would open a "
                    "transaction that nothing commits, in the session of the batch that runs the module"
                )
                out.append(_finding("FORBIDDEN_TOKEN", m.path, t.line, message))
    found = lex.directives(m.text)
    known = " and ".join(lex.DIRECTIVE_PREFIX + name for name in _MODULE_DIRECTIVES)
    for d in found:
        if d.name not in _MODULE_DIRECTIVES:
            message = f"{lex.DIRECTIVE_PREFIX}{d.name} does nothing in a module file; it reads only {known}"
            out.append(_finding("DIR001", m.path, d.line, message))
    read = {d.line for d in found}
    for t in lex.tokens(m.text):
        if t.kind == "comment" and t.text.startswith(lex.DIRECTIVE_PREFIX) and t.line not in read:
            message = "a directive starts in column 0; after other text on its line it does nothing"
            out.append(_finding("DIR001", m.path, t.line, message))
    # read_module keeps the targets of each directive name in the order of the file
    for name, targets in (("after", m.after), ("ignore-dep", m.ignore_dep)):
        lines = [d.line for d in found if d.name == name]
        for line, (schema, target) in zip(lines, targets, strict=True):
            if (schema.casefold(), target.casefold()) not in taken:
                message = (
                    f"{lex.DIRECTIVE_PREFIX}{name} {names.qualified(schema, target)} names no module file "
                    "of the repository, so it changes nothing"
                )
                out.append(_finding("DIR002", m.path, line, message))
    return out


def _module_findings(mods: tuple[modules.ModuleFile, ...]) -> list[Finding]:
    """One object for each name (MODULE_DUPLICATE); the deploy order is computable (ORD004,
    CYCLE_BROKEN); EXECUTE AS in a header (EXA001); the rules of _module_text_findings."""
    out: list[Finding] = []
    # the names of one schema are one namespace for every kind, and the catalog compares without case
    first: dict[tuple[str, str], modules.ModuleFile] = {}
    for m in sorted(mods, key=lambda m: m.path):
        other = first.setdefault((m.schema.casefold(), m.name.casefold()), m)
        if other is not m:
            message = f"{other.path} names the same object; a database holds one object with a name"
            out.append(_finding("MODULE_DUPLICATE", m.path, 1, message))
    for m in mods:
        # every rule of one file before the next file: its text is scanned once (lex.tokens)
        out += _module_text_findings(m, first.keys())
        out += _execute_as_findings(m)
    ordered = list(first.values())  # a second file of a name would be a cycle with the first
    by_key = {m.key: m for m in sorted(ordered, key=lambda m: m.key)}
    try:
        _, warnings = modules.deploy_order(ordered, modules.build_edges(ordered, ()))
    except ToolError as e:
        for cycle in e.detail["cycles"]:
            message = "dependency cycle through a view or a function: " + " -> ".join(cycle)
            out.append(_finding("ORD004", by_key[cycle[0]].path, 1, message))
    else:
        for warning in warnings:
            # the warning lists the keys of the cycle in key order
            path = next(m.path for key, m in by_key.items() if key in warning)
            out.append(_finding("CYCLE_BROKEN", path, 1, warning))
    return out


def _execute_as_findings(m: modules.ModuleFile) -> list[Finding]:
    who = modules.execute_as(m.text)
    if who in (None, "CALLER"):
        return []
    sig = lex.significant(lex.tokens(m.text))
    # the header stands before the body, so the first EXECUTE AS is the one of the header
    line = next(
        (t.line for t, nxt in zip(sig, sig[1:], strict=False) if _word(t) in _EXEC and _word(nxt) == "AS"),
        1,
    )
    message = f"EXECUTE AS {who}: the module runs with the permissions of another principal"
    return [_finding("EXA001", m.path, line, message)]


def _tombstone_findings(files: Mapping[str, bytes], parsed: _Parsed) -> list[Finding]:
    """TMB002: a tombstone for a module that still has its file. The release must say one thing.

    The object key decides, as in the plan: [a.b].[c] and [a].[b.c] have one path, and a tombstone
    for the one beside the file of the other is no conflict. A file that cannot be read has no key
    (it has its own finding); then the path decides.
    """
    out: list[Finding] = []
    text = files.get(chain.TOMBSTONES_PATH, b"").decode("utf-8", "replace")
    keys = {m.path: m.key for m in parsed.modules}
    # the plan compares the keys without case (TOMBSTONE_CONFLICT): [s].[P] and [S].[p] are one object
    folded = {m.key.casefold(): m.path for m in parsed.modules}
    for t in parsed.tombstones or ():
        path = _tombstone_path(t)  # None: a name that cannot be a file name has no file
        if not (path is not None and path in files and keys.get(path, t.object_key) == t.object_key):
            path = folded.get(t.object_key.casefold())
        if path is not None:
            at = text.find(t.object_key)
            line = 1 + text.count("\n", 0, at) if at >= 0 else 1
            message = f"{t.object_key} has a tombstone and {path} still exists"
            out.append(_finding("TMB002", chain.TOMBSTONES_PATH, line, message))
    return out


def _read_project(files: Mapping[str, bytes]) -> tuple[config.Project | None, str | None]:
    """([project] of azsqlcd.toml, None), or (None, why the file gives no project). A revision
    without a project has every switch of the project off."""
    if _CONFIG_PATH not in files:
        return None, f"{_CONFIG_PATH} does not exist"
    try:
        return config.load_config(_utf8(_CONFIG_PATH, files[_CONFIG_PATH])).project, None
    except ToolError as e:
        return None, e.message


def lint_repo(
    files: Mapping[str, bytes],
    *,
    table_file_check: TableFileCheck | None = None,
    parsed: _Parsed | None = None,
) -> list[Finding]:
    """The rules that need one revision. files: path -> file bytes, as release.read_tree returns.

    table_file_check(path, text) is called for each table-class file that can be lexed; its
    findings are returned with the others. The findings are sorted by path, line and code.

    [project] data_batches of the azsqlcd.toml in files decides the rules of a data batch: false
    (the default, and a file that does not load) gives DATA000 for each data batch and nothing else
    for that batch; true gives the data rules.

    parsed: parse_revision(files) when the caller has it already; else the files are read here.
    """
    if parsed is None:
        parsed = _parse(files)
    out = list(parsed.findings)
    project, problem = _read_project(files)
    if problem is not None:
        out.append(_finding("CONFIG_INVALID", _CONFIG_PATH, 1, problem))
    data_batches = project is not None and project.data_batches
    for path in files:
        out += secret_findings(path, files[path])
    out += _path_findings(files, table_file_check)
    out += _chain_findings(files, parsed)
    for migration in parsed.migrations.values():
        out += _migration_findings(f"migrations/{migration.file}", migration, data_batches)
    out += _module_findings(parsed.modules)
    out += _tombstone_findings(files, parsed)
    return _sorted(out)


# ------------------------------------------------------------------ rules of a change
def _module_paths(files: Mapping[str, bytes]) -> set[str]:
    return {path for path in files if _kind(path) in names.MODULE_KINDS}


def _module_changed(base: bytes | None, head: bytes) -> bool:
    """New, or another checksum: what a deploy of the head revision sends again."""
    if base is None:
        return True
    if base == head:
        return False
    try:
        return modules.checksum(base) != modules.checksum(head)
    except ToolError:
        return True


def _tombstone_path(tombstone: chain.Tombstone) -> str | None:
    kind, schema, name = names.parse_object_key(tombstone.object_key)
    try:
        return names.path_for(kind, schema, name)
    except ValueError:
        return None


def _removed_key(path: str, data: bytes) -> str | None:
    """The object key of a removed module file; None when the base file cannot be read."""
    try:
        return modules.read_module(path, data).key
    except ToolError:
        return None


def _has_tombstone(path: str, key: str | None, tombstones: tuple[chain.Tombstone, ...]) -> bool:
    """A tombstone holds the object key of the module of the removed file, as the planner compares it.

    The path is not enough: [a.b].[c] and [a].[b.c] have one path. A base file that cannot be read
    has no key (None), and only then the path decides.
    """
    if key is None:
        return any(_tombstone_path(t) == path for t in tombstones)
    return any(t.object_key == key for t in tombstones)


def _case_twin(path: str, key: str | None, head: _Parsed, added: Collection[str]) -> str | None:
    """The new head file that holds the object of a removed file under a name that differs in
    letter case only; None when there is none. The object key decides; the path decides only for
    a file that cannot be read on one side."""
    if key is not None:
        twin = next(
            (m.path for m in head.modules if m.path in added and m.key.casefold() == key.casefold()), None
        )
        if twin is not None:
            return twin
    readable = {m.path for m in head.modules}
    return next(
        (p for p in sorted(added) if p.casefold() == path.casefold() and (key is None or p not in readable)),
        None,
    )


def _fold(name: tuple[str, ...] | None) -> tuple[str, ...]:
    return tuple(part.casefold() for part in name or ())


def _residue(sig: list[Tok], parts: tuple[str, ...]) -> int | None:
    """Line where a module still uses an old name. [s].[t]: the name. [s].[t].[c]: the module names
    the table and holds c as an identifier."""
    if len(parts) == 2:
        return _mention(sig, parts[0], parts[1])
    if len(parts) == 3 and _mention(sig, parts[0], parts[1]) is not None:
        column = parts[2].casefold()
        return next((t.line for t in sig if t.kind in _IDENT and t.value.casefold() == column), None)
    return None


def lint_change(
    base_files: Mapping[str, bytes], head_files: Mapping[str, bytes], *, parsed: _Parsed | None = None
) -> list[Finding]:
    """The rules that need the base revision. Both arguments: path -> file bytes.

    The findings of lint_repo(head_files) are not repeated: a head file that cannot be read is
    reported there and its rules are skipped here. A migrations.sum or _tombstones.toml of the base
    revision that cannot be read raises ToolError, because no rule can be decided without it.

    MODULE_CASE (warning): a module file is renamed and the new name differs in letter case only.
    That is one object for the database, so no tombstone is asked for (TMB001).

    parsed: parse_revision(head_files) when the caller has it already; else the files are read here.
    """
    base_chain = _read_sum(base_files)
    base_tombstones = {t.object_key for t in _read_tombstones(base_files)}
    head = parsed if parsed is not None else _parse(head_files)
    project, _ = _read_project(head_files)
    data_batches = project is not None and project.data_batches
    table_model = project is not None and project.table_model
    out: list[Finding] = []

    new: list[chain.ChainEntry] = []
    if head.chain is not None:
        for problem in chain.check_immutable(base_chain, head.chain):
            m = _LINE.match(problem)
            out.append(_finding("CHN001", chain.SUM_PATH, int(m[1]) if m else 1, problem))
        merged = {entry.file for entry in base_chain.entries}
        new = [entry for entry in head.chain.entries if entry.file not in merged]
        if not table_model:
            # with table_model = true the model proof of `verify` holds this rule: it has the tables
            was_live = {entry.file for entry in base_chain.entries if not entry.withdrawn}
            replaced = {entry.replaces for entry in new}
            for n, entry in enumerate(head.chain.entries):
                if entry.withdrawn and entry.file in was_live and entry.file not in replaced:
                    out.append(
                        _finding(
                            "CHN006",
                            chain.SUM_PATH,
                            n + 2 + head.chain.baseline,
                            f"{entry.file} is merged and this pull request withdraws it with no "
                            "replacement: a database that did not run it would skip it for good. Add "
                            f"a migration whose chain line holds replaces={entry.file} in the same "
                            "pull request",
                        )
                    )

    base_paths, head_paths = _module_paths(base_files), _module_paths(head_files)
    removed = sorted(base_paths - head_paths)
    changed = {path for path in head_paths if _module_changed(base_files.get(path), head_files[path])}
    for path in sorted(head_paths - base_paths):
        kind, stem = names.key_for_path(path)
        for old in removed:
            old_kind, old_stem = names.key_for_path(old)
            if old_kind != kind and old_stem.casefold() == stem.casefold():
                out.append(
                    _finding(
                        "KND001",
                        path,
                        1,
                        f"{old} was a {old_kind} and this file makes the name a {kind}. A module keeps "
                        "its kind: drop the old object in one release, or use a new name",
                    )
                )

    fresh: list[chain.Tombstone] = []
    if head.tombstones is not None:
        fresh = [t for t in head.tombstones if t.object_key not in base_tombstones]
        for path in removed:
            key = _removed_key(path, base_files[path])
            twin = _case_twin(path, key, head, head_paths - base_paths)
            if twin is not None:
                # N2-F3: with a tombstone the plan refuses the release (TOMBSTONE_CONFLICT), because
                # it compares keys without case
                message = (
                    f"{path} is renamed to this file and the names differ in letter case only. The "
                    "database compares names without case, so this is the same object: no tombstone is "
                    "needed, and the name in the catalog does not change (CREATE OR ALTER keeps the "
                    "letter case that the object was created with)"
                )
                out.append(_finding("MODULE_CASE", twin, 1, message))
            elif not _has_tombstone(path, key, head.tombstones):
                entry = key if key is not None else f"{_kind(path)}:[<schema>].[<name>]"
                message = (
                    f"this module file is removed and {chain.TOMBSTONES_PATH} has no [[drop]] for it. "
                    f'Add to that file the three lines: [[drop]] / object = "{entry}" / '
                    'reason = "<why the module is dropped>"'
                )
                out.append(_finding("TMB001", path, 1, message))

    alone = len(new) == 1 and not changed and not removed and not fresh
    changed_modules = [m for m in head.modules if m.path in changed]
    old_names: list[tuple[str, str, tuple[str, ...]]] = []  # (code, what, name parts)
    for t in fresh:
        _, schema, name = names.parse_object_key(t.object_key)
        old_names.append(("DRP002", f"{t.object_key} has a tombstone", (schema or "", name)))
    for entry in new:
        path = f"migrations/{entry.file}"
        migration = head.migrations.get(entry.file)
        if not alone and "nontx" in (entry.mode, migration.mode if migration else ""):
            out.append(
                _finding(
                    "NTX003",
                    path,
                    1,
                    "a nontx migration is the only change of its release: no other new migration, "
                    "module change or tombstone in the same pull request",
                )
            )
        if migration is None:
            continue
        deployed: set[tuple[str, ...]] = set()
        for batch in migration.batches:
            for item in classify_batch(batch, migration.mode).needs_allow:
                parts = _name_parts(item.object)
                if item.code == "RENAME" and parts:
                    old_names.append(("REN001", f"a new migration renames {item.object}", parts))
                elif item.code in ("DROP_TABLE", "DROP_COLUMN") and parts:
                    old_names.append(("DRP003", f"a new migration drops {item.object}", parts))
            deployed |= {
                _fold(_name_parts(d.object_key))
                for d in batch.directives
                if isinstance(d, chain.DeployModule)
            }
            if batch.kind == "model" or (batch.kind == "data" and not data_batches):
                continue  # a data batch that is switched off is DATA000 in lint_repo
            covered = deployed | {
                _fold(_name_parts(d.object))
                for d in batch.directives
                if isinstance(d, chain.Allow) and d.code == "OLD_MODULE"
            }
            sig = lex.significant(lex.tokenize(batch.text))
            for m in changed_modules:
                line = _mention(sig, m.schema, m.name)
                if line is not None and _fold((m.schema, m.name)) not in covered:
                    name = names.qualified(m.schema, m.name)
                    out.append(
                        _finding(
                            "DAT001",
                            path,
                            batch.first_line - 1 + line,
                            f"this {batch.kind} batch uses {name}, which this pull request adds or changes; "
                            f"the batch would run the old text. Write '-- azsqlcd:deploy-module {name}' "
                            f"above it, or '-- azsqlcd:allow OLD_MODULE {name} reason: <text>'",
                        )
                    )

    if old_names:
        for m in head.modules:
            sig = lex.significant(lex.tokenize(m.text))
            for code, what, parts in old_names:
                line = None if _fold(parts) == _fold((m.schema, m.name)) else _residue(sig, parts)
                if line is not None:
                    out.append(_finding(code, m.path, line, f"{what} and this module still uses the name"))
    return _sorted(out)
