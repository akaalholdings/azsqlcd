"""Temporary database repositories for the gen and verify tests."""

import os
import subprocess
from pathlib import Path

from azsqlcd import chain

ROOT = Path(__file__).resolve().parent
TOML = (ROOT / "azsqlcd.toml").read_text(encoding="utf-8")
TOML_NO_MODEL = TOML.replace("table_model = true", "table_model = false")
EMPTY_SUM = "azsqlcd-sum 1\n"

# The temporary repositories do not depend on the git configuration of the machine.
GIT_ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
}

type Files = dict[str, str | bytes | None]  # path -> content; None removes the file


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, env=GIT_ENV, capture_output=True, check=True)
    return done.stdout.decode().strip()


def put(repo: Path, files: Files) -> None:
    """Write (or remove) files of the working tree. Text is written with LF line ends."""
    for name, content in files.items():
        target = repo / name
        if content is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)


def commit(repo: Path, message: str, files: Files | None = None) -> str:
    """Commit the whole working tree (after writing files). Returns the commit sha."""
    put(repo, files or {})
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def new_repo(path: Path, files: Files) -> Path:
    """A repository whose branch main holds one commit with azsqlcd.toml, an empty chain and files.

    The working tree is then on a new branch `work`, so `main` is the base of a pull request.
    """
    path.mkdir(parents=True)
    git(path, "-c", "init.defaultBranch=main", "init", "-q")
    for key, value in (
        ("user.name", "Test"),
        ("user.email", "test@example.invalid"),
        ("core.autocrlf", "false"),
        ("commit.gpgsign", "false"),
    ):
        git(path, "config", key, value)
    commit(path, "base", {"azsqlcd.toml": TOML, chain.SUM_PATH: EMPTY_SUM, **files})
    git(path, "checkout", "-q", "-b", "work")
    return path


def migration(file: str, body: str, mode: str = "tx") -> str:
    """The text of a migration file: the header, then the body as written."""
    return f"-- azsqlcd:migration {file.removesuffix('.sql')}\n-- azsqlcd:mode {mode}\n{body}"


def sum_text(files: dict[str, str], extra: dict[str, str] | None = None, baseline: bool = False) -> str:
    """migrations.sum for migration files (id -> text) in the given order.

    extra: id -> the words after the mode, for example {'0001__a.sql': 'withdrawn'}.
    """
    lines = ["azsqlcd-sum 1"] + (["baseline"] if baseline else [])
    for file, text in files.items():
        mode = "nontx" if "-- azsqlcd:mode nontx" in text else "tx"
        words = (extra or {}).get(file)
        lines.append(
            f"{file} sha256:{chain.file_sha256(text.encode('utf-8'))} {mode}" + (f" {words}" if words else "")
        )
    return "\n".join(lines) + "\n"


def with_migrations(files: dict[str, str], extra: dict[str, str] | None = None) -> Files:
    """The migration files under migrations/ and the migrations.sum that lists them."""
    return {
        **{f"migrations/{file}": text for file, text in files.items()},
        chain.SUM_PATH: sum_text(files, extra),
    }


# ------------------------------------------------------------------ object files in normal form
SALES: Files = {"schema/schemas/sales.sql": "CREATE SCHEMA [sales];\n"}
ORDER = "schema/tables/sales.Order.sql"
ORDER_ID = "[OrderId] int NOT NULL"
PK_ORDER = "CONSTRAINT [PK_Order] PRIMARY KEY CLUSTERED ([OrderId])"


def table(name: str, *elements: str) -> str:
    """The object file of table [sales].[name]: columns, then constraints, as the emitter writes them."""
    return f"CREATE TABLE [sales].[{name}] (\n" + ",\n".join(f"    {e}" for e in elements) + "\n);\n"


def order(*columns: str) -> str:
    """[sales].[Order] with its key column, the given columns and its primary key."""
    return table("Order", ORDER_ID, *columns, PK_ORDER)
