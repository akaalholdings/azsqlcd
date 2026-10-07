# Fixture pairs

Input for `tests/unit/test_diff.py` and `tests/unit/test_proof_property.py`. One directory is one case: a
base revision and a head revision of a database repository.

```
<case>/base/schema/<kind dir>/<name>.sql    object files of the base revision, in normal form
<case>/head/schema/<kind dir>/<name>.sql    object files of the head revision, in normal form
<case>/renames.txt                          optional: one --rename value on each line, in order
<case>/expected.sql                         the statements of diff(base, head, renames), GO between them
<case>/expected_refusals.txt                or: what diff refuses, one "<CODE> <object key>" on each line
```

A case has `expected.sql` or `expected_refusals.txt`, never both. The cases named `refuse_*` are the
refusals. `loader.py` reads a case.

## After an intended change of behaviour

```
AZSQLCD_UPDATE_GOLDEN=1 .venv/bin/python -m pytest tests/unit/test_diff.py -q
```

This writes the expected files again. Read the diff before you keep it: the expected file is the
proof that a human looked at the SQL.
