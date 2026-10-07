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
    SqlError,
    classify,
    known_number,
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


@pytest.mark.parametrize("label", sorted(LIVE_ERRORS))
def test_a_text_that_the_live_driver_returned_is_stored_without_its_values(label):
    case = LIVE_ERRORS[label]
    stored = redact(case["text"])
    assert stored == case["redacted"] == sql_error(case["text"]).message
    for value in ("azsqlcd_spike", "x1_", "l11_", "l15_", "l2_", "x3_", "'abc'", "0001__a.sql", "Process ID"):
        assert value not in stored
    assert "'" not in stored and '"' not in stored and len(stored) <= 300


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


# ------------------------------------------------------------------ SqlError
def test_an_error_shows_class_number_and_sqlstate_with_the_redacted_text():
    error = SqlError("Invalid object name 'dbo.x'.", number=208, sqlstate="42S02")
    assert str(error) == f"[OTHER 208 42S02] Invalid object name {R}."
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
