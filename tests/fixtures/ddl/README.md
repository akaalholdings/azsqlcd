# DDL fixtures

Input for `tests/unit/test_parse.py`. The leading `-- key: value` comment lines of a file are its header.

## ok/

Text that the parser accepts.

- `-- path: schema/<dir>/<name>.sql` on line 1: the text is an object file. It is parsed with
  `parse_object_file(text, path)`.
- No `-- path:` line: the text is one migration model batch. It is parsed with `parse_statement(text)`.
  These files are named `stmt_*.sql`.
- `<name>.json` is the expected result: every field of every dataclass, names and expression tokens as
  written (no case folding).

Many object files are not in the canonical form on purpose (bare names, lower case, column-level
constraints, comments). They test the parser, not the emitter.

## bad/

Text that the parser rejects. One rejected construct per file.

```
-- expect: <CODE>            SYNTAX, UNSUPPORTED or NF001 to NF006
-- says: <phrase>            a phrase that the message must hold
-- line: <n>                 the line of the error, in this file
-- path: schema/...          only for an object file
```

## After an intended change of behaviour

```
AZSQLCD_UPDATE_GOLDEN=1 .venv/bin/python -m pytest tests/unit/test_parse.py -q
```

This writes the `.json` files again. Read the diff before you keep it.
