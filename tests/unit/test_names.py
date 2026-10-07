import pytest

from azsqlcd.names import (
    key_for_path,
    object_key,
    parse_object_key,
    path_for,
    path_problem,
    quote,
    sql_literal,
)


def test_closing_bracket_cannot_end_a_quoted_identifier_early():
    assert quote("a]b") == "[a]]b]"


def test_quote_in_a_literal_cannot_end_the_literal_early():
    assert sql_literal("it's; DROP TABLE x --") == "N'it''s; DROP TABLE x --'"


@pytest.mark.parametrize("text", ["a\x00b", "a\\\nb", "a\\\r\nb", "a\\\rb"])
def test_a_literal_refuses_text_that_the_engine_or_a_driver_would_change(text):
    # NUL can end the batch text in a driver; T-SQL removes a backslash together with the line break after it
    with pytest.raises(ValueError):
        sql_literal(text)


def test_a_backslash_or_a_line_break_alone_stays_in_a_literal():
    assert sql_literal("a\\nb") == "N'a\\nb'"  # a backslash and the letter n
    assert sql_literal("a\nb\\") == "N'a\nb\\'"  # a line break, and a backslash at the end


@pytest.mark.parametrize(
    ("kind", "schema", "name"),
    [("PROCEDURE", "sales", "usp_x"), ("TABLE", "my schema", "weird]name"), ("SCHEMA", None, "sales")],
)
def test_object_key_round_trips(kind, schema, name):
    assert parse_object_key(object_key(kind, schema, name)) == (kind, schema, name)


@pytest.mark.parametrize("bad", ["PROCEDURE:sales.usp_x", "THING:[a].[b]", "TABLE:[a]", "SCHEMA:[a].[b]", ""])
def test_text_that_is_not_a_key_is_rejected(bad):
    with pytest.raises(ValueError):
        parse_object_key(bad)


def test_object_file_path_names_kind_directory_and_two_part_name():
    assert path_for("TABLE", "sales", "Order") == "schema/tables/sales.Order.sql"
    assert path_for("SCHEMA", None, "sales") == "schema/schemas/sales.sql"
    assert key_for_path("schema/procedures/sales.usp_x.sql") == ("PROCEDURE", "sales.usp_x")


def test_a_name_that_would_escape_the_directory_is_rejected():
    with pytest.raises(ValueError):
        path_for("VIEW", "dbo", "../../x")


# ------------------------------------------------------------------ names that Windows cannot hold (N4-04)
DEVICES = [
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CONIN$",
    "CONOUT$",
    *(f"{d}{n}" for d in ("COM", "LPT") for n in range(1, 10)),
]
DEVICE_CASES = [*DEVICES, "con", "Aux", "nUl", "prn", "com1", "Lpt9", "conin$", "aux ", "NUL  "]


@pytest.mark.parametrize("device", DEVICE_CASES)
def test_a_windows_device_name_is_refused_as_the_first_part_of_an_object_file_name(device):
    # Windows reads the part before the first dot as a device, with any extension and in any letter
    # case; Git for Windows refuses to check such a path out, so one file blocks every Windows clone
    with pytest.raises(ValueError, match="device name on Windows"):
        path_for("PROCEDURE", device, "usp_Log")
    with pytest.raises(ValueError, match="device name on Windows"):
        key_for_path(f"schema/procedures/{device}.usp_Log.sql")
    if not device.endswith(" "):  # 'aux ' as a whole name is refused for the space at its end
        with pytest.raises(ValueError, match="device name on Windows"):
            path_for("SCHEMA", None, device)
        with pytest.raises(ValueError, match="device name on Windows"):
            key_for_path(f"schema/schemas/{device}.sql")


@pytest.mark.parametrize(
    ("kind", "schema", "name", "path"),
    [
        ("TABLE", "sales", "nul", "schema/tables/sales.nul.sql"),  # only the first part is the device
        ("TABLE", "console", "t", "schema/tables/console.t.sql"),
        ("TABLE", "com10", "t", "schema/tables/com10.t.sql"),
        ("TABLE", "auxiliary", "t", "schema/tables/auxiliary.t.sql"),
        ("TABLE", "nullable", "t", "schema/tables/nullable.t.sql"),
        ("TABLE", "com", "t", "schema/tables/com.t.sql"),
        ("SCHEMA", None, "lpt", "schema/schemas/lpt.sql"),
        ("VIEW", "my schema", "a view", "schema/views/my schema.a view.sql"),
        ("VIEW", " lead", "v", "schema/views/ lead.v.sql"),
    ],
)
def test_a_name_that_only_looks_like_a_device_name_keeps_its_file(kind, schema, name, path):
    assert path_for(kind, schema, name) == path
    assert key_for_path(path) == (kind, path.rpartition("/")[2].removesuffix(".sql"))


@pytest.mark.parametrize("char", [":", "?", "*", '"', "<", ">", "|", "\t", "\x1f"])
def test_a_character_that_a_windows_file_name_cannot_hold_is_refused_in_both_directions(char):
    with pytest.raises(ValueError, match="cannot hold"):
        path_for("VIEW", "dbo", f"a{char}b")
    with pytest.raises(ValueError, match="cannot hold"):
        path_for("VIEW", f"d{char}bo", "v")
    # a hand-made file gets the rule of export: the reader refuses what the writer never writes
    with pytest.raises(ValueError, match="cannot hold"):
        key_for_path(f"schema/views/dbo.a{char}b.sql")


@pytest.mark.parametrize("name", ["v.", "v ", "v. ", "v .", "."])
def test_a_name_that_ends_with_a_dot_or_a_space_is_refused_in_both_directions(name):
    with pytest.raises(ValueError, match="ends with a dot or a space"):
        path_for("VIEW", "dbo", name)
    with pytest.raises(ValueError, match="ends with a dot or a space"):
        path_for("SCHEMA", None, name)
    with pytest.raises(ValueError, match="ends with a dot or a space"):
        key_for_path(f"schema/views/dbo.{name}.sql")


def test_a_file_name_longer_than_255_bytes_is_refused_and_255_bytes_is_not():
    # N4-05: schema and name are sysname (128 characters each); NTFS, ext4 and APFS take 255
    with pytest.raises(ValueError, match="255"):
        path_for("PROCEDURE", "s" * 128, "n" * 128)
    longest = "n" * (255 - len("dbo..sql"))
    assert path_for("PROCEDURE", "dbo", longest) == f"schema/procedures/dbo.{longest}.sql"
    with pytest.raises(ValueError, match="255"):
        path_for("PROCEDURE", "dbo", longest + "n")
    with pytest.raises(ValueError, match="255"):
        key_for_path(f"schema/procedures/dbo.{longest}n.sql")
    # the limit is in bytes of UTF-8: 100 characters of 3 bytes each do not fit
    with pytest.raises(ValueError, match="255"):
        path_for("PROCEDURE", "dbo", chr(0x20AC) * 100)


def test_every_path_that_path_for_gives_is_read_back_and_the_refusal_names_the_name():
    for kind, schema, name in [("TABLE", "sales", "Order"), ("SCHEMA", None, "sales"), ("VIEW", "a.b", "c")]:
        path = path_for(kind, schema, name)
        assert key_for_path(path)[0] == kind
        assert path_problem(path) is None
    with pytest.raises(ValueError) as e:
        path_for("PROCEDURE", "aux", "usp_Log")
    assert "'aux.usp_Log'" in str(e.value) and "AUX" in str(e.value)


@pytest.mark.parametrize(
    ("path", "why"),
    [
        ("schema/views/aux.v.sql", "device name"),
        ("schema/views/dbo.a:b.sql", "cannot hold"),
        ("schema/views/dbo.v .sql", "ends with a dot or a space"),
        ("migrations/NUL", "device name"),
        ("migrations/com1.txt", "device name"),
        ("onboarding/prod./export.md", "ends with a dot or a space"),
        ("onboarding/con/export.md", "device name"),
        ("onboarding/prod/what?.md", "cannot hold"),
        (f"migrations/{'m' * 252}.sql", "255"),
    ],
)
def test_path_problem_tells_why_windows_cannot_check_a_path_out(path, why):
    problem = path_problem(path)
    assert problem is not None and why in problem


@pytest.mark.parametrize(
    "path",
    [
        "azsqlcd.toml",
        "schema/_tombstones.toml",
        "schema/views/dbo.v.sql",
        "migrations/0001__a.sql",
        "migrations/migrations.sum",
        "onboarding/prod/export.md",
        f"schema/views/dbo.caf{chr(0xE9)}.sql",
        "schema/views/dbo.\udcff.sql",  # bytes that are not UTF-8: another rule refuses them
    ],
)
def test_path_problem_accepts_the_paths_of_the_layout(path):
    assert path_problem(path) is None
