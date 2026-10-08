# When a run fails: what to send

For the owner. A run failed and you want help from a person, or from an AI assistant, who was not
there and has no access to the database. Send one zip: the triage log of the run and its reports.

The log tells what the tool did, in order, and where it stopped. It holds no SQL text, no object
definition, no data value, no token and no connection string (design rules A1 and A25). Section 4
has the full list, and section 5 says what it does hold that you may want to check before it
leaves your organisation.

Status, 2026-10-07. The recorder is `src/azsqlcd/trace.py`; `tests/unit/test_trace.py` proves the
rules of section 4 with a fake session. The commands and options of this document were run on a
copy of the tool with the unit tests of the command line. Not proven: no log of a run against a
real database has been read, and the upload step of section 1 ran on GitHub only in jobs that
stopped at the Azure login (`docs/known-gaps.md`, section 11). Check that your version has the
commands: `azsqlcd show-log --help`. If it answers `invalid choice`, use the line at the end of
section 2.

## 1. Where the log is

Each call of a database command (`plan`, `deploy`, `drift`, `export`, `baseline`, `resolve`) writes
one file, `azsqlcd-<UTC time>-<command>.jsonl`, for example `azsqlcd-20261007T140311Z-deploy.jsonl`.
The offline commands write none. A non-zero exit prints the line `log: <path>` on stderr:

```
BATCH_FAILED: step 0007__add_status.sql#1 failed: [OTHER 207] [Microsoft][SQL Server]Invalid column name 'Status'.. ...
log: report/logs/azsqlcd-20261007T140311Z-deploy.jsonl
azsqlcd deploy: exit 21
```

The directory, first match:

| Rule | Directory |
|---|---|
| `--log-dir DIR` | `DIR` |
| The variable `AZSQLCD_LOG_DIR` is set | its value |
| `plan`, `deploy`, `baseline`, `resolve` with `--out DIR` | `DIR/logs` |
| `drift`, `export`, and `baseline` or `resolve` with no `--out` | `.azsqlcd/logs` under the current directory |

`--no-log` writes no log. In a database repository, add the line `.azsqlcd/` to `.gitignore`.

GitHub runs: each job that runs a database command sets `AZSQLCD_LOG_DIR` to `azsqlcd-logs` and
uploads that directory as the artefact `azsqlcd-logs-<env>-<target>-<job>`, for example
`azsqlcd-logs-prod-sales-eu-deploy`, also when the job failed. The jobs are `plan`, `deploy`,
`drift`, `resolve`, `baseline` and `read` (export and baseline report). Download it:

```
gh run download <run-id> -R <owner>/<database-repository> -n azsqlcd-logs-prod-sales-eu-deploy -D triage/logs
gh run download <run-id> -R <owner>/<database-repository> -n report-prod-sales-eu -D triage
```

The second line gets `report.json` (artefact `report-<env>-<target>`; for a plan job
`plan-<env>-<target>`). With the log in `triage/logs` and the reports in `triage`, the bundle
command of section 2 finds both.

## 2. The one command that makes the bundle

```
azsqlcd support-bundle --latest --log-dir report/logs --out azsqlcd-support.zip
```

- `--log-dir` is the directory of section 1. Without it the command reads `AZSQLCD_LOG_DIR`, then
  `.azsqlcd/logs`.
- `--latest` (the default) takes the log that was written last in that directory. `--log FILE`
  names one log.

The command prints the names in the zip. It refuses (exit 22 `BUNDLE_REFUSED`) a log over 5 MB and
a path that is not a regular file. Exit 22 `LOG_NOT_FOUND`: the directory holds no log; give
`--log` or `--log-dir`.

A version of the tool with no `support-bundle` command, from the checkout of the tool:

```
uv run --project "$TOOL" --no-sync python -c "from azsqlcd import trace; print(trace.support_bundle(trace.latest_log('report/logs'), 'azsqlcd-support.zip'))"
```

## 3. What is inside

| File in the zip | From | Holds |
|---|---|---|
| `azsqlcd-<time>-<command>.jsonl` | the log directory | the events of this section |
| `plan.json` | the directory of the log; the directory above a directory named `logs`; else the `--out` and `--bundle` that the log names | the plan: steps, names, hashes |
| `report.json` | same | the run: exit and reason code, steps applied, redacted error text |
| `manifest.json` | same | the release: commit, paths and hashes of its files |
| `versions.txt` | written by the command | tool version and digest, Python, platform, driver versions: of the machine that made the zip, and of the run in the log |

A file that is missing is left out. Nothing else is added: no `bundle.tar`, no `.sql` file, no
`azsqlcd.toml`.

The log is one JSON object per line. Every event has `ts` (UTC), `seq` (1, 2, 3 ... in the order
of writing) and `kind`. One example of each kind:

`run`: the first event. `argv` holds option names; a value is shown only for `--env`, `--target`,
`--base`, `--name`, `--commit`, `--out`, `--bundle`, `--root`, `--digest`, `--ci`, `--log`,
`--log-dir` and `--expect-plan-file`. Any other value is `<set>`. `ci` holds `GITHUB_RUN_ID`,
`GITHUB_RUN_ATTEMPT`, `GITHUB_REPOSITORY`, `GITHUB_REF`, `GITHUB_SHA`, `GITHUB_WORKFLOW`,
`GITHUB_JOB`, `RUNNER_OS` and `RUNNER_NAME` when they are set.

```json
{"ts": "2026-10-07T14:03:11.120Z", "seq": 1, "kind": "run", "command": "deploy", "argv": ["deploy", "--bundle", "release", "--digest", "3f657d51...", "--env", "dev", "--target", "sales-dev", "--inline-plan", "--out", "report", "--triggering-actor", "<set>", "--ci", "github"], "tool_version": "0.1.0", "tool_digest": "c96a705c...", "python": "3.12.12", "platform": {"system": "Linux", "release": "6.8.0", "machine": "x86_64"}, "packages": {"mssql-python": "1.15.0", "azure-identity": "1.26.0"}, "ci": {"GITHUB_RUN_ID": "991", "GITHUB_JOB": "deploy"}}
```

`config`: the target, when the command has read it, and `auth`: the kind of sign-in (`entra`,
`managed-identity` or `sql`), never a login or a client id. None of these values is a secret.

```json
{"ts": "2026-10-07T14:03:11.130Z", "seq": 2, "kind": "config", "project": "sales", "environment": "dev", "target": "sales-dev", "server": "sql-sales-dev.database.windows.net", "database": "sales", "table_model": false, "auth": "entra"}
```

`connect`: one session was opened, or could not be opened (`"ok": false` with `error`). Sessions
are named by the order in which the call opens them: `main` is the first. `parse` is the second:
the session of the syntax check of a plan (its first batch is `SET PARSEONLY ON`). In a deploy
that lost its connection, a later session is the one that reads the outcome and closes the run
row; from the third on the name is `session-3`, `session-4`.

```json
{"ts": "2026-10-07T14:03:12.410Z", "seq": 3, "kind": "connect", "session": "main", "ms": 1270.4, "ok": true}
```

`batch`: one batch that was sent, with its result or its error.

| Field | Meaning |
|---|---|
| `session`, `n` | the session, and the count of the batch in that session |
| `tag` | the name in the leading `/* azsqlcd:<name> */` comment of a query of the tool; `null` for a batch of the release and for the write statements of the tool |
| `head` | the class of the first statement: keywords and object names, nothing after the name |
| `then` | the same for each later statement after a `;` (at most 10); absent for one statement and for a module |
| `sha256`, `chars` | hash and length of the batch text. The hash finds the batch in the release without the text |
| `ms` | duration |
| `result_sets` | the row count of each result set; never a row |
| `error` | on failure, in place of `result_sets`: `number`, `sqlstate`, `class` and the redacted `message` |

```json
{"ts": "2026-10-07T14:03:12.530Z", "seq": 7, "kind": "batch", "session": "main", "n": 4, "tag": "lock", "head": "DECLARE", "then": ["EXEC [sys].[sp_getapplock]", "SELECT"], "sha256": "3fb39a4c...", "chars": 178, "ms": 12.9, "result_sets": [1]}
{"ts": "2026-10-07T14:03:13.871Z", "seq": 33, "kind": "batch", "session": "main", "n": 23, "tag": null, "head": "ALTER TABLE [sales].[Order]", "sha256": "4380df30...", "chars": 93, "ms": 41.0, "error": {"number": 207, "sqlstate": null, "class": "OTHER", "message": "[Microsoft][SQL Server]Invalid column name 'Status'."}}
```

Other heads: `CREATE OR ALTER PROCEDURE [sales].[usp_x]`, `CREATE INDEX [IX_a] ON [sales].[Order]`,
`SET XACT_ABORT ON`, `SELECT`, `EXEC [sys].[sp_rename]`, `BEGIN TRANSACTION`, `COMMIT TRANSACTION`,
`IF ... ROLLBACK TRANSACTION`. A batch that the lexer of the tool cannot read is `unreadable`.

`session_closed`: the session was closed, with its count of batches and their total time.

```json
{"ts": "2026-10-07T14:03:13.990Z", "seq": 38, "kind": "session_closed", "session": "main", "batches": 27, "ms": 311.2}
```

`exception`: the error that ended the command. `stack` is file, line and function of each frame,
the place of the raise last; no local value, no source line. `causes` is the chain behind the
error. An engine error has `number`, `sqlstate`, `class` and the redacted `message`. An error of
the tool has `reason_code`, `exit_code` and the key names of its detail. An error of any other
type has its type and stack only: its message is never written.

```json
{"ts": "2026-10-07T14:03:13.995Z", "seq": 39, "kind": "exception", "type": "RunError", "reason_code": "BATCH_FAILED", "exit_code": 21, "detail_keys": ["error_class", "error_number", "step"], "stack": [["cli.py", 1223, "main"], ["runner.py", 1378, "deploy"], ["runner.py", 309, "_command"]], "causes": [{"type": "SqlError", "number": 207, "sqlstate": null, "class": "OTHER", "message": "[Microsoft][SQL Server]Invalid column name 'Status'.", "stack": [["runner.py", 600, "_send"]]}]}
```

`end`: the last event: the exit code, the reason code and the message that stderr got.

```json
{"ts": "2026-10-07T14:03:14.002Z", "seq": 40, "kind": "end", "exit_code": 21, "reason_code": "BATCH_FAILED", "message": "step 0007__add_status.sql#1 failed: [OTHER 207] [Microsoft][SQL Server]Invalid column name 'Status'.. The unit of work was rolled back; nothing of it remains"}
```

A log with no `end` event is of a process that was stopped: job cancelled, runner lost, or killed.
The last `batch` event is then the last batch that came back; a batch that was sent and never came
back has no event. Each event is flushed to the file when it is written.

## 4. What is never inside

- The text of a batch: no statement, no literal, no number, no variable, no comment. A test sends
  batches with literals, a fake token and a connection string, and searches the file for each.
- The text of an object definition. A module is its head: `CREATE OR ALTER PROCEDURE [s].[n]`.
- A value from the database: a result set is a row count.
- The full engine message. The log holds the redacted message (every quoted and parenthesised
  value is `<redacted>`), also when the command ran with `--show-error-text`. One exception:
  engine messages that hold only names of objects and columns. Four name objects that exist in
  the database and keep the names (1913, 2714, 3726, 5074), for example
  `[Microsoft][SQL Server]The index 'IX_Order_Cust' is dependent on column 'CustId'.`. Five print
  a name as the statement wrote it (207, 208, 2705, 3701, 4902), for example
  `[Microsoft][SQL Server]Invalid column name 'Status'.`. They keep the name only when the batch
  that was sent holds it as an identifier; a statement that dynamic SQL built from data can put
  a row value there, and then the part is `<redacted>`.
- The login and the password of SQL authentication, when the sign-in is `sql`
  (`AZSQLCD_AUTH=sql`). The value of `AZSQLCD_SQL_PASSWORD` is replaced by `<hidden>` wherever
  it stands, and the value of `AZSQLCD_SQL_USER` where it stands as a whole word, in every string
  of the log, before a long string is cut. Both are also hidden in the form that a connection
  string holds them (in braces, with `}` doubled). With another sign-in the two variables are
  not read and nothing is replaced.
- The access token, a password, a connection string. The recorder refuses a field named `text`,
  `sql`, `batch`, `definition`, `token`, `password`, `secret` or `connection_string`.
- The value of `--reason`, `--approved-by`, `--triggering-actor`, `--ci-run-url`,
  `--confirm-database`, `--rename` and of every resolve action: `<set>`.
- The message of an exception that the tool does not know, and the detail of an error of the tool
  (its key names only).
- Environment variables other than the nine of the `run` event.

## 5. What is inside, and you may want to check

Open the `.jsonl` file in a text editor before you send it. It is plain text.

- Names: schemas, tables, indexes, procedures, the project, the environment, the target id, the
  database, the server (`<name>.database.windows.net`), the GitHub repository.
- Paths of the runner or of your machine in `argv` (`--out`, `--bundle`, `--root`): a home
  directory holds a user name.
- Names of migration files and steps, in the message of `end` and in `report.json`.
- The sha256 of each batch. A hash of a short batch with a guessable value can be tested against
  guesses by someone who has the rest of the text.
- With SQL authentication, `<hidden>` in a place where you expect a name: an object name, a
  folder of a path or a word that is equal to the login is hidden too, also in `argv`. A reader
  of the log can then tell that the login is that name. A longer word that only holds the login
  (`deploy_login_old` for the login `deploy_login`) is not changed.
- Text of an engine message outside quotes and parentheses. The message of a `THROW` in your own
  script passes as written: the redaction is not a secret scanner.

## 6. Read a log yourself

```
azsqlcd show-log --latest --log-dir report/logs
```

```
log: azsqlcd-20261007T140311Z-deploy.jsonl
command: azsqlcd deploy  (tool 0.1.0, digest c96a705cbd93, python 3.12.12)
argv: deploy --bundle release --digest 3f657d51... --env dev --target sales-dev --inline-plan --out report --ci github
target: sales-dev (dev), database sales on sql-sales-dev.database.windows.net, project sales, table_model false
sign-in: entra
time: 2026-10-07T14:03:11.120Z to 2026-10-07T14:03:14.002Z (2.9 s), 40 event(s)
batches: main 27 (311 ms), parse 5 (48 ms)
last batch: main #27: UPDATE [azsqlcd].[run] (tag none, sha256 51c1c3de1e5f, 321 chars, 9.1 ms)
  later statements of that batch: IF ... THROW
error in batch main #23 (ALTER TABLE [sales].[Order]): [OTHER 207] [Microsoft][SQL Server]Invalid column name 'Status'.
exception: RunError BATCH_FAILED at runner.py:309 in _command
end: exit 21 BATCH_FAILED: step 0007__add_status.sql#1 failed: [OTHER 207] [Microsoft][SQL Server]Invalid column name 'Status'.. The unit of work was rolled back; nothing of it remains
```

How to read it:

1. `end` gives the exit code and the reason code. Find the row in `docs/runbook.md`: the exit code
   tells the state of the database. `end: none` means that the process was stopped; treat the run
   as the runbook case "Job cancelled or runner lost".
2. `error in batch` names the batch that the engine refused. `last batch` is the last batch that
   came back. After a failed batch the tool still sends its rollback and closes the run row, so
   the two differ.
3. `sha256` is the hash of the batch text as it was sent. The same hash on two lines is the same
   text sent twice: a pending batch goes to the session `parse` for the syntax check and then to
   the session `main`. To find the batch in the release, use the step that `report.json` and the
   message of `end` name, then `head` (the object) and `chars` (the length).
4. One line per batch, for a closer look:

   ```
   jq -r 'select(.kind=="batch") | [.seq, .session, .n, .tag // "-", .head, .ms, (.error.number // "")] | @tsv' report/logs/azsqlcd-20261007T140311Z-deploy.jsonl
   ```

## 7. Text to paste for an AI assistant

Attach `azsqlcd-support.zip`, then paste this and fill in the last block.

```
This zip is a triage bundle of azsqlcd, a Python command-line tool that deploys schema changes
(tables, views, procedures, functions, triggers) to Azure SQL Database from GitHub Actions.

Files in the zip:
- azsqlcd-<time>-<command>.jsonl: the triage log, one JSON object per line, in order (seq).
  Kinds: run (command, argv, versions, CI facts), config (the target), connect (a session was
  opened), batch (one batch that was sent), session_closed, exception, end (exit code and reason
  code). A batch event has no SQL text. It has: session, n, tag (the name of a query of the tool),
  head (the statement class: keywords and object names), then (later statements of the batch),
  sha256, chars, ms, result_sets (row counts), and on failure error (number, sqlstate, class,
  redacted message). "<redacted>" and "<set>" stand for values that were removed on purpose.
- report.json, plan.json, manifest.json, when the run wrote them.
- versions.txt: versions of the tool, Python, the platform and the driver.

Facts of the tool:
- Exit codes: 0 ok; 21 failed and rolled back; 22 refused, nothing was executed; 23 outcome
  unknown, a human must inspect; 24 clean stop, safe to start again; 25 another run holds the
  lock; 30 drift found. Any other exit code: the tool did not start.
- Every non-zero exit has a reason code (UPPER_SNAKE). docs/runbook.md of the tool repository has
  one row for each reason code under its exit code: the state of the database and what to do.
  docs/triage.md describes the log. docs/known-gaps.md lists what is not built or not proven.
- A log with no "end" event is of a process that was stopped from outside.
- The first session is "main". The session "parse" is the syntax check of the plan.

You have no access to the database and I cannot send SQL text, object definitions or data. If
you need more, ask me for a fact that I can read for you (an object name, a count, a row of the
runbook, the output of a command).

What I need from you:
1. What the tool did, in order, in a few lines, and the exact batch (session, n, head) where it
   stopped.
2. The most likely cause, with the evidence from the log, and what else could explain it.
3. The state of the database according to the exit code and the reason code.
4. The next step: the runbook row to follow, or the exact command to run, or the fact you need.
5. Whether this looks like a defect of the tool (say which file and function of the stack) or a
   problem of the release, the database or the environment.

What happened, in my words: <one or two sentences>
Environment and target: <env> / <target>
What I ran, or the link of the workflow run: <command or URL>
```
