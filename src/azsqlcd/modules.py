"""Module files (views, procedures, functions, DML triggers).

Holds the frozen normal form and checksum, the reader of a module file, the two header rewrites
(export and unbind) and the dependency order.

A module body is never parsed. Every rewrite is a token-level edit in the header; the text from
the start of the body to the end is never changed.
"""

from __future__ import annotations

import hashlib
import heapq
import re
from collections import deque
from collections.abc import Collection, Iterable, Iterator
from dataclasses import dataclass

from azsqlcd import lex, names
from azsqlcd.errors import ToolError, refused
from azsqlcd.lex import LexError, Tok

_IDENT = ("word", "bident", "qident")
# Tie order of a deploy (design (d) step 2). A drop uses the reverse.
_KIND_RANK = {"FUNCTION": 0, "VIEW": 1, "PROCEDURE": 2, "TRIGGER": 3}
_LINE_BREAK = re.compile(rb"\r\n|\r|\n")

type _Name = tuple[str, str]  # (schema, name), decoded
type _Index = dict[_Name, set[str]]  # case-folded (schema, name) -> object keys


# ------------------------------------------------------------------ normal form and checksum
def normal_form(data: bytes) -> bytes:
    """The frozen normal form of a module text. This definition never changes.

    UTF-8 (invalid bytes are refused); one leading BOM removed; CRLF and lone CR become LF; space,
    tab, vertical tab and form feed removed at the end of each line; trailing empty lines removed.
    The result has no line break at the end. Leading and inner empty lines are content.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise refused("MODULE_INVALID", f"not valid UTF-8 at byte {e.start}", offset=e.start) from e
    text = text.removeprefix(lex.BOM).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip(" \t\v\f") for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines).encode("utf-8")


def checksum(data: bytes) -> str:
    """sha256 (lower-case hex) of the normal form. Compared with azsqlcd.object.source_sha256."""
    return hashlib.sha256(normal_form(data)).hexdigest()


# ------------------------------------------------------------------ header analysis
@dataclass(frozen=True)
class _Head:
    header: lex.ModuleHeader
    toks: list[Tok]  # significant tokens of the whole text
    first: int  # index of the first token after the name
    end: int  # index of the token that starts the body; the header is toks[:end]


def _is_op(t: Tok, text: str) -> bool:
    return t.kind == "op" and t.text == text


def _word(t: Tok) -> str:
    """Upper-case text of an unquoted word, else ''. A quoted identifier is never a keyword."""
    return t.text.upper() if t.kind == "word" else ""


def _depth0(toks: list[Tok], start: int, end: int) -> Iterator[int]:
    """Indexes in [start, end) of the tokens that are outside every parenthesis."""
    depth = 0
    for i in range(start, end):
        if _is_op(toks[i], "("):
            depth += 1
        elif _is_op(toks[i], ")"):
            depth -= 1
        elif depth == 0:
            yield i


def _head(text: str) -> _Head:
    """Locate the header of a module. Raises LexError when it cannot be located.

    The body starts at the first AS outside parentheses after the name. Two AS words belong to
    the header and are passed over: the AS of EXECUTE AS, and the AS between a parameter name and
    its type. A function may leave AS out; its body then starts at BEGIN or RETURN.
    """
    header = lex.module_header(text)
    toks = lex.significant(lex.tokens(text))
    first = next((i for i, t in enumerate(toks) if t.pos >= header.name_span[1]), len(toks))
    for i in _depth0(toks, first, len(toks)):
        word, before = _word(toks[i]), toks[i - 1]
        if word == "AS" and before.kind != "var" and _word(before) not in ("EXECUTE", "EXEC"):
            return _Head(header, toks, first, i)
        if header.kind == "FUNCTION" and word in ("BEGIN", "RETURN"):
            return _Head(header, toks, first, i)
    raise LexError("the start of the module body (AS) was not found", toks[-1].line)


def _schemabinding(head: _Head) -> int | None:
    """Index of the SCHEMABINDING option in a WITH option list of the header, or None."""
    in_with = False
    for i in _depth0(head.toks, head.first, head.end):
        word, before = _word(head.toks[i]), head.toks[i - 1]
        if word == "WITH":
            in_with = True
        elif word == "SCHEMABINDING" and in_with and (_word(before) == "WITH" or _is_op(before, ",")):
            return i
    return None


def execute_as(text: str) -> str | None:
    """The EXECUTE AS option of the header: CALLER, SELF, OWNER or the user literal as written.

    None when the header has no EXECUTE AS. An EXECUTE AS statement in the body is not reported.
    """
    try:
        head = _head(text)
    except LexError as e:
        raise refused("MODULE_INVALID", str(e), line=e.line) from e
    toks = head.toks
    for i in _depth0(toks, head.first, head.end - 2):
        if _word(toks[i]) in ("EXECUTE", "EXEC") and _word(toks[i + 1]) == "AS":
            return _word(toks[i + 2]) or toks[i + 2].text
    return None


# ------------------------------------------------------------------ module file
@dataclass(frozen=True)
class ModuleFile:
    key: str  # 'VIEW:[sales].[vw_x]'
    kind: str  # VIEW | FUNCTION | PROCEDURE | TRIGGER
    schema: str  # decoded
    name: str  # decoded
    path: str  # 'schema/views/sales.vw_x.sql'
    text: str  # the file text, BOM removed, line ends as in the file: the batch that is sent
    checksum: str  # sha256 hex of the normal form
    schema_bound: bool  # SCHEMABINDING is in a WITH option list of the header
    after: tuple[tuple[str, str], ...]  # (schema, name) of each `-- azsqlcd:after [s].[n]` line
    ignore_dep: tuple[tuple[str, str], ...]  # (schema, name) of each `-- azsqlcd:ignore-dep [s].[n]` line


def _two_part(directive: lex.Directive) -> _Name:
    try:
        toks = lex.significant(lex.tokenize(directive.args))
    except LexError:
        toks = []  # refused below, with the line of the directive in the file
    if len(toks) == 3 and toks[0].kind in _IDENT and _is_op(toks[1], ".") and toks[2].kind in _IDENT:
        return toks[0].value, toks[2].value
    raise LexError(f"-- azsqlcd:{directive.name} needs one two-part name [schema].[name]", directive.line)


def _first_go_line(text: str) -> int:
    """Line of the first GO that starts a line. For the message only; lex decides what a batch is."""
    line_start = True
    for t in lex.tokens(text):
        if t.kind == "nl":
            line_start = True
        elif t.kind != "ws":
            if line_start and _word(t) == "GO":
                return t.line
            line_start = False
    return 1


def read_module(path: str, data: bytes) -> ModuleFile:
    """Read one module file. path is 'schema/<kind dir>/<schema>.<name>.sql'.

    Refused (MODULE_INVALID, with path and line): not UTF-8; text that cannot be lexed; not exactly
    one batch (any GO separator); not CREATE OR ALTER; a kind other than the kind of the directory;
    a name that is not two-part or is not the file stem; a header with no body start; an `after`
    or `ignore-dep` directive that is not one two-part name.
    """
    try:
        try:
            text = data.decode("utf-8").removeprefix(lex.BOM)
        except UnicodeDecodeError as e:
            line = 1 + len(_LINE_BREAK.findall(data[: e.start]))
            raise LexError("not valid UTF-8", line) from e
        try:
            kind, stem = names.key_for_path(path)
        except ValueError as e:
            raise LexError(str(e)) from e
        if kind not in names.MODULE_KINDS:
            raise LexError(f"a {kind} file is not a module file")
        batches = lex.split_batches(text)
        if not batches:
            raise LexError("empty module file", 1)
        if len(batches) > 1 or batches[0].text != text.strip("\r\n"):
            raise LexError("GO separator: a module file is exactly one batch", _first_go_line(text))
        head = _head(text)
        header = head.header
        line = head.toks[0].line
        if header.verb != "CREATE OR ALTER":
            raise LexError(f"a module file starts with CREATE OR ALTER, found {header.verb}", line)
        if header.kind != kind:
            raise LexError(f"{header.kind} in a file of the {names.KIND_DIRS[kind]} directory", line)
        if header.schema is None:
            raise LexError(f"one-part name {names.quote(header.name)}: a two-part name is required", line)
        if f"{header.schema}.{header.name}" != stem:
            found = names.qualified(header.schema, header.name)
            raise LexError(f"the name {found} is not the file name {stem!r}", line)
        found_directives = lex.directives(text)
        return ModuleFile(
            key=names.object_key(kind, header.schema, header.name),
            kind=kind,
            schema=header.schema,
            name=header.name,
            path=path,
            text=text,
            checksum=checksum(data),
            schema_bound=_schemabinding(head) is not None,
            after=tuple(_two_part(d) for d in found_directives if d.name == "after"),
            ignore_dep=tuple(_two_part(d) for d in found_directives if d.name == "ignore-dep"),
        )
    except LexError as e:
        raise refused("MODULE_INVALID", f"{path}: {e}", path=path, line=e.line) from e


# ------------------------------------------------------------------ header rewrites
def _splice(text: str, edits: list[tuple[tuple[int, int], str]]) -> str:
    for (start, end), new in sorted(edits, reverse=True):
        text = text[:start] + new + text[end:]
    return text


def _unreadable(reason_code: str, catalog: str, e: LexError) -> ToolError:
    # The lexer message can quote the text. A live definition is never printed (A25): line only.
    message = f"definition of {catalog}: the module header cannot be read (line {e.line})"
    return refused(reason_code, message, line=e.line)


def _verb_edit(text: str, header: lex.ModuleHeader, verb: str) -> tuple[tuple[int, int], str]:
    """The edit that writes `verb` and one space before the kind word (VIEW, PROC, FUNCTION, ...).

    The white space between the verb and the kind word is not content. The engine stores
    "CREATE   VIEW" for CREATE OR ALTER VIEW: it deletes the words OR and ALTER and keeps the white
    space around them (live spike L7). Kept as it is, the export of a deployed module would not be
    its file. A comment between the verb and the kind word stays, with one space before it; a line
    comment keeps its line break. The kind word and all text after it are not touched.
    """
    start, end = header.verb_span
    new, gap, kind_at = verb, " ", end
    for tok in lex.tokens(text):
        if tok.pos < end:
            continue
        kind_at = tok.pos
        if tok.kind not in lex.TRIVIA:
            break
        if tok.kind == "comment":
            new += gap + tok.text
            gap = "" if tok.text.startswith("--") else " "
        elif tok.kind == "nl" and not gap:
            gap = tok.text  # the line break that ends a line comment
    return (start, kind_at), new + gap


def rewrite_for_export(definition: str, catalog_schema: str, catalog_name: str) -> tuple[str, list[str]]:
    """Make a catalog definition a module file text. Returns (text, edits made).

    Two edits, both in the header: the verb becomes CREATE OR ALTER, with one space between it and
    the kind word (_verb_edit); the name becomes the bracket-quoted catalog name when the decoded
    name differs (sys.sql_modules keeps the old name after sp_rename, and a one-part name has no
    schema). Nothing after the name is changed.

    So the export of a module that was deployed from a file is that file, byte for byte, when the
    file writes its verb as `CREATE OR ALTER <kind>` (A15). A file with another verb text (lower
    case, a comment or a line break inside the verb) gets the verb as this function writes it.
    """
    catalog = names.qualified(catalog_schema, catalog_name)
    try:
        header = lex.module_header(definition)
    except LexError as e:
        raise _unreadable("MODULE_INVALID", catalog, e) from e
    edits: list[tuple[tuple[int, int], str]] = []
    made: list[str] = []
    if header.verb != "CREATE OR ALTER":
        edits.append(_verb_edit(definition, header, "CREATE OR ALTER"))
        made.append(f"verb: {header.verb} -> CREATE OR ALTER")
    if (header.schema, header.name) != (catalog_schema, catalog_name):
        edits.append((header.name_span, catalog))
        made.append(f"name: {definition[header.name_span[0] : header.name_span[1]]} -> {catalog}")
    return _splice(definition, edits), made


def stored_text(sent: str) -> str:
    """The text that sys.sql_modules holds after the engine ran the module batch `sent`.

    Measured on Azure SQL Database (live spike L7): the engine keeps the batch byte for byte, with
    one edit in the verb. After CREATE OR ALTER the words OR and ALTER are deleted; the white space
    and the comments around them stay ("CREATE   VIEW"). After ALTER the word is replaced by
    CREATE in upper case. After CREATE nothing changes. Raises LexError when the text has no module
    header.
    """
    header = lex.module_header(sent)
    if header.verb == "CREATE":
        return sent
    if header.verb == "ALTER":
        return _splice(sent, [(header.verb_span, "CREATE")])
    words = lex.significant(lex.tokenize(sent))[1:3]  # OR, ALTER
    return _splice(sent, [((word.pos, word.pos + len(word.text)), "") for word in words])


def stored_checksum(sent: str) -> str:
    """checksum() of the definition that the catalog holds after the batch `sent` (read-back)."""
    return checksum(stored_text(sent).encode("utf-8"))


def rewrite_for_unbind(definition: str, catalog_schema: str, catalog_name: str) -> str:
    """The ALTER statement that removes the schema binding of a live module.

    The verb becomes ALTER, with one space between it and the kind word (_verb_edit); the name
    becomes the bracket-quoted catalog name (A15); SCHEMABINDING leaves the header WITH list, with
    its comma, or with WITH when it was the only option. The body is not changed. Refused
    (UNBIND_HEADER) when the header or the option cannot be located.
    """
    catalog = names.qualified(catalog_schema, catalog_name)
    try:
        head = _head(definition)
    except LexError as e:
        raise _unreadable("UNBIND_HEADER", catalog, e) from e
    at = _schemabinding(head)
    if at is None:
        raise refused("UNBIND_HEADER", f"definition of {catalog}: no SCHEMABINDING in the header WITH list")
    before, option, after = head.toks[at - 1 : at + 2]  # after exists: the body start follows
    if _is_op(after, ","):
        start, end, tidy = option.pos, after.pos + 1, True
    elif _is_op(before, ","):
        start, end, tidy = before.pos, option.pos + len(option.text), False
    else:  # before is WITH and the list is now empty
        start, end, tidy = before.pos, option.pos + len(option.text), True
    while tidy and definition[end] in " \t":
        end += 1
    header = head.header
    verb = _verb_edit(definition, header, "ALTER")
    return _splice(definition, [verb, (header.name_span, catalog), ((start, end), "")])


# ------------------------------------------------------------------ dependency order
def _fold(name: _Name) -> _Name:
    return name[0].casefold(), name[1].casefold()


def _index(keys: Iterable[str]) -> _Index:
    index: _Index = {}
    for key in keys:
        _, schema, name = names.parse_object_key(key)
        if schema is not None:  # a SCHEMA key names no object
            index.setdefault(_fold((schema, name)), set()).add(key)
    return index


def _scan(module: ModuleFile, index: _Index) -> set[str]:
    toks = lex.significant(lex.tokens(module.text))
    found: set[str] = set()
    i = 0
    while i < len(toks):
        if toks[i].kind not in _IDENT:
            i += 1
            continue
        member = i > 0 and _is_op(toks[i - 1], ".")  # @x.method, never an object name
        parts = [toks[i].value]
        i += 1
        while i + 1 < len(toks) and _is_op(toks[i], ".") and toks[i + 1].kind in _IDENT:
            parts.append(toks[i + 1].value)
            i += 2
        if not member:
            # [s].[n] and [s].[n].[column]; a one-part name is looked up in dbo only (A17)
            name = (parts[0], parts[1]) if len(parts) > 1 else ("dbo", parts[0])
            found |= index.get(_fold(name), set())
    found.discard(module.key)
    return found


def scan_references(module: ModuleFile, known_keys: Collection[str]) -> set[str]:
    """Keys of known_keys that the module text names. A token scan, not a parse.

    Strings and comments are not scanned. A two-part name ([s].[n], s.n, "s".n) matches schema
    and name; a one-part name matches only an object of schema dbo. Matching is case-insensitive.
    The module itself is never in the result.
    """
    return _scan(module, _index(known_keys))


def build_edges(modules: Iterable[ModuleFile], known_keys: Collection[str]) -> dict[str, set[str]]:
    """key -> keys of the modules it needs, among the given modules only.

    Scanned references, plus `-- azsqlcd:after`, minus `-- azsqlcd:ignore-dep`. A directive that
    names an object outside the given modules changes nothing.
    """
    mods = list(modules)
    mod_keys = {m.key for m in mods}
    index = _index(mod_keys | set(known_keys))

    def resolve(listed: tuple[_Name, ...]) -> set[str]:
        return {key for name in listed for key in index.get(_fold(name), ())}

    edges: dict[str, set[str]] = {}
    for m in mods:
        edges[m.key] = (((_scan(m, index) | resolve(m.after)) - resolve(m.ignore_dep)) & mod_keys) - {m.key}
    return edges


def _cycles(needs: dict[str, set[str]]) -> list[set[str]]:
    """Strongly connected components with more than one member (Tarjan, iterative)."""
    number: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    found: list[set[str]] = []
    for root in needs:
        if root in number:
            continue
        work = [(root, iter(needs[root]))]
        number[root] = low[root] = len(number)
        stack.append(root)
        on_stack.add(root)
        while work:
            node, rest = work[-1]
            for nxt in rest:
                if nxt not in number:
                    number[nxt] = low[nxt] = len(number)
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(needs[nxt])))
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], number[nxt])
            else:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == number[node]:
                    component: set[str] = set()
                    while node not in component:
                        component.add(stack.pop())
                    on_stack -= component
                    if len(component) > 1:
                        found.append(component)
    return found


def _cycle_path(start: str, component: set[str], needs: dict[str, set[str]]) -> list[str]:
    """The shortest path start -> ... -> start inside one component (A -> B: A needs B)."""
    came_from: dict[str, str] = {}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for nxt in sorted(needs[node] & component):
            if nxt == start:
                path = [node]
                while path[-1] != start:
                    path.append(came_from[path[-1]])
                return [*reversed(path), start]
            if nxt not in came_from:
                came_from[nxt] = node
                queue.append(nxt)
    raise AssertionError(f"{start} is not on a cycle")


def _order(kinds: dict[str, str], edges: dict[str, set[str]]) -> tuple[list[str], list[str]]:
    needs = {key: (edges.get(key, set()) & kinds.keys()) - {key} for key in kinds}
    warnings: list[str] = []
    refusals: list[list[str]] = []
    for component in sorted(_cycles(needs), key=min):
        strict = sorted(key for key in component if kinds[key] in ("VIEW", "FUNCTION"))
        if strict:
            refusals.append(_cycle_path(strict[0], component, needs))
            continue
        # Procedures and triggers resolve names when they run, so any order creates them (A17).
        warnings.append(
            "dependency cycle of procedures and triggers, broken in key order: "
            + ", ".join(sorted(component))
        )
        for key in component:
            needs[key] -= component
    if refusals:
        shown = "; ".join(" -> ".join(path) for path in refusals)
        raise refused(
            "ORD004",
            f"dependency cycle through a view or a function: {shown}. A -> B means A needs B. "
            "Change a module, or remove one edge with -- azsqlcd:ignore-dep [schema].[name]",
            cycles=refusals,
        )
    needed_by: dict[str, list[str]] = {key: [] for key in needs}
    for key, deps in needs.items():
        for dep in deps:
            needed_by[dep].append(key)
    waiting = {key: len(deps) for key, deps in needs.items()}
    ready = [(_KIND_RANK[kinds[key]], key) for key, count in waiting.items() if count == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        _, key = heapq.heappop(ready)
        order.append(key)
        for dependant in needed_by[key]:
            waiting[dependant] -= 1
            if waiting[dependant] == 0:
                heapq.heappush(ready, (_KIND_RANK[kinds[dependant]], dependant))
    return order, warnings


def deploy_order(modules: Iterable[ModuleFile], edges: dict[str, set[str]]) -> tuple[list[str], list[str]]:
    """(keys in deploy order, warnings). Topological: a module comes after the modules it needs.

    Ties: functions, views, procedures, triggers, then key. A cycle of procedures and triggers
    only is broken in key order with a warning. A cycle with a view or a function is refused
    (ORD004). Edges to keys outside the given modules are ignored.
    """
    return _order({m.key: m.kind for m in modules}, edges)


def drop_order(keys: Iterable[str], edges: dict[str, set[str]]) -> list[str]:
    """Keys in drop order: a dependant before the module it needs. The reverse of the deploy order."""
    order, _ = _order({key: names.parse_object_key(key)[0] for key in keys}, edges)
    return order[::-1]
