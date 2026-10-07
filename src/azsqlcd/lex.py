"""T-SQL tokenizer, GO batch splitter, module-header detector and directive reader.

Stdlib only. Fails loud (LexError) on unterminated strings, comments and quoted identifiers.
QUOTED_IDENTIFIER ON is assumed (the Azure SQL Database default): "x" is an identifier.

The lexer also fails loud where the engine could split the text in another way than these rules,
because the lint rules read these tokens and the engine runs the text:
    NUL anywhere (a driver can end the batch text there);
    a control character, U+2028 or U+2029 inside a comment (it could end the comment);
    a number with a word directly after it (1eDELETE is the number 1e and the keyword DELETE);
    a backslash directly before a line break outside a string (a line continuation);
    a digit that is not 0-9 at the start of a token;
    an unquoted word with a character that is not a letter or a digit, or that a case or width
    mapping turns into an ASCII character (a keyword could hide in it). In brackets it is a name.

Public API (other modules depend on these names and shapes; do not change them):
    Tok, LexError, TRIVIA, tokenize, tokens, significant,
    Batch, split_batches,
    ModuleHeader, module_header,
    Directive, directives, DIRECTIVE_PREFIX, CURRENCY
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_left
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import NamedTuple

BOM = "﻿"
DIRECTIVE_PREFIX = "-- azsqlcd:"


class LexError(ValueError):
    """Source text that cannot be tokenized or is not the expected shape. Carries the line."""

    def __init__(self, message: str, line: int = 0) -> None:
        super().__init__(f"line {line}: {message}" if line else message)
        self.line = line


@dataclass(frozen=True)
class Tok:
    kind: str  # ws nl comment string nstring qident bident word var number op
    text: str  # exact source text
    value: str  # decoded value (unescaped string or identifier), else text
    pos: int  # offset in the source string
    line: int  # 1-based line of the first character


TRIVIA = frozenset({"ws", "nl", "comment"})

_WS = " \t\f\v "
_WORD_START = re.compile(r"[^\W\d]|[_#]", re.UNICODE)
_WORD = re.compile(r"[\w@$#]+", re.UNICODE)
_DIGITS = "0123456789"
_NUMBER = re.compile(r"0[xX][0-9A-Fa-f]*|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
# What cannot stand in a comment: C0 without TAB, LF and CR; DEL; C1 (U+0085 is a line break to
# some readers); the Unicode line and paragraph separators.
_COMMENT_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u2028\u2029]")
_ASCII = re.compile(r"[\x00-\x7f]")
# ß and ẞ fold to 'ss' and to nothing else; no keyword of the lint rules can come from them
_FOLD_EXEMPT = "ßẞ"
_OPS2 = ("<=", ">=", "<>", "!=", "!<", "!>", "::", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "||")
_OPS1 = "()[],.;=<>+-*/%&|^~!:$?{}\\"
# The currency symbols that T-SQL reads as the start of a money literal ($5, \u00a35). Each one is
# an 'op' token of one character; the number after it is a token of its own.
CURRENCY = frozenset(
    "$\u00a3\u00a4\u00a5\u09f2\u09f3\u0e3f\u17db\ufdfc\ufe69\uff04\uffe0\uffe1\uffe5\uffe6"
    + "".join(map(chr, range(0x20A0, 0x20B2)))
)


# The texts whose tokens are kept (tokens()). Every rule of a file asks for the tokens of the same
# text, one rule after the other, so a small number serves. Not more: a token is about 200 bytes,
# and the tokens of every file of a large repository are about 1 GB.
_TOKEN_CACHE = 16
_LINE_BREAK = re.compile(r"\r\n|\r|\n")


def _line_at(src: str, pos: int) -> int:
    """1-based line of the character at pos; CRLF counts once."""
    before = src[:pos]
    return 1 + before.count("\n") + len(re.findall(r"\r(?!\n)", before))


def _check_comment(src: str, start: int, end: int) -> None:
    m = _COMMENT_CONTROL.search(src, start, end)
    if m:
        raise LexError(
            f"control character U+{ord(m[0]):04X} in a comment: it could end the comment for the "
            "engine and not for this tool; remove it",
            _line_at(src, m.start()),
        )


def _check_word(text: str, line: int) -> None:
    """An unquoted word is letters, digits, _ @ $ #. A character that a case or width mapping turns
    into an ASCII character (fullwidth ＤＥＬＥＴＥ, ſ, ı, the Kelvin sign) could be a keyword for the
    engine and a name for this tool, or the reverse."""
    for c in text:
        if c.isascii() or c in _FOLD_EXEMPT:
            continue
        category = unicodedata.category(c)
        mapped = unicodedata.normalize("NFKC", c) + c.upper() + c.lower() + c.casefold()
        if not (category[0] == "L" or category == "Nd") or _ASCII.search(mapped):
            raise LexError(
                f"the character {c!r} (U+{ord(c):04X}) in an unquoted word: write the name in brackets", line
            )


def tokenize(src: str) -> list[Tok]:
    """Split source text into tokens. Trivia is kept, so text can be sliced byte-exact by pos.

    A new list for each call; the caller may change it. The text is scanned once: see tokens().
    """
    return list(tokens(src))


@lru_cache(maxsize=_TOKEN_CACHE)
def tokens(src: str) -> tuple[Tok, ...]:
    """The tokens of tokenize(src) as a tuple that is shared between callers: read it only.

    The tokens of the last few texts are kept, so the rules that read one file after each other
    scan its text once. A text that raises LexError is not kept.
    """
    return tuple(_scan(src))


def _scan(src: str) -> list[Tok]:
    """One pass over the text. Only tokens() calls it (the performance tests count the calls)."""
    toks: list[Tok] = []
    i, n, line = 0, len(src), 1
    nul = src.find("\x00")
    if nul >= 0:
        raise LexError("NUL character: a driver can end the batch text there", _line_at(src, nul))
    if src.startswith(BOM):
        i = 1
    while i < n:
        c = src[i]
        start = i
        if c == "\n" or c == "\r":
            i += 2 if (c == "\r" and i + 1 < n and src[i + 1] == "\n") else 1
            toks.append(Tok("nl", src[start:i], "\n", start, line))
            line += 1
            continue
        if c in _WS:
            while i < n and src[i] in _WS:
                i += 1
            toks.append(Tok("ws", src[start:i], " ", start, line))
            continue
        if c == "-" and src.startswith("--", i):
            while i < n and src[i] not in "\r\n":
                i += 1
            _check_comment(src, start, i)
            toks.append(Tok("comment", src[start:i], src[start:i], start, line))
            continue
        if c == "/" and src.startswith("/*", i):
            depth, i, line0 = 1, i + 2, line
            while i < n and depth:
                if src.startswith("/*", i):
                    depth += 1
                    i += 2
                elif src.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    if src[i] == "\n" or (src[i] == "\r" and not src.startswith("\r\n", i)):
                        line += 1
                    i += 1
            if depth:
                raise LexError("unterminated /* comment", line0)
            _check_comment(src, start, i)
            toks.append(Tok("comment", src[start:i], src[start:i], start, line0))
            continue
        if c == "'" or (c in "Nn" and i + 1 < n and src[i + 1] == "'"):
            kind = "string" if c == "'" else "nstring"
            i += 1 if c == "'" else 2
            line0 = line
            buf: list[str] = []
            while True:
                if i >= n:
                    raise LexError("unterminated string literal", line0)
                if src[i] == "'":
                    if i + 1 < n and src[i + 1] == "'":
                        buf.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                if src[i] == "\n" or (src[i] == "\r" and not src.startswith("\r\n", i)):
                    line += 1
                buf.append(src[i])
                i += 1
            toks.append(Tok(kind, src[start:i], "".join(buf), start, line0))
            continue
        if c == "[" or c == '"':
            close = "]" if c == "[" else '"'
            kind = "bident" if c == "[" else "qident"
            i += 1
            buf = []
            while True:
                if i >= n or src[i] in "\r\n":
                    raise LexError(f"unterminated {c}identifier{close}", line)
                if src[i] == close:
                    if i + 1 < n and src[i + 1] == close:
                        buf.append(close)
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(src[i])
                i += 1
            toks.append(Tok(kind, src[start:i], "".join(buf), start, line))
            continue
        if c == "@":
            m = _WORD.match(src, i)
            if m is None:  # never an AssertionError: the callers catch LexError only
                raise LexError(f"unexpected character {c!r}", line)
            i = m.end()
            toks.append(Tok("var", src[start:i], src[start:i], start, line))
            continue
        if c in _DIGITS or (c == "." and i + 1 < n and src[i + 1] in _DIGITS):
            m = _NUMBER.match(src, i)
            if m is None:
                raise LexError(f"unexpected character {c!r}", line)
            i = m.end()
            if i < n and _WORD_START.match(src[i]):
                # T-SQL ends a number where it can: 1eDELETE is the float 1e and the keyword DELETE,
                # 1a is 1 AS a. These rules would read one other token, so the text is refused.
                raise LexError(
                    f"the number {src[start:i]} has a word directly after it; write a space between them",
                    line,
                )
            toks.append(Tok("number", src[start:i], src[start:i], start, line))
            continue
        if _WORD_START.match(c):
            m = _WORD.match(src, i)
            if m is None:
                raise LexError(f"unexpected character {c!r}", line)
            i = m.end()
            _check_word(src[start:i], line)
            toks.append(Tok("word", src[start:i], src[start:i], start, line))
            continue
        two = src[i : i + 2]
        if two in _OPS2:
            i += 2
            toks.append(Tok("op", two, two, start, line))
            continue
        if c in _OPS1 or c in CURRENCY:
            if c == "\\" and i + 1 < n and src[i + 1] in "\r\n":
                # the engine removes both characters and joins the lines: DEL\<line break>ETE
                raise LexError("a backslash directly before a line break is a line continuation", line)
            i += 1
            toks.append(Tok("op", c, c, start, line))
            continue
        raise LexError(f"unexpected character {c!r}", line)
    return toks


def significant(toks: Iterable[Tok]) -> list[Tok]:
    return [t for t in toks if t.kind not in TRIVIA]


# ------------------------------------------------------------------ GO batch splitter
class Batch(NamedTuple):
    text: str  # exact batch text, surrounding line breaks removed
    repeat: int  # count after GO; 1 when absent. The tool refuses anything other than 1.
    first_line: int  # 1-based line in the file where the batch text starts


def split_batches(src: str) -> list[Batch]:
    """Split a script into batches on GO.

    GO is a separator only when it is the first non-trivia token on its line and the rest of
    the line holds nothing but an optional integer count and a line comment. Strings, comments
    and quoted identifiers are consumed by the tokenizer first, so a GO inside any of them is
    never a candidate. Empty batches are dropped.
    """
    toks = tokens(src)
    # start of each line break (CRLF is one): the first line of a batch is one bisect, not one more
    # read of all text before it
    breaks = [m.start() for m in _LINE_BREAK.finditer(src)]
    batches: list[Batch] = []
    cur_start = 1 if src.startswith(BOM) else 0
    i, n = 0, len(toks)
    line_start = True
    while i < n:
        t = toks[i]
        if t.kind == "nl":
            line_start = True
            i += 1
            continue
        if t.kind == "ws":
            i += 1
            continue
        if line_start and t.kind == "word" and t.text.upper() == "GO":
            j, count, ok, seen_count = i + 1, 1, True, False
            while j < n and toks[j].kind != "nl":
                k = toks[j]
                if k.kind == "ws" or (k.kind == "comment" and k.text.startswith("--")):
                    pass
                elif k.kind == "number" and k.text.isascii() and k.text.isdigit() and not seen_count:
                    count, seen_count = int(k.text), True
                else:
                    ok = False
                    break
                j += 1
            if ok:
                _append_batch(batches, src, breaks, cur_start, t.pos, count)
                cur_start = toks[j].pos + len(toks[j].text) if j < n else len(src)
                i = j
                continue
        line_start = False
        i += 1
    _append_batch(batches, src, breaks, cur_start, len(src), 1)
    return batches


def _append_batch(
    batches: list[Batch], src: str, breaks: list[int], start: int, end: int, repeat: int
) -> None:
    raw = src[start:end]
    if not raw.strip():
        return
    lead = len(raw) - len(raw.lstrip("\r\n"))
    text = raw.strip("\r\n")
    # count line breaks before the first kept character; \r\n counts once (the rule of _line_at)
    first_line = 1 + bisect_left(breaks, start + lead)
    batches.append(Batch(text, repeat, first_line))


# ------------------------------------------------------------------ module header detector
_KINDS = {
    "PROCEDURE": "PROCEDURE",
    "PROC": "PROCEDURE",
    "FUNCTION": "FUNCTION",
    "VIEW": "VIEW",
    "TRIGGER": "TRIGGER",
}


@dataclass(frozen=True)
class ModuleHeader:
    verb: str  # CREATE | CREATE OR ALTER | ALTER
    kind: str  # PROCEDURE | FUNCTION | VIEW | TRIGGER
    schema: str | None  # decoded; None when the name has one part
    name: str  # decoded
    verb_span: tuple[int, int]  # source offsets of the verb tokens (first char, one past last)
    name_span: tuple[int, int]  # source offsets of the whole (one- or two-part) name


def module_header(src: str) -> ModuleHeader:
    """Read the head of a module batch: CREATE [OR ALTER] | ALTER, kind, one- or two-part name.

    The body is not parsed. Raises LexError for anything else, including three-part names.
    """
    toks = significant(tokens(src))
    if not toks:
        raise LexError("empty module batch")
    p = 0

    def word(k: int) -> str:
        if p + k < len(toks) and toks[p + k].kind == "word":
            return toks[p + k].text.upper()
        return ""

    if word(0) == "CREATE":
        verb, last = "CREATE", toks[0]
        p = 1
        if word(0) == "OR" and word(1) == "ALTER":
            verb, last = "CREATE OR ALTER", toks[p + 1]
            p += 2
    elif word(0) == "ALTER":
        verb, last = "ALTER", toks[0]
        p = 1
    else:
        raise LexError(
            f"expected CREATE or ALTER at the start of a module, found {toks[0].text!r}", toks[0].line
        )
    verb_span = (toks[0].pos, last.pos + len(last.text))
    kind = _KINDS.get(word(0))
    if not kind:
        found = toks[p].text if p < len(toks) else "end of text"
        raise LexError(f"not a module header: {found!r}", toks[min(p, len(toks) - 1)].line)
    p += 1
    parts: list[Tok] = []
    while True:
        if p >= len(toks) or toks[p].kind not in ("word", "bident", "qident"):
            raise LexError("expected an identifier in the module name", toks[p - 1].line)
        parts.append(toks[p])
        p += 1
        if p < len(toks) and toks[p].kind == "op" and toks[p].text == ".":
            p += 1
            continue
        break
    if len(parts) > 2:
        raise LexError(f"{len(parts)}-part module name is not valid in Azure SQL Database", parts[0].line)
    if any(not part.value for part in parts):
        raise LexError("empty identifier in the module name", parts[0].line)
    return ModuleHeader(
        verb=verb,
        kind=kind,
        schema=parts[0].value if len(parts) == 2 else None,
        name=parts[-1].value,
        verb_span=verb_span,
        name_span=(parts[0].pos, parts[-1].pos + len(parts[-1].text)),
    )


# ------------------------------------------------------------------ directives
@dataclass(frozen=True)
class Directive:
    name: str  # text between "-- azsqlcd:" and the first space, for example "allow"
    args: str  # rest of the line, stripped
    line: int  # 1-based line in the text that was passed in


def directives(src: str) -> list[Directive]:
    """Return every `-- azsqlcd:<name> <args>` line comment that starts in column 0.

    A directive inside a string or a block comment is not a directive; the tokenizer decides.
    """
    out: list[Directive] = []
    for t in tokens(src):
        if t.kind != "comment" or not t.text.startswith(DIRECTIVE_PREFIX):
            continue
        at_col0 = t.pos == 0 or src[t.pos - 1] in "\r\n" or (t.pos == 1 and src.startswith(BOM))
        if not at_col0:
            continue
        body = t.text[len(DIRECTIVE_PREFIX) :]
        name, _, args = body.partition(" ")
        out.append(Directive(name.strip(), args.strip(), t.line))
    return out
