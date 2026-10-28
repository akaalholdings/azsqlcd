"""Products (products.py): which product an object belongs to, where a product is held, and what
a hold does to the set-aside facts and to the notes of a plan."""

import pytest

from azsqlcd import chain, cli, products, set_aside
from azsqlcd.config import load_config
from azsqlcd.errors import Exit, ToolError
from azsqlcd.modules import read_module

HEAD = """
[project]
name = "sales"
tenant_id = "11111111-1111-1111-1111-111111111111"
table_model = true
min_token_minutes = 20

[identities]
plan = "22222222-2222-2222-2222-222222222222"

[env.dev]
plan_identity = "plan"
deploy_identity = "plan"
drift = "report"
lock_timeout_ms = 30000
applock_wait_s = 600
job_timeout_minutes = 120
targets = [{ id = "sales-dev", server = "sql-sales-dev.database.windows.net", database = "sales" }]
"""
PRODUCTS = """
[products.balance]
objects = ["sales.Balance*", "sales.vw_Balance"]
owners = ["@bal-owner"]
promote_to = "dev"

[products.legacy]
objects = ["dbo.old_*"]
owners = ["@sales/legacy-team"]
"""
CONFIG = load_config(HEAD + PRODUCTS)


TEXTS: dict[str, str] = {}


def migration(file: str, *batches: str) -> chain.Migration:
    header = f"-- azsqlcd:migration {file.removesuffix('.sql')}\n-- azsqlcd:mode tx\n"
    TEXTS[file] = header + "\nGO\n".join(batches) + "\n"
    return chain.parse_migration(TEXTS[file], file)


BALANCE = migration("0002__balance.sql", "CREATE TABLE [sales].[Balance] ([BalanceId] int NOT NULL);")
SHARED = migration("0003__order_note.sql", "ALTER TABLE [sales].[Order] ADD [Note] nvarchar(200) NULL;")
MIXED = migration(
    "0004__mixed.sql",
    "ALTER TABLE [sales].[Balance] ADD [Amount] int NULL;",
    "ALTER TABLE [sales].[Order] ADD [Rank] tinyint NULL;",
)


def files_of(*migrations: chain.Migration) -> dict[str, bytes]:
    found = {f"migrations/{m.file}": TEXTS[m.file].encode() for m in migrations}
    lines = [f"{m.file} sha256:{chain.file_sha256(found['migrations/' + m.file])} tx" for m in migrations]
    found[chain.SUM_PATH] = ("azsqlcd-sum 1\n" + "\n".join(lines) + "\n").encode()
    return found


def test_an_object_belongs_to_the_first_product_that_matches_it_and_else_is_shared():
    assert products.product_of(CONFIG, "SALES", "balance_line").name == "balance"
    assert products.product_of_key(CONFIG, "VIEW:[sales].[vw_Balance]").name == "balance"
    assert products.product_of(CONFIG, "sales", "Order") is None
    assert products.product_of_key(CONFIG, "SCHEMA:[sales]") is None
    assert products.overlapping(CONFIG, "dbo", "old_x") == ["legacy"]


@pytest.mark.parametrize(
    ("promote_to", "held_in"),
    [
        ("dev", {"sandbox", "test", "preprod", "prod"}),
        ("test", {"preprod", "prod"}),
        ("prod", set()),
    ],
)
def test_a_product_is_held_in_every_environment_after_its_promote_to(promote_to, held_in):
    product = load_config(
        HEAD + PRODUCTS.replace('promote_to = "dev"', f'promote_to = "{promote_to}"')
    ).products[0]
    for env in ("dev", "sandbox", "test", "preprod", "prod", "disposable"):
        assert products.is_held(product, env) is (env in held_in), env
    assert not any(products.is_held(CONFIG.products[1], env) for env in ("dev", "prod"))  # no promote_to
    # disposable, the environment of the live tests, is outside the promotion order: nothing is held there


def test_a_migration_that_mixes_a_held_product_with_shared_objects_is_named_and_one_that_does_not_is_not():
    assert products.mixed_hold(CONFIG, BALANCE) is None and products.mixed_hold(CONFIG, SHARED) is None
    why = products.mixed_hold(CONFIG, MIXED)
    assert why is not None and "product balance" in why and "[sales].[order]" in why
    # a product with no promote_to is never held, so a mix with it holds nothing back
    both = migration(
        "0005__legacy.sql",
        "ALTER TABLE [dbo].[old_x] ADD [y] int NULL;",
        "ALTER TABLE [sales].[Order] ADD [z] int NULL;",
    )
    assert products.mixed_hold(CONFIG, both) is None


def test_a_product_migration_of_an_object_outside_scope_is_named_while_the_product_has_promote_to():
    scoped = load_config(
        HEAD + PRODUCTS + '\n[scope]\nschemas = ["sales", "dbo"]\nexclude_objects = ["sales.Balance"]\n'
    )
    why = products.hold_out_of_scope(scoped, BALANCE)
    assert why is not None and BALANCE.file in why and "[sales].[balance]" in why and "OUT_OF_SCOPE" in why
    assert products.hold_out_of_scope(CONFIG, BALANCE) is None  # in scope
    assert products.hold_out_of_scope(scoped, SHARED) is None  # no product
    # no promote_to: the migration is held nowhere
    free = load_config(
        HEAD + PRODUCTS.replace('promote_to = "dev"\n', "") + '\n[scope]\nschemas = ["sales", "dbo"]\n'
        'exclude_objects = ["sales.Balance"]\n'
    )
    assert products.hold_out_of_scope(free, BALANCE) is None


def test_without_products_there_is_no_hold():
    assert products.hold(load_config(HEAD), "sandbox", files_of(BALANCE)) is None


def test_a_hold_sets_aside_the_migrations_of_a_held_product_and_releases_them_where_it_is_not_held():
    files = files_of(BALANCE, SHARED)
    sandbox = products.hold(CONFIG, "sandbox", files)
    assert sandbox is not None and set(sandbox.held_migrations) == {BALANCE.file}
    merged = sandbox.merge(set_aside.NOTHING, (), {BALANCE.file: 2}, 2)
    assert merged.active == {
        BALANCE.file: set_aside.SetAside(BALANCE.file, set_aside.HELD, 'product balance, promote_to = "dev"')
    }
    assert merged.active[BALANCE.file].note == f'held: {BALANCE.file} (product balance, promote_to = "dev")'
    assert (
        sandbox.holds("VIEW:[sales].[vw_Balance]").name == "balance"
        and sandbox.holds("TABLE:[sales].[Order]") is None
    )

    raised = load_config(HEAD + PRODUCTS.replace('promote_to = "dev"', 'promote_to = "test"'))
    later = products.hold(raised, "sandbox", files)
    assert later is not None and later.held_migrations == {}
    assert later.merge(set_aside.NOTHING, (), {BALANCE.file: 2}, 2) == set_aside.Aside(
        {}, frozenset({BALANCE.file})
    )
    # only a migration that the database could have held: of a release it holds, and not applied
    assert later.merge(set_aside.NOTHING, (), {BALANCE.file: 3}, 2) == set_aside.NOTHING
    assert later.merge(set_aside.NOTHING, (BALANCE.file,), {BALANCE.file: 2}, 2) == set_aside.NOTHING
    # nothing is held in the first environment, so nothing there was set aside before
    dev = products.hold(CONFIG, "dev", files)
    assert dev.merge(set_aside.NOTHING, (), {BALANCE.file: 2}, 2) == set_aside.NOTHING


def test_a_park_stays_a_park_when_the_migration_is_also_held():
    parked = set_aside.SetAside(BALANCE.file, set_aside.PARKED, "run 4, BATCH_FAILED")
    merged = products.hold(CONFIG, "sandbox", files_of(BALANCE)).merge(
        set_aside.Aside({BALANCE.file: parked}), (), {}, 0
    )
    assert merged.active == {BALANCE.file: parked}


def test_the_notes_count_each_product_and_list_what_is_held_here():
    sandbox = products.hold(CONFIG, "sandbox", files_of(BALANCE, SHARED))
    notes = sandbox.notes([SHARED], [BALANCE.file], [], ["VIEW:[sales].[vw_Balance]"])
    assert notes == [
        "products: balance (1 migration, 1 module)",
        f'held in sandbox: balance (promote_to = "dev"): {BALANCE.file}, VIEW:[sales].[vw_Balance]',
    ]
    assert sandbox.notes([SHARED], [], ["VIEW:[sales].[vw_OpenOrders]"], []) == []


def test_a_module_that_names_a_held_module_that_the_database_lacks_is_refused():
    sandbox = products.hold(CONFIG, "sandbox", files_of(BALANCE))
    user = read_module(
        "schema/views/sales.vw_Report.sql",
        b"CREATE OR ALTER VIEW [sales].[vw_Report] AS\nSELECT 1 AS [x] FROM [sales].[vw_Balance];\n",
    )
    # the database holds the held view with the text of its file
    products.check_held_modules(sandbox, [], [user], [], ["VIEW:[sales].[vw_Balance]"])
    with pytest.raises(ToolError) as e:
        products.check_held_modules(sandbox, [], [user], ["VIEW:[sales].[vw_Balance]"], [])
    assert (e.value.exit_code, e.value.reason_code) == (Exit.REFUSED, "SET_ASIDE_DEPENDENCY")
    assert "VIEW:[sales].[vw_Report] names VIEW:[sales].[vw_Balance]" in e.value.message
    assert "is not in this database" in e.value.message


def test_a_module_that_names_a_held_module_with_another_text_here_is_refused():
    """The database holds the held view, but not the text of its file: what names the view can need
    the held text, which does not come here."""
    sandbox = products.hold(CONFIG, "sandbox", files_of(BALANCE))
    user = read_module(
        "schema/views/sales.vw_Report.sql",
        b"CREATE OR ALTER VIEW [sales].[vw_Report] AS\nSELECT 1 AS [x] FROM [sales].[vw_Balance];\n",
    )
    held = ["VIEW:[sales].[vw_Balance]"]
    with pytest.raises(ToolError) as e:
        products.check_held_modules(sandbox, [], [user], held, held)
    assert (e.value.exit_code, e.value.reason_code) == (Exit.REFUSED, "SET_ASIDE_DEPENDENCY")
    assert e.value.detail["set_aside"] == "VIEW:[sales].[vw_Balance]"
    assert "holds it with another text than its file" in e.value.message


def test_codeowners_gives_one_glob_where_a_folder_holds_only_files_of_the_product_and_else_one_line_a_file():
    files = {
        "schema/tables/sales.Balance.sql": b"",
        "schema/tables/sales.BalanceLine.sql": b"",
        "schema/tables/sales.Order.sql": b"",
        "schema/views/sales.vw_Balance.sql": b"",
        "schema/views/sales.vw_BalanceOld.sql": b"",  # matches no pattern: shared
        "schema/tables/dbo.old_a.sql": b"",
        "schema/tables/DBO.OLD_B.sql": b"",  # its product by the rule without case, not by the glob
        "migrations/0002__balance.sql": b"",
    }
    assert products.object_files(CONFIG, files) == {
        "balance": [
            "schema/tables/sales.Balance.sql",
            "schema/tables/sales.BalanceLine.sql",
            "schema/views/sales.vw_Balance.sql",
        ],
        "legacy": ["schema/tables/DBO.OLD_B.sql", "schema/tables/dbo.old_a.sql"],
    }
    assert products.codeowners(CONFIG, files) == [
        "/schema/tables/sales.Balance*.sql @bal-owner",
        "/schema/views/sales.vw_Balance.sql @bal-owner",
        "/schema/tables/dbo.old_*.sql @sales/legacy-team",
        "/schema/tables/DBO.OLD_B.sql @sales/legacy-team",
    ]


def test_codeowners_writes_a_space_of_a_path_with_a_backslash_so_that_it_stays_one_path():
    files = {"schema/tables/sales.Balance Line.sql": b"", "schema/tables/sales.Order.sql": b""}
    lower = load_config(HEAD + PRODUCTS.replace('"sales.Balance*"', '"sales.balance*"'))
    line = products.codeowners(lower, files)[0]
    assert line == "/schema/tables/sales.Balance\\ Line.sql @bal-owner"
    assert line.replace("\\ ", "_").split() == ["/schema/tables/sales.Balance_Line.sql", "@bal-owner"]


# ------------------------------------------------------------------ the command `products`
VIEW_TEXT = "CREATE OR ALTER VIEW [sales].[vw_Balance]\nAS\nSELECT 1 AS [One];\n"


def repository(folder, config_text: str) -> str:
    files = {
        "azsqlcd.toml": config_text,
        "schema/tables/sales.Balance.sql": "CREATE TABLE [sales].[Balance] (\n    [Id] int NOT NULL\n);\n",
        "schema/tables/sales.Order.sql": "CREATE TABLE [sales].[Order] (\n    [Id] int NOT NULL\n);\n",
        "schema/views/sales.vw_Balance.sql": VIEW_TEXT,
        "migrations/" + BALANCE.file: TEXTS[BALANCE.file],
        "migrations/" + SHARED.file: TEXTS[SHARED.file],
        chain.SUM_PATH: files_of(BALANCE, SHARED)[chain.SUM_PATH].decode(),
    }
    for path, text in files.items():
        (folder / path).parent.mkdir(parents=True, exist_ok=True)
        (folder / path).write_text(text)
    return str(folder)


def run(capsys, *argv: str) -> tuple[int, str]:
    capsys.readouterr()
    code = cli.main(list(argv))
    return code, capsys.readouterr().out


def test_products_lists_each_product_with_its_owners_promote_to_and_object_count(tmp_path, capsys):
    root = repository(tmp_path, HEAD + PRODUCTS)
    code, out = run(capsys, "products", "--root", root)
    assert code == 0
    assert out.splitlines() == [
        'balance: 2 object file(s); owners @bal-owner; promote_to = "dev"',
        "legacy: 0 object file(s); owners @sales/legacy-team; promote_to: none (every environment)",
    ]


def test_products_with_env_says_what_is_held_there_from_the_files_only(tmp_path, capsys):
    root = repository(tmp_path, HEAD + PRODUCTS)
    code, out = run(capsys, "products", "--root", root, "--env", "sandbox")
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == 'balance: 2 object file(s); owners @bal-owner; promote_to = "dev"'
    assert lines[1] == f"  held in sandbox: migrations {BALANCE.file}; modules VIEW:[sales].[vw_Balance]"
    assert lines[2].startswith("legacy:") and lines[3] == "  not held in sandbox"
    assert lines[-1].startswith("offline: no database is read")
    code, out = run(capsys, "products", "--root", root, "--env", "dev")
    assert "  not held in dev" in out.splitlines()


def test_products_with_env_lists_apart_what_not_managed_takes_out_there_as_the_plan_does(tmp_path, capsys):
    sandbox = HEAD[HEAD.index("[env.dev]") :].replace("dev", "sandbox")
    sandbox = sandbox.replace("[env.sandbox]\n", '[env.sandbox]\nnot_managed = ["sales.vw_Balance"]\n')
    root = repository(tmp_path, HEAD + sandbox + PRODUCTS)
    code, out = run(capsys, "products", "--root", root, "--env", "sandbox")
    assert code == 0
    lines = out.splitlines()
    assert lines[1] == f"  held in sandbox: migrations {BALANCE.file}; modules none"
    assert lines[2] == "  not managed in sandbox: migrations none; modules VIEW:[sales].[vw_Balance]"
    assert lines[3].startswith("legacy:")
    # a held migration of objects that sandbox does not manage only is skipped there, not held
    (tmp_path / "two").mkdir()
    root = repository(
        tmp_path / "two", HEAD + sandbox.replace('"sales.vw_Balance"', '"sales.*Balance"') + PRODUCTS
    )
    code, out = run(capsys, "products", "--root", root, "--env", "sandbox")
    assert out.splitlines()[1:3] == [
        "  held in sandbox: migrations none; modules none",
        f"  not managed in sandbox: migrations {BALANCE.file}; modules VIEW:[sales].[vw_Balance]",
    ]


def test_products_codeowners_prints_the_lines_of_the_products_with_owners(tmp_path, capsys):
    root = repository(tmp_path, HEAD + PRODUCTS)
    code, out = run(capsys, "products", "--root", root, "--codeowners")
    assert code == 0
    assert out.splitlines() == [
        "/schema/tables/sales.Balance*.sql @bal-owner",
        "/schema/views/sales.vw_Balance.sql @bal-owner",
    ]


def test_products_without_a_products_table_says_so(tmp_path, capsys):
    code, out = run(capsys, "products", "--root", repository(tmp_path, HEAD))
    assert (code, out) == (0, "azsqlcd.toml has no [products]\n")
