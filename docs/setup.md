# azsqlcd setup

One-time setup, in order. Sections 1 and 5 are done once for the organisation. Sections 2 to 4 and
6 to 8 are done once for each database repository. Section 9 is done for each tool upgrade.

Status: this document was not run step by step against GitHub and Azure. Two parts ran on
2026-10-07: the command-line path of section 7, on a disposable database with one environment,
and the workflows in a scratch demo repository of a user account, up to the Azure login step
(`README.md`, section "Status"; `docs/known-gaps.md`, section 12). Each behaviour that the steps
depend on is in `docs/known-gaps.md` with the test that proves it. Run those tests on a scratch
repository first (G1 to G7, W1 to W15 and B1 to B6).

Assumed product: GitHub Enterprise Cloud. Section 10 lists what differs elsewhere.

## 1. Tool repository rules

The tool repository `akaalholdings/azsqlcd` holds code that later runs with the production deploy
identity of every database. Its rules are as strict as those of a database repository.

These rules are for the copy of the tool that an organisation keeps for its own databases
(`docs/porting.md` makes that copy). The upstream repository, where the tool is developed, can be
public: rule 1 is not for it, and rules 2 and 3 are. A database repository runs the code behind
the tag that its workflows name, with its own deploy identity. That is why an organisation keeps
its own copy and protects the tag.

1. Visibility of the organisation's own copy: private or internal (recommended), so that only
   the database repositories of the organisation can call the workflows. Settings > Actions >
   General > Access: "Accessible from repositories in the organization". Without it no database
   repository can call the workflows of a private or internal repository.
2. Branch ruleset on `main`: pull request, one approval, stale approvals dismissed, approval of
   the most recent push, required check = the four `test` jobs of `ci.yml`, no bypass actors.
3. Tag ruleset on `v*`: no update, no deletion, no bypass actors. Restrict creation to the people
   who release the tool. A tag that moves changes the code in every database repository.
4. Organisation Actions policy: allow only the actions that the workflows pin by commit:
   `actions/checkout`, `actions/upload-artifact`, `actions/download-artifact`, `azure/login`,
   `astral-sh/setup-uv`, and `akaalholdings/*`.
5. Release of the tool: one pull request changes the version in `pyproject.toml`, in
   `src/azsqlcd/__init__.py` and in every `@v...` reference of `action.yml`, `.github/workflows/`
   and `templates/`. The test `test_workflow_files.py` fails if one place is missed. After the
   merge, tag the merge commit `v<version>`.

## 2. Identities and federated credentials

Six user-assigned managed identities for each database repository. No identity gets an Azure
role; all rights are database rights (section 6).

| Identity | Environments | Rights in the database |
|---|---|---|
| `<project>-nonprod-plan` | dev-plan, sandbox-plan, test-plan | read |
| `<project>-prod-plan` | preprod-plan, prod-plan | read |
| `<project>-nonprod-deploy` | dev, sandbox | deploy |
| `<project>-test-deploy` | test | deploy |
| `<project>-preprod-deploy` | preprod | deploy |
| `<project>-prod-deploy` | prod | deploy |

1. Print the commands: `uv run python scripts/setup_repo.py --repo <owner>/<name> --print-azure`
   The subject of a federated credential must be equal to the subject that GitHub sends. GitHub
   can send the immutable form `repo:OWNER@OWNER_ID/REPO@REPO_ID:environment:ENV` (seen on GitHub
   Actions on 2026-10-07), and not `repo:OWNER/REPO:environment:ENV`. Read what GitHub sends for
   the repository: `gh api repos/OWNER/REPO/actions/oidc/customization/sub`, field
   `sub_claim_prefix`. `scripts/setup_repo.py --print-azure` reads that prefix and builds each
   subject from it. Compare the prefix with the printed subjects before you create a credential.
   No credential was tested against Entra (`docs/known-gaps.md`, section 12).
2. Read them. Set `RG` and `LOCATION`. Run section 1 (identities) and section 2 (credentials with
   the default subject) of the output. Section 2 has no credential for environment `prod`.
3. Put the six client ids and the tenant id into `azsqlcd.toml` (section 3).
4. **Blocking for prod (A29).** The subject of the prod deploy identity must hold
   `job_workflow_ref`, so that only the reusable workflows of the tool, at the tool tag, can get the
   prod token. A workflow file that a repository administrator adds cannot. GitHub sets the subject
   form for the whole repository, so all ten environments change together. Follow section 3 of the
   output in its order: create the long-subject credentials, switch the subject form with the `gh`
   command, delete the short-subject credentials. Prove it on a scratch repository first (G6).
   `setup_repo.py --check` stays red until the subject form holds `job_workflow_ref`.
   One identity holds at most 20 federated credentials (not verified). `<project>-nonprod-plan`
   needs 9 for each tool tag, so the credentials of two tags fit and those of three do not.
5. No credential exists for a pull request subject or a branch subject. Never add one.

### Three ways to sign in

A database command (`plan`, `deploy`, `drift`, `export`, `baseline`, `resolve`) signs in as the
variable `AZSQLCD_AUTH` says. The value is exact and in lower case. Not set or empty means `entra`.
Any other value is exit 22 `AUTH_INVALID`, before the release is read. All three use the same
connection settings (`Encrypt=yes`, `TrustServerCertificate=no`), the same fence and the same
state tables. The plan output, the step summary, `plan.json` (note `sign-in: <kind>`),
`report.json` (key `auth`) and the triage log name the kind of sign-in. They never name a login or
a client id.

Status: only `entra` ran against a database. `managed-identity` and `sql` are proven by unit tests
only (`docs/known-gaps.md`, section 13).

| `AZSQLCD_AUTH` | Where the identity comes from | Where it can be used | What the tool checks |
|---|---|---|---|
| not set, or `entra` | The Azure CLI session: `az login` on a workstation, the step `azure/login` with OIDC in a workflow | Workstation and workflow | A token can be had (24 `TOKEN_UNAVAILABLE`). Its life: `min_token_minutes` for a command that writes, 2 minutes for a read-only command (24 `TOKEN_TOO_SHORT`) |
| `managed-identity` | The managed identity of the machine, asked at its identity endpoint. No Azure CLI is used. `AZSQLCD_MANAGED_IDENTITY_CLIENT_ID` names a user-assigned identity; not set means the system-assigned identity | A machine in Azure that has the identity: a self-hosted runner, or a virtual machine that a person works on. A GitHub-hosted runner has none | `AZSQLCD_MANAGED_IDENTITY_CLIENT_ID`, when set, is a GUID (22 `AUTH_INVALID`). A token can be had (24 `TOKEN_UNAVAILABLE`). The same token life as `entra` |
| `sql` | SQL authentication: the login in `AZSQLCD_SQL_USER`, the password in `AZSQLCD_SQL_PASSWORD` | Workstation only. Refused in GitHub Actions, with no override | Both variables are set (22 `SQL_AUTH_MISSING`). Neither holds a control character, the password has at least 8 characters, and the command does not run in GitHub Actions (22 `AUTH_INVALID`). No token exists, so no token life is checked |

#### Entra through the Azure CLI (the default)

- Workstation: `az login`, then run the command. Sections 2 to 6 describe this sign-in for the
  workflows.
- Workflow: the step `azure/login` signs the Azure CLI in with OIDC, with the plan or the deploy
  client id of the environment. This is `auth = "oidc"` in `azsqlcd.toml`, the default. The
  workflows then set `AZSQLCD_AUTH=entra` for the job.

#### Managed identity

Setting, by pull request, for each environment that uses it:

```toml
[env.dev]
auth = "managed-identity"   # default: "oidc". Any other value: CONFIG_INVALID
```

What the reusable workflows (`stage.yml`, `drift.yml`, `onboard.yml`, `resolve.yml`) then do for
that environment:

- The step `azure/login` does not run.
- The job gets `AZSQLCD_AUTH=managed-identity`, and `AZSQLCD_MANAGED_IDENTITY_CLIENT_ID` with the
  client id that `azure/login` got before: the plan client id in a plan, drift, export or
  baseline-report job, the deploy client id in a deploy, baseline or resolve job.
- The tool asks the managed identity of the runner machine for the token.

What you must do:

1. The identities of `[identities]` in `azsqlcd.toml` are then the client ids of user-assigned
   managed identities. Section 2 creates such identities already; the same six can be used.
2. Assign each identity to the runner machines that run its jobs (step 4 below says which).
3. `azsqlcd setup-sql` makes the database users in the same way (`WITH SID = <client id>,
   TYPE = E`). Section 6 does not change.
4. Runners: a GitHub-hosted runner has no managed identity. This sign-in needs self-hosted runners
   in Azure (section 5).
5. Federated credentials are not needed. `scripts/setup_repo.py --print-azure` prints them for
   OIDC only: skip sections 2 and 3 of its output for an environment with a managed identity.
   Section 2, step 4 (the subject with `job_workflow_ref`) then protects nothing for that
   environment.

**The trust boundary changes.** Read this before you choose the setting.

- With OIDC the identity is bound to the workflow run. Entra gives a token only to a job whose
  subject matches the federated credential: the environment, and with section 2, step 4 also
  `job_workflow_ref`.
- With a managed identity the identity is bound to the runner machine. Every job that can run on
  that runner can ask for the token of every identity of that runner. The subject of the job is
  not checked by anyone.
- The approval of a GitHub environment still gates the deploy job of the tool. It does not bind
  the identity: another job on the same runner needs no approval to use it.
- The reusable workflows have one `runs-on` input for the plan jobs and the deploy jobs of a
  stage. With the template as it is, both identities of an environment sit on the same runners.

So:

1. Separate runner groups for the plan identities and for the deploy identities, and for
   non-production and production. A runner holds only the identities of its group. The plan jobs
   and the deploy jobs of one stage share one `runs-on` value, so this split needs a change of
   the workflows; it is not built (`docs/known-gaps.md`, section 13). Until then a runner of a
   stage holds the plan identity and the deploy identity of that stage.
2. Give a runner group only to the database repositories that need it. Where the GitHub plan
   allows it, limit the group also to the reusable workflows of the tool at a tag (section 5,
   step 3, "Workflow access"). With a managed identity this limit is the only thing that keeps
   other workflow code away from the deploy identity.
3. Prefer runners that are used for one job and then destroyed (section 5, step 1).
4. Do not set `AZSQLCD_AUTH` as a machine variable of a runner. The workflows set it for each job.

#### SQL authentication on a workstation

For a workstation that cannot use an Entra sign-in. Not for a pipeline: with `AZSQLCD_AUTH=sql`
the tool stops with 22 `AUTH_INVALID` when the job shows that it runs in GitHub Actions. It reads
three things: the variable `GITHUB_ACTIONS` is `true` or `1` (any letter case, spaces around it
dropped), the variable `GITHUB_RUN_ID` is set, or the command has `--ci github`. The tool has no
option that switches the refusal off. The refusal guards against a mistake. It is not a control
against the author of a workflow: a step that removes both variables and leaves out `--ci github`
is not refused. Branch protection and the review of workflow files are the control.

- The login is read from `AZSQLCD_SQL_USER`, the password from `AZSQLCD_SQL_PASSWORD`. The
  password is read from the environment only. No argument takes it, `azsqlcd.toml` refuses a key
  for it, and no file holds it.
- The tool writes neither value. They are taken out of every message, of `plan.json`, of
  `report.json` and of the triage log (`<hidden>`): the password wherever it stands, the login
  where it stands as a whole word, both also in the form of a connection string (in braces, `}`
  doubled). A driver text that names `UID=` or `PWD=` is cut at that keyword. A failed login
  prints the error number and the class, not the text of the driver (24 `CONNECT_FAILED`).
- The password has at least 8 characters, as Azure SQL Database asks. A shorter value is refused
  (22 `AUTH_INVALID`): it is a mistake, for example the wrong variable.
- A name of the triage log that is equal to the login (a schema, a folder) is written as
  `<hidden>` too. Choose a login that is not the name of a schema, a database or an environment.
- The token-life checks do not apply. The plan prints
  `token minutes left: not applicable (SQL authentication has no access token)`.
- `setup-sql` creates no user for a SQL login. A DBA creates the user of the login and gives it
  the rights that the script gives to the plan user or the deploy user (section 6, step 2).
- The engine stores the login name in `azsqlcd.run.principal_name` of each run row. The tool
  never reads that column back.

Set the password for one session. Do not type it on a command line: the shell history keeps
command lines. Read it from a prompt.

PowerShell (the variables live in this process only):

```powershell
$env:AZSQLCD_AUTH = "sql"
$env:AZSQLCD_SQL_USER = "deploy_login"
$secure = Read-Host -Prompt "Password of deploy_login" -AsSecureString
$env:AZSQLCD_SQL_PASSWORD = [System.Net.NetworkCredential]::new("", $secure).Password
Remove-Variable secure
```

bash (the variables live in this shell and its child processes):

```bash
export AZSQLCD_AUTH=sql AZSQLCD_SQL_USER=deploy_login
read -r -s -p "Password of deploy_login: " AZSQLCD_SQL_PASSWORD && export AZSQLCD_SQL_PASSWORD
```

When the work is done, close the shell, or remove the variable:
`Remove-Item Env:AZSQLCD_SQL_PASSWORD` (PowerShell), `unset AZSQLCD_SQL_PASSWORD` (bash). A
password with a control character (a tab or a line break included) is refused.

### User account instead of an organisation

This document assumes an organisation. When the owner of the repositories is a GitHub user
account, three things differ:

- The Actions access level of the tool repository is `user`, not `organization`:
  `gh api -X PUT repos/OWNER/TOOL_REPO/actions/permissions/access -f access_level=user`.
  GitHub refuses the value `organization` there.
- Runner groups do not exist (section 5). The `runs-on` value of each caller workflow cannot name
  a group.
- A team does not exist. Name users in `.github/CODEOWNERS`, not a team.

## 3. Database repository

1. Create the repository (private). One repository for each database lineage.
2. Copy `templates/db-repo/` of the tool repository into it.
3. Edit `azsqlcd.toml`: project name, tenant id, client ids, one target for each environment.
   Use the failover-group listener name as `server` when the database is in a failover group.
   Rules that the tool checks (`CONFIG_INVALID`):
   - Every key is required except `gated`, `auth` (default `"oidc"`; section 2, "Three ways to
     sign in"), `[unmanaged]`, `[ack]`, `[scope]` (section 7, "One schema at a time"), and in
     `[project]` `module_chunk` (default 100),
     `data_batches` (default `false`) and `server_suffixes` (default: the four Azure SQL Database
     suffixes). A target server that ends with none of the suffixes is `CONFIG_INVALID`. An
     unknown key is an error. The message for a missing required key names the key, its table
     and one example line.
   - No key name may hold `key`, `secret` or `password`, in any letter case. This includes the
     names under `[identities]`: `monkey_deploy` is refused. The file holds no secrets.
   - An environment name is one of dev, sandbox, test, preprod, prod, disposable.
   - A target id is unique in the file and holds only letters, digits, `_` and `-`.
   - Leave `table_model = false` until the onboarding pull request (section 7).
4. Edit `.github/CODEOWNERS`: the DBA team. The team needs write access to the repository.
5. `gated` in `azsqlcd.toml` and `gated` of the stage in `.github/workflows/db.yml` must be equal.
   The stage stops if they differ. `gated = true` means: plan job, approval, deploy of that plan.
   The tool also refuses `deploy --inline-plan` for a gated environment (`INLINE_PLAN_GATED`).
6. Keep the verify job of `db.yml` as the template has it: it calls the reusable `verify.yml`,
   whose checkout has `fetch-depth: 0`. `verify` reads the files of the base commit and the
   first-parent history of `main`; with a shallow checkout it cannot read them and the check
   fails. A verify job that you write yourself needs `fetch-depth: 0` too. On a workstation run
   `git fetch origin` before `azsqlcd verify --base "$(git merge-base origin/main HEAD)"`.
7. Limit the admin role of the repository to the DBA team. An administrator can change
   environments and rulesets.
8. Add the topic: `gh repo edit <owner>/<name> --add-topic azsqlcd`
9. Push to `main` before the rulesets exist (section 4), or by pull request after.

## 4. Environments and rulesets

`scripts/setup_repo.py` needs the `gh` command line and a login with admin rights on the repository.

1. Apply: `uv run python scripts/setup_repo.py --repo <owner>/<name> --dba-team <team-slug>`
   It creates or corrects:
   - ten environments: dev, sandbox, test, preprod, prod and a `-plan` twin of each;
   - deployment branch `main` only, on all ten;
   - reviewers: the DBA team on preprod and prod; prevent self-review on prod;
   - branch ruleset `azsqlcd-main` on `main`: pull request, one approval, code-owner review, stale
     approvals dismissed, approval of the most recent push, squash merge only, required check
     `verify / verify`, no deletion, no force push, no bypass actors;
   - tag ruleset `azsqlcd-tags` on `r*` and `v*`: no update, no deletion, no bypass actors.
   The script reads first and sends only the difference. A second run sends nothing.
   Why squash merge only: one push to `main` builds one release, for the head commit, and a
   release applies only the migrations that its own commit added. A pull request of two commits
   that is merged with "Rebase and merge" puts the first migration in a commit that gets no
   release; every database then refuses the release of the head with `CATCHUP_REQUIRED`, and no
   workflow can create the missing release (second review, WP2-1). With a squash merge one pull
   request is one commit and one release. `--check` fails while the ruleset allows another
   method.
2. Do the items that the script prints as `manual:`:
   - Settings > Environments > preprod and prod: turn off "Allow administrators to bypass
     configured protection rules";
   - the subject form (section 2, step 4).
3. Check: `uv run python scripts/setup_repo.py --repo <owner>/<name> --dba-team <team-slug> --check`
   Exit 0 = the repository meets the policy. Exit 1 = a difference; each one is printed.
   Run the check on a schedule for every database repository. The script does not do this itself.
4. Open a first pull request. The check must appear with the name `verify / verify`. If GitHub
   shows another name, no pull request can merge: change `REQUIRED_CHECK` in `setup_repo.py` and
   apply again (W4).
5. Enable secret scanning and push protection on the repository.

## 5. Runners

Jobs without a database identity (`runs-on-light`: targets, gate, record, incident, verify,
release) run on a GitHub-hosted runner. They need bash, gh, jq and git.

Jobs with a database identity (`runs-on`: plan, deploy, drift, resolve, onboard) run on
self-hosted runners with a network path to the databases. Requirements:

1. **Ephemeral.** One job, then the machine is destroyed. A runner that lives on keeps the az
   login cache and the tool environment of the last job for the next one.
2. **Two runner groups.** `azsql-nonprod` for dev, sandbox, test and preprod; `azsql-prod` for
   prod. The prod group reaches only the prod private endpoint. The non-production group does not
   reach it.
3. **Group access.** Each group is limited to the database repositories. Limit both groups also
   to the workflows `akaalholdings/azsqlcd/.github/workflows/stage.yml@<tag>`, `resolve.yml@<tag>`,
   `drift.yml@<tag>` and `onboard.yml@<tag>` (runner group setting "Workflow access"). Without this
   limit a pull request that adds a job with `runs-on: {group: azsql-prod}` runs code in the prod
   network before any review. No script checks this setting.
4. **Image.** bash, gh, az, uv, Python 3.12, and the libraries of the driver: `libltdl7`,
   `libkrb5-3`, `libgssapi-krb5-2`.
5. **Offline mode (recommended for prod).** Fill the uv cache in the image for the tool tag
   (`uv sync --frozen --no-dev --no-install-project --extra db` in a checkout of the tag) and set
   the machine variable `AZSQLCD_OFFLINE=1`. The action then skips the uv download and runs
   `uv sync` with no network. A deploy then needs neither PyPI nor the setup-uv download.
6. **Network to the database.** Private endpoint; public access denied. Link the private DNS zone
   `privatelink.database.windows.net` to the runner network. Connect to
   `<server>.database.windows.net`, never to the private address. With the Redirect connection
   policy the runner needs ports 1433 to 65535 to the endpoint; with Proxy, 1433.
7. **Egress.** GitHub (API, action download, artefact storage, release assets, the OIDC token
   endpoint), `login.microsoftonline.com`, and PyPI unless offline mode is on.

A temporary firewall rule for hosted runners is not built.

## 6. setup-sql for each target

Only this script creates the state schema (A2). `deploy` and `baseline` stop with
`STATE_MISSING` when it did not run.

1. Print it: `azsqlcd setup-sql --env <env> --target <target>` (in a checkout of the database
   repository; `--root DIR` names another directory; no database access is needed).
2. An administrator of the database (Entra admin) reads it and runs it once on the target.
   It creates schema `azsqlcd` with owner `dbo`, the four state tables, the `meta` row (project,
   environment), the two database users of the environment and their grants. The users are named
   `azsqlcd-<project>-<env>-plan` and `azsqlcd-<project>-<env>-deploy`, each made from the client
   id of its identity (`CREATE USER ... WITH SID = ..., TYPE = E`). When plan and deploy have one
   client id the script makes the deploy user only. The deploy user gets `db_ddladmin`, SELECT,
   INSERT and UPDATE on schema `azsqlcd` (no DELETE), and no `db_securityadmin`. The plan user gets
   read rights only. Both get VIEW DEFINITION, VIEW DATABASE STATE and SELECT on
   `sys.sql_expression_dependencies`. With `auth = "managed-identity"` the script is the same:
   the client ids are those of the managed identities. For a SQL login (`AZSQLCD_AUTH=sql` on a
   workstation) the script makes no user: a DBA creates it by hand.
3. The script stops with an error in three cases. The database has another name than the target,
   or the server is not Azure SQL Database: nothing is changed. The database holds the state of
   another project or of another state version: no user is made and no right is granted. The name
   or the SID of a user is taken by another principal: no right is granted to it. A second run on
   the same target is written to change nothing.
4. On a database that is bound to another environment the script makes the users and their grants
   and then ends with the error "this database is bound to another environment". After a refresh
   from another environment this is expected: go on with `resolve --rebind-environment`
   (`docs/runbook.md`, "Refresh of an environment from prod"). In any other case the script ran in
   the wrong database: drop the two users that it made.
5. Data batches are switched off by default (`[project] data_batches`), and then the deploy user
   needs no data rights: skip this step. Only for a repository that sets `data_batches = true`,
   grant data rights on a schema to the deploy user where migrations hold data batches:
   `GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::[<schema>] TO [azsqlcd-<project>-<env>-deploy];`
6. Repeat for every target of every environment.
7. A repository with partitioned tables: a DBA creates each partition function and partition
   scheme in every target before the first release that uses it (section 7, "Partitioned
   tables"). `setup-sql` creates none.

## 7. Onboarding a database

Reference environment: prod. All database steps use the workflow `onboard.yml` of the database
repository. `<n>`, `<m>` and `<k>` are releases of the repository. The first push to `main` that
holds `azsqlcd.toml` creates the first one.

While a target is onboarded, the stages of each release stop for it: with `STATE_MISSING` until
section 6 is done, and with `BASELINE_REQUIRED` from the release that holds the line `baseline`
until step 7 is done. This is expected. For test, preprod and prod each stop opens an incident
issue; close them after the baseline.

Two modes. With `table_model = false` the tool manages modules only (views, procedures, functions,
triggers); table changes are hand-written migrations with no proof. With `table_model = true`
schemas, tables, types, sequences and synonyms are managed too, and `verify` proves each migration
against the files. The pull request that sets `table_model = true` must hold the table files and
no migration (`PRF004`); from then on every change of a table file needs a migration (`PRF002`).

Existing database:

1. Export from prod with the plan identity (read only):
   `gh workflow run onboard.yml --ref main -f environment=prod -f target=<target> -f action=export -f release=r<n>`
   The export reads the catalog. It needs the plan user of section 6; it does not read the state.
   It always writes the module files and the table-class files. An object (module or
   table-class) that `[unmanaged] objects` of `azsqlcd.toml` lists is left out, and its entry
   stays in the list at the end of `export.md`. The export stops with exit 22 when a
   definition cannot be read (`NO_VIEW_DEFINITION`) and when a table file would hold a
   `PASSWORD =` or `SECRET =` literal (`SECRET_LITERAL`).
2. Download the artefact: `gh run download <run-id> -n onboard-export-prod-<target> -D export`.
   It has the shape of a repository:
   - `schema/**`: one file for each module and each table-class object that the tool can manage;
   - `onboarding/prod/export.md`: each header edit of a module file, each object that stays
     unmanaged with its code and reason, and at the end the whole list for `[unmanaged] objects`.
     There is no `unmanaged.md`;
   - `onboarding/prod/snapshot.json`: the table captures of prod. The baseline report of each
     environment compares with it;
   - `onboarding/prod/rename-constraints.sql`, only when prod has constraints with a name that
     the engine made. The files use fixed names. A DBA reads the script and runs it on prod by
     hand; it holds `EXEC sys.sp_rename` only. The tool does not run it.
3. Read `export.md`. Each object in its unmanaged lists stays outside the tool; its change path is
   a `raw` batch. Then commit by one pull request:
   - `onboarding/prod/` of the artefact;
   - the list at the end of `export.md` as `[unmanaged] objects` in `azsqlcd.toml`;
   - the line `baseline` as line 2 of `migrations/migrations.sum`;
   - modules only: the directories `views`, `functions`, `procedures` and `triggers` of `schema/`.
     Leave the table-class directories out; a later export gives fresh ones for the switch;
   - table model: all of `schema/`, and `table_model = true` in `azsqlcd.toml`, in this same pull
     request, with no migration. `verify` gives the warning `PRF000`: there is no base model, so
     the table files are taken as they are.
4. Merge. The merge creates release r<m>.
5. For each environment, in the order dev, sandbox, test, preprod, prod, compare (read only):
   `gh workflow run onboard.yml --ref main -f environment=<env> -f target=<target> -f action=baseline-report -f release=r<m> -f confirm_database=<database>`
   `confirm_database` is the name exactly as `SELECT DB_NAME();` prints it. Read the artefact
   `onboard-baseline-report-<env>-<target>`: `baseline-diff.md`, the directory `modules/`, and
   `rename-constraints.sql` when the report made one.
   - Module `equal`: recorded with the checksum of its file. The first deploy does not send it.
   - Module `differs`: recorded with no source. The first deploy overwrites the live text with the
     file text, and the plan lists `OVERWRITE_MODULE`. `modules/` holds the live text. Step 6.
   - Module `only here`: it has no file. It stays unmanaged in this target and is never dropped.
   - Module `missing here`: the first deploy creates it. If an object of another kind has the
     name, the report says so and the plan will stop with `NAME_COLLISION`.
   - Table-class objects, with `table_model = true`: each one against `onboarding/prod/snapshot.json`
     of the release. `differs` or `missing_here`: step 7 will refuse. Refresh the environment from
     prod (runbook), or align it ("A database that differs from the files", below). `only_here`: unmanaged in this target. If the
     release holds no `snapshot.json` the report says that tables were not compared.
   - A constraint with a name that the engine made, and the shape of a constraint of prod: the
     report holds `rename-constraints.sql`. A DBA reads it and runs it on this database. The
     baseline accepts such a constraint under its fixed name; a later migration that names the
     constraint fails in a database where the script did not run.
6. Acknowledge the modules that the first deploy will overwrite. For each environment whose report
   has a module with `differs`, add `onboarding/<env>/overwrite-ack.toml` by pull request. The
   file has exactly one key, and the report prints the list to copy:

   ```toml
   modules = [
     "PROCEDURE:[sales].[usp_PlaceOrder]",
   ]
   ```

   A live change that must stay goes into the module file first, in the same pull request. The
   files under `modules/` are for the review; do not commit them. The merge creates release r<k>.
   Without the file the baseline stops with `BASELINE_ACK_REQUIRED`. If no module differs
   anywhere, r<k> is r<m>.
7. Record the database (writes state rows only; behind the approval of the deploy environment):
   `gh workflow run onboard.yml --ref main -f environment=<env> -f target=<target> -f action=baseline -f release=r<k> -f confirm_database=<database>`
   The baseline writes one object row for each module that has a file and a live module, with
   `table_model = true` one for each table-class object of the files, and a baseline step. With
   `table_model = true` it first reads every table-class object back against the files and stops
   with `READBACK_MISMATCH` when one differs or is missing. Its run row copies the recorded
   release; it does not move it. A second baseline stops with `ALREADY_BASELINED`.
8. After all five: `gh workflow run db.yml --ref main -f release=r<k>`. Every stage must end green.
   The first deploy overwrites the acknowledged modules and creates the missing ones.

Switch from `table_model = false` to `true`, for a repository that ran with modules only:

1. Do it when no migration is pending on any target: promote the newest release to prod first.
   The tool does not check this. A target that is behind differs from the table files, and step 5
   refuses it.
2. Export from prod again (step 1 above). Commit by one pull request: the table-class directories
   of `schema/` (`schemas`, `types`, `sequences`, `tables`, `synonyms`), `onboarding/prod/`, the
   new `[unmanaged] objects` list, and `table_model = true`. No migration in this pull request
   (`PRF004`). The merge creates release r<s>.
3. From r<s> on, every plan stops with `BASELINE_REQUIRED` until the target is recorded again:
   the table-class objects have no row yet.
4. For each environment run `baseline-report` with r<s> (step 5 above), and align what differs.
5. For each environment run `baseline` with r<s> (step 7 above). This second baseline is allowed
   once, while the state holds no row of a table-class object. It records the table-class objects
   only; the module rows stay as they are.
6. `gh workflow run db.yml --ref main -f release=r<s>`.

New, empty database: run section 6, then merge the first schema and migrations. The chain has no
line `baseline`, and the first deploy applies it. With the table model: first merge a pull request
that only sets `table_model = true`, then add the table files together with their migrations
(`azsqlcd gen --name <name>`). `init` is cut from this build.

On a workstation, with no workflow (the path of the live onboarding run of 2026-10-07, on one
environment `dev`; `<bundle>` is the `--out` of `build`, `<digest>` the digest that `build`
prints):

1. `azsqlcd export --root . --env dev --target <target> --out ../export`. Copy `schema/` and
   `onboarding/dev/` of the export into the repository, write the `[unmanaged] objects` list from
   `export.md`, set `table_model = true`, add the line `baseline` to `migrations/migrations.sum`.
2. `azsqlcd lint`, then `azsqlcd verify --base "$(git merge-base origin/main HEAD)"`. Commit to
   `main`.
3. `azsqlcd build --commit HEAD --out ../dist`.
4. `azsqlcd setup-sql --env dev --target <target>`: an administrator runs the script that it
   prints. Before that, `baseline` stops with `STATE_MISSING`.
5. `azsqlcd baseline --report-only --bundle ../dist --digest <digest> --env dev --target <target> --confirm-database <database>`.
   It writes `onboarding/dev/baseline-diff.md` and the live text of each module that differs
   under `onboarding/dev/modules/`. These files land in the working directory: do not commit
   `modules/`. With no `onboarding/prod/snapshot.json` in the release the report says "Modules
   only" and names no constraint (`docs/known-gaps.md`, section 11, T1).
6. A constraint with a name that the engine made: the baseline of step 8 stops with
   `CONSTRAINT_NAMES` and writes `onboarding/dev/rename-constraints.sql` (names and `sp_rename`
   only). A DBA reviews and runs it; then run step 8 again.
7. A module that differs: write `onboarding/dev/overwrite-ack.toml` (step 6 of the list above),
   merge, build again. The live run had no module that differed, so this file was not exercised
   there.
8. `azsqlcd baseline --bundle ../dist --digest <digest> --env dev --target <target> --confirm-database <database> --out ../out`.
9. `azsqlcd plan ...` now says `record, pending = true`, recorded r0: the baseline does not move
   the recorded release. The first `azsqlcd deploy` records the release and sends only the
   acknowledged and the missing modules (in the live run: 0 steps, 0 modules). After it the plan
   says `noop`.

The table comparison of `baseline --report-only` reads `onboarding/prod/snapshot.json` of the
release. A repository that starts with another environment has no such file; the write baseline
still reads every table-class object back against the files.

An export is not a round trip for table-class files. Table and type files come back with the
expression text as the engine stores it (`DEFAULT ((1))`, `sysutcdatetime()`, `IN` as `OR`, a
filter in parentheses) and with the indexes of a file in name order; module files and the file of
a temporal table came back byte-equal in the live run (13 of 14 table files and 1 of 2 type files
differed). The exported files are the model at onboarding only. Do not copy an export over a
repository that already runs with `table_model = true`: `verify` then fails with `PRF002` and
`gen` refuses (`COMPUTED_CHANGE`, `USER_TYPE_CHANGE`). `schema/_tombstones.toml` is not exported.

### A database that differs from the files

Lower environments often differ from prod: other columns, indexes, constraints or compression;
foreign keys that are disabled or not trusted; older modules. `baseline` refuses such a table
(`READBACK_MISMATCH`). `azsqlcd align` writes the script that makes the database equal to the
files. It only reads the database. It never runs the script.

1. In the repository, with the files that the next release will hold:
   `azsqlcd align --root . --env <env> --target <target> [--schema <schema>] [--out <dir>]`.
   It writes `onboarding/<env>/align.sql` and `onboarding/<env>/align.md` under `--out` (default:
   `--root`). Do not commit them.
2. Read `align.md`. It gives the count of each section and each object with what differs.
   - "To review": a dropped column, or a type that may not hold every value. The statement is in
     `align.sql`, commented out, after a line `-- REVIEW: loses data: <why>`. Also a mask that
     only the database has: after `-- REVIEW: unmasks column ...`, every reader sees the real
     values. Remove the comment marks only when the loss or the unmask is accepted.
   - "Hand-write": a change that `gen` refuses (for example `PARTITION_CHANGE`,
     `TEMPORAL_CHANGE`, `IDENTITY_CHANGE`, `COLLATION_CHANGE`), an object that the tool cannot
     read (`UNSUPPORTED`), a file for the name of an external table or a history table, or a
     module whose name another kind of object holds. `align.sql` has no statement for it. Write
     it by hand, or refresh the environment from prod. When only these items differ, `align.sql`
     holds `-- no statement: see Hand-write in align.md`.
   - "Only in the database": an object with no file, and a count for each kind and schema. It is
     never dropped, and the engine names of its constraints stay. Add its file, exclude it in
     `[scope]` of `azsqlcd.toml`, or drop it by hand.
3. `align.sql` runs in this order: constraint names (`sp_rename`), table-class objects, modules,
   constraint state. The constraint state statements build an index again
   (`ALTER INDEX ... REBUILD`) or check every row of their table (`WITH CHECK CHECK CONSTRAINT`).
   Every rebuild comes first, the clustered index first in its table: a foreign key cannot be
   enabled while the index of its key is disabled. Each statement has the row count of its table
   in a comment, smallest table first.
4. A DBA runs `align.sql` in a maintenance window, batch by batch.
5. Run `baseline --report-only`, then `baseline` (or `baseline --schema <schema>`). It must pass.
   Run `align` again: on a database that equals the files it writes "nothing to align".

### One schema at a time

A database with many schemas is put under the tool one schema at a time. The optional table
`[scope]` of `azsqlcd.toml` says which schemas the tool manages:

```toml
[scope]
schemas = ["sales", "dbo"]            # the managed schemas; ["*"] = every schema not excluded
exclude = ["dba", "scratch"]          # schemas that were reviewed and are not managed
exclude_objects = ["dbo.temp_*"]      # schema.name patterns inside managed schemas; * and ? are wildcards
```

Without `[scope]` every schema is managed, as before. With it:

- The tool manages every object of a schema in `schemas`, except an object whose `schema.name`
  matches a pattern of `exclude_objects` (`*` is any text, `?` one character, every other
  character is itself: `_` is no wildcard), an object of `[unmanaged] objects`, a history table of
  a temporal table (as before) and an external table. External tables are skipped by their type
  everywhere; a report gives their number, never one line for each. A DML trigger follows its
  table: the trigger of a table outside the scope is outside too.
- A pattern is read as `schema.name` split at the first dot, and each part matches its own name:
  `a.b.c*` is the schema `a` and the name `b.c*`. It matches `[a].[b.c1]`, never `[a.b].[c1]`. A
  pattern cannot name a schema that holds a dot: list such objects in `[unmanaged] objects`.
- What the scope does not manage is invisible: `export`, `plan` and `drift` do not read the
  schemas outside it, list no object of them as unmanaged and compare nothing there.
- A schema of the database in neither `schemas` nor `exclude` (`schemas` is not `["*"]`) is the
  finding `SCHEMA_UNSCOPED`: a note of the plan (output, `plan.json`, job summary) and a line of
  `drift` with the class `unscoped`. It never fails a command and does not change an exit code.
- A file of the repository for an object outside the scope is `OUT_OF_SCOPE` in `lint`, `build`
  and `verify`: the config and the files contradict each other. A trigger file on a table outside
  the scope too.
- A managed table with a foreign key to a table outside the scope stays unmanaged at export
  (`DEPENDS_ON_UNMANAGED`), as with a key to an unmanaged table. A managed module may reference an
  object outside the scope, as it may reference an unmanaged one.
- A module outside the scope is, for a release that changes a table, what an unmanaged module is:
  a dependant that blocks the release (`TABLE_BLOCKER`) unless `[ack] unmanaged_dependants` lists
  it, never unbound and never refreshed. A schema-bound one blocks the change of its column until
  a DBA changes it. `resolve --adopt-module` refuses it (`OUT_OF_SCOPE`).
- Rules of the table (`CONFIG_INVALID`, the message names the key path): `schemas` is required and
  not empty; `"*"` stands alone in `schemas`; no schema is in both lists; no entry is listed twice;
  a pattern is `schema.name` with both parts written, and its schema part (when it has no
  wildcard) is a managed schema, else it could never match; no list names `azsqlcd`, the schema of
  the tool. A name is written as the catalog holds it: no brackets, no space at either end, no
  control character. Names are compared without letter case, as the catalog compares them.

Onboarding, first schema: list it in `[scope] schemas` (and `exclude` what was reviewed), then
follow "Existing database" above. The export writes files for the managed objects only, and
`export.md` has a part "Scope".

Each further schema, here `billing` (the order dev, sandbox, test, preprod, prod holds as above):

1. Export the schema alone from the reference environment:
   `gh workflow run onboard.yml --ref main -f environment=prod -f target=<target> -f action=export -f release=r<n> -f schema=billing`
   (on a workstation: `azsqlcd export --root . --env prod --target <target> --out ../export --schema billing`).
   It writes the files of `billing` only. `onboarding/prod/snapshot.json` is the snapshot of the
   repository with the captures of `billing` added; the captures of the other schemas stay as they
   are. A key of `billing` to a table of a schema that the scope manages already is kept. The
   command prints what to add: `Add "billing" to [scope] schemas of azsqlcd.toml`. The tool never
   edits `azsqlcd.toml`. A schema in `exclude` or the schema `azsqlcd` is refused (`OUT_OF_SCOPE`),
   a schema that the database does not have too (`SCHEMA_NOT_FOUND`).
2. One pull request: `schema/**` of `billing` and `onboarding/prod/` of the export, the
   `[unmanaged] objects` list of `export.md`, and `"billing"` in `[scope] schemas`. No change to
   the files of the other schemas. `verify` gives the warning `PRF000`: the objects that enter the
   scope are taken as their files say, and the message names `azsqlcd baseline --schema billing`.
   `gen` writes no `CREATE` for them. Merge; the merge creates release r<b>.
3. For each environment, the report, then the record (behind the approval of the environment):
   `gh workflow run onboard.yml --ref main -f environment=<env> -f target=<target> -f action=baseline-report -f release=r<b> -f confirm_database=<database> -f schema=billing`
   `gh workflow run onboard.yml --ref main -f environment=<env> -f target=<target> -f action=baseline -f release=r<b> -f confirm_database=<database> -f schema=billing`
   The baseline of a schema compares and records the objects of `billing` that have no managed
   row in this database, with a baseline step whose note starts with `schema [billing]:`. A row
   with the status managed is not read or written, of this schema or of another. Until it ran, the
   plan of r<b> stops with `BASELINE_REQUIRED` and names the command. When nothing is left to
   record it is `ALREADY_BASELINED`; a schema that `[scope]` of the release does not manage is
   `OUT_OF_SCOPE`. A table that differs from its file refuses the baseline (`READBACK_MISMATCH`):
   align the database by hand, or correct the file by pull request, then run it again.
4. Deploy r<b> as usual.

Repeat until the plan has no `SCHEMA_UNSCOPED` note: every schema is in `schemas` or in `exclude`.

The same baseline takes an object that enters the scope in a schema that is recorded already: a
pattern of `exclude_objects` that goes, with the file of the object. `verify` gives `PRF000`, the
plan stops with `BASELINE_REQUIRED` and names `azsqlcd baseline --schema <schema>`, which records
that object and nothing else.

To stop managing a schema, move it from `schemas` to `exclude` and delete its files in one pull
request: no migration and no tombstone are needed, and the tool drops nothing (`verify` gives no
`PRF000` and no `TMB001`). The first deploy of that release marks the managed rows of its objects
with the status `dropped` (a unit of work with one `unscope:<key>` step for each, state writes
only): the rows say "no longer managed", the objects stay as they are. When the schema comes back
into the scope, its objects have no managed row: `verify` gives `PRF000`, the plan stops with
`BASELINE_REQUIRED`, and `azsqlcd baseline --schema <schema>` compares each object with its file
again and records it, which replaces the old row; an object that differs from its file refuses the
baseline.

A managed table with a foreign key to a table that leaves the scope fails `verify` (`MDL001`: the
referenced table has no file) until the pull request drops the foreign key or keeps the table in
the scope.

The workflow `onboard.yml` takes the input `schema` from the release that holds this change on:
a database repository whose workflows call the reusable workflows of an older tag runs the export
and the baselines of one schema from a workstation (section 2, "Three ways to sign in"), with the
plan identity for `export` and `--report-only` and the deploy identity for the baseline that
writes.

### Withdraw and replace a merged migration

A merged migration never changes. When it is wrong (it failed in a database, or the plan refuses
its release), one pull request withdraws it and adds its replacement. The two cases of the live
runs:

- `0007` added a `NOT NULL` column with no `DEFAULT` and failed with exit 21 on a table with rows.
- `0002` altered a column that a schema-bound view names, with no unbind line; the plan refused
  the merged release with `TABLE_BLOCKER` (`verify` now reports this before the merge: `PRF007`).

Steps:

1. In `migrations/migrations.sum`, add the word `withdrawn` to the end of the line of the old
   migration. Do not change or delete its file.
2. Write the new migration file. Its first allow line names the old one:
   `-- azsqlcd:allow REPLACEMENT_EDGE 0007__r7_warehouse_region reason: <why>`.
   It must give the same table files as the withdrawn migration would have given. In the live
   run: `ADD [Region] varchar(10) NOT NULL CONSTRAINT [DF_Warehouse_Region_tmp] DEFAULT ('EU')`
   in one batch, then `DROP CONSTRAINT [DF_Warehouse_Region_tmp]` in the next.
3. `azsqlcd gen --resum`. Then add `replaces=0007__r7_warehouse_region.sql` to the end of the
   new line of `migrations.sum`, and run `azsqlcd gen --resum` again if lint asks for it.
4. `azsqlcd lint`, `azsqlcd verify --base "$(git merge-base origin/main HEAD)"`, merge.
5. The plan of the new release lists `REPLACEMENT_EDGE` as a destructive item. A database that
   never ran the old migration runs only the replacement.

Later releases wait behind a failed release. A release applies only the migrations that it
added, so a database that did not take release r3 refuses r4 and r5 with 22 `CATCHUP_REQUIRED`
("promote r3 first"). When the migration of r3 fails in that database (21 `BATCH_FAILED`, rolled
back, no step row), r3 can never be promoted there, and r4 and r5 wait, also when they change
other tables. The way out, as the scenario test of the tool walks it:

1. One pull request withdraws the migration of r3: the word `withdrawn` on its line of
   `migrations/migrations.sum`. With a replacement (the steps above), or with no replacement.
   With no replacement, put the table files back to the model without the migration; else
   `verify` refuses the pull request (`WDR004`). With `table_model = false` a withdrawal with no
   replacement is refused (`CHN006`).
2. The merge creates r6. Promote r6. Its plan holds the migrations of r4 and r5 and the note
   `catch-up in one release: ...`. The deploy sends them in one transaction, in chain order. The
   withdrawn migration is never sent to this database and gets no step row.
3. After a withdrawal with no replacement, the corrected change comes in a later pull request as
   a normal migration from `azsqlcd gen`, with no `replaces=` line. `verify` refuses a
   `replaces=` line for a migration that an earlier pull request withdrew (`WDR005`). That
   release (r7) runs only its own migration; the migrations of r4 and r5 do not run again.

Withdraw with no replacement only when no database applied the migration. A database that did
apply it keeps the change, and the tool does not report that until the next change of that table
stops with 21 `READBACK_MISMATCH`, or the corrected migration fails there with 21 `BATCH_FAILED`
because the change exists already (`docs/known-gaps.md`, N2-F5). The pipeline runs dev before
test, so a migration that failed in test was usually applied in dev: add the replacement in the
pull request of the withdrawal. The message of `CATCHUP_REQUIRED` says the same. The later migrations must not need the withdrawn one:
`docs/known-gaps.md`, section 13.

### Park a migration that fails

Opt-in. Without the key, a migration that fails in an environment stays pending there, and every
later release to that environment is refused with `CATCHUP_REQUIRED` until the migration is
withdrawn. With the key, the later releases deploy past it:

```toml
[project]
park_failed = true   # default false
```

What the tool does:

1. A deploy fails on a batch of migration M (a transaction rolled back on M: 21 `BATCH_FAILED` or
   21 `GOVERNANCE_LIMIT`; or a non-transactional batch of M: 23 `NONTX_FAILED`). The run ends as
   it does without the key: the same exit code and reason code.
2. From then on M is parked in that environment. A non-transactional M needs the two resolve
   steps of the "Unknown run" case of `docs/runbook.md` first (`--mark-not-applied`, then
   `--clear-run`). A lost connection, a lock timeout or a deadlock parks nothing.
3. The next deploy, of the same release or of a later one, runs everything but M. The plan has
   the note `parked: <file> (run <id>, <reason code>)`. The pending migrations of earlier
   releases run with the release, in chain order (note `catch-up past set-aside: ...`), in place
   of `CATCHUP_REQUIRED`. `azsqlcd drift` lists the park too (`parked: <file> (run <id>, <reason
   code>)`); a park is no drift.
4. What runs must be independent of M. A pending migration may not touch an object that M
   touches or names, and may not name an object that M touches. A module of the release may not
   name an object that M touches. Else the plan stops with 22 `SET_ASIDE_DEPENDENCY`. A raw or
   data batch is compared by the names in its text (note `independence not proven for a raw
   batch: <file>`). In a raw or data batch only names written with their schema
   (`[sales].[Order]`) count. In a module, a name with no schema is an object of dbo, as for the
   order of modules. A rename is compared by the names of its statement and, with
   `table_model = true`, also by the tables with a key to the renamed table.
5. The park ends in three ways. M is applied (for example after `resolve --mark-applied`). M is
   withdrawn by pull request: it never runs, and its replacement runs. Or an operator runs
   `azsqlcd resolve --unpark <file>` (RESOLVE with `-f action=unpark -f subject=<file>`) after the
   cause is fixed: the next deploy runs M at its place among the pending migrations. If M fails
   again, it is parked again.
6. A DBA applied the statements of M by hand: run `azsqlcd resolve --unpark <file>` first, then
   `azsqlcd resolve --mark-applied <file>`. `--mark-applied` of a parked migration is refused with
   `RESOLVE_NOT_APPLICABLE`.
7. Keep `park_failed = true` while a park holds, or while a migration that was parked is still
   pending. With the key off, a migration that later releases ran past is a hole: the plan stops
   with `CHAIN_DIVERGED`, and `--unpark` is refused. Switch the key on again, then unpark or
   withdraw the migration.

No column and no status was added to the state tables. A park is the run row of the failed deploy
(status `failed` or `unknown`, `failed_step` `<file>#<n>`, the reason code in `note`); an unpark is
a `resolve` step with the note `unpark <file>`.

### Products and holds

Opt-in. A product is a named group of objects of the database, for example the tables, views and
procedures of `Balance`. Name each product in `azsqlcd.toml`:

```toml
[products.balance]
objects = ["sales.Balance*", "sales.vw_Balance*", "sales.usp_Balance*"]  # required
owners = ["@bal-owner", "@example-org/balance-team"]                       # optional
promote_to = "dev"                                                        # optional
```

- The name holds lower case letters, digits, `-` and `_`.
- `objects` holds patterns `schema.name`, with `*` and `?`, as `[scope] exclude_objects`. Names
  are compared without letter case. With `[scope]`, the schema part of a pattern must be a
  managed schema.
- An object belongs to one product at most. An object that two products match is the lint error
  `PRODUCT_OVERLAP`. An object that no product matches is shared.
- `owners` are GitHub owners for CODEOWNERS.
- `promote_to` is the last environment that the changes of the product reach. The order is dev,
  sandbox, test, preprod, prod. Without it, the changes reach every environment. `disposable`, the
  environment of the live tests, is not in the order: `promote_to` cannot name it, and nothing is
  held there.

What the tool does in an environment that comes after `promote_to` (the product is held there):

1. A pending migration that touches an object of the product is held: it does not run there. The
   plan has the note `held: <file> (product <name>, promote_to = "<env>")`. The later releases
   deploy past it, as past a parked migration (the "Park a migration that fails" section above):
   what runs must be independent of it, else 22 `SET_ASIDE_DEPENDENCY`.
2. A change of a module of the product is not sent, and a tombstone of it is not dropped. A
   pending migration or a module that names a held module that the database does not have is
   refused with 22 `SET_ASIDE_DEPENDENCY`.
3. The plan text, `plan.json` and the job summary have the notes `products: balance (2
   migrations, 1 module)` and `held in <env>: balance (promote_to = "<env>"): <files and keys>`.
4. An object of the product that the database does not have is not expected there. `plan` does
   not ask for a baseline of it, and `baseline` does not compare or record it.

To promote the product, raise or remove `promote_to` by pull request. The next release runs the
held migrations of the earlier releases first, in chain order (note `catch-up past set-aside:
...`), and sends the held module changes.

One rule for pull requests: a new migration must not touch the objects of a product that has
`promote_to` and other objects too. `verify` and `gen` give `MIXED_HOLD`. Change the files of the
product and run `azsqlcd gen`, then the other files and `gen` again, or use two pull requests.

What a hold does to the rest of the pipeline:

- While a migration is held in an environment, a skipped release is not refused there with
  `CATCHUP_REQUIRED`. Its migrations run with the next release, in chain order (note `catch-up
  past set-aside: ...`). For a hold that stays, this is so in every environment after `promote_to`.
- A held migration freezes the shared objects that it names, for example the table that a foreign
  key of the product names. After `promote_to`, a later shared migration that changes such a
  table is refused with 22 `SET_ASIDE_DEPENDENCY`, and so is each later release that holds it,
  until `promote_to` is raised. `verify` gives the warning `HOLD_DEPENDENCY` for such a migration.
- A held module that an environment holds already keeps its old text there. A shared migration
  that unbinds it, while its file text changed, is refused with 22 `SET_ASIDE_DEPENDENCY`. A shared
  migration after which the old text does not bind stops with 21 `DEPENDANT_BROKEN` and is rolled
  back.
- To remove a product that has `promote_to`, or to take objects out of it, use two pull requests.
  First remove `promote_to` and promote that release to every environment, so that the held
  migrations run. Then remove the product or change its `objects`. `verify` refuses the change in
  one step with `HOLD_DROPPED`: a database where a migration of the product is still held would
  stop with `CHAIN_DIVERGED`.
- When a pull request adds `promote_to`, a merged migration that touches the product and other
  objects is held as a whole where it is still pending. `verify` gives the warning
  `MIXED_HOLD_MERGED`.
- Keep the objects of a held migration in `[scope]` until the hold ends everywhere. A migration
  that runs late (a held one after `promote_to` is raised, a parked one after `--unpark`) and
  touches an object that `[scope]` of the release does not manage is refused with 22
  `OUT_OF_SCOPE`: the release does not model that object. Withdraw the migration, or bring the
  object back into `[scope]` with its file. When a pull request changes `[scope]`, `verify` gives
  the warning `HOLD_OUT_OF_SCOPE` for each migration of a product with `promote_to` that touches
  an object outside `[scope]`.

The command `azsqlcd products` lists each product with its owners, its `promote_to` and the count
of its object files. It reads only the working tree:

```bash
azsqlcd products                    # the products of azsqlcd.toml
azsqlcd products --env sandbox      # also what the files hold back from sandbox
azsqlcd products --codeowners >> .github/CODEOWNERS
```

`--env` reads no database: `azsqlcd plan` shows what a target holds and what is parked there.
With `[env.<name>] not_managed`, the held items that the patterns take out of that environment are
listed apart (`not managed in <env>: ...`), as the plan of the environment does: a migration that
touches only matching objects, and a module that a pattern matches.
`--codeowners` prints one line for each object file of a product that has owners, or one glob
when every file in a folder that a pattern of the product matches is a file of the product. A
space in a path is written as `\ `.

No column and no status was added to the state tables. A hold is a fact of `azsqlcd.toml` of the
release and of the environment. A held migration has no step row, as a pending one has none.

### Objects that one environment does not manage

Opt-in. Use it when the objects of one environment are not at the level of the files yet, and the
tool must not touch them there. Each environment can have its own list. Without the key, every
object of the scope is managed in every environment, as before.

```toml
[env.test]
# ... the other keys of the environment
not_managed = ["legacy.*"]   # patterns schema.name; * and ? are wildcards
```

The patterns have the syntax and the checks of `[scope] exclude_objects`. Names are compared
without letter case. With `[scope]`, the schema part of a pattern must be a schema that the scope
manages (or hold a wildcard).

What the tool does in that environment:

1. The matching objects are outside the scope, as for `[scope] exclude_objects`: they are not read,
   compared, baselined or listed as unmanaged, `drift` reports nothing for them, and `align`
   writes no statement for them. A DML
   trigger follows its table: the trigger of a matching table is not managed either. A schema
   itself is never matched: `legacy.*` leaves the schema `legacy` managed, while the release holds
   one of its objects that no pattern matches. When the patterns match every object of a schema in
   the release, the schema is not managed either: the plan and `baseline` do not ask for it, and a
   migration that creates the schema and its objects is skipped.
2. A managed object that a pattern matches leaves: the next deploy marks its row `dropped`.
   Nothing is sent to the object and nothing is dropped.
3. A module that a pattern matches is not sent: its file changes nothing in that environment.
4. A pending migration that touches only matching objects is skipped in that environment for good.
   The deploy records it as a migration step with the status `not_applied` and the note
   `not_managed in <env>`. It never runs there, also after the pattern is taken away. The other
   migrations run past it, in chain order. The other environments run it as usual.
5. A pending migration that touches matching objects and managed objects is refused with 22
   `MIXED_NOT_MANAGED`. Split it into two migrations by withdraw and replace (see "Withdraw and
   replace a merged migration").
6. `plan` and `deploy` give the note `not managed in <env>: <count> object(s); skipped migrations:
   ...`.
7. A skipped migration does not change the catch-up rule: a release that adds migrations is still
   promoted after the releases before it (`CATCHUP_REQUIRED`).
8. The tool cannot prove all that it sends. The plan has a note for each data or raw batch that
   runs (`not_managed not proven for a <kind> batch: <file> ...`): a name in dynamic SQL is not
   seen. It has a note for each migration or module to send that names a matching object
   (`not_managed: <what> names ...`), for example a view of the table, or a foreign key to it. Such
   a statement can fail at deploy, because the object is not at the level of its file. Read these
   notes before you approve.

What a migration touches: the object of each model statement; for a data or raw batch, every name
of its text that is written with its schema.

To manage the objects again in that environment:

1. Take the pattern away by pull request. The next plan for that environment stops with 22
   `BASELINE_REQUIRED` and names `azsqlcd baseline --schema <name>`. Without `[scope]`, a module
   that comes back stops it with 22 `NAME_COLLISION`: do the next steps all the same.
2. Make the objects equal to their files by hand. The migrations that were skipped never run
   there: their changes are part of this work. Create the objects that are not in the database by
   hand too (for example a schema and a table that a skipped migration creates): `baseline
   --schema` stops with 22 `READBACK_MISMATCH` for an object that is not there.
3. Run `azsqlcd baseline --schema <name>` on each target of the environment. It compares the
   objects with the files and records them.
4. Promote the release again.

No column and no status was added to the state tables. A skip is a row of `azsqlcd.step` with the
kind `migration`, the status `not_applied` and the note `not_managed in <env>`.

With parking and products in the same environment, each migration keeps one reason. A
migration that this environment does not manage is skipped, also when it is a migration of a
product that is held here: it does not run here after `promote_to` is raised. A parked migration
stays parked. `drift` lists the parked migrations only; `plan` and `deploy` show the skipped and
the held ones.

A held migration that touches matching objects and managed objects is not refused while it is
held: it does not run here. The other releases deploy past it. When `promote_to` is raised, the
plan for that environment stops with 22 `MIXED_NOT_MANAGED`; split the migration then. A held
migration that touches only matching objects is skipped for good, as above.

### Partitioned tables

Not run on a database yet: `docs/known-gaps.md`, section 14.

A table, a `PRIMARY KEY` or `UNIQUE` constraint and an index can be on a partition scheme. The
table file names the scheme and the one partitioning column. The tool does not manage the
partition function or the partition scheme: a DBA creates them, moves their boundaries (`SPLIT`,
`MERGE`) and switches partitions (`SWITCH`).

The form of the file. The table states `ON [scheme]([column])` after its column list. Every key
and every index of a partitioned table states its place as the last clause:
`ON [scheme]([column])` when it is on a scheme, `ON [PRIMARY]` when it is not. `export` writes
this form; `lint` gives `NF000` for a file that leaves a clause out.

```sql
CREATE TABLE [sales].[OrderLine] (
    [OrderLineId] bigint NOT NULL,
    [OrderDate] date NOT NULL,
    [ProductId] int NOT NULL,
    CONSTRAINT [PK_OrderLine] PRIMARY KEY CLUSTERED ([OrderDate], [OrderLineId]) ON [PS_OrderYear]([OrderDate]),
    CONSTRAINT [UQ_OrderLine_Id] UNIQUE NONCLUSTERED ([OrderLineId]) ON [PRIMARY]
)
ON [PS_OrderYear]([OrderDate]);
GO
CREATE NONCLUSTERED INDEX [IX_OrderLine_Product] ON [sales].[OrderLine] ([ProductId])
    ON [PS_OrderYear]([OrderDate]);
```

Rules:

- The partitioning column is a column of the table. A `PRIMARY KEY`, a `UNIQUE` constraint and a
  unique index on a scheme hold it in their key.
- A table that is not partitioned has no `ON` clause. An index of it can be on a scheme and then
  states `ON [scheme]([column])`.
- One compression for a table or an index. A table whose partitions do not all have the same
  compression stays unmanaged: `export` lists it with the reason.
- `STATISTICS_INCREMENTAL = ON` is an option of an index or a key on a scheme.

Before a release. The partition function and the partition scheme must exist, with the name of
the file, in the database of every environment (dev, sandbox, test, preprod, prod) before the
release reaches it. If a scheme is missing the plan refuses the release with 22
`PARTITION_SCHEME_MISSING`, names the scheme, and nothing runs. The plan identity must see
`sys.partition_schemes`; `setup-sql` grants `VIEW DEFINITION` to it.

A change. `gen` writes the changes of a partitioned table as for any table: columns,
constraints, indexes, the place of a nonclustered index. `gen` does not move a table. For a
table that goes on a scheme, off one, to another scheme or to another partitioning column it
refuses with `PARTITION_CHANGE`, and a person writes the migration: the clustered key or index
is dropped and created again with the new `ON` clause. The nonclustered keys and indexes stay
where they are, so each one that the file puts on the scheme is dropped and created too. For
the table above, when it was not partitioned before:

```sql
-- azsqlcd:allow LONG_LOCK [sales].[OrderLine] reason: <why the lock is acceptable>
ALTER TABLE [sales].[OrderLine] DROP CONSTRAINT [PK_OrderLine];
GO
-- azsqlcd:allow LONG_LOCK [sales].[OrderLine] reason: <why the lock is acceptable>
ALTER TABLE [sales].[OrderLine] ADD CONSTRAINT [PK_OrderLine] PRIMARY KEY CLUSTERED ([OrderDate], [OrderLineId]) ON [PS_OrderYear]([OrderDate]);
GO
DROP INDEX [IX_OrderLine_Product] ON [sales].[OrderLine];
GO
-- azsqlcd:allow LONG_LOCK [sales].[OrderLine] reason: <why the lock is acceptable>
CREATE NONCLUSTERED INDEX [IX_OrderLine_Product] ON [sales].[OrderLine] ([ProductId]) ON [PS_OrderYear]([OrderDate]);
GO
```

A heap gets a clustered index with the place, and the index is dropped again. The drop of a
clustered index or key makes the table a heap and writes every row again, so it needs its allow
`LONG_LOCK` line too: `lint` cannot see that an index is clustered, `verify` asks for the line
(`PRF003`) and `gen` writes it. `ON [PRIMARY]` in
place of the scheme ends the partitioning. Change the table file to the new form in the same
pull request, then `azsqlcd gen --resum`; `azsqlcd verify` proves that the migration gives the
model of the file. `docs/runbook.md`, "Partitioned tables", lists the findings. The engine
writes the rows again and holds a long lock for each build: plan the time.

## 8. Operation

- `docs/runbook.md`: exit codes, reason codes and recovery; one line for each finding code of
  `lint` and `verify`.
- `docs/design.md`, Part 1, "As built": the command-line reference.
- Incidents: issues with the label `azsqlcd-incident`, one for each target, opened by the stage
  workflow for test, preprod and prod. Subscribe the DBA team to the label.
- Drift: `db.yml` runs the drift report every night for all five environments. A failed scheduled
  run notifies only the person who last changed the schedule; watch the workflow, or route the
  notification.
- A constraint that somebody dropped and added again with no name is drift after the baseline.
  The drift report shows the property `constraint_name [<name of the file>] (<kind>, live name
  [<name in the database>])` and one line `rename <table key> EXEC sys.sp_rename ...;`. A plan that
  stops for it prints the same line. Do not drop the constraint and do not use `accept-drift`. A
  DBA reviews the statement and runs it, then the drift report is clean again. The statement
  gives the recorded name also when the next release renames the constraint in its file.
  `docs/runbook.md`, "A constraint with a name that the engine made", has the special cases.
- Approvers: open the compare link in the plan summary before you approve. Reject approvals of
  older releases.

### Dependants of a change

With `table_model = true` the plan reads the modules that depend on the release, at any depth: the
modules that use a table that a migration changes, the modules that use a module that the release
changes or drops, and the modules that use those modules. A view on a view on a changed table is
one of them.

- The deploy refreshes the managed, unchanged views and table-valued functions of that set after
  the changes, in dependency order: a view before the views that use it. It does not refresh a
  schema-bound module.
- After the refresh the deploy checks every managed module of the set (21 `DEPENDANT_BROKEN`).
- A release that changes or drops modules only does the same for the modules that use the changed
  or dropped modules. A view on a dropped view fails the deploy (21 `DEPENDANT_BROKEN`, failed step
  `refresh:<key>`).
- The plan gives a note for each dependant that it found through other modules, with the path:
  `note: dependant through other modules: VIEW:[bal].[vw_b] -> VIEW:[bal].[vw_a] -> TABLE:[bal].[Orders]`.
- An unmanaged module at any depth is a `TABLE_BLOCKER`. The detail line names the path. Accept it
  with the same line in `[ack] unmanaged_dependants`: `"[bal].[vw_b] -> [bal].[Orders]"` or
  `"[bal].[vw_b] -> [bal].[Orders].[Status]"`.
- The search stops after 32 levels. The plan then gives a note: modules that are further away are
  not refreshed and not checked.

When a module on a changed or dropped module breaks, change that module (or tombstone it) in the
same pull request. The message of a release with no migration says so.

## 9. Upgrading the tool across repositories

A database repository names the tool version in `db.yml`, `resolve.yml` and `onboard.yml`. All
references in one repository must be the same tag.

1. Tool repository: release `v<new>` (section 1, step 5).
2. Private runner images: add the uv cache for `v<new>` (section 5, step 5). Keep the old one
   until every repository has moved.
3. For each database repository:
   a. Create the federated credentials for the new tag:
      `uv run python scripts/setup_repo.py --repo <owner>/<name> --print-azure` in a checkout of
      `v<new>`, section 3 of the output. The subject holds the tag, so the old credentials do not
      match the new workflows.
   b. Runner group "Workflow access" of both groups: add the workflows at `v<new>`.
   c. Run `azsqlcd lint` of the new version in the repository first. `build` runs lint over every
      migration, merged ones included; a new rule that fails a merged migration stops every
      release, and a merged migration cannot change (`docs/known-gaps.md`, section 4). Then one
      pull request replaces every `@v<old>` with `@v<new>` in `.github/workflows/`. The DBA team
      reviews it (CODEOWNERS).
   d. After the merge, the next release runs with the new tool. A plan that the old tool made is
      stale (`STALE_PLAN`): the tool digest is part of the plan hash. Plan again.
   e. Delete the federated credentials of `v<old>` and remove the old workflows from the runner
      groups.
4. Run `setup_repo.py --check` on every repository.

A release that an older tool built can be promoted with a newer tool: the manifest digest does not
depend on the tool version.

## 10. Other GitHub products

Not verified; no evidence file covers these. Decide before any other work.

| Topic | Enterprise Cloud | Enterprise Cloud with data residency, Enterprise Server |
|---|---|---|
| OIDC issuer | `https://token.actions.githubusercontent.com` | Another issuer and subject host. `ISSUER` in `setup_repo.py` must change. Entra must reach the issuer over the internet; an internal-only server cannot be an issuer, and then the identity model of this design does not work |
| Hosted runners | Used for the light jobs | May not exist. Every `runs-on` is an input |
| Third-party actions | Downloaded from github.com | Must be mirrored or reached through GitHub Connect |
| `gh` | Uses `GH_TOKEN` | Also needs `GH_HOST`; the workflows do not set it |
| Rulesets, runner group workflow access, subject customisation | Available | Depends on the server version |

## Windows

On Windows only the unit tests ran: they pass on the `windows-latest` runners of `ci.yml`
(2026-10-07). No command ran on Windows against a database, and no workflow of a database
repository ran on a Windows runner. The points below come from a simulation on macOS and from
reading the code (`docs/known-gaps.md`, section 10).

Workstation:

- Use Git Bash for the commands of `README.md` and `docs/quickstart.md`; they are POSIX shell.
- `git config --global core.longpaths true`, and switch the system setting `LongPathsEnabled` on.
  An object file can have a name of up to 255 bytes; without long paths a full path above 259
  characters cannot be written (`export` then ends with `EXPORT_INCOMPLETE`).
- Keep the `.gitattributes` of the template in the database repository. It pins LF. With CRLF
  files in the working tree `verify` gives a false `WDR001`.
- Rename a file by letter case only with `git mv`. The root folders are `schema`, `migrations`
  and `onboarding` in lower case; `build` refuses another letter case (`TREE_INVALID`).
- An object whose name holds one of `: ? * " < > |`, ends with a dot or a space, or is a device
  name (`aux`, `con`, `nul`, `com1`, ...) has no file: `export` lists it as unsupported.
- The tool prints in the encoding of the console and writes a character outside it as an escape
  (`Ω`). Set `PYTHONUTF8=1` to see such names as they are.

Runner:

- The workflows are written for Linux runners: 25 steps use `shell: bash`. A self-hosted Windows
  runner needs Git for Windows with `Git\bin` on PATH (bash, awk, sed, sort, paste, cmp, date, cp,
  printf), `gh`, `az`, `uv`, and `jq`. Two differences are known and not fixed: Git Bash rewrites
  an argument that starts with `/` to a Windows path (`action.yml`), and `jq` writes CRLF
  (`stage.yml`, `onboard.yml`, `resolve.yml`). Use Linux runners until a Windows runner has
  passed G5 of `docs/live-testing.md`.
