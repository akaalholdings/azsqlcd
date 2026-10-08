"""Engine errors: number from text, class, redaction.

The driver gives message text only. Every decision that follows an error (retry the connect,
exit code, hint) hangs on these functions, so the texts here have the shape that the driver
really produces: "[Microsoft][SQL Server]" + the en-US engine message.
"""

import json
from pathlib import Path

import pytest

from azsqlcd.sqlerrors import (
    GOVERNANCE_NUMBERS,
    TRANSIENT_CONNECT_NUMBERS,
    ErrorClass,
    SignInFilter,
    SqlError,
    classify,
    known_number,
    names_written,
    parse_number,
    redact,
    sql_error,
)

ODBC = "[Microsoft][SQL Server]"

# number -> engine message as documented by Microsoft (en-US), with realistic values
ENGINE_TEXT = {
    208: "Invalid object name 'sales.Ordr'.",
    2714: "There is already an object named 'Order' in the database.",
    2020: 'The dependencies reported for entity "sales.vw_OpenOrders" might not include references to all '
    "columns. This is either because the entity references an object that does not exist or because "
    "of an error in one or more statements in the entity.",
    1222: "Lock request time out period exceeded.",
    1205: "Transaction (Process ID 57) was deadlocked on lock resources with another process and has "
    "been chosen as the deadlock victim. Rerun the transaction.",
    4060: 'Cannot open database "sales" requested by the login. The login failed.',
    40613: "Database 'sales' on server 'contoso-sql' is not currently available.  Please retry the "
    "connection later.  If the problem persists, contact customer support, and provide them the "
    "session tracing ID of '{8C1A6B0E-0D4B-4C55-9A2B-3F6C1E7D9A10}'.",
    40197: "The service has encountered an error processing your request. Please try again. Error code 4221.",
    40501: "The service is currently busy. Retry the request after 10 seconds. Incident ID: "
    "{2D6C3F1A-7B21-4E0C-9C55-0A1B2C3D4E5F}. Code: 131.",
    49918: "Cannot process request. Not enough resources to process request. Please retry you request later.",
    49919: "Cannot process create or update request. Too many create or update operations in progress "
    'for subscription "6f1c0d2e-0000-4000-8000-1234567890ab".',
    49920: "Cannot process request. Too many operations in progress for subscription "
    '"6f1c0d2e-0000-4000-8000-1234567890ab".',
    10928: "Resource ID: 1. The request limit for the database is 90 and has been reached. See "
    "'http://go.microsoft.com/fwlink/?LinkId=267637' for assistance.",
    10929: "Resource ID: 1. The request minimum guarantee is 0, maximum limit is 90 and the current "
    "usage for the database is 0. However, the server is currently too busy to support requests "
    "greater than 0 for this database.",
    40552: "The session has been terminated because of excessive transaction log space usage. Try "
    "modifying fewer rows in a single transaction.",
    9002: "The transaction log for database 'sales' is full due to 'ACTIVE_TRANSACTION'.",
    40549: "Session is terminated because you have a long-running transaction. Try shortening your "
    "transaction.",
    40550: "The session has been terminated because it has acquired too many locks. Try reading or "
    "modifying fewer rows in a single transaction.",
    40551: "The session has been terminated because of excessive TEMPDB usage. Try modifying your "
    "query to reduce the temporary table space usage.",
    40544: "The database 'sales' has reached its size quota. Partition or delete data, drop indexes, "
    "or consult the documentation for possible resolutions.",
}


# ------------------------------------------------------------------ number
@pytest.mark.parametrize(
    ("text", "number"),
    [
        # mssql-python: one diagnostic record as cursor.messages holds it ("[SQLSTATE] (native)")
        ("[42000] (1222)", 1222),
        # pyodbc
        (
            "[42000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Lock request time out period "
            "exceeded. (1222) (SQLExecDirectW)",
            1222,
        ),
        # pyodbc, two records: the first one is the error
        (
            "[23000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Violation of PRIMARY KEY "
            "constraint 'PK_t'. (2627) (SQLExecDirectW); [01000] [Microsoft][ODBC Driver 18 for SQL "
            "Server][SQL Server]The statement has been terminated. (3621)",
            2627,
        ),
        # an ODBC message that ends with the native error
        ("[Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Invalid object name 'x'. (208)", 208),
        # sqlcmd and SSMS
        ("Msg 1205, Level 13, State 51, Line 3\nTransaction (Process ID 57) was deadlocked", 1205),
        ("msg 40613, level 17, state 1", 40613),
    ],
)
def test_a_number_written_in_the_text_is_read_in_every_usual_shape(text, number):
    assert parse_number(text) == number


@pytest.mark.parametrize("number", [208, 2714, 1222, 1205, 40613])
def test_the_text_that_the_driver_really_raises_has_no_number_and_is_still_recognised(number):
    raised = f"Driver Error: Syntax error or access violation; DDBC Error: {ODBC}{ENGINE_TEXT[number]}"
    assert parse_number(raised) == number


def test_a_written_number_wins_over_the_recognised_message():
    # a script that raises 50000 with the text of a lock timeout is not a lock timeout
    assert parse_number(f"{ODBC}Lock request time out period exceeded. (50000) (SQLExecDirectW)") == 50000


def test_a_sub_code_inside_the_message_is_not_the_error_number():
    assert parse_number(ODBC + ENGINE_TEXT[40197]) == 40197
    assert parse_number(ODBC + ENGINE_TEXT[40501]) == 40501


def test_native_error_zero_is_no_number():
    assert parse_number("[08S01] (0)") is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        f"{ODBC}Something that this tool has never seen.",
        f"{ODBC}Could not find stored procedure 'dbo.usp_x'.",
        f"{ODBC}Row 1205 of 40613 failed.",
    ],
)
def test_text_without_a_number_and_without_a_known_message_gives_no_number(text):
    assert parse_number(text) is None


@pytest.mark.parametrize("number", sorted(ENGINE_TEXT))
def test_every_engine_message_that_drives_a_decision_is_recognised(number):
    assert known_number(ODBC + ENGINE_TEXT[number]) == number


# ------------------------------------------------------------------ texts of the live spike
# What mssql-python 1.15.0 returned from Azure SQL Database (live spike, 2026-10-07). error_texts.json
# is the capture of the last full run as the spike wrote it; error_texts_items.json holds the texts
# that only the item outputs hold. label -> text, sqlstate, expected_number (the number that the tool
# must read from the text, for the driver gives none; null where the spike asked for no number: a
# syntax error, a divide by zero, a RAISERROR of a script), class, redacted.
LIVE = Path(__file__).resolve().parents[1] / "fixtures" / "live"
LIVE_ERRORS: dict[str, dict] = {
    **json.loads((LIVE / "error_texts.json").read_text(encoding="utf-8")),
    **json.loads((LIVE / "error_texts_items.json").read_text(encoding="utf-8")),
}
# The numbers that the spike named and asked for; each has a captured text. 4060 has none: a token
# connect to a database that does not exist gave the text of 18456 (engine fact E4).
LIVE_NUMBERS = {207, 208, 245, 1205, 1222, 2020, 2601, 2627, 2714, 3729, 3998, 18456}
# session.py gives this class by the text of the ODBC layer (T5); sqlerrors sees no SQLSTATE and no number
LOST_WHILE_WAITING = "session killed while the client waits for a batch"


def test_the_live_fixture_holds_a_text_for_every_number_of_the_spike():
    assert len(LIVE_ERRORS) == 32
    assert {case["expected_number"] for case in LIVE_ERRORS.values()} - {None} == LIVE_NUMBERS
    assert all(case["text"].startswith("[Microsoft]") for case in LIVE_ERRORS.values())


@pytest.mark.parametrize("label", sorted(LIVE_ERRORS))
def test_a_text_that_the_live_driver_returned_gives_its_number_and_class(label):
    # The driver gives no number: a text that is not recognised makes a lock timeout exit 21 and
    # hides a dependant that does not bind (207 before 2020).
    case = LIVE_ERRORS[label]
    text, number = case["text"], case.get("number", case["expected_number"])
    expected_class = ErrorClass.OTHER if label == LOST_WHILE_WAITING else ErrorClass[case["class"]]

    assert known_number(text) == number
    assert parse_number(text) == number
    error = sql_error(text, sqlstate=case["sqlstate"])
    assert (error.number, error.cls, error.sqlstate) == (number, expected_class, case["sqlstate"])
    if case["expected_number"] is not None:
        assert number == case["expected_number"]


# The live texts whose quoted parts are names of objects that exist in the database (2714, 5074)
NAMES_OF_THE_CATALOG = ("There is already an object named '", "The object '")
# The live texts whose quoted part is a name as the statement wrote it (207, 208). With no batch to
# compare it with, the part is redacted: dynamic SQL can put a row value there.
NAMES_OF_THE_STATEMENT = ("Invalid column name '", "Invalid object name '")


@pytest.mark.parametrize("label", sorted(LIVE_ERRORS))
def test_a_text_that_the_live_driver_returned_is_stored_without_its_values(label):
    case = LIVE_ERRORS[label]
    stored = redact(case["text"])
    assert stored == case["redacted"] == sql_error(case["text"]).message
    assert len(stored) <= 300
    if case["text"].removeprefix(ODBC).startswith(NAMES_OF_THE_CATALOG):
        assert stored == case["text"]  # the names are what the reader of a failed deploy needs
        return
    for value in ("azsqlcd_spike", "x1_", "l11_", "l15_", "l2_", "x3_", "'abc'", "0001__a.sql", "Process ID"):
        assert value not in stored
    assert "'" not in stored and '"' not in stored


@pytest.mark.parametrize(
    "label",
    sorted(
        k for k, c in LIVE_ERRORS.items() if c["text"].removeprefix(ODBC).startswith(NAMES_OF_THE_STATEMENT)
    ),
)
def test_a_live_text_of_207_or_208_keeps_its_name_for_a_batch_that_wrote_the_name(label):
    text = LIVE_ERRORS[label]["text"]
    name = text[text.index("'") + 1 : text.rindex("'")]
    wrote = f"SELECT [{'].['.join(name.split('.'))}] FROM sales.Customer;"
    assert redact(text, names_written(wrote)) == text
    assert R in redact(text, names_written("EXEC sys.sp_refreshsqlmodule N'sales.vw_Open';"))


def test_the_live_fixture_holds_texts_that_keep_their_names_and_texts_that_lose_their_values():
    live = LIVE_ERRORS.values()
    kept = [c for c in live if c["text"].removeprefix(ODBC).startswith(NAMES_OF_THE_CATALOG)]
    assert len(kept) == 2 and all(c["redacted"] == c["text"] for c in kept)
    of_the_statement = [c for c in live if c["text"].removeprefix(ODBC).startswith(NAMES_OF_THE_STATEMENT)]
    assert len(of_the_statement) == 11 and all(R in c["redacted"] for c in of_the_statement)
    assert sum(R in c["redacted"] for c in live) == 23


def test_the_raw_driver_text_with_its_label_gives_the_same_number():
    # str() of the driver exception: "Driver Error: <label>; DDBC Error: <text>" (probe of the spike)
    raised = "Driver Error: Column not found; DDBC Error: [Microsoft][SQL Server]Invalid column name 'b'."
    assert parse_number(raised) == 207


def test_a_login_that_fails_for_a_database_that_does_not_exist_is_not_transient():
    # live spike X1: a token connect to a missing database gives the text of 18456, not of 4060.
    # One attempt, no retry for 180 s.
    case = LIVE_ERRORS["18456 connect to a database that does not exist"]
    error = sql_error(case["text"], at_connect=True)
    assert (error.number, error.cls) == (18456, ErrorClass.OTHER)


def test_a_message_that_holds_the_text_of_4060_and_of_a_failed_login_is_4060():
    # 4060 is transient at connect and 18456 is not: the text of 18456 must not hide the text of 4060
    text = f"{ODBC}{ENGINE_TEXT[4060]} Login failed for user 'deploy'."
    error = sql_error(text, at_connect=True)
    assert (error.number, error.cls) == (4060, ErrorClass.TRANSIENT_CONNECT)


@pytest.mark.parametrize(
    "text",
    [
        # near texts that are other errors: 2628 and 8152, 213, 4104, 15151, 18452, 911
        "String or binary data would be truncated in table 'sales.dbo.Customer', column 'Name'.",
        "Column name or number of supplied values does not match table definition.",
        'The multi-part identifier "o.Total" could not be bound.',
        "Cannot alter the view 'sales.vw_x', because it does not exist or you do not have permission.",
        "Login failed. The login is from an untrusted domain and cannot be used with Integrated "
        "authentication.",
        "Database 'sales' does not exist. Make sure that the name is entered correctly.",
        "Incorrect syntax near 'ONLINE'.",
        "The transaction is rolled back.",
    ],
)
def test_a_text_that_is_near_a_live_text_is_not_taken_for_it(text):
    assert known_number(ODBC + text) is None


def test_every_classified_number_has_a_recognised_message():
    # without this, a class could exist that the real driver can never produce
    assert (TRANSIENT_CONNECT_NUMBERS | GOVERNANCE_NUMBERS | {1222, 1205}) <= set(ENGINE_TEXT)


def test_a_known_message_is_recognised_across_line_breaks_and_letter_case():
    assert known_number("LOCK REQUEST TIME OUT\r\n period exceeded.") == 1222


# ------------------------------------------------------------------ class
def test_the_transient_and_governance_numbers_are_the_lists_of_the_design():
    assert TRANSIENT_CONNECT_NUMBERS == {40613, 40197, 40501, 49918, 49919, 49920, 10928, 10929, 4060}
    assert GOVERNANCE_NUMBERS == {40552, 9002, 40549, 40550, 40551, 40544}


@pytest.mark.parametrize("number", sorted(TRANSIENT_CONNECT_NUMBERS))
def test_a_transient_number_is_transient_only_at_connect(number):
    assert classify(number, None, "", at_connect=True) is ErrorClass.TRANSIENT_CONNECT
    assert classify(number, None, "", at_connect=False) is ErrorClass.OTHER


@pytest.mark.parametrize("number", sorted(GOVERNANCE_NUMBERS))
def test_a_governance_error_is_never_transient(number):
    assert classify(number, None, "", at_connect=True) is ErrorClass.GOVERNANCE
    assert classify(number, None, "", at_connect=False) is ErrorClass.GOVERNANCE


def test_lock_timeout_and_deadlock_have_their_own_class():
    assert classify(1222, None, "", at_connect=False) is ErrorClass.LOCK_TIMEOUT
    assert classify(1205, "40001", "", at_connect=False) is ErrorClass.DEADLOCK


@pytest.mark.parametrize("sqlstate", ["08S01", "08001", "08003", "08007", "40003"])
def test_a_connection_sqlstate_means_the_session_is_lost(sqlstate):
    assert classify(None, sqlstate, "Communication link failure", at_connect=False) is ErrorClass.SESSION_LOST


def test_a_governance_number_wins_over_the_lost_connection_that_comes_with_it():
    assert classify(40552, "08S01", "", at_connect=False) is ErrorClass.GOVERNANCE


@pytest.mark.parametrize("sqlstate", [None, "42000", "23000", "HY000", "40001", "S0002"])
def test_unknown_error_text_is_not_transient(sqlstate):
    for at_connect in (True, False):
        assert (
            classify(None, sqlstate, "Something new went wrong.", at_connect=at_connect) is ErrorClass.OTHER
        )


def test_an_unknown_number_is_not_transient():
    assert classify(
        18456, "28000", "Login failed for user '<token-identified principal>'.", at_connect=True
    ) is (ErrorClass.OTHER)


def test_without_a_number_the_class_comes_from_the_recognised_message():
    text = ODBC + ENGINE_TEXT[40613]
    assert classify(None, "HY000", text, at_connect=True) is ErrorClass.TRANSIENT_CONNECT
    assert classify(None, None, ODBC + ENGINE_TEXT[1222], at_connect=False) is ErrorClass.LOCK_TIMEOUT
    assert classify(None, None, ODBC + ENGINE_TEXT[9002], at_connect=True) is ErrorClass.GOVERNANCE


def test_a_given_number_wins_over_the_message_text():
    assert classify(50000, None, ODBC + ENGINE_TEXT[1222], at_connect=False) is ErrorClass.OTHER


# ------------------------------------------------------------------ redaction (A25)
R = "<redacted>"


@pytest.mark.parametrize(
    ("number", "text", "stored"),
    [
        (
            245,
            "Conversion failed when converting the varchar value 'ACME Ltd' to data type int.",
            f"Conversion failed when converting the varchar value {R} to data type int.",
        ),
        (
            547,
            'The INSERT statement conflicted with the FOREIGN KEY constraint "FK_Order_Customer". The '
            'conflict occurred in database "sales", table "dbo.Customer", column \'CustomerId\'.',
            f"The INSERT statement conflicted with the FOREIGN KEY constraint {R}. The conflict occurred "
            f"in database {R}, table {R}, column {R}.",
        ),
        (
            1205,
            ENGINE_TEXT[1205],
            f"Transaction {R} was deadlocked on lock resources with another process and has been chosen "
            "as the deadlock victim. Rerun the transaction.",
        ),
        (1222, ENGINE_TEXT[1222], "Lock request time out period exceeded."),
        (
            2601,
            "Cannot insert duplicate key row in object 'sales.Customer' with unique index "
            "'UX_Customer_Email'. The duplicate key value is (jane.doe@example.com).",
            f"Cannot insert duplicate key row in object {R} with unique index {R}. The duplicate key "
            f"value is {R}",
        ),
        (
            2627,
            "Violation of PRIMARY KEY constraint 'PK_Customer'. Cannot insert duplicate key in object "
            "'sales.Customer'. The duplicate key value is (4711, O'Neil (Dublin), 2).",
            f"Violation of PRIMARY KEY constraint {R}. Cannot insert duplicate key in object {R}. The "
            f"duplicate key value is {R}",
        ),
        (
            2628,
            "String or binary data would be truncated in table 'sales.dbo.Customer', column 'Name'. "
            "Truncated value: 'Jane Doe-Smith of Lond'.",
            f"String or binary data would be truncated in table {R}, column {R}. Truncated value: {R}",
        ),
        (8152, "String or binary data would be truncated.", "String or binary data would be truncated."),
        (
            8114,
            "Error converting data type varchar to numeric.",
            "Error converting data type varchar to numeric.",
        ),
        (
            515,
            "Cannot insert the value NULL into column 'Status', table 'sales.dbo.Order'; column does not "
            "allow nulls. INSERT fails.",
            f"Cannot insert the value NULL into column {R}, table {R}; column does not allow nulls. "
            "INSERT fails.",
        ),
        (
            547,
            'The ALTER TABLE statement conflicted with the CHECK constraint "CK_Order_Total". The conflict '
            'occurred in database "sales", table "sales.Order", column \'Total\'.',
            f"The ALTER TABLE statement conflicted with the CHECK constraint {R}. The conflict occurred in "
            f"database {R}, table {R}, column {R}.",
        ),
        (
            40613,
            ENGINE_TEXT[40613],
            f"Database {R} on server {R} is not currently available. Please retry the connection later. "
            "If the problem persists, contact customer support, and provide them the session tracing "
            f"ID of {R}.",
        ),
    ],
)
def test_an_engine_message_is_stored_without_its_values(number, text, stored):
    assert redact(ODBC + text) == ODBC + stored


def test_a_duplicate_key_message_is_stored_without_the_key_value():
    raw = (
        f"{ODBC}Violation of UNIQUE KEY constraint 'UQ_Patient_NhsNo'. Cannot insert duplicate key in "
        "object 'dbo.Patient'. The duplicate key value is (943 476 5919, Smith)."
    )
    error = SqlError(raw, number=2627, sqlstate="23000")
    for shown in (error.message, str(error), repr(error), " ".join(map(str, error.args))):
        assert "943 476 5919" not in shown
        assert "Smith" not in shown
        assert "UQ_Patient_NhsNo" not in shown
    assert error.message.endswith(f"The duplicate key value is {R}")
    assert error.raw_message == raw  # kept for --show-error-text


def test_a_value_with_an_apostrophe_is_hidden_whole():
    text = "Conversion failed when converting the nvarchar value 'O'Neil' to data type int."
    assert redact(text) == f"Conversion failed when converting the nvarchar value {R} to data type int."


def test_a_value_that_the_driver_cut_before_its_closing_quote_is_hidden_to_the_end():
    assert redact("Cannot find the object 'dbo.Customer_with_a_very_long_na") == f"Cannot find the object {R}"
    assert redact('Cannot open database "sal') == f"Cannot open database {R}"
    assert redact("Check values (1, 'a', (2") == f"Check values {R}"


def test_nested_parentheses_are_hidden_as_one_value():
    assert redact("Bad rows (id (42), name (x)) found.") == f"Bad rows {R} found."


def test_the_stored_text_is_one_line_of_at_most_300_characters():
    stored = redact("Line one.\r\nLine two.\n" + "x" * 500)
    assert stored.startswith("Line one. Line two. xxx")
    assert len(stored) == 300


def test_the_cut_never_brings_a_value_back():
    stored = redact("a" * 290 + " value 'secret-secret-secret'")
    assert "secret" not in stored
    assert len(stored) <= 300


def test_text_with_nothing_to_hide_passes_unchanged():
    assert (
        redact(f"{ODBC}Lock request time out period exceeded.")
        == f"{ODBC}Lock request time out period exceeded."
    )


# ------------------------------------------------------------------ messages of names only (pilot)
# (number, the en-US message with neutral names, a batch that wrote the names). Batch None: every
# quoted part of the message is the name of an object or a column that exists in the database, so
# the names stay whatever was sent. With a batch: a quoted part is a name as the statement wrote
# it. It stays only when the batch that was sent holds it as an identifier: a statement that
# dynamic SQL built from data puts a row value into that part.
LONG_NAME = "IX_" + "n" * 125  # 128 characters, the longest name of the engine
ADD_COLUMN = "ALTER TABLE [sales].[Order] ADD [Status] int NULL, [Status] int NULL;"
NAME_MESSAGES = [
    (207, "Invalid column name 'CustId'.", "UPDATE sales.[Order] SET CustId = 1;"),
    (207, "Invalid column name 'Customer Id'.", "SELECT [Customer Id] FROM sales.[Order];"),
    (208, "Invalid object name 'sales.Ordr'.", "SELECT 1 FROM sales.Ordr;"),
    (208, "Invalid object name 'srv.db.sales.Order'.", 'SELECT 1 FROM "srv".db.[sales].[Order];'),
    (208, "Invalid object name '#stage'.", "INSERT INTO #stage SELECT 1;"),
    (2714, "There is already an object named 'IX_Order_Cust' in the database.", None),
    (3701, "Cannot drop the table 'sales.Order', because it does not exist or you do not have permission.",
     "DROP TABLE [sales].[Order];"),
    (3701, "Cannot drop the index 'sales.Order.IX_Order_Cust', because it does not exist or you do not "
     "have permission.", "DROP INDEX [IX_Order_Cust] ON [sales].[Order];"),
    (3701, "Cannot drop the sequence object 'sales.OrderNo', because it does not exist or you do not "
     "have permission.", "DROP SEQUENCE sales.OrderNo;"),
    (3726, "Could not drop object 'sales.Customer' because it is referenced by a FOREIGN KEY constraint.",
     None),
    (4902, 'Cannot find the object "sales.Order" because it does not exist or you do not have permissions.',
     "ALTER TABLE sales.[Order] ADD c int NULL;"),
    (5074, "The index 'IX_Order_Cust' is dependent on column 'CustId'.", None),
    (5074, "The object 'DF_Order_Status' is dependent on column 'Status'.", None),
    (5074, "The statistics 'ST_Order_Cust' is dependent on column 'CustId'.", None),
    (1913, "The operation failed because an index or statistics with name 'IX_Order_Cust' already exists "
     "on table 'sales.Order'.", None),
    (2705, "Column names in each table must be unique. Column name 'Status' in table 'sales.Order' is "
     "specified more than once.", ADD_COLUMN),
    (2714, f"There is already an object named '{LONG_NAME}' in the database.", None),
]  # fmt: skip
PREFIXES = ["", ODBC, "[Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"]


class _EveryName:
    """The names of a batch that wrote every name: a test of the form of a message, not of its names."""

    def __contains__(self, name: object) -> bool:
        return True


EVERY_NAME = _EveryName()


@pytest.mark.parametrize(
    ("number", "text", "batch"),
    NAME_MESSAGES,
    ids=[f"{number}-{n}" for n, (number, _, _) in enumerate(NAME_MESSAGES)],
)
@pytest.mark.parametrize("prefix", PREFIXES, ids=["engine text", "driver", "odbc driver"])
def test_a_message_whose_quoted_parts_are_always_names_is_stored_with_its_names(prefix, number, text, batch):
    """The first pilot: 'The index <redacted> is dependent on column <redacted>' named nothing that
    the reader of a failed deploy could act on."""
    written = names_written(batch or "SELECT 1;")
    assert redact(prefix + text, written) == prefix + text
    assert SqlError(prefix + text).for_batch(batch or "SELECT 1;").message == prefix + text
    assert redact(prefix + text + "\r\n", written) == prefix + text  # white space is no second message
    if batch is None:  # names of the catalog: no batch is needed
        assert redact(prefix + text) == prefix + text == SqlError(prefix + text).message


STATEMENT_NAMES = [case for case in NAME_MESSAGES if case[2] is not None]


@pytest.mark.parametrize(
    ("number", "text", "batch"),
    STATEMENT_NAMES,
    ids=[f"{number}-{n}" for n, (number, _, _) in enumerate(STATEMENT_NAMES)],
)
@pytest.mark.parametrize(
    "sent",
    [
        None,  # no batch is known: an error of a connect, or a session that does not say
        "SELECT 1;",
        # dynamic SQL: the name of the message is not in the text that was sent
        "DECLARE @v nvarchar(100) = (SELECT TOP (1) Name FROM sales.Customer);\n"
        "EXEC (N'UPDATE sales.Customer SET Name = \"' + @v + N'\"');",
        "EXEC sys.sp_refreshsqlmodule N'sales.vw_Open';",
    ],
    ids=["no batch", "another batch", "dynamic sql", "refresh"],
)
def test_a_name_that_the_statement_wrote_is_redacted_when_the_batch_that_was_sent_does_not_hold_it(
    number, text, batch, sent
):
    """207, 208, 2705, 3701 and 4902 print a name as the statement wrote it. A statement that
    dynamic SQL builds from data puts a row value there: under QUOTED_IDENTIFIER ON,
    SET c = "<value>" gives Invalid column name '<value>'."""
    stored = redact(text) if sent is None else redact(text, names_written(sent))
    assert "'" not in stored and '"' not in stored and R in stored
    error = SqlError(ODBC + text, number=number)
    if sent is not None:
        error = error.for_batch(sent)
    assert error.message == ODBC + stored == error.args[0] and error.raw_message == ODBC + text
    assert (error.number, str(error)) == (number, f"[OTHER {number}] {ODBC}{stored}")


@pytest.mark.parametrize(
    ("text", "sent"),
    [
        # the value stands in a string of the batch: a string is not a name
        ("Invalid column name 'S3CR3TVALUE'.", "EXEC (N'UPDATE sales.Customer SET Name = \"S3CR3TVALUE\"');"),
        ("Invalid column name 'S3CR3TVALUE'.", "SELECT 1; -- S3CR3TVALUE\n"),
        ("Invalid column name 'S3CR3TVALUE'.", "SELECT 1 /* [S3CR3TVALUE] */;"),
        # one part of the name is written, the other is not
        ("Invalid object name 'sales.S3CR3TVALUE'.", "SELECT 1 FROM sales.Customer;"),
        ("Column names in each table must be unique. Column name 'S3CR3TVALUE' in table 'sales.Order' is "
         "specified more than once.", ADD_COLUMN),
        # another letter case is another text: the engine prints the name as the statement wrote it
        ("Invalid column name 'CUSTID'.", "UPDATE sales.[Order] SET CustId = 1;"),
        # a batch that cannot be read into tokens gives no names
        ("Invalid column name 'CustId'.", "SELECT CustId, N'never closed"),
    ],
)  # fmt: skip
def test_a_value_in_the_place_of_a_name_is_redacted_whatever_else_the_batch_holds(text, sent):
    stored = redact(ODBC + text, names_written(sent))
    assert "S3CR3TVALUE" not in stored and "CUSTID" not in stored and "'CustId'" not in stored
    assert R in stored


def test_the_names_that_a_batch_wrote_are_its_identifiers_and_nothing_of_a_string_or_a_comment():
    batch = (
        'ALTER TABLE [sales].[Order Line] ADD "Net Total" int NULL; -- note\n'
        "SELECT N'text', #stage.c FROM #stage;"
    )
    assert {"sales", "Order Line", "Net Total", "#stage", "c"} <= names_written(batch)
    assert not {"text", "note", "N'text'"} & names_written(batch)
    assert names_written("") == frozenset() == names_written("SELECT 'never closed")


@pytest.mark.parametrize(
    "character",
    ["\x80", "\x85", "\x9b", "\x9f", "\u200b", "\u200e", "\u200f", "\u202a", "\u202e", "\u2066", "\u2069",
     "\ufeff", "\u061c", "\u2028", "\u2029"],
)  # fmt: skip
def test_a_name_with_a_control_or_a_format_character_is_not_kept(character):
    """C1 controls (U+009B starts a terminal sequence) and the characters that change the order
    in which a line is shown (U+202E) have no place in a name that is printed."""
    name = f"IX{character}Order"
    for text in (
        f"There is already an object named '{name}' in the database.",
        f"The index '{name}' is dependent on column 'CustId'.",
        f"Invalid column name '{name}'.",
    ):
        stored = redact(ODBC + text, names_written(f"SELECT [{name}], CustId;"))
        assert character not in stored and R in stored


def test_a_message_without_quotes_passes_with_its_name_as_before():
    # 4922, the second message of a refused ALTER COLUMN: it has no quoted part, so nothing was hidden
    text = f"{ODBC}ALTER TABLE ALTER COLUMN CustId failed because one or more objects access this column."
    assert redact(text) == text


@pytest.mark.parametrize(
    ("text", "stored"),
    [
        # two messages in one text
        (
            "Invalid column name 'CustId'. Invalid column name 'Status'.",
            f"Invalid column name {R}. Invalid column name {R}.",
        ),
        (
            "The index 'IX_Order_Cust' is dependent on column 'CustId'. ALTER TABLE ALTER COLUMN CustId "
            "failed because one or more objects access this column.",
            f"The index {R} is dependent on column {R}. ALTER TABLE ALTER COLUMN CustId failed because "
            "one or more objects access this column.",
        ),
        (
            "There is already an object named 'Order' in the database. The duplicate key value is (4711).",
            f"There is already an object named {R} in the database. The duplicate key value is {R}",
        ),
        (
            "Invalid object name 'sales.Ordr'.\nInvalid object name 'sales.Custmer'.",
            f"Invalid object name {R}. Invalid object name {R}.",
        ),
        # text before or after the message
        (
            "Driver Error: Column not found; DDBC Error: Invalid column name 'CustId'.",
            f"Driver Error: Column not found; DDBC Error: Invalid column name {R}.",
        ),
        ("Invalid column name 'CustId'. (207) (SQLExecDirectW)", f"Invalid column name {R}. {R} {R}"),
        ("Invalid column name 'CustId'", f"Invalid column name {R}"),
        ("[Some other layer]Invalid column name 'CustId'.", f"[Some other layer]Invalid column name {R}."),
        # a quoted part that no name can be
        ("Invalid column name 'O'Neil'.", f"Invalid column name {R}."),
        ("Invalid column name 'Jane\nDoe'.", f"Invalid column name {R}."),
        ("Invalid column name 'a\tb'.", f"Invalid column name {R}."),
        ("Invalid column name 'a\x1b[31mb'.", f"Invalid column name {R}."),  # a control character
        ("Invalid column name 'Total (net)'.", f"Invalid column name {R}."),
        ("Invalid column name 'say \"x\"'.", f"Invalid column name {R}."),
        ("Invalid column name ''.", f"Invalid column name {R}."),
        (f"Invalid column name '{LONG_NAME}n'.", f"Invalid column name {R}."),  # 129 characters
        ("Invalid object name 'a.b.c.d.e'.", f"Invalid object name {R}."),  # five parts
        # the kind is not one of the schema objects: the name of a user can be an e-mail address
        (
            "Cannot drop the user 'jane.doe@example.com', because it does not exist or you do not have "
            "permission.",
            f"Cannot drop the user {R}, because it does not exist or you do not have permission.",
        ),
        (
            "The value 'ACME Ltd' is dependent on column 'Name'.",
            f"The value {R} is dependent on column {R}.",
        ),
        # not the text of the product letter for letter
        ("invalid column name 'CustId'.", f"invalid column name {R}."),
        ("Invalid column name  'CustId'.", f"Invalid column name {R}."),
        ('Invalid column name "CustId".', f"Invalid column name {R}."),
        ("Cannot find the object 'sales.Order' because it does not exist or you do not have permissions.",
         f"Cannot find the object {R} because it does not exist or you do not have permissions."),
    ],
)  # fmt: skip
def test_only_the_whole_message_from_its_first_to_its_last_character_keeps_its_names(text, stored):
    # the batch wrote every name: only the form of the text decides here
    assert redact(text, EVERY_NAME) == stored == redact(text)
    assert redact(ODBC + text, EVERY_NAME) == ODBC + stored


def test_a_message_of_names_is_still_one_line_of_at_most_300_characters():
    text = (
        f"{ODBC}Column names in each table must be unique. Column name '{LONG_NAME}' in table "
        f"'{LONG_NAME}.{LONG_NAME}' is specified more than once."
    )
    assert redact(text, EVERY_NAME) == text[:300]


# ------------------------------------------------------------------ SqlError
def test_an_error_shows_class_number_and_sqlstate_with_the_redacted_text():
    error = SqlError("Invalid object name 'dbo.x'.", number=208, sqlstate="42S02")
    assert str(error) == f"[OTHER 208 42S02] Invalid object name {R}."  # no batch is known
    # for the batch that wrote the name, the name stays; the facts of the error are what they were
    told = error.for_batch("SELECT 1 FROM dbo.x;")
    assert str(told) == "[OTHER 208 42S02] Invalid object name 'dbo.x'."
    assert (told.raw_message, told.number, told.sqlstate, told.cls) == (
        error.raw_message, error.number, error.sqlstate, error.cls,
    )  # fmt: skip
    error = SqlError("There is already an object named 'x' in the database.", number=2714)
    assert str(error) == "[OTHER 2714] There is already an object named 'x' in the database."
    error = SqlError("Could not find stored procedure 'dbo.usp_x'.", number=2812, sqlstate="42000")
    assert str(error) == f"[OTHER 2812 42000] Could not find stored procedure {R}."
    assert str(SqlError("gone", cls=ErrorClass.SESSION_LOST)) == "[SESSION_LOST] gone"


def test_sql_error_fills_number_and_class_from_a_recognised_message():
    error = sql_error(ODBC + ENGINE_TEXT[1222])
    assert (error.number, error.cls, error.sqlstate) == (1222, ErrorClass.LOCK_TIMEOUT, None)


def test_sql_error_keeps_a_given_number_and_classifies_by_it():
    error = sql_error("boom", number=40613, sqlstate="HY000", at_connect=True)
    assert (error.number, error.cls, error.sqlstate) == (40613, ErrorClass.TRANSIENT_CONNECT, "HY000")


def test_sql_error_for_unknown_text_has_no_number_and_is_not_transient():
    error = sql_error(f"{ODBC}Something new went wrong.", at_connect=True)
    assert (error.number, error.cls) == (None, ErrorClass.OTHER)


# ------------------------------------------------------------------ the sign-in of SQL authentication
@pytest.mark.parametrize(
    ("login", "password", "text", "hidden"),
    [
        # the password starts inside the place of the login, and the reverse: no rest of either stays
        ("deploy-login-x1", "x1-Secret9", "as deploy-login-x1-Secret9 now", "as <hidden> now"),
        ("deploy", "ploy-Secret9", "as deploy-Secret9 now", "as <hidden> now"),
        ("Secret9-login", "pw-Secret9", "as pw-Secret9-login now", "as <hidden> now"),
        # a value that overlaps itself
        ("deploy_login", "abcabcabc", "x abcabcabcabc y", "x <hidden> y"),
        # two places that touch are one place
        ("deploy_login", "Passw0rd-x", "Passw0rd-xPassw0rd-x deploy_login", "<hidden> <hidden>"),
        # the login in the password, and the password in the login
        ("deploy", "the-deploy-word", "the-deploy-word of deploy", "<hidden> of <hidden>"),
        ("svc-Passw0rd-x-01", "Passw0rd-x", "svc-Passw0rd-x-01 and Passw0rd-x", "<hidden> and <hidden>"),
    ],
)
def test_places_of_the_login_and_the_password_that_overlap_are_hidden_as_one_place(
    login, password, text, hidden
):
    assert SignInFilter(login, password).hide(text) == hidden


def test_a_filter_shows_no_value_and_a_filter_of_no_value_changes_nothing():
    made = SignInFilter("deploy_login", "Passw0rd-x")
    assert bool(made) and "deploy_login" not in f"{made!r} {made}" and "Passw0rd" not in f"{made!r} {made}"
    assert not SignInFilter() and SignInFilter().hide("UID={x};PWD={y}") == "UID={x};PWD={y}"
    assert SignInFilter().hide_echo("UID={x};PWD={y}") == "UID={x};PWD={y}"
    assert made.hide_echo("failed: Server={s};uid = deploy_lo") == "failed: Server={s};<hidden>"
    assert made.hide_echo("failed: User ID=dep") == "failed: <hidden>"
    assert made.hide_echo("the GUID=1 of fluid = 2") == "the GUID=1 of fluid = 2"  # no keyword of a sign-in
