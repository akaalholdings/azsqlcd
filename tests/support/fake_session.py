"""FakeSession: the database session for unit tests. A fake, not a mock. No driver, no network.

It has the shape of azsqlcd.session.Session. It records every batch in order and answers from
rules, then from a small model of the session state that the runner asks about.

    from azsqlcd.sqlerrors import sql_error
    from support.fake_session import FakeSession

    def test_a_batch_error_leaves_no_step_row():
        db = FakeSession()
        db.respond("FROM [azsqlcd].[meta]", [[("sales", "dev")]])
        db.fail_on("ALTER TABLE", sql_error("Lock request time out period exceeded."))
        with pytest.raises(ToolError):
            deploy(db, plan)
        assert db.sent("INSERT INTO [azsqlcd].[step]") == []
        db.assert_order("BEGIN TRANSACTION", "ALTER TABLE", "ROLLBACK")

Rules
    respond(matcher, result)   result = list of result sets, or callable(batch) -> list of result sets
    fail_on(matcher, error, times=1, keeps_transaction=False)
    kill_on(matcher)           the connection dies: SESSION_LOST, session closed, later calls fail
  A matcher is a substring, a compiled regex (search) or a callable(batch) -> bool. The first
  matching rule wins, in the order the rules were added. A fail_on rule is used up after `times`
  batches. A batch with no rule and no built-in answer returns [] (no result set).

Built-in model (a rule for the same batch replaces the answer)
    BEGIN TRAN[SACTION], COMMIT, ROLLBACK in the batch text move trancount, xact_state and
      transaction_id. Words in comments, strings and in the body of a CREATE/ALTER module do not
      count; a word behind an IF does. This runs for every batch that does not fail, also when a
      rule gives the answer.
    An error from a rule ends the transaction, as XACT_ABORT ON does (keeps_transaction=True: a
      compile error, the transaction stays open).
    A batch that is one SELECT of @@TRANCOUNT, XACT_STATE(), CURRENT_TRANSACTION_ID(), @@SPID,
      APPLOCK_MODE(...), APPLOCK_TEST(...) gets one row with those values, in the order asked.
    A batch with sp_getapplock returns [[(applock_result,)]] and holds the lock when the result is
      0 or 1. Set applock_result = -1 for "another run holds the lock": APPLOCK_TEST is then 0.
      sp_releaseapplock gives the lock back and returns [[(0,)]].
  The state is plain attributes; a test may set them: spid, trancount, xact_state, transaction_id,
  applock_result, applock_held. Nothing else of T-SQL is evaluated. To stage what the model does
  not know (the THROW of a lost-lock guard, a script that dooms the transaction), use fail_on, or
  respond with a callable that sets the attributes.

Questions
    batches                    every batch that reached the session, in order
    sent(matcher)              the matching batches
    index_of(matcher)          position of the first matching batch (AssertionError when none)
    assert_order(*matchers)    each matcher matches a later batch than the one before it
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from azsqlcd.lex import LexError, Tok, module_header, significant, tokenize
from azsqlcd.session import ResultSets
from azsqlcd.sqlerrors import ErrorClass, SqlError, sql_error

Matcher = str | re.Pattern[str] | Callable[[str], bool]
Result = ResultSets | Callable[[str], ResultSets]


@dataclass
class _Rule:
    matcher: Matcher
    result: Result | None = None
    error: BaseException | None = None
    remaining: int | None = None  # None = no limit
    keeps_transaction: bool = False
    kills: bool = False


def _matches(matcher: Matcher, batch: str) -> bool:
    if isinstance(matcher, str):
        return matcher in batch
    if isinstance(matcher, re.Pattern):
        return matcher.search(batch) is not None
    return bool(matcher(batch))


class FakeSession:
    def __init__(self) -> None:
        self.batches: list[str] = []
        self.closed = False
        self.spid = 57
        self.trancount = 0
        self.xact_state = 0
        self.transaction_id = 1000
        self.applock_result = 0
        self.applock_held = False
        self._rules: list[_Rule] = []

    # ------------------------------------------------------------------ rules
    def respond(self, matcher: Matcher, result: Result) -> None:
        self._rules.append(_Rule(matcher, result=result))

    def fail_on(
        self, matcher: Matcher, error: BaseException, times: int = 1, *, keeps_transaction: bool = False
    ) -> None:
        self._rules.append(_Rule(matcher, error=error, remaining=times, keeps_transaction=keeps_transaction))

    def kill_on(self, matcher: Matcher) -> None:
        self._rules.append(_Rule(matcher, kills=True))

    # ------------------------------------------------------------------ Session
    def execute(self, batch: str) -> ResultSets:
        if self.closed:
            raise SqlError("fake: the session is closed", cls=ErrorClass.SESSION_LOST)
        self.batches.append(batch)
        rule = next((r for r in self._rules if r.remaining != 0 and _matches(r.matcher, batch)), None)
        answer: ResultSets | None = None
        if rule is not None:
            if rule.remaining is not None:
                rule.remaining -= 1
            if rule.kills:
                self.closed = True
                raise sql_error("fake: Communication link failure", sqlstate="08S01")
            if rule.error is not None:
                if not rule.keeps_transaction:
                    self._end_transaction()
                raise rule.error
            try:
                answer = rule.result(batch) if callable(rule.result) else rule.result
            except BaseException:
                self._end_transaction()
                raise
        tokens = _tokens(batch)
        self._track_transaction(batch, tokens)
        if answer is None:
            answer = self._builtin_answer(tokens)
        return [list(result_set) for result_set in answer or []]  # a caller may change its lists

    def close(self) -> None:
        self.closed = True

    # ------------------------------------------------------------------ questions
    def sent(self, matcher: Matcher) -> list[str]:
        return [batch for batch in self.batches if _matches(matcher, batch)]

    def index_of(self, matcher: Matcher) -> int:
        for index, batch in enumerate(self.batches):
            if _matches(matcher, batch):
                return index
        raise AssertionError(f"no batch matches {matcher!r}; sent: {self._summary()}")

    def assert_order(self, *matchers: Matcher) -> None:
        position = -1
        for matcher in matchers:
            later = range(position + 1, len(self.batches))
            position = next((i for i in later if _matches(matcher, self.batches[i])), -1)
            if position < 0:
                raise AssertionError(
                    f"no batch matches {matcher!r} after the matchers before it; sent: {self._summary()}"
                )

    def _summary(self) -> list[str]:
        return [" ".join(batch.split())[:80] for batch in self.batches]

    # ------------------------------------------------------------------ model
    def _end_transaction(self) -> None:
        self.trancount, self.xact_state = 0, 0

    def _track_transaction(self, batch: str, tokens: list[Tok]) -> None:
        try:
            module_header(batch)
            return  # a module definition: its body does not run
        except LexError:
            pass
        words = [t.text.upper() if t.kind == "word" else "" for t in tokens]
        for index, word in enumerate(words):
            if word == "BEGIN" and words[index + 1 : index + 2] in (["TRAN"], ["TRANSACTION"]):
                if self.trancount == 0:
                    self.transaction_id += 1
                self.trancount += 1
                self.xact_state = 1
            elif word == "COMMIT" and self.trancount > 0:
                self.trancount -= 1
                if self.trancount == 0:
                    self.xact_state = 0
            elif word == "ROLLBACK":
                self._end_transaction()

    def _builtin_answer(self, tokens: list[Tok]) -> ResultSets | None:
        names = [t.text.lower() for t in tokens]
        if "sp_getapplock" in names:
            self.applock_held = self.applock_result >= 0
            return [[(self.applock_result,)]]
        if "sp_releaseapplock" in names:
            self.applock_held = False
            return [[(0,)]]
        if not names or names[0] != "select":
            return None
        row = [self._scalar(item) for item in _select_items(tokens[1:])]
        return None if not row or _UNKNOWN in row else [[tuple(row)]]

    def _scalar(self, item: list[Tok]) -> Any:
        name = item[0].text.upper() if item else ""
        if name == "@@TRANCOUNT":
            return self.trancount
        if name == "XACT_STATE":
            return self.xact_state
        if name == "CURRENT_TRANSACTION_ID":
            if self.trancount == 0:
                self.transaction_id += 1  # outside a transaction every statement has its own id
            return self.transaction_id
        if name == "@@SPID":
            return self.spid
        if name == "APPLOCK_MODE":
            return "Exclusive" if self.applock_held else "NoLock"
        if name == "APPLOCK_TEST":
            return 1 if self.applock_result >= 0 else 0
        return _UNKNOWN


_UNKNOWN = object()


def _tokens(batch: str) -> list[Tok]:
    try:
        return significant(tokenize(batch))
    except LexError:
        return []  # text the lexer refuses has no built-in meaning


def _select_items(tokens: list[Tok]) -> list[list[Tok]]:
    """The select list, split at top-level commas. A trailing ; is dropped."""
    if tokens and tokens[-1].text == ";":
        tokens = tokens[:-1]
    items: list[list[Tok]] = [[]]
    depth = 0
    for token in tokens:
        if token.kind == "op" and token.text in "()":
            depth += 1 if token.text == "(" else -1
        if token.kind == "op" and token.text == "," and depth == 0:
            items.append([])
        else:
            items[-1].append(token)
    return items
