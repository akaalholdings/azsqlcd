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
   - Every key is required except `gated`, `[unmanaged]`, `[ack]`, and in `[project]`
     `data_batches` (default `false`) and `server_suffixes` (default: the four Azure SQL Database
     suffixes). A target server that ends with none of the suffixes is `CONFIG_INVALID`. An
     unknown key is an error.
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
   `sys.sql_expression_dependencies`.
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
     hand; it holds `EXEC sys.sp_rename` only. The tool does not run it (`align` is cut).
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
     prod (runbook), or a DBA aligns it by hand. `only_here`: unmanaged in this target. If the
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

## 8. Operation

- `docs/runbook.md`: exit codes, reason codes and recovery; one line for each finding code of
  `lint` and `verify`.
- `docs/design.md`, Part 1, "As built": the command-line reference.
- Incidents: issues with the label `azsqlcd-incident`, one for each target, opened by the stage
  workflow for test, preprod and prod. Subscribe the DBA team to the label.
- Drift: `db.yml` runs the drift report every night for all five environments. A failed scheduled
  run notifies only the person who last changed the schedule; watch the workflow, or route the
  notification.
- Approvers: open the compare link in the plan summary before you approve. Reject approvals of
  older releases.

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
