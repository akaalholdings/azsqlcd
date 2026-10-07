"""Identifier quoting, object keys and the file layout of a database repository.

One place for these rules, because the parser, the module reader, the catalog reader and the
runner must all agree on them.
"""

from __future__ import annotations

import re

MODULE_KINDS = ("VIEW", "FUNCTION", "PROCEDURE", "TRIGGER")
TABLE_CLASS_KINDS = ("SCHEMA", "TYPE", "SEQUENCE", "TABLE", "SYNONYM")
KINDS = TABLE_CLASS_KINDS + MODULE_KINDS

# kind -> directory under schema/
KIND_DIRS = {
    "SCHEMA": "schemas",
    "TYPE": "types",
    "SEQUENCE": "sequences",
    "TABLE": "tables",
    "SYNONYM": "synonyms",
    "VIEW": "views",
    "FUNCTION": "functions",
    "PROCEDURE": "procedures",
    "TRIGGER": "triggers",
}
DIR_KINDS = {v: k for k, v in KIND_DIRS.items()}

_KEY = re.compile(r"^([A-Z]+):((?:\[(?:[^\]]|\]\])+\])(?:\.\[(?:[^\]]|\]\])+\])?)$")
_PART = re.compile(r"\[((?:[^\]]|\]\])+)\]")
# characters that cannot appear in a file name on Windows or in a git path we want to keep simple
_UNSAFE_IN_PATH = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Windows reads the part of a file name before the first dot as a device when it is one of these
# names, in any letter case, with any extension, also with spaces after it. Git for Windows refuses
# to check such a path out (core.protectNTFS), so one committed file blocks every Windows clone.
_WINDOWS_DEVICE = re.compile(
    r"(con|prn|aux|nul|conin\$|conout\$|(?:com|lpt)[0-9\u00b9\u00b2\u00b3]) *(?:[.:]|$)", re.IGNORECASE
)
# NTFS, ext4 and APFS hold a file name of 255 units at most; bytes of UTF-8 is the strictest count
MAX_FILE_NAME_BYTES = 255
_LINE_CONTINUATION = re.compile(r"\\[\r\n]")


def quote(identifier: str) -> str:
    """Bracket-quote an identifier. ] is doubled."""
    if not identifier:
        raise ValueError("empty identifier")
    return "[" + identifier.replace("]", "]]") + "]"


def qualified(schema: str, name: str) -> str:
    return f"{quote(schema)}.{quote(name)}"


def sql_literal(text: str) -> str:
    """An N'...' literal. ' is doubled. Use for every value that is sent inside batch text.

    Raises ValueError for text that would not arrive as written. NUL can end the batch text inside
    a driver. A backslash directly before a line break is a line continuation in T-SQL: the engine
    removes both characters, also inside a literal.
    """
    if "\x00" in text or _LINE_CONTINUATION.search(text):
        raise ValueError("text with NUL, or with a backslash before a line break, cannot be sent in a batch")
    return "N'" + text.replace("'", "''") + "'"


def object_key(kind: str, schema: str | None, name: str) -> str:
    """Stable identity of an object: 'PROCEDURE:[sales].[usp_x]'. A schema is 'SCHEMA:[sales]'."""
    if kind not in KINDS:
        raise ValueError(f"unknown object kind {kind!r}")
    if kind == "SCHEMA":
        if schema is not None:
            raise ValueError("a SCHEMA key has one part")
        return f"SCHEMA:{quote(name)}"
    if schema is None:
        raise ValueError(f"{kind} needs a schema")
    return f"{kind}:{qualified(schema, name)}"


def parse_object_key(key: str) -> tuple[str, str | None, str]:
    """Inverse of object_key: (kind, schema or None, name). Raises ValueError on any other text."""
    m = _KEY.match(key)
    if not m or m.group(1) not in KINDS:
        raise ValueError(f"not an object key: {key!r}")
    parts = [p.replace("]]", "]") for p in _PART.findall(m.group(2))]
    kind = m.group(1)
    if kind == "SCHEMA":
        if len(parts) != 1:
            raise ValueError(f"a SCHEMA key has one part: {key!r}")
        return kind, None, parts[0]
    if len(parts) != 2:
        raise ValueError(f"{kind} key needs schema and name: {key!r}")
    return kind, parts[0], parts[1]


def file_name_problem(name: str) -> str | None:
    """Why one file or folder name cannot be in a repository that Windows must check out too.

    None when it can. Refused: a character that a Windows file name cannot hold (< > : " / \\ | ? *
    and the control characters), a dot or a space at the end, a device name as the part before the
    first dot (CON, PRN, AUX, NUL, COM1 to COM9, LPT1 to LPT9, CONIN$, CONOUT$; any letter case),
    and more than 255 bytes of UTF-8.
    """
    if not name:
        return "is empty"
    unsafe = _UNSAFE_IN_PATH.search(name)
    if unsafe:
        return f"holds {unsafe.group()!r}, a character that a file name on Windows cannot hold"
    if name[-1] in ". ":
        return "ends with a dot or a space, which a file name on Windows cannot"
    device = _WINDOWS_DEVICE.match(name)
    if device:
        return f"starts with {device.group(1).upper()}, a device name on Windows, which is no file there"
    if len(name.encode("utf-8", "surrogatepass")) > MAX_FILE_NAME_BYTES:
        return f"is longer than {MAX_FILE_NAME_BYTES} bytes, the longest file name of a file system"
    return None


def _stem_problem(stem: str) -> str | None:
    """file_name_problem for an object file: the name without .sql, then the file name itself."""
    return file_name_problem(stem) or file_name_problem(f"{stem}.sql")


def path_problem(path: str) -> str | None:
    """Why a path (forward slashes) cannot be in a repository that Windows must check out too.

    None when it can. Every part is held to file_name_problem. A .sql file is held to the rule of
    path_for also: the name without the extension must pass, so the reader of a repository refuses
    what export never writes.
    """
    for part in path.split("/"):
        problem = _stem_problem(part[: -len(".sql")]) if part.endswith(".sql") else file_name_problem(part)
        if problem:
            return f"the name {part!r} {problem}"
    return None


def path_for(kind: str, schema: str | None, name: str) -> str:
    """Path of the object file, relative to the repository root, with forward slashes.

    Raises ValueError for a name that file_name_problem refuses: export then leaves the object
    unmanaged and never writes a file that Windows cannot check out.
    """
    stem = name if kind == "SCHEMA" else f"{schema}.{name}"
    problem = _stem_problem(stem)
    if problem:
        raise ValueError(f"name cannot be used as a file name: {stem!r} {problem}")
    return f"schema/{KIND_DIRS[kind]}/{stem}.sql"


def key_for_path(path: str) -> tuple[str, str]:
    """(kind, file stem) for 'schema/<kind dir>/<stem>.sql'. Raises ValueError for other paths,
    and for a file name that path_for never gives (file_name_problem)."""
    parts = path.split("/")
    if len(parts) != 3 or parts[0] != "schema" or parts[1] not in DIR_KINDS or not parts[2].endswith(".sql"):
        raise ValueError(f"not an object file path: {path!r}")
    stem = parts[2][: -len(".sql")]
    problem = _stem_problem(stem)
    if problem:
        raise ValueError(f"not an object file path: {path!r}: the name {stem!r} {problem}")
    return DIR_KINDS[parts[1]], stem
