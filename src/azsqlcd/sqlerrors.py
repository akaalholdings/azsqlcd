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
from collections.abc import Collection
from enum import Enum

from azsqlcd import lex

REDACTED = "<redacted>"
HIDDEN = "<hidden>"  # stands where the login or the password of SQL authentication was
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

    message is redacted (A25: no value; the names of a message of names only stay, see redact); it
    is the only text in str() and in args. raw_message is the full text: print it only when the
    operator asked for it (--show-error-text), never for prod.

    written: the names that the batch of the error wrote (names_written). Without them a name as
    the statement wrote it (207, 208 and three more, see redact) is redacted; for_batch() gives
    the error with the names of its batch.
    """

    def __init__(
        self,
        raw_message: str,
        *,
        number: int | None = None,
        sqlstate: str | None = None,
        cls: ErrorClass = ErrorClass.OTHER,
        written: Collection[str] = (),
    ) -> None:
        self.raw_message = raw_message
        self.message = redact(raw_message, written)
        self.number = number
        self.sqlstate = sqlstate
        self.cls = cls
        super().__init__(self.message)

    def for_batch(self, batch: str) -> SqlError:
        """This error as the session that sent the batch raises it: the same facts, and a message
        that keeps a name of the statement when the batch wrote that name."""
        return SqlError(
            self.raw_message,
            number=self.number,
            sqlstate=self.sqlstate,
            cls=self.cls,
            written=names_written(batch),
        )

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


# Messages of names only. "The index <redacted> is dependent on column <redacted>" tells the reader
# of a failed deploy nothing (first pilot), so the names of some messages stay. Which names:
#
#   names of the catalog (_CATALOG_NAME_MESSAGES): every quoted part is the name of an object or
#     a column that exists in the database: 1913, 2714, 3726, 5074. They stay.
#   names of the statement (_STATEMENT_NAME_MESSAGES): the quoted part is what the statement used
#     as a name, and nothing with that name exists: 207, 208, 2705, 3701, 4902. This is a row
#     value when dynamic SQL builds the statement from data: under QUOTED_IDENTIFIER ON,
#     N'UPDATE t SET c = "' + @v + N'"' gives "Invalid column name '<the value>'.". So such a
#     name stays only when the batch that was sent holds each part of it as an identifier
#     (names_written): then it is text of the release, not a value of a row.
#
# The templates: 207, 208, 2714 and 5074 are compared with the live texts (tests/fixtures/live);
# the others are from the Microsoft documentation of each error.
#   4922 ("ALTER TABLE ALTER COLUMN c failed because one or more objects access this column") has
#   no quoted part: redact never changed it.
# Not here, for a quoted part can be a value or the form is not sure: 245, 2601, 2627, 2628 (they
# print values), 515 and 547 (names, in more than one form).
# NAME: one to four parts with dots between them, each of 1 to 128 characters with no quote, no
# parenthesis, no white space but the space, no control character (C0, DEL, C1) and no character
# that changes how a line is shown (zero width, the marks and the overrides of the text direction,
# the line and paragraph separators). KIND: %S_MSG, a word of the product; only the kinds of
# schema objects are taken, so the name of a user or a login stays redacted (it can be an e-mail
# address).
_NOT_IN_A_NAME = r"'\"().\s\x00-\x1f\x7f-\x9f؜​-‏ -‮⁠-⁯﻿￹-￻"
_NAME_PART = rf"(?: |[^{_NOT_IN_A_NAME}]){{1,128}}"
_NAME = rf"({_NAME_PART}(?:\.{_NAME_PART}){{0,3}})"  # the one group: the name
_KIND = (
    "(?:table|view|procedure|function|trigger|index|statistics|constraint|column|type|schema|synonym|"
    "sequence object|object|default|rule)"
)
# what the ODBC layer writes before the engine message
_ODBC_LAYERS = r"(?:\[Microsoft\](?:\[ODBC Driver \d+ for SQL Server\])?\[SQL Server\])?"


def _name_messages(*templates: str) -> tuple[re.Pattern[str], ...]:
    return tuple(
        re.compile(_ODBC_LAYERS + re.escape(template).replace("NAME", _NAME).replace("KIND", _KIND))
        for template in templates
    )


_CATALOG_NAME_MESSAGES = _name_messages(
    "The operation failed because an index or statistics with name 'NAME' already exists on "
    "KIND 'NAME'.",  # 1913
    "There is already an object named 'NAME' in the database.",  # 2714
    "Could not drop object 'NAME' because it is referenced by a FOREIGN KEY constraint.",  # 3726
    "The KIND 'NAME' is dependent on KIND 'NAME'.",  # 5074
)
_STATEMENT_NAME_MESSAGES = _name_messages(
    "Invalid column name 'NAME'.",  # 207
    "Invalid object name 'NAME'.",  # 208
    "Column names in each table must be unique. Column name 'NAME' in table 'NAME' is specified "
    "more than once.",  # 2705
    "Cannot drop the KIND 'NAME', because it does not exist or you do not have permission.",  # 3701
    'Cannot find the object "NAME" because it does not exist or you do not have permissions.',  # 4902
)
_IDENTIFIER_TOKENS = frozenset({"word", "bident", "qident"})


def names_written(batch: str) -> frozenset[str]:
    """The identifiers of a batch, as the batch wrote them, without their brackets or quotes: what
    redact() compares a name of the statement with. Nothing of a string or a comment is in the
    result. A batch that cannot be read into tokens gives no names."""
    try:
        tokens = lex.tokenize(batch)
    except lex.LexError:
        return frozenset()
    return frozenset(token.value for token in tokens if token.kind in _IDENTIFIER_TOKENS)


def _names_stay(whole: str, written: Collection[str]) -> bool:
    if any(message.fullmatch(whole) for message in _CATALOG_NAME_MESSAGES):
        return True
    for message in _STATEMENT_NAME_MESSAGES:
        found = message.fullmatch(whole)
        if found is not None:
            return all(part in written for name in found.groups() for part in name.split("."))
    return False


def redact(text: str, written: Collection[str] = ()) -> str:
    """Engine message text that is safe to store and print: no quoted or parenthesised value.

    A message of names only keeps its names, and only when the whole text is that one message
    from its first to its last character: text before it, text after it, a second message or a
    quoted part that no name can be, and everything is redacted as below. A message that names
    objects of the catalog (_CATALOG_NAME_MESSAGES) always keeps them. A message that prints a
    name as the statement wrote it (_STATEMENT_NAME_MESSAGES) keeps it only when every part of
    the name is in written: the identifiers of the batch that raised the error (names_written).
    With no batch (the default) such a name is redacted, for it can be a row value.

    A quote or parenthesis with no end (the driver cuts long messages) hides the rest of the text.
    The result is one line of at most 300 characters. Text without quotes or parentheses, for
    example the message of a THROW in a script, passes unchanged: this is not a secret scanner.
    """
    whole = text.strip()
    if _names_stay(whole, written):
        return whole[:MAX_MESSAGE_LENGTH]
    out = _VALUE_TAIL.sub(rf"\1 {REDACTED}", text)
    out = _SINGLE_QUOTED.sub(REDACTED, out)
    out = _DOUBLE_QUOTED.sub(REDACTED, out)
    count = 1
    while count:  # innermost first, until no pair is left
        out, count = _PARENTHESISED.subn(REDACTED, out)
    out = _OPEN_PARENTHESIS.sub(REDACTED, out)
    return " ".join(out.split())[:MAX_MESSAGE_LENGTH]


# ------------------------------------------------------------------ the sign-in of SQL authentication
# The keywords of a connection string that are followed by the login or the password.
_SIGN_IN_KEYWORD = re.compile(r"\b(?:pwd|uid|password|user id)\s*=", re.IGNORECASE)


class SignInFilter:
    """Takes the login and the password of SQL authentication out of text. Each one is hidden in
    three forms: as it is, with } doubled, and in braces with } doubled (what a connection string
    holds, session.open_session).

    The password is hidden wherever it stands. The login is hidden where it stands as a whole
    word: a login is short and can be a part of another word (the login "a" in "failed"), and a
    replacement there shows the login by its places and damages the text. A login that equals
    another name of a text (a schema, a database) is hidden there too.

    Neither value is in repr() or str() of the filter. A filter of no value changes nothing.
    """

    def __init__(self, login: str = "", password: str = "") -> None:
        forms: dict[str, str] = {}
        for value, whole_word in ((password, False), (login, True)):
            if not value:
                continue
            doubled = value.replace("}", "}}")
            forms.setdefault("{" + doubled + "}", re.escape("{" + doubled + "}"))
            for form in (doubled, value):
                pattern = re.escape(form)
                if whole_word:
                    before = r"(?<!\w)" if re.match(r"\w", form) else ""
                    after = r"(?!\w)" if re.search(r"\w\Z", form) else ""
                    pattern = before + pattern + after
                forms.setdefault(form, pattern)
        # In a lookahead: every place is found, also one that starts inside another place.
        self._patterns = tuple(re.compile(f"(?=({pattern}))") for pattern in forms.values())

    def __bool__(self) -> bool:
        return bool(self._patterns)

    def __repr__(self) -> str:
        return HIDDEN

    def hide(self, text: str) -> str:
        """text with the login and the password taken out. Places that overlap or touch are one
        place: where a value starts inside another one, no rest of either stays."""
        places = sorted(found.span(1) for pattern in self._patterns for found in pattern.finditer(text))
        if not places:
            return text
        out: list[str] = []
        at, (start, end) = 0, places[0]
        for next_start, next_end in places[1:]:
            if next_start <= end:
                end = max(end, next_end)
                continue
            out += [text[at:start], HIDDEN]
            at, start, end = end, next_start, next_end
        return "".join([*out, text[at:start], HIDDEN, text[end:]])

    def hide_echo(self, text: str) -> str:
        """hide(), and the text of a driver ends where it names a connection keyword of the sign-in
        (UID=, PWD=): a driver can put the connection string into a message, whole or cut inside
        a value, and a part of a value cannot be found by its text."""
        if not self._patterns:
            return text
        text = self.hide(text)
        found = _SIGN_IN_KEYWORD.search(text)
        return text if found is None else text[: found.start()] + HIDDEN
