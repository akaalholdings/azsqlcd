"""The migration chain (migrations/migrations.sum), migration files and module tombstones.

The migration id is the file name, for example '0001__add_order_status.sql'. It is the id in the
chain, in the manifest and in the state tables. The header directive of the file holds the same
name without '.sql'.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace

from azsqlcd import lex, names
from azsqlcd.errors import ToolError, refused

SUM_PATH = "migrations/migrations.sum"
TOMBSTONES_PATH = "schema/_tombstones.toml"

_SUM_HEADER = "azsqlcd-sum 1"
_FILE = re.compile(r"[0-9]{4,}__[A-Za-z0-9_]+\.sql")
_ENTRY = re.compile(
    r"(?P<file>\S+) sha256:(?P<sha>[0-9a-f]{64}) (?P<mode>tx|nontx)"
    r"(?P<withdrawn> withdrawn)?(?: replaces=(?P<replaces>\S+))?"
)
_BRACKETED = r"\[(?:[^\]]|\]\])+\]"
_TWO_PART = re.compile(rf"{_BRACKETED}\.{_BRACKETED}")
# an object in a directive: no white space outside brackets, so ' reason:' ends it
_OBJECT = rf"(?:{_BRACKETED}|[^\s\[\]])+"
_ALLOW = re.compile(
    rf"(?P<code>[A-Z][A-Z0-9_]*)[ \t]+(?P<object>{_OBJECT})[ \t]+reason:[ \t]*(?P<reason>\S.*)"
)
_RAW = re.compile(rf"(?P<object>{_OBJECT})[ \t]+reason:[ \t]*(?P<reason>\S.*)")
_MODE = re.compile(r"(?P<mode>tx|nontx)(?:[ \t]+expected-minutes:[ \t]*(?P<minutes>[0-9]+))?")
_DROP_HEADER = re.compile(r"\[\[\s*drop\s*\]\]\s*(?:#.*)?")
_TOML_FENCE = re.compile(r'"""|\'\'\'')
_TOML_LINE = re.compile(r"\(at line (\d+),")


# ------------------------------------------------------------------ migrations.sum
@dataclass(frozen=True)
class ChainEntry:
    file: str  # the migration id
    sha256: str  # file_sha256 of the migration file, 64 lower-case hex
    mode: str  # tx | nontx
    withdrawn: bool = False
    replaces: str | None = None  # file of the withdrawn migration that this one replaces


@dataclass(frozen=True)
class Chain:
    baseline: bool = False
    entries: tuple[ChainEntry, ...] = ()  # file order


def _valid_file(name: str) -> bool:
    # azsqlcd.step.migration_id is nvarchar(200)
    return len(name) <= 200 and _FILE.fullmatch(name) is not None


def is_migration_file(name: str) -> bool:
    """The name is a migration id: NNNN__name.sql, at most 200 characters."""
    return _valid_file(name)


def _number(file: str) -> int:
    return int(file.split("__", 1)[0])


def _line_of(chain_has_baseline: bool, index: int) -> int:
    return index + 2 + chain_has_baseline


def _problems(entries: Sequence[ChainEntry]) -> Iterator[tuple[int, str]]:
    """(entry index, problem) for every rule that one chain must keep on its own.

    Numbers that rise also make every file name unique.
    """
    earlier: dict[str, ChainEntry] = {}
    replaced: set[str] = set()
    highest = -1
    for i, entry in enumerate(entries):
        if _number(entry.file) <= highest:
            yield (
                i,
                "has a number that is not higher than every earlier one; when the migration is not "
                "merged yet (the usual state after a merge of main), run azsqlcd gen --resum: it gives "
                "the new migrations the next numbers",
            )
        if entry.replaces is not None:
            target = earlier.get(entry.replaces)
            if target is None:
                yield i, f"replaces {entry.replaces}, which is not an earlier line"
            elif not target.withdrawn:
                yield i, f"replaces {entry.replaces}, which is not withdrawn"
            elif entry.replaces in replaced:
                yield i, f"replaces {entry.replaces}, which has a replacement already"
            replaced.add(entry.replaces)
        earlier.setdefault(entry.file, entry)
        highest = max(highest, _number(entry.file))


def parse_sum(text: str) -> Chain:
    """Read migrations.sum. Raises ToolError REFUSED CHAIN_INVALID with the line number.

    The grammar is exact (one space between words, lower-case hex, no blank line, a line break after
    the last line), so format_sum(parse_sum(text)) == text and equal entries mean equal lines.
    Only a leading BOM and CRLF or CR line ends are accepted and removed (Windows checkout).
    """

    def bad(line: int, problem: str) -> ToolError:
        return refused("CHAIN_INVALID", f"{SUM_PATH} line {line}: {problem}", line=line)

    lines = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines[0] != _SUM_HEADER:
        raise bad(1, f"the first line must be '{_SUM_HEADER}'")
    if lines[-1] != "":
        raise bad(len(lines), "the last line must end with a line break")
    baseline = False
    entries: list[ChainEntry] = []
    for n, line in enumerate(lines[1:-1], start=2):
        if line == "baseline":
            if n != 2:
                raise bad(n, "'baseline' can only be the first entry")
            baseline = True
            continue
        m = _ENTRY.fullmatch(line)
        if (
            not m
            or not _valid_file(m["file"])
            or (m["replaces"] is not None and not _valid_file(m["replaces"]))
        ):
            raise bad(
                n,
                "expected '<NNNN__name.sql> sha256:<64 hex> <tx|nontx> [withdrawn] [replaces=<file>]' "
                "with one space between the words",
            )
        entries.append(ChainEntry(m["file"], m["sha"], m["mode"], m["withdrawn"] is not None, m["replaces"]))
    for i, problem in _problems(entries):
        raise bad(_line_of(baseline, i), f"{entries[i].file} {problem}")
    return Chain(baseline, tuple(entries))


def format_sum(chain: Chain) -> str:
    """The text of migrations.sum. The inverse of parse_sum."""
    lines = [_SUM_HEADER]
    if chain.baseline:
        lines.append("baseline")
    for entry in chain.entries:
        line = f"{entry.file} sha256:{entry.sha256} {entry.mode}"
        if entry.withdrawn:
            line += " withdrawn"
        if entry.replaces is not None:
            line += f" replaces={entry.replaces}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def effective_order(chain: Chain) -> list[ChainEntry]:
    """Apply order: a replacement stands directly after the migration it replaces.

    Withdrawn entries stay in the list; the planner decides what to do with them.
    """
    order: list[ChainEntry] = []
    for entry in chain.entries:
        if entry.replaces is None:
            order.append(entry)
            continue
        at = next((i for i, placed in enumerate(order) if placed.file == entry.replaces), None)
        if at is None:
            raise refused(
                "CHAIN_INVALID", f"{entry.file} replaces {entry.replaces}, which is not an earlier line"
            )
        order.insert(at + 1, entry)
    return order


def file_sha256(data: bytes) -> str:
    """Checksum of a migration file, as written in migrations.sum.

    Frozen definition, never to change: sha256 (lower-case hex) over the file bytes after one
    leading UTF-8 BOM is removed and every CRLF and every lone CR is changed to LF. A Windows
    checkout therefore gives the same hash as the git blob.
    """
    return hashlib.sha256(
        data.removeprefix(b"\xef\xbb\xbf").replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    ).hexdigest()


def check_immutable(base: Chain, head: Chain) -> list[str]:
    """Findings for a head chain that does not keep the base chain. An empty list is a pass.

    Rules: every base line is at head, in the same order, identical except for the added word
    'withdrawn'; new lines are only at the end; each new file has a number higher than every
    earlier one; 'replaces' names a withdrawn line; a baseline line cannot disappear, and it can
    appear only when the base chain has no migration (pass Chain() when the file is new).
    """
    out: list[str] = []
    if base.baseline and not head.baseline:
        out.append("line 2: the 'baseline' line cannot be removed")
    if head.baseline and not base.baseline and base.entries:
        out.append("line 2: a 'baseline' line cannot be added to a chain that has migrations")
    for i, merged in enumerate(base.entries):
        line = _line_of(head.baseline, i)
        now = head.entries[i] if i < len(head.entries) else None
        if now is None or now.file != merged.file:
            out.append(
                f"line {line}: the line of {merged.file} must be here; a merged line is never removed "
                "or moved, and new lines go at the end"
            )
            break
        if now != merged and now != replace(merged, withdrawn=True):
            out.append(
                f"line {line}: {merged.file} is merged; only the word 'withdrawn' can be added to its line"
            )
    for i, problem in _problems(head.entries):
        if i >= len(base.entries):
            out.append(f"line {_line_of(head.baseline, i)}: {head.entries[i].file} {problem}")
    return out


# ------------------------------------------------------------------ migration file
@dataclass(frozen=True)
class DeployModule:
    object_key: str  # '[schema].[name]' as written; the kind is resolved later from the module files
    line: int


@dataclass(frozen=True)
class Unbind:
    object_key: str  # '[schema].[name]' as written; the kind is resolved later from the module files
    line: int


@dataclass(frozen=True)
class Allow:
    code: str
    object: str  # as written, for example '[sales].[Order].[Stat]'
    reason: str
    line: int


@dataclass(frozen=True)
class MigrationBatch:
    kind: str  # model | data | raw
    text: str  # exactly as it is sent; directive lines stay, they are comments
    first_line: int  # 1-based line in the file
    # in file order; DeployModule and Unbind always stand above the first statement of the batch
    directives: tuple[DeployModule | Unbind | Allow, ...]
    raw_object: str | None = None  # raw only: the object key of the unmanaged object
    raw_reason: str | None = None  # raw only


@dataclass(frozen=True)
class Migration:
    file: str  # the migration id
    mode: str  # tx | nontx
    expected_minutes: int | None
    batches: tuple[MigrationBatch, ...]


def parse_migration(text: str, file_name: str) -> Migration:
    """Read a migration file. Raises ToolError REFUSED MIGRATION_INVALID with file and line.

    Header, before the first statement, each exactly once: '-- azsqlcd:migration <file name without
    .sql>' and '-- azsqlcd:mode tx|nontx [expected-minutes: N]'. A batch is 'data' or 'raw' when
    that directive is its first line (in the first batch: the first line after the header);
    otherwise it is 'model'. Every directive belongs to the batch whose text holds it.
    """

    def bad(line: int, problem: str) -> ToolError:
        return refused("MIGRATION_INVALID", f"{file_name} line {line}: {problem}", file=file_name, line=line)

    if not _valid_file(file_name):
        raise bad(1, "the file name must be NNNN__name.sql (digits, then letters, digits and '_')")
    try:
        split = lex.split_batches(text)
    except lex.LexError as e:
        raise bad(e.line, str(e).removeprefix(f"line {e.line}: ")) from None

    header: dict[str, tuple[str, int]] = {}  # directive name -> (text, line)
    batches: list[MigrationBatch] = []
    for index, batch in enumerate(split):
        offset = batch.first_line - 1
        if batch.repeat != 1:
            raise bad(batch.first_line, f"this batch ends with GO {batch.repeat}; a GO count is not allowed")
        tokens = lex.tokenize(batch.text)
        statement_lines = [t.line for t in tokens if t.kind not in lex.TRIVIA]
        if not statement_lines:
            raise bad(batch.first_line, "this batch has no statement")
        found = {d.line: d for d in lex.directives(batch.text)}
        kind, raw_object, raw_reason = "model", None, None
        attached: list[DeployModule | Unbind | Allow] = []
        above_statements = True
        kind_line = True  # only blank lines and header directives so far
        for t in tokens:
            if t.kind in ("ws", "nl"):
                continue
            is_directive = t.kind == "comment" and t.text.startswith(lex.DIRECTIVE_PREFIX)
            d = found.get(t.line) if is_directive else None
            line = offset + t.line
            if d is None:
                if is_directive:
                    raise bad(line, "a directive starts in column 0")
                if t.kind != "comment":
                    above_statements = False
                kind_line = False
                continue
            if d.name in ("migration", "mode"):
                if index or not above_statements:
                    raise bad(line, f"'{d.name}' must be above the first statement of the file")
                if d.name in header:
                    raise bad(line, f"'{d.name}' is given more than once")
                header[d.name] = (d.args, line)
                kind_line = True
                continue
            if d.name in ("data", "raw"):
                if not kind_line:
                    raise bad(line, f"'{d.name}' must be the first line of its batch")
                kind_line = False
                kind = d.name
                if kind == "data":
                    if d.args:
                        raise bad(line, "'data' takes no text")
                    continue
                m = _RAW.fullmatch(d.args)
                if not m:
                    raise bad(line, "expected: raw <object key> reason: <text>")
                try:
                    names.parse_object_key(m["object"])
                except ValueError:
                    raise bad(line, "'raw' needs an object key, for example TABLE:[audit].[Log]") from None
                raw_object, raw_reason = m["object"], m["reason"]
                continue
            kind_line = False
            if d.name == "allow":
                m = _ALLOW.fullmatch(d.args)
                if not m:
                    raise bad(line, "expected: allow <CODE> <object> reason: <text>")
                if statement_lines[-1] < d.line:
                    raise bad(line, "an allow line must stand above a statement of the same batch")
                attached.append(Allow(m["code"], m["object"], m["reason"], line))
            elif d.name in ("deploy-module", "unbind"):
                if not _TWO_PART.fullmatch(d.args):
                    raise bad(line, f"expected: {d.name} [schema].[name]")
                if not above_statements:
                    # the batch is sent as one piece, so the directive cannot act in the middle of it
                    raise bad(line, f"'{d.name}' must be above the first statement of its batch")
                attached.append(
                    DeployModule(d.args, line) if d.name == "deploy-module" else Unbind(d.args, line)
                )
            else:
                raise bad(line, f"unknown directive '{d.name}'")
        batches.append(
            MigrationBatch(kind, batch.text, batch.first_line, tuple(attached), raw_object, raw_reason)
        )

    for name in ("migration", "mode"):
        if name not in header:
            raise bad(1, f"'{lex.DIRECTIVE_PREFIX}{name}' is missing above the first statement")
    stem, stem_line = header["migration"]
    if stem != file_name.removesuffix(".sql"):
        raise bad(
            stem_line, f"'migration' must be {file_name.removesuffix('.sql')}, the file name without .sql"
        )
    mode_text, mode_line = header["mode"]
    mode = _MODE.fullmatch(mode_text)
    minutes = int(mode["minutes"]) if mode and mode["minutes"] else None
    if not mode or minutes == 0:
        raise bad(mode_line, "expected: mode tx|nontx [expected-minutes: N], N at least 1")
    if mode["mode"] == "nontx" and len(batches) != 1:
        raise bad(batches[1].first_line, "a nontx migration is exactly one batch")
    return Migration(file=file_name, mode=mode["mode"], expected_minutes=minutes, batches=tuple(batches))


# ------------------------------------------------------------------ tombstones
@dataclass(frozen=True)
class Tombstone:
    object_key: str
    reason: str


def _drop_lines(text: str) -> list[int]:
    """The 1-based line of each [[drop]] header of a tombstone file that is valid TOML, in file
    order. A header inside a multi-line string or a comment is not one."""
    out: list[int] = []
    fence = None  # the quotes of the multi-line string that is open
    for n, line in enumerate(re.split(r"\r\n|\r|\n", text.removeprefix("\ufeff")), start=1):
        if fence is None:
            if _DROP_HEADER.fullmatch(line.strip()):
                out.append(n)
                continue
            if line.lstrip().startswith("#"):
                continue
        for m in _TOML_FENCE.finditer(line):
            if fence is None:
                fence = m[0]
            elif fence == m[0]:
                fence = None
    return out


def parse_tombstones(text: str) -> list[Tombstone]:
    """Read schema/_tombstones.toml. Raises ToolError REFUSED TOMBSTONE_INVALID, with the line of
    the [[drop]] table that is wrong (detail 'line') when the problem belongs to one table."""

    def bad(problem: str, drop: int = 0, line: int = 0) -> ToolError:
        # drop: the 1-based number of the [[drop]] table of the problem
        if drop and drop <= len(headers):
            line = headers[drop - 1]
        where = f"{TOMBSTONES_PATH} line {line}" if line else TOMBSTONES_PATH
        return refused("TOMBSTONE_INVALID", f"{where}: {problem}", **({"line": line} if line else {}))

    headers: list[int] = []
    try:
        # one leading BOM is no content, as in a .sql file and in migrations.sum; tomllib refuses it
        doc = tomllib.loads(text.removeprefix("\ufeff"))
    except tomllib.TOMLDecodeError as e:
        m = _TOML_LINE.search(str(e))
        raise bad(f"not valid TOML: {e}", line=int(m[1]) if m else 0) from None
    headers = _drop_lines(text)
    drops = doc.pop("drop", [])
    if doc or not isinstance(drops, list):
        raise bad("the file holds only [[drop]] tables")
    out: list[Tombstone] = []
    for n, drop in enumerate(drops, start=1):
        if not isinstance(drop, dict) or set(drop) != {"object", "reason"}:
            raise bad(f"drop {n}: needs the keys 'object' and 'reason' and no other key", n)
        key, reason = drop["object"], drop["reason"]
        if not isinstance(reason, str) or not reason.strip():
            raise bad(f"drop {n}: 'reason' must be text that is not empty", n)
        try:
            kind = names.parse_object_key(key)[0] if isinstance(key, str) else None
        except ValueError:
            kind = None
        if kind not in names.MODULE_KINDS:
            raise bad(
                f"drop {n}: 'object' must be a module key: KIND:[schema].[name] with the kind PROCEDURE, "
                "FUNCTION, VIEW or TRIGGER, for example PROCEDURE:[sales].[usp_x]",
                n,
            )
        if any(t.object_key == key for t in out):
            raise bad(f"drop {n}: {key} is listed more than once", n)
        out.append(Tombstone(key, reason))
    return out
