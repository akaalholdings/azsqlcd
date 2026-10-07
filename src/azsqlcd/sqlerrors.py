"""Engine errors: the number behind a message text, the class of an error, and redaction.

mssql-python 1.15.0 gives no engine error number. Its exceptions hold the label of the SQLSTATE and
the text of the first ODBC diagnostic record, nothing else (design Part 2 (k), first containment
row). So the number comes from text, from two sources:
  - a number that is written in the text (parse_number: shapes of pyodbc, sqlcmd and ODBC
    diagnostic records);
  - the en-US text of the few engine messages on which the tool takes a decision (known_number).
Text that is not recognised gives no number and the class OTHER: never transient, never retried.
The class selects only the connect retry, the exit code and a hint. Nothing is replayed.
"""

from __future__ import annotations

import re
from enum import Enum

REDACTED = "<redacted>"
MAX_MESSAGE_LENGTH = 300  # azsqlcd.run.error_text is nvarchar(300)

TRANSIENT_CONNECT_NUMBERS = frozenset({40613, 40197, 40501, 49918, 49919, 49920, 10928, 10929, 4060})
GOVERNANCE_NUMBERS = frozenset({40552, 9002, 40549, 40550, 40551, 40544})
LOCK_TIMEOUT_NUMBER = 1222
DEADLOCK_NUMBER = 1205


class ErrorClass(Enum):
    TRANSIENT_CONNECT = "transient_connect"  # only at connect; the only class that is retried
    LOCK_TIMEOUT = "lock_timeout"
    DEADLOCK = "deadlock"
    GOVERNANCE = "governance"  # resource limit of the service; never retried
    SESSION_LOST = "session_lost"  # the connection is gone; nothing more can be sent on it
    OTHER = "other"


class SqlError(Exception):
    """An error from the engine or the driver.

    message is redacted (A25); it is the only text in str() and in args. raw_message is the full
    text: print it only when the operator asked for it (--show-error-text), never for prod.
    """

    def __init__(
        self,
        raw_message: str,
        *,
        number: int | None = None,
        sqlstate: str | None = None,
        cls: ErrorClass = ErrorClass.OTHER,
    ) -> None:
        self.raw_message = raw_message
        self.message = redact(raw_message)
        self.number = number
        self.sqlstate = sqlstate
        self.cls = cls
        super().__init__(self.message)

    def __str__(self) -> str:
        facts = [self.cls.name]
        if self.number is not None:
            facts.append(str(self.number))
        if self.sqlstate:
            facts.append(self.sqlstate)
        return f"[{' '.join(facts)}] {self.message}"


def sql_error(
    text: str, *, number: int | None = None, sqlstate: str | None = None, at_connect: bool = False
) -> SqlError:
    """A classified SqlError for message text. Without a number, a recognised engine message gives it."""
    if number is None:
        number = known_number(text)
    return SqlError(
        text, number=number, sqlstate=sqlstate, cls=classify(number, sqlstate, text, at_connect=at_connect)
    )


# ------------------------------------------------------------------ number from text
_NUMBER_SHAPES = (
    re.compile(r"\bMsg (\d+), Level \d+", re.IGNORECASE),  # sqlcmd, SSMS
    re.compile(r"\((\d+)\) \(SQL[A-Za-z]+\)"),  # pyodbc: "... exceeded. (1222) (SQLExecDirectW)"
    # mssql-python DDBCSQLGetAllDiagRecords (cursor.messages): "[42000] (1222)"
    re.compile(r"\[[0-9A-Z]{5}\] \((\d+)\)"),
    re.compile(r"\((\d+)\)\s*\Z"),  # ODBC text that ends with the native error
)

# en-US texts, matched on a distinctive part. 207, 208, 245, 1205, 1222, 2020, 2601, 2627, 2714,
# 3729, 3998 and 18456 are compared with the texts that mssql-python 1.15.0 returned from Azure SQL
# Database (live spike: tests/fixtures/live/error_texts.json). The others are from the Microsoft
# documentation of each error; the spike did not produce them (4060 too: a token connect to a
# database that does not exist gave the text of 18456).
# A decision hangs on the numbers of the two lists above, on 1222 and 1205, and on 207, 208 and 2020
# (catalog.broken_references). The rest make the number of a report exact.
# The first match wins: 18456 is last, for its text can follow the text of 4060 in one message.
_KNOWN_MESSAGES = tuple(
    (number, re.compile(pattern, re.IGNORECASE))
    for number, pattern in (
        (207, r"Invalid column name '"),
        (208, r"Invalid object name '"),
        (245, r"Conversion failed when converting the .* value '.*' to data type"),
        (2601, r"Cannot insert duplicate key row in object '.*' with unique index '"),
        (
            2627,
            r"Violation of (?:PRIMARY|UNIQUE) KEY constraint '.*'\. Cannot insert duplicate key in object",
        ),
        (3729, r"Cannot ALTER '.*' because it is being referenced by object '"),
        (3998, r"Uncommittable transaction is detected at the end of the batch"),
        (2714, r"There is already an object named '.*' in the database"),
        (2020, r"The dependencies reported for entity \".*\" might not include references to all columns"),
        (1222, r"Lock request time out period exceeded"),
        (1205, r"was deadlocked on .* resources with another process and has been chosen as the deadlock"),
        (4060, r"Cannot open database \".*\" requested by the login"),
        (40613, r"Database '.*' on server '.*' is not currently available"),
        (40197, r"The service has encountered an error processing your request\. Please try again"),
        (40501, r"The service is currently busy\. Retry the request after \d+ seconds"),
        (49918, r"Cannot process request\. Not enough resources to process request"),
        (49919, r"Cannot process create or update request\. Too many create or update operations"),
        (49920, r"Cannot process request\. Too many operations in progress for subscription"),
        (10928, r"Resource ID ?: ?\d+\. The .* limit for the database is \d+ and has been reached"),
        (10929, r"Resource ID ?: ?\d+\. The .* minimum guarantee is \d+, maximum limit is \d+"),
        (40552, r"session has been terminated because of excessive transaction log space usage"),
        (9002, r"The transaction log for database '.*' is full due to"),
        (40549, r"Session is terminated because you have a long[- ]running transaction"),
        (40550, r"session has been terminated because it has acquired too many locks"),
        (40551, r"session has been terminated because of excessive TEMPDB usage"),
        (40544, r"The database '.*' has reached its size quota"),
        (18456, r"Login failed for user '"),
    )
)


def known_number(text: str) -> int | None:
    """The number of a recognised engine message, or None. The text must be the en-US message."""
    flat = " ".join(text.split())
    for number, pattern in _KNOWN_MESSAGES:
        if pattern.search(flat):
            return number
    return None


def parse_number(text: str) -> int | None:
    """The engine error number for message text, or None.

    A number that is written in the text wins. Then a recognised engine message gives its number.
    "Error code 4221" inside a message is a sub-code of the service and is never read as the number.
    """
    for shape in _NUMBER_SHAPES:
        found = shape.search(text)
        if found and int(found.group(1)) > 0:  # 0 is "no native error" in an ODBC record
            return int(found.group(1))
    return known_number(text)


# ------------------------------------------------------------------ class
def classify(number: int | None, sqlstate: str | None, text: str, *, at_connect: bool) -> ErrorClass:
    """The class that selects the reaction. Without a number, a recognised engine message gives it."""
    if number is None:
        number = known_number(text)
    if number in GOVERNANCE_NUMBERS:
        return ErrorClass.GOVERNANCE
    if number == LOCK_TIMEOUT_NUMBER:
        return ErrorClass.LOCK_TIMEOUT
    if number == DEADLOCK_NUMBER:
        return ErrorClass.DEADLOCK
    if at_connect and number in TRANSIENT_CONNECT_NUMBERS:
        return ErrorClass.TRANSIENT_CONNECT
    if sqlstate is not None and (sqlstate.startswith("08") or sqlstate == "40003"):
        return ErrorClass.SESSION_LOST
    return ErrorClass.OTHER


# ------------------------------------------------------------------ redaction (A25)
# After these phrases the engine prints row values up to the end of the message. The values can
# hold quotes and parentheses, so everything after the phrase goes.
_VALUE_TAIL = re.compile(r"(The duplicate key value is|Truncated value:).*", re.IGNORECASE | re.DOTALL)
# A quote that is followed by a letter or digit is inside the value ('O'Neil'), not its end.
_SINGLE_QUOTED = re.compile(r"'(?:[^']|'(?=\w))*(?:'|\Z)")
_DOUBLE_QUOTED = re.compile(r'"[^"]*(?:"|\Z)')
_PARENTHESISED = re.compile(r"\([^()]*\)")
_OPEN_PARENTHESIS = re.compile(r"\(.*", re.DOTALL)


def redact(text: str) -> str:
    """Engine message text that is safe to store and print: no quoted or parenthesised value.

    A quote or parenthesis with no end (the driver cuts long messages) hides the rest of the text.
    The result is one line of at most 300 characters. Text without quotes or parentheses, for
    example the message of a THROW in a script, passes unchanged: this is not a secret scanner.
    """
    out = _VALUE_TAIL.sub(rf"\1 {REDACTED}", text)
    out = _SINGLE_QUOTED.sub(REDACTED, out)
    out = _DOUBLE_QUOTED.sub(REDACTED, out)
    count = 1
    while count:  # innermost first, until no pair is left
        out, count = _PARENTHESISED.subn(REDACTED, out)
    out = _OPEN_PARENTHESIS.sub(REDACTED, out)
    return " ".join(out.split())[:MAX_MESSAGE_LENGTH]
