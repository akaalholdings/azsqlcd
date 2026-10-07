"""The migration chain is append-only, a migration file has one meaning, and both fail loud."""

import hashlib
from dataclasses import replace

import pytest

from azsqlcd.chain import (
    Allow,
    Chain,
    ChainEntry,
    DeployModule,
    Tombstone,
    Unbind,
    check_immutable,
    effective_order,
    file_sha256,
    format_sum,
    is_migration_file,
    parse_migration,
    parse_sum,
    parse_tombstones,
)
from azsqlcd.errors import Exit, ToolError

A, B, C, D = "a" * 64, "b" * 64, "c" * 64, "d" * 64

SUM = f"""azsqlcd-sum 1
baseline
0001__add_order_status.sql sha256:{A} tx
0002__ix_order_status.sql sha256:{B} nontx
0003__status_not_null.sql sha256:{C} tx withdrawn
0004__status_not_null_v2.sql sha256:{D} tx replaces=0003__status_not_null.sql
"""

E1 = ChainEntry("0001__a.sql", A, "tx")
E2 = ChainEntry("0002__b.sql", B, "tx")
E3 = ChainEntry("0003__c.sql", C, "nontx")


def refusal(code: str, call, *args) -> ToolError:
    with pytest.raises(ToolError) as e:
        call(*args)
    assert (e.value.exit_code, e.value.reason_code) == (Exit.REFUSED, code)
    return e.value


# ------------------------------------------------------------------ migrations.sum
def test_the_sum_file_is_read_into_entries_in_file_order():
    chain = parse_sum(SUM)
    assert chain.baseline is True
    assert chain.entries == (
        ChainEntry("0001__add_order_status.sql", A, "tx"),
        ChainEntry("0002__ix_order_status.sql", B, "nontx"),
        ChainEntry("0003__status_not_null.sql", C, "tx", withdrawn=True),
        ChainEntry("0004__status_not_null_v2.sql", D, "tx", replaces="0003__status_not_null.sql"),
    )


def test_format_is_the_exact_inverse_of_parse():
    assert format_sum(parse_sum(SUM)) == SUM
    both = Chain(
        False, (replace(E1, withdrawn=True), ChainEntry("0002__b.sql", B, "tx", True, "0001__a.sql"))
    )
    assert format_sum(both).splitlines()[2] == f"0002__b.sql sha256:{B} tx withdrawn replaces=0001__a.sql"
    assert parse_sum(format_sum(both)) == both
    assert format_sum(Chain()) == "azsqlcd-sum 1\n"
    assert parse_sum("azsqlcd-sum 1\n") == Chain()


def test_a_windows_checkout_of_the_sum_file_reads_as_the_same_chain():
    assert parse_sum(chr(0xFEFF) + SUM.replace("\n", "\r\n")) == parse_sum(SUM)


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("", 1),
        ("azsqlcd-sum 2\n", 1),
        (f"0001__a.sql sha256:{A} tx\n", 1),
        (f"azsqlcd-sum 1\n0001__a.sql sha256:{A} tx\nbaseline\n", 3),
        ("azsqlcd-sum 1\nbaseline\nbaseline\n", 3),
        (f"azsqlcd-sum 1\n0001__a.sql  sha256:{A} tx\n", 2),
        (f"azsqlcd-sum 1\n0001__a.sql sha256:{A} tx \n", 2),
        (f"azsqlcd-sum 1\n\n0001__a.sql sha256:{A} tx\n", 2),
        (f"azsqlcd-sum 1\n0001__a.sql sha256:{A} tx", 2),
        (f"azsqlcd-sum 1\n0001__a.sql sha256:{A.upper()} tx\n", 2),
        (f"azsqlcd-sum 1\n0001__a.sql sha256:{A[:-1]} tx\n", 2),
        (f"azsqlcd-sum 1\n0001__a.sql md5:{A} tx\n", 2),
        (f"azsqlcd-sum 1\n0001__a.sql sha256:{A} maybe\n", 2),
        (f"azsqlcd-sum 1\n0001__a.sql sha256:{A} tx replaces=0001__a.sql withdrawn\n", 2),
        (f"azsqlcd-sum 1\n0001__a.sql sha256:{A} tx\n0002__b.sql sha256:{B} tx undoes=0001__a.sql\n", 3),
        (f"azsqlcd-sum 1\n001__a.sql sha256:{A} tx\n", 2),
        (f"azsqlcd-sum 1\n0001_a.sql sha256:{A} tx\n", 2),
        (f"azsqlcd-sum 1\n../0001__a.sql sha256:{A} tx\n", 2),
        (f"azsqlcd-sum 1\n0001__a.txt sha256:{A} tx\n", 2),
        (f"azsqlcd-sum 1\n0001__{'x' * 200}.sql sha256:{A} tx\n", 2),
    ],
)
def test_text_outside_the_exact_grammar_is_refused_with_its_line_number(text, line):
    assert refusal("CHAIN_INVALID", parse_sum, text).detail["line"] == line


@pytest.mark.parametrize(
    ("second", "third"),
    [
        (f"0002__b.sql sha256:{B} tx", f"0002__c.sql sha256:{C} tx"),  # number repeats
        (f"0003__b.sql sha256:{B} tx", f"0002__c.sql sha256:{C} tx"),  # number goes down
        (f"0002__b.sql sha256:{B} tx", f"0003__c.sql sha256:{C} tx replaces=0002__b.sql"),  # not withdrawn
        (f"0002__b.sql sha256:{B} tx", f"0003__c.sql sha256:{C} tx replaces=0009__z.sql"),  # no such line
        (f"0002__b.sql sha256:{B} tx", f"0003__c.sql sha256:{C} tx replaces=0003__c.sql"),  # itself
    ],
)
def test_a_chain_that_breaks_its_own_rules_cannot_be_parsed(second, third):
    text = f"azsqlcd-sum 1\n0001__a.sql sha256:{A} tx\n{second}\n{third}\n"
    assert refusal("CHAIN_INVALID", parse_sum, text).detail["line"] == 4


def test_a_withdrawn_migration_has_at_most_one_replacement():
    text = (
        f"azsqlcd-sum 1\n0001__a.sql sha256:{A} tx withdrawn\n"
        f"0002__b.sql sha256:{B} tx replaces=0001__a.sql\n0003__c.sql sha256:{C} tx replaces=0001__a.sql\n"
    )
    assert refusal("CHAIN_INVALID", parse_sum, text).detail["line"] == 4


def test_a_replacement_runs_at_the_position_of_the_migration_it_replaces():
    chain = parse_sum(
        f"azsqlcd-sum 1\n0001__a.sql sha256:{A} tx\n0002__b.sql sha256:{B} tx withdrawn\n"
        f"0003__c.sql sha256:{C} tx\n0004__d.sql sha256:{D} tx replaces=0002__b.sql\n"
    )
    order = effective_order(chain)
    assert [e.file for e in order] == ["0001__a.sql", "0002__b.sql", "0004__d.sql", "0003__c.sql"]
    assert [e.withdrawn for e in order] == [False, True, False, False]


def test_a_replacement_of_a_replacement_stays_at_the_first_position():
    chain = parse_sum(
        f"azsqlcd-sum 1\n0001__a.sql sha256:{A} tx withdrawn\n0002__b.sql sha256:{B} tx\n"
        f"0003__c.sql sha256:{C} tx withdrawn replaces=0001__a.sql\n"
        f"0004__d.sql sha256:{D} tx replaces=0003__c.sql\n"
    )
    assert [e.file for e in effective_order(chain)] == [
        "0001__a.sql",
        "0003__c.sql",
        "0004__d.sql",
        "0002__b.sql",
    ]


def test_effective_order_without_replacements_is_file_order_and_keeps_withdrawn_entries():
    chain = Chain(False, (E1, replace(E2, withdrawn=True), E3))
    assert effective_order(chain) == list(chain.entries)


def test_effective_order_refuses_a_replacement_of_an_unknown_migration():
    chain = Chain(False, (E1, replace(E2, replaces="0009__z.sql")))
    refusal("CHAIN_INVALID", effective_order, chain)


# ------------------------------------------------------------------ file checksum
def test_the_file_checksum_is_the_same_for_a_windows_checkout_and_the_git_blob():
    blob = b"-- azsqlcd:migration 0001__a\nSELECT N'x';\nGO\n"
    assert file_sha256(blob) == hashlib.sha256(blob).hexdigest()
    assert file_sha256(blob.replace(b"\n", b"\r\n")) == file_sha256(blob)
    assert file_sha256(blob.replace(b"\n", b"\r")) == file_sha256(blob)
    assert file_sha256(b"\xef\xbb\xbf" + blob) == file_sha256(blob)


def test_the_file_checksum_definition_is_frozen():
    # A change of this value breaks every migrations.sum that exists.
    assert file_sha256(b"\xef\xbb\xbfa\r\nb\rc\n") == hashlib.sha256(b"a\nb\nc\n").hexdigest()
    assert file_sha256(b"a\n") == "87428fc522803d31065e7bce3cf03fe475096631e5e07bbd7a0fde60c4cf25c7"


def test_the_file_checksum_keeps_every_other_byte():
    assert file_sha256(b"a \n") != file_sha256(b"a\n")
    assert file_sha256(b"a\n\n") != file_sha256(b"a\n")
    assert file_sha256(b"a\xef\xbb\xbf\n") != file_sha256(b"a\n")  # only a leading BOM is removed
    assert file_sha256(b"\xef\xbb\xbf\xef\xbb\xbfa\n") != file_sha256(b"a\n")  # and only one


# ------------------------------------------------------------------ immutability
def test_an_unchanged_chain_and_a_chain_with_new_lines_at_the_end_pass():
    base = Chain(True, (E1, E2))
    assert check_immutable(base, base) == []
    assert check_immutable(base, Chain(True, (E1, E2, E3))) == []


def test_the_word_withdrawn_may_be_added_to_a_merged_line():
    base = Chain(False, (E1, E2))
    head = Chain(
        False, (E1, replace(E2, withdrawn=True), ChainEntry("0003__c.sql", C, "tx", replaces=E2.file))
    )
    assert check_immutable(base, head) == []


@pytest.mark.parametrize(
    "changed",
    [
        replace(E2, sha256=D),
        replace(E2, mode="nontx"),
        replace(E2, file="0002__renamed.sql"),
        replace(E2, withdrawn=True, sha256=D),
    ],
)
def test_a_merged_line_cannot_change(changed):
    findings = check_immutable(Chain(False, (E1, E2)), Chain(False, (E1, changed)))
    assert len(findings) == 1 and findings[0].startswith("line 3:")


def test_the_word_withdrawn_cannot_be_taken_away_again():
    base = Chain(False, (E1, replace(E2, withdrawn=True)))
    assert len(check_immutable(base, Chain(False, (E1, E2)))) == 1


def test_a_merged_line_cannot_gain_a_replaces_word():
    base = Chain(False, (replace(E1, withdrawn=True), E2))
    head = Chain(False, (replace(E1, withdrawn=True), replace(E2, replaces=E1.file)))
    assert len(check_immutable(base, head)) == 1


def test_a_merged_line_cannot_be_removed():
    assert len(check_immutable(Chain(False, (E1, E2)), Chain(False, (E1,)))) == 1
    assert len(check_immutable(Chain(False, (E1, E2)), Chain(False, (E2,)))) == 1
    assert len(check_immutable(Chain(False, (E1, E2)), Chain())) == 1


def test_merged_lines_keep_their_order():
    findings = check_immutable(Chain(False, (E1, E2)), Chain(False, (E2, E1)))
    assert len(findings) == 1 and findings[0].startswith("line 2:")


def test_a_new_line_can_only_be_at_the_end():
    base = Chain(False, (E1, E3))
    assert check_immutable(base, Chain(False, (E1, E2, E3))) != []
    assert check_immutable(base, Chain(False, (E2, E1, E3))) != []


def test_a_new_file_has_a_number_higher_than_every_earlier_one():
    base = Chain(False, (E1, E3))
    for new in ("0003__again.sql", "0002__late.sql", "0001__a.sql"):
        findings = check_immutable(base, Chain(False, (E1, E3, ChainEntry(new, D, "tx"))))
        assert len(findings) == 1 and findings[0].startswith(f"line 4: {new} "), new
    two_new = Chain(False, (E1, E3, ChainEntry("0005__x.sql", D, "tx"), ChainEntry("0004__y.sql", D, "tx")))
    assert [f[:24] for f in check_immutable(base, two_new)] == ["line 5: 0004__y.sql has "]


def test_replaces_must_name_a_withdrawn_line():
    base = Chain(False, (E1, E2))
    new = ChainEntry("0003__c.sql", C, "tx", replaces=E2.file)
    assert len(check_immutable(base, Chain(False, (E1, E2, new)))) == 1  # 0002 is not withdrawn
    assert check_immutable(base, Chain(False, (E1, replace(E2, withdrawn=True), new))) == []
    unknown = replace(new, replaces="0009__z.sql")
    assert len(check_immutable(base, Chain(False, (E1, E2, unknown)))) == 1


def test_a_baseline_line_cannot_disappear():
    assert len(check_immutable(Chain(True, (E1,)), Chain(False, (E1,)))) == 1
    assert len(check_immutable(Chain(True), Chain(False))) == 1


def test_a_baseline_line_cannot_appear_once_the_chain_has_migrations():
    assert len(check_immutable(Chain(False, (E1,)), Chain(True, (E1,)))) == 1


def test_the_first_chain_of_a_repository_may_start_with_baseline():
    # the onboarding pull request creates migrations.sum; the caller passes Chain() as the base
    assert check_immutable(Chain(), Chain(True)) == []
    assert check_immutable(Chain(), Chain(True, (E1,))) == []


# ------------------------------------------------------------------ migration file
HEADER = "-- azsqlcd:migration 0001__add_order_status\n-- azsqlcd:mode tx\n"
FILE = "0001__add_order_status.sql"


def invalid_line(text: str, file_name: str = FILE) -> int:
    e = refusal("MIGRATION_INVALID", parse_migration, text, file_name)
    assert e.detail["file"] == file_name
    return e.detail["line"]


def test_a_migration_is_read_into_header_and_batches():
    text = (
        HEADER
        + "ALTER TABLE [sales].[Order] ADD [Status] tinyint NULL;\nGO\n"
        + "-- azsqlcd:data\nUPDATE [sales].[Order] SET [Status] = 1 WHERE [ShippedUtc] IS NOT NULL;\nGO\n"
    )
    m = parse_migration(text, FILE)
    assert (m.file, m.mode, m.expected_minutes) == (FILE, "tx", None)
    assert [(b.kind, b.first_line, b.directives) for b in m.batches] == [("model", 1, ()), ("data", 5, ())]


def test_batch_text_is_exactly_what_is_sent_and_keeps_its_directive_lines():
    first = HEADER + "-- azsqlcd:unbind [sales].[vw_Open]\nALTER TABLE [t] DROP COLUMN [c];  -- note"
    second = "-- azsqlcd:data\n\tUPDATE [t] SET [d] = N'GO';"
    m = parse_migration(first + "\r\nGO\r\n" + second + "\r\n", FILE)
    assert [b.text for b in m.batches] == [first, second]


def test_the_migration_directive_must_equal_the_file_name_without_sql():
    assert invalid_line(HEADER + "SELECT 1;\n", "0001__other_name.sql") == 1
    assert (
        invalid_line("-- azsqlcd:migration 0001__add_order_status.sql\n-- azsqlcd:mode tx\nSELECT 1;\n") == 1
    )


@pytest.mark.parametrize(
    "name", ["add_order_status.sql", "0001__add_order_status", "migrations/" + FILE, "1__a.sql"]
)
def test_the_file_name_must_be_a_plain_numbered_migration_name(name):
    refusal("MIGRATION_INVALID", parse_migration, HEADER + "SELECT 1;\n", name)


@pytest.mark.parametrize(
    "text",
    [
        "-- azsqlcd:mode tx\nSELECT 1;\n",
        "-- azsqlcd:migration 0001__add_order_status\nSELECT 1;\n",
        "SELECT 1;\n",
        "",
    ],
)
def test_both_header_directives_are_required(text):
    invalid_line(text)


def test_a_header_directive_below_the_first_statement_is_refused():
    assert invalid_line("-- azsqlcd:migration 0001__add_order_status\nSELECT 1;\n-- azsqlcd:mode tx\n") == 3
    assert (
        invalid_line(
            "-- azsqlcd:migration 0001__add_order_status\nSELECT 1;\nGO\n-- azsqlcd:mode tx\nSELECT 2;\n"
        )
        == 4
    )


def test_a_header_directive_given_twice_is_refused():
    assert invalid_line(HEADER + "-- azsqlcd:mode nontx\nSELECT 1;\n") == 3


def test_comments_and_blank_lines_may_stand_above_the_header():
    m = parse_migration(
        "/* why this exists */\n\n" + HEADER + "\n-- azsqlcd:data\nDELETE [t] WHERE 1 = 0;\n", FILE
    )
    assert (m.mode, m.batches[0].kind) == ("tx", "data")


@pytest.mark.parametrize(
    ("mode_line", "mode", "minutes"),
    [
        ("tx", "tx", None),
        ("nontx", "nontx", None),
        ("nontx expected-minutes: 40", "nontx", 40),
        ("nontx expected-minutes:7", "nontx", 7),
        ("tx expected-minutes: 3", "tx", 3),
    ],
)
def test_the_mode_directive_gives_mode_and_expected_minutes(mode_line, mode, minutes):
    m = parse_migration(
        f"-- azsqlcd:migration 0001__add_order_status\n-- azsqlcd:mode {mode_line}\nSELECT 1;\n", FILE
    )
    assert (m.mode, m.expected_minutes) == (mode, minutes)


@pytest.mark.parametrize(
    "mode_line",
    ["", "TX", "auto", "nontx expected-minutes: 0", "nontx expected-minutes: soon", "nontx 40", "tx tx"],
)
def test_a_mode_directive_outside_the_grammar_is_refused(mode_line):
    assert (
        invalid_line(f"-- azsqlcd:migration 0001__add_order_status\n-- azsqlcd:mode {mode_line}\nSELECT 1;\n")
        == 2
    )


def test_a_go_count_is_refused_because_no_batch_is_sent_twice():
    assert invalid_line(HEADER + "INSERT [t] DEFAULT VALUES;\nGO 2\n") == 1
    assert invalid_line(HEADER + "SELECT 1;\nGO\nINSERT [t] DEFAULT VALUES;\nGO 5 -- five rows\n") == 5
    assert len(parse_migration(HEADER + "SELECT 1;\nGO 1\nSELECT 2;\nGO\n", FILE).batches) == 2


def test_a_nontx_migration_is_exactly_one_batch():
    head = "-- azsqlcd:migration 0001__add_order_status\n-- azsqlcd:mode nontx expected-minutes: 40\n"
    one = head + "CREATE INDEX [IX] ON [sales].[Order] ([Status]) WITH (ONLINE = ON, RESUMABLE = ON);\nGO\n"
    assert len(parse_migration(one, FILE).batches) == 1
    assert invalid_line(one + "UPDATE STATISTICS [sales].[Order];\nGO\n") == 5


def test_an_allow_line_is_bound_to_the_batch_that_follows_it():
    text = (
        HEADER
        + "ALTER TABLE [sales].[Order] ADD [Status] tinyint NULL;\n"
        + "GO\n"
        + "-- azsqlcd:allow DROP_COLUMN [sales].[Order].[Stat] reason: unused since r30\n"
        + "ALTER TABLE [sales].[Order] DROP COLUMN [Stat];\n"
        + "GO\n"
        + "ALTER TABLE [sales].[Order] ADD [Note] nvarchar(50) NULL;\n"
    )
    m = parse_migration(text, FILE)
    allow = Allow("DROP_COLUMN", "[sales].[Order].[Stat]", "unused since r30", 5)
    assert [b.directives for b in m.batches] == [(), (allow,), ()]


def test_an_allow_line_with_no_statement_below_it_in_its_batch_is_refused():
    # written above GO it would belong to the batch before the one it is meant for
    text = (
        HEADER
        + "SELECT 1;\n-- azsqlcd:allow DROP_TABLE [dbo].[t] reason: obsolete\nGO\nDROP TABLE [dbo].[t];\n"
    )
    assert invalid_line(text) == 4


def test_an_allow_line_inside_a_data_batch_keeps_its_own_line():
    text = (
        HEADER
        + "-- azsqlcd:data\n"
        + "UPDATE [t] SET [a] = 1 WHERE [a] IS NULL;\n"
        + "-- azsqlcd:allow DATA_NO_WHERE [dbo].[t] reason: every row is in scope\n"
        + "UPDATE [t] SET [b] = 0;\n"
    )
    batch = parse_migration(text, FILE).batches[0]
    assert batch.directives == (Allow("DATA_NO_WHERE", "[dbo].[t]", "every row is in scope", 5),)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (
            "RENAME [sales].[Order Lines].[a]]b] reason: name of the API",
            ("RENAME", "[sales].[Order Lines].[a]]b]", "name of the API"),
        ),
        (
            "REPLACEMENT_EDGE 0003__status_not_null.sql reason: see #12",
            ("REPLACEMENT_EDGE", "0003__status_not_null.sql", "see #12"),
        ),
        (
            "RAW TABLE:[audit].[Log] reason: don't model, reason: temporal",
            ("RAW", "TABLE:[audit].[Log]", "don't model, reason: temporal"),
        ),
        ("OLD_MODULE [s].[reason: x] reason:r", ("OLD_MODULE", "[s].[reason: x]", "r")),
    ],
)
def test_an_allow_line_gives_code_object_and_reason(args, expected):
    batch = parse_migration(HEADER + f"-- azsqlcd:allow {args}\nSELECT 1;\n", FILE).batches[0]
    assert batch.directives == (Allow(*expected, 3),)


@pytest.mark.parametrize(
    "args",
    [
        "",
        "DROP_TABLE [dbo].[t]",
        "DROP_TABLE [dbo].[t] reason:",
        "DROP_TABLE [dbo].[t] reason:   ",
        "DROP_TABLE reason: no object",
        "drop_table [dbo].[t] reason: lower-case code",
        "[dbo].[t] reason: no code",
        "DROP_TABLE [dbo].[t] because: wrong word",
    ],
)
def test_an_allow_line_needs_code_object_and_a_reason_that_is_not_empty(args):
    assert invalid_line(HEADER + f"-- azsqlcd:allow {args}\nDROP TABLE [dbo].[t];\n") == 3


def test_a_directive_inside_a_string_or_a_block_comment_is_not_a_directive():
    text = (
        HEADER
        + "INSERT [dbo].[Doc] ([body]) VALUES (N'\n"
        + "-- azsqlcd:data\n"
        + "-- azsqlcd:allow TRUNCATE [dbo].[Doc] reason: not real\n"
        + "-- azsqlcd:no-such-directive\n"
        + "');\n"
        + "/*\n"
        + "-- azsqlcd:deploy-module [dbo].[fn_Tax]\n"
        + "*/\n"
    )
    batch = parse_migration(text, FILE).batches[0]
    assert (batch.kind, batch.directives) == ("model", ())


def test_the_data_directive_makes_a_data_batch_only_as_the_first_line():
    assert (
        parse_migration(HEADER + "SELECT 1;\nGO\n-- azsqlcd:data\nUPDATE [t] SET [a] = 1;\n", FILE)
        .batches[1]
        .kind
        == "data"
    )
    assert invalid_line(HEADER + "SELECT 1;\nGO\n-- why\n-- azsqlcd:data\nUPDATE [t] SET [a] = 1;\n") == 6
    assert (
        invalid_line(
            HEADER + "SELECT 1;\nGO\nUPDATE [t] SET [a] = 1;\n-- azsqlcd:data\nUPDATE [t] SET [a] = 2;\n"
        )
        == 6
    )
    assert (
        invalid_line(
            HEADER
            + "-- azsqlcd:allow TRUNCATE [dbo].[t] reason: x\n-- azsqlcd:data\nTRUNCATE TABLE [dbo].[t];\n"
        )
        == 4
    )
    assert invalid_line(HEADER + "-- azsqlcd:data\n-- azsqlcd:data\nUPDATE [t] SET [a] = 1;\n") == 4
    assert invalid_line(HEADER + "-- azsqlcd:data now\nUPDATE [t] SET [a] = 1;\n") == 3


def test_the_raw_directive_gives_the_object_key_and_the_reason():
    text = (
        HEADER
        + "-- azsqlcd:raw TABLE:[audit].[Log] reason: temporal table, not modelled\n"
        + "ALTER TABLE [audit].[Log] ADD [x] int NULL;\n"
    )
    batch = parse_migration(text, FILE).batches[0]
    assert (batch.kind, batch.raw_object, batch.raw_reason) == (
        "raw",
        "TABLE:[audit].[Log]",
        "temporal table, not modelled",
    )
    model = parse_migration(HEADER + "SELECT 1;\n", FILE).batches[0]
    assert (model.raw_object, model.raw_reason) == (None, None)


@pytest.mark.parametrize(
    "args",
    [
        "",
        "TABLE:[audit].[Log]",
        "TABLE:[audit].[Log] reason:",
        "[audit].[Log] reason: no kind",
        "THING:[a].[b] reason: x",
    ],
)
def test_a_raw_directive_needs_an_object_key_and_a_reason(args):
    assert invalid_line(HEADER + f"-- azsqlcd:raw {args}\nALTER TABLE [audit].[Log] ADD [x] int NULL;\n") == 3


def test_deploy_module_and_unbind_keep_the_two_part_name_and_their_order():
    text = (
        HEADER
        + "-- azsqlcd:unbind [sales].[vw_Open]\n"
        + "-- azsqlcd:unbind [sales].[vw ]]odd]\n"
        + "-- azsqlcd:deploy-module [dbo].[fn_Tax]\n"
        + "-- azsqlcd:allow DROP_COLUMN [sales].[Order].[Stat] reason: unused\n"
        + "ALTER TABLE [sales].[Order] DROP COLUMN [Stat];\n"
    )
    assert parse_migration(text, FILE).batches[0].directives == (
        Unbind("[sales].[vw_Open]", 3),
        Unbind("[sales].[vw ]]odd]", 4),
        DeployModule("[dbo].[fn_Tax]", 5),
        Allow("DROP_COLUMN", "[sales].[Order].[Stat]", "unused", 6),
    )


@pytest.mark.parametrize(
    "name",
    ["", "dbo.fn_Tax", "[fn_Tax]", "[db].[dbo].[fn_Tax]", "[dbo].[fn_Tax] now", "FUNCTION:[dbo].[fn_Tax]"],
)
@pytest.mark.parametrize("directive", ["deploy-module", "unbind"])
def test_deploy_module_and_unbind_need_a_bracketed_two_part_name(directive, name):
    assert invalid_line(HEADER + f"-- azsqlcd:{directive} {name}\nSELECT 1;\n") == 3


@pytest.mark.parametrize("directive", ["deploy-module", "unbind"])
def test_deploy_module_and_unbind_below_a_statement_are_refused_because_a_batch_is_sent_as_one_piece(
    directive,
):
    text = (
        HEADER
        + "-- azsqlcd:data\nUPDATE [t] SET [a] = 1;\n"
        + f"-- azsqlcd:{directive} [dbo].[m]\nUPDATE [t] SET [b] = 2;\n"
    )
    assert invalid_line(text) == 5


def test_an_unknown_directive_is_refused_so_a_typing_mistake_is_not_ignored():
    assert (
        invalid_line(HEADER + "-- azsqlcd:alow DROP_TABLE [dbo].[t] reason: x\nDROP TABLE [dbo].[t];\n") == 3
    )
    assert invalid_line(HEADER + "-- azsqlcd:after [dbo].[v]\nSELECT 1;\n") == 3


def test_a_directive_that_does_not_start_in_column_zero_is_refused():
    assert (
        invalid_line(HEADER + "  -- azsqlcd:allow DROP_TABLE [dbo].[t] reason: x\nDROP TABLE [dbo].[t];\n")
        == 3
    )
    assert (
        invalid_line(HEADER + "DROP TABLE [dbo].[t]; -- azsqlcd:allow DROP_TABLE [dbo].[t] reason: x\n") == 3
    )


def test_a_batch_with_no_statement_is_refused():
    assert invalid_line(HEADER + "GO\nSELECT 1;\n") == 1
    assert invalid_line(HEADER + "SELECT 1;\nGO\n-- the end\n") == 5


def test_text_that_cannot_be_tokenized_is_refused_with_its_line():
    assert invalid_line(HEADER + "SELECT 1;\nSELECT 'abc\n") == 4


def test_line_numbers_are_file_lines_also_with_windows_line_ends():
    text = (
        HEADER + "SELECT 1;\nGO\n\n-- azsqlcd:allow DROP_TABLE [dbo].[t] reason: x\nDROP TABLE [dbo].[t];\n"
    ).replace("\n", "\r\n")
    batch = parse_migration(text, FILE).batches[1]
    assert (batch.first_line, batch.directives[0].line) == (6, 6)


# ------------------------------------------------------------------ tombstones
TOMBSTONES = """
[[drop]]
object = "PROCEDURE:[sales].[usp_LegacyExport]"
reason = "replaced by usp_Export in r40"

[[drop]]
object = "VIEW:[sales].[vw ]]old]"
reason = "unused"
"""


def test_tombstones_are_read_in_file_order():
    assert parse_tombstones(TOMBSTONES) == [
        Tombstone("PROCEDURE:[sales].[usp_LegacyExport]", "replaced by usp_Export in r40"),
        Tombstone("VIEW:[sales].[vw ]]old]", "unused"),
    ]
    assert parse_tombstones("") == []
    assert parse_tombstones("# nothing dropped yet\n") == []


def test_a_tombstone_file_with_a_utf8_bom_or_crlf_line_ends_reads_the_same():
    # N4-07: Windows PowerShell 5.1 and older Notepad write a BOM, which tomllib refuses at line 1
    plain = parse_tombstones(TOMBSTONES)
    assert plain
    assert parse_tombstones("\ufeff" + TOMBSTONES) == plain
    assert parse_tombstones("\ufeff" + TOMBSTONES.replace("\n", "\r\n")) == plain
    assert parse_tombstones("\ufeff") == []
    refusal("TOMBSTONE_INVALID", parse_tombstones, "\ufeff\ufeff" + TOMBSTONES)  # one BOM only


@pytest.mark.parametrize(
    "text",
    [
        '[[drop]]\nobject = "PROCEDURE:[s].[p]"\nreason = ""\n',
        '[[drop]]\nobject = "PROCEDURE:[s].[p]"\nreason = "   "\n',
        '[[drop]]\nobject = "PROCEDURE:[s].[p]"\n',
        '[[drop]]\nreason = "gone"\n',
        '[[drop]]\nobject = "PROCEDURE:[s].[p]"\nreason = 5\n',
        '[[drop]]\nobject = "TABLE:[s].[t]"\nreason = "tables are dropped by a migration"\n',
        '[[drop]]\nobject = "SCHEMA:[s]"\nreason = "not a module"\n',
        '[[drop]]\nobject = "[s].[p]"\nreason = "no kind"\n',
        '[[drop]]\nobject = "PROCEDURE:s.p"\nreason = "not bracketed"\n',
        '[[drop]]\nobject = "PROCEDURE:[s].[p]"\nreason = "x"\nforce = true\n',
        '[drop]\nobject = "PROCEDURE:[s].[p]"\nreason = "a table, not an array of tables"\n',
        'drop = ["PROCEDURE:[s].[p]"]\n',
        '[[remove]]\nobject = "PROCEDURE:[s].[p]"\nreason = "wrong table name"\n',
        "[[drop]\n",
    ],
)
def test_a_tombstone_file_outside_the_rules_is_refused(text):
    refusal("TOMBSTONE_INVALID", parse_tombstones, text)


def test_a_module_cannot_have_two_tombstones():
    one = '[[drop]]\nobject = "VIEW:[s].[v]"\nreason = "first"\n'
    refusal("TOMBSTONE_INVALID", parse_tombstones, one + one.replace("first", "second"))


@pytest.mark.parametrize(
    ("body", "line", "why"),
    [
        ("-- azsqlcd:data\nSELECT 1;\nSELECT 1eDELETE FROM [dbo].[T];", 5, "space"),  # LB-04
        ("-- azsqlcd:data\nDELETE FROM [dbo].[T] --\x00\nWHERE [id] = 1;", 4, "NUL"),  # LB-08
        ("-- azsqlcd:data\nDELETE FROM [dbo].[T] -- x\u2028WHERE [id] = 1;", 4, "U+2028"),
        ("-- azsqlcd:data\nSELECT 1\nDEL\\\nETE FROM [dbo].[T];", 5, "line continuation"),
        ("-- azsqlcd:data\nUPDATE \uff34 SET [a] = 1;", 4, "brackets"),
    ],
)
def test_text_that_the_engine_could_split_in_another_way_is_refused_before_any_rule_reads_it(body, line, why):
    """The plan and the deploy read a migration through parse_migration too, so the lexer is the
    second gate behind lint for text that the engine and the lint rules could read differently."""
    text = f"-- azsqlcd:migration 0001__change\n-- azsqlcd:mode tx\n{body}\n"
    with pytest.raises(ToolError) as e:
        parse_migration(text, "0001__change.sql")
    assert (e.value.reason_code, e.value.detail["line"]) == ("MIGRATION_INVALID", line)
    assert why in e.value.message


@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("0001__a.sql", True),
        ("12345__add_Order_2.sql", True),
        ("001__a.sql", False),
        ("0001_a.sql", False),
        ("0001__a.SQL", False),
        ("../0001__a.sql", False),
        ("0001__a b.sql", False),
        ("0001__" + "a" * 200 + ".sql", False),
    ],
)
def test_is_migration_file_is_the_rule_of_the_chain_for_a_migration_id(name, valid):
    assert is_migration_file(name) is valid


# ------------------------------------------------------------------ review 2: messages that name the fix
def test_a_number_that_does_not_rise_names_the_command_that_renumbers():
    """CU-06. Two equal numbers are the usual state after a merge of main; gen --resum repairs it."""
    text = f"azsqlcd-sum 1\n0002__a.sql sha256:{A} tx\n0002__b.sql sha256:{B} tx\n"
    error = refusal("CHAIN_INVALID", parse_sum, text)
    assert error.detail["line"] == 3
    assert "not higher than every earlier one" in error.message
    assert "azsqlcd gen --resum" in error.message


def test_a_wrong_tombstone_carries_the_line_of_its_drop_table():
    """CU-08. Every problem of a [[drop]] was reported for the whole file."""
    wrong = '[[drop]]\nobject = "sales.usp_CancelOrder"\nreason = "x"\n'
    error = refusal(
        "TOMBSTONE_INVALID", parse_tombstones, "# header\r\n" + TOMBSTONES.replace("\n", "\r\n") + wrong
    )
    assert error.detail["line"] == 10  # header, blank, two tables of three lines with a blank between
    assert "PROCEDURE:[sales].[usp_x]" in error.message
    # the same module twice: the line of the second table
    one = '[[drop]]\nobject = "VIEW:[s].[v]"\nreason = "first"\n'
    assert refusal("TOMBSTONE_INVALID", parse_tombstones, one + "\n" + one).detail["line"] == 5
    # [[drop]] inside a string or a comment is no table
    tricky = (
        '# [[drop]]\n[[drop]]\nobject = "VIEW:[s].[v]"\nreason = """\n[[drop]]\n"""\n\n'
        '[[drop]]\nobject = 5\nreason = "x"\n'
    )
    assert refusal("TOMBSTONE_INVALID", parse_tombstones, tricky).detail["line"] == 8
    # a file that is not TOML: the line of the TOML reader
    assert refusal("TOMBSTONE_INVALID", parse_tombstones, "[[drop]]\nobject = \n").detail["line"] == 2
