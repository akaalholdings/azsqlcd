"""Complexity of the pull-request check (audit N5, 2,000 tables and 4,500 modules).

No test here reads a clock. Each one counts calls of the function that carried the cost and fails
when the count grows again with the number of rules, of statements or of batches.
"""

from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest

from azsqlcd import gen, lex, lint, model, modules
from fixtures.gen import repo as R

CONFIG = R.TOML.encode()


def view(n: int, uses: str = "SELECT 1 AS [One]") -> tuple[str, bytes]:
    text = f"CREATE OR ALTER VIEW [sales].[vw_{n:03d}]\nAS\n{uses};\n"
    return f"schema/views/sales.vw_{n:03d}.sql", text.encode()


def procedure(n: int) -> tuple[str, bytes]:
    text = (
        f"-- azsqlcd:after [sales].[vw_000]\nCREATE OR ALTER PROCEDURE [sales].[usp_{n:03d}]\n"
        f"WITH EXECUTE AS OWNER\nAS\nSET NOCOUNT ON;\nSELECT [One] FROM [sales].[vw_000] WHERE 1 = {n};\n"
    )
    return f"schema/procedures/sales.usp_{n:03d}.sql", text.encode()


def revision(module_count: int) -> dict[str, bytes]:
    """One revision: views, procedures with every module rule in play, one migration of two batches."""
    files = {"azsqlcd.toml": CONFIG, **{k: str(v).encode() for k, v in R.SALES.items()}}
    files[R.ORDER] = R.order().encode()
    files |= dict(view(n) for n in range(module_count // 2))
    files |= dict(procedure(n) for n in range(module_count // 2))
    body = "CREATE SCHEMA [sales];\nGO\n" + R.order().rstrip(";\n") + ";\nGO\n"
    for name, content in R.with_migrations({"0001__start.sql": R.migration("0001__start.sql", body)}).items():
        files[name] = str(content).encode()
    return files


@pytest.fixture
def scans(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    """Text -> the number of passes of the tokenizer over it, from an empty token cache."""
    seen: Counter[str] = Counter()
    scan = lex._scan

    def counted(src: str) -> list[lex.Tok]:
        seen[src] += 1
        return scan(src)

    monkeypatch.setattr(lex, "_scan", counted)
    lex.tokens.cache_clear()
    return seen


def counted_calls[**P, T](
    monkeypatch: pytest.MonkeyPatch, owner: object, name: str, key: Callable[P, str]
) -> Counter[str]:
    """Replace owner.name with a wrapper that counts its calls under key(arguments)."""
    seen: Counter[str] = Counter()
    real: Callable[P, T] = getattr(owner, name)

    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        seen[key(*args, **kwargs)] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, wrapper)
    return seen


# ------------------------------------------------------------------ N5-02: one tokenization
def test_one_lint_run_scans_each_text_once(scans: Counter[str]):
    """Was 11 passes over a module file and 5 over a table file: every rule called the tokenizer."""
    files = revision(module_count=6)
    assert [f for f in lint.lint_repo(files) if f.severity == "error"] == []
    module_texts = {files[p].decode() for p in files if "/views/" in p or "/procedures/" in p}
    assert len(module_texts) == 6 and module_texts <= scans.keys()
    assert {text: n for text, n in scans.items() if n != 1} == {}


def test_the_passes_over_a_module_file_do_not_grow_with_the_rules_in_a_large_revision(scans: Counter[str]):
    """More files than the token cache holds: a file is scanned once for each loop over all
    files of lint_repo (read, secrets, module rules, dependency edges), not once for each rule."""
    files = revision(module_count=60)
    lint.lint_repo(files)
    module_scans = [scans[files[p].decode()] for p in files if "/views/" in p or "/procedures/" in p]
    assert len(module_scans) == 60 and max(module_scans) <= 4


def test_tokenize_scans_once_and_gives_each_caller_its_own_list(scans: Counter[str]):
    text = "SELECT 1;\nGO\nSELECT 2;\n"
    first = lex.tokenize(text)
    first.clear()  # a caller may change its list
    assert [t.text for t in lex.tokenize(text)][:3] == ["SELECT", " ", "1"]
    assert lex.tokens(text) is lex.tokens(text)
    assert len(lex.split_batches(text)) == 2 and lex.directives(text) == []
    assert scans[text] == 1


def test_a_text_that_does_not_lex_is_refused_each_time(scans: Counter[str]):
    for _ in range(2):
        with pytest.raises(lex.LexError, match="unterminated string"):
            lex.tokenize("SELECT 'a")
    assert scans["SELECT 'a"] == 2  # an error is not kept


# ------------------------------------------------------------------ N5-08: split_batches
def test_split_batches_does_not_read_the_text_before_each_batch(monkeypatch: pytest.MonkeyPatch):
    """Was one count of all line breaks before each batch: 20 s for 12,000 batches."""
    calls = counted_calls(monkeypatch, lex, "_line_at", lambda src, pos: "line")
    text = "".join(f"SELECT {n};\r\nGO\n" for n in range(300))
    batches = lex.split_batches(text)
    assert [b.first_line for b in batches] == list(range(1, 600, 2))
    assert calls["line"] == 0


# ------------------------------------------------------------------ N5-03, N5-04: one read of a module
def test_verify_reads_each_module_file_of_the_head_revision_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Was 3 reads: lint_repo, lint_change, and the key set of the raw-batch rule with no raw batch."""
    modules_of_main = {k: v.decode() for k, v in [view(0), view(1), procedure(0), procedure(1)]}
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order(), **modules_of_main})
    add = "ALTER TABLE [sales].[Order] ADD [Note] nvarchar(200) NULL;\nGO\n"
    R.put(
        repo,
        {
            R.ORDER: R.order("[Note] nvarchar(200) NULL"),
            **R.with_migrations({"0001__add_note.sql": R.migration("0001__add_note.sql", add)}),
            view(1)[0]: view(1, "SELECT 2 AS [Two]")[1],
        },
    )
    reads = counted_calls(monkeypatch, modules, "read_module", lambda path, data: path)
    found = gen.verify(repo, R.git(repo, "rev-parse", "main"))
    assert [f for f in found if f.severity == "error"] == []
    assert dict(reads) == dict.fromkeys(modules_of_main, 1)


def test_verify_reads_the_modules_for_the_raw_batch_rule_when_a_migration_has_a_raw_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The other side of N5-03: RAW002 is still found, and it needs the keys of the modules."""
    main = {view(0)[0]: view(0)[1].decode()}
    repo = R.new_repo(tmp_path / "r", {**R.SALES, R.ORDER: R.order(), **main})
    raw = "-- azsqlcd:raw TABLE:[audit].[Log] reason: housekeeping\nDROP VIEW [sales].[vw_000];\nGO\n"
    R.put(repo, R.with_migrations({"0001__raw.sql": R.migration("0001__raw.sql", raw)}))
    reads = counted_calls(monkeypatch, modules, "read_module", lambda path, data: path)
    found = gen.verify(repo, R.git(repo, "rev-parse", "main"))
    assert "RAW002" in {f.code for f in found}
    assert reads[view(0)[0]] == 2  # lint, and the key set of the raw-batch rule


# ------------------------------------------------------------------ N5-07: unbind lines
def test_gen_reads_only_the_base_modules_that_hold_the_word_schemabinding(monkeypatch: pytest.MonkeyPatch):
    """Was a read of all 4,500 module files for each dropped, altered or renamed column."""
    bound = (
        "schema/views/sales.vw_Bound.sql",
        b"CREATE OR ALTER VIEW [sales].[vw_Bound]\nwith SchemaBinding\nAS\nSELECT [OrderId] FROM [sales].[Order];\n",  # noqa: E501
    )
    worded = (
        "schema/views/sales.vw_Worded.sql",
        b"CREATE OR ALTER VIEW [sales].[vw_Worded]\nAS\nSELECT [OrderId] AS [schemabinding] FROM [sales].[Order];\n",  # noqa: E501
    )
    base = dict([bound, worded, view(0, "SELECT [OrderId] FROM [sales].[Order]"), procedure(0)])
    reads = counted_calls(monkeypatch, modules, "read_module", lambda path, data: path)
    lines = gen._unbind_lines(base, {"TABLE:[sales].[Order]"})
    assert lines == ["-- azsqlcd:unbind [sales].[vw_Bound]"]
    assert sorted(reads) == [bound[0], worded[0]] and set(reads.values()) == {1}


# ------------------------------------------------------------------ N5-01: validate_model
def wide_model(tables: int) -> model.Model:
    files = {"schema/schemas/sales.sql": b"CREATE SCHEMA [sales];\n"}
    for n in range(tables):
        text = R.table(
            f"T{n:03d}", "[Id] int NOT NULL", f"CONSTRAINT [PK_T{n:03d}] PRIMARY KEY CLUSTERED ([Id])"
        )
        files[f"schema/tables/sales.T{n:03d}.sql"] = text.encode()
    return gen.load_model(files)


@pytest.mark.xfail(
    strict=True,
    reason="N5-01: Model.add / replace / remove of model.py build a whole Model for each statement. "
    "The change is in needs_from_others of group P (model.py is not a file of this group). Remove "
    "this marker with that change",
)
@pytest.mark.parametrize("tables", [5, 40])
def test_validate_model_does_not_build_the_whole_model_for_each_statement(
    tables: int, monkeypatch: pytest.MonkeyPatch
):
    """Was 12,263 constructions of a 2,209-object Model for 12,260 statements: 40 s of verify."""
    head = wide_model(tables)
    built = counted_calls(monkeypatch, model.Model, "__init__", lambda *args, **kwargs: "built")
    assert gen.validate_model(head) == []
    assert built["built"] <= 2  # the empty start model; not one for each statement
