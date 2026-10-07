# Port azsqlcd to another GitHub organisation

For the person who moves the tool. The tool was built in the repository of one GitHub account.
This document moves it to another GitHub organisation, the target organisation, and brings one
low-risk database onto it. Do the sections in order. Each step has the command and a check.

Status, 2026-10-07. Run for this document: `scripts/port_to_org.py` on copies of this repository
(organisation names of 4, 7, 12 and 37 characters, with and without a new repository name), then
the unit tests on each copy, and ruff on the copies with the shortest and the longest name; and
the git lines of section B, steps 4, 5 (the form with no history) and 8, against a local bare
repository. Not run: a push to GitHub,
every `gh` and `az` command, every PowerShell line, and every step of sections C and D on a
Windows machine or on the target GitHub. Sections C and D follow `docs/setup.md` and
`docs/quickstart.md`; where those documents and this one differ, they win.

Names in this document:

| Name | Meaning |
|---|---|
| `<target-org>` | The target organisation: the organisation that gets the tool, in the letter case that GitHub shows |
| `<tool-repo>` | The name of the tool repository there. `azsqlcd` unless you rename it |
| `<db-repo>` | The repository of the pilot database, for example `db-<name>` |
| `<dba-team>` | The slug of the GitHub team that reviews database changes |
| `<source-org>` | The owner of the repository where the tool was built: a user account or an organisation |
| `TOOL` | The path of the checkout of the tool on the machine where you install it, for example `C:\src\azsqlcd` |

`docs/agent-install.md` holds the part that an AI coding assistant can do for you: sections C and
the files of D1 and D2. Everything that needs a right in Azure, a repository setting, an approval
or a database stays with you.

## A. What you need before you start

| # | Item | Who | Check |
|---|---|---|---|
| 1 | The GitHub product. This build assumes GitHub Enterprise Cloud on `github.com` | You | The address of the target GitHub. Any other host: read `docs/setup.md`, section 10, first. The OIDC issuer, the hosted runners, the third-party actions and `gh` (`GH_HOST`) differ, and none of it is verified |
| 2 | Leave to bring this code into the organisation | The target organisation | Outside this document |
| 3 | A private or internal repository `<target-org>/<tool-repo>`, empty, and admin rights on it | GitHub administrators | `gh repo view <target-org>/<tool-repo> --json isPrivate,isEmpty` |
| 4 | The Actions policy of the organisation allows `actions/checkout`, `actions/upload-artifact`, `actions/download-artifact`, `azure/login`, `astral-sh/setup-uv` and `<target-org>/*` | GitHub administrators | `docs/setup.md`, section 1, step 4 |
| 5 | A person who can create user-assigned managed identities and federated credentials. Six identities for each database repository. No identity gets an Azure role | Azure or Entra team | They name a resource group and a region |
| 6 | Runners that reach the SQL servers: self-hosted, one job each, in two groups, `azsql-nonprod` and `azsql-prod` | Platform team | `docs/setup.md`, section 5. A firewall rule for hosted runners is not built |
| 7 | GitHub-hosted runners for the jobs with no database identity (`ubuntu-latest`), and for the CI of the tool (`ubuntu-latest`, `windows-latest`) | GitHub administrators | Ask. If there are none, every `runs-on` must change |
| 8 | An administrator (Entra admin) of the pilot database in each environment, to run `setup-sql` once | DBA team | Name the person for each of the five environments |
| 9 | A second DBA in `<dba-team>`. prod prevents self-review: you cannot approve a run that you started | DBA team | The team has two members with write access |
| 10 | The machine where you install the tool: Python 3.12 or later, uv, git 2.24 or later, the Azure CLI, the GitHub CLI | You | Section C, step 1 |
| 11 | One low-risk database for the pilot that exists in dev, sandbox, test, preprod and prod. The template pipeline has these five stages; `docs/setup.md` does not say what to do when one is missing | You | You can name server and database for each of the five |
| 12 | One disposable Azure SQL Database in the target tenant, for section E | You | Its name does not contain `prod` |

## B. Copy the code

Do this on the machine that holds the source checkout.

1. Start from a clean checkout of the commit that you want to port.

   ```
   git status --short
   git log -1 --oneline
   ```

   Check: the first command prints nothing.

2. Rewrite the references. The workflows, the action, the templates, the example, the documents,
   `scripts/setup_repo.py` and four test files name the tool repository, the example database
   repository and the DBA team with the old owner. GitHub resolves them by name.

   ```
   git switch -c chore/port-to-target-org
   python scripts/port_to_org.py --org <target-org> --check
   python scripts/port_to_org.py --org <target-org>
   python scripts/port_to_org.py --org <target-org> --check
   ```

   - Give `<target-org>` exactly as GitHub shows it: `gh api orgs/<target-org> --jq .login`. The
     federated-credential subject holds this name. A subject that differs in letter case is
     expected not to match (not tested here).
   - A new repository name: add `--repo <tool-repo>` to all three lines. Only the name after the
     owner changes. The command, the package, the action name and the folder name in
     `cd azsqlcd` (`README.md`, `docs/quickstart.md`) stay `azsqlcd`; edit those two lines by hand.
   - The script never changes `src/azsqlcd`. No source file holds the owner name today, and a
     unit test keeps it so.

   Check: the first line exits 1 and lists each file with its count (more than 80 references in
   about 25 files today). The second line ends with `rewrote <n> reference(s) in <m> file(s)`. The third
   line exits 0 and prints `0 reference(s) to <source-org> left in 0 file(s)`. Then:

   ```
   git grep -il "<source-org>"
   ```

   prints `scripts/port_to_org.py` and `LICENSE` only. The script keeps the name of the source so
   that `--check` knows what to look for. `LICENSE` holds the copyright line of the source: the
   MIT licence requires that this line stays in every copy, so do not change it. Any other file
   in the list holds the name inside another word; read it.

3. Run the tests of the ported tree.

   ```
   uv sync --extra db
   uv run ruff format
   uv run ruff check
   uv run pyright
   uv run pytest -q
   ```

   Check: `ruff check` and `pyright` report no error, and `pytest` reports no failed test.
   Run the same four checks on the unported tree first: a failure that is there before the port
   is not caused by the port.

   A new name changes line lengths. `ruff format` wraps what it can. A string that it cannot
   wrap stays as `E501 Line too long`; split that string into two strings by hand. Measured: a
   37-character organisation name with a 21-character repository name left two such lines
   (`tests/unit/test_setup_repo.py`, `tests/unit/test_workflow_files.py`); a 4-character name left
   none. On every copy the unit tests gave the same result as the unported tree.

4. Commit. `git add -u` stages only files that git already tracks.

   ```
   git add -u
   git status --short
   git commit -m "Port the references to <target-org>"
   ```

   Check: `git status --short` shows only lines that start with `M`.

5. Push to the target GitHub as `main`.

   ```
   git remote add target https://github.com/<target-org>/<tool-repo>.git
   git push target chore/port-to-target-org:main
   ```

   Check: `git ls-remote target refs/heads/main` prints the commit of `git rev-parse HEAD`.

   - Do not push a `v*` tag of the source. The tag of step 8 must point at a commit that holds
     the port: a workflow at the tag must name `<target-org>`. The source has the tag `v0.1.0`;
     its commit names `<source-org>`. A checkout that holds this tag refuses the `git tag` line
     of step 8 (`fatal: tag 'v0.1.0' already exists`). Before step 8, remove the tag from this
     checkout only: `git tag -d v0.1.0`. It changes nothing in the source repository.
   - The pushed history holds the old name and the author addresses of the old commits. If the
     target GitHub refuses the push for that reason, or you do not want the history there, push
     one commit with no history instead:

     ```
     git checkout --orphan target-main
     git commit -m "Import azsqlcd 0.1.0"
     git push target target-main:main
     ```

   - No network path from this machine to the target GitHub: `git bundle create azsqlcd.bundle
     chore/port-to-target-org`, carry the file, `git clone azsqlcd.bundle azsqlcd` on the machine
     where you install the tool, and push from there. The import page of GitHub is another way;
     then do steps 2 to 4 in a clone of the imported repository and open a pull request.

6. Repository settings of `<target-org>/<tool-repo>` (`docs/setup.md`, section 1). This repository
   holds code that later runs with the production deploy identity of every database.

   - Settings > Actions > General > Access: "Accessible from repositories in the organization".
     Check: `gh api repos/<target-org>/<tool-repo>/actions/permissions/access --jq .access_level`
     prints `organization`.
   - Branch ruleset on `main`: pull request, one approval, stale approvals dismissed, approval of
     the most recent push, required check = the four `test` jobs of `ci.yml`, no bypass actors.
   - Tag ruleset on `v*`: no update, no deletion, no bypass actors; creation only by the people
     who release the tool.

   No script sets these three. Check: `gh api repos/<target-org>/<tool-repo>/rulesets --jq '.[].name'`
   prints two names.

7. Read the first run of `ci.yml`. The push of step 5 starts it: four jobs, `ubuntu-latest` and
   `windows-latest`, Python 3.12 and 3.14.

   ```
   gh run list -R <target-org>/<tool-repo> --workflow ci.yml --limit 1
   gh run watch -R <target-org>/<tool-repo> <run-id> --exit-status
   ```

   Check: exit 0. This is the first run of a workflow of the tool in the target organisation. In
   the source repository `ci.yml` passed on all four jobs on `main` on 2026-10-07. A red job is
   a finding: `docs/known-gaps.md`, section 10, "Windows (N4)", lists what is known. Do not tag
   before you have read it.

8. Tag the commit of `main`. The tag is `v` and the version of `pyproject.toml`; the workflows
   name it, and a unit test fails when one reference has another version.

   ```
   git fetch target
   git tag v0.1.0 target/main
   git push target v0.1.0
   ```

   Check: `git ls-remote --tags target v0.1.0` and `git ls-remote target refs/heads/main` print the
   same commit. A later release of the tool: `docs/setup.md`, section 1, step 5, and section 9.

A later move of the repository (a second organisation, or a rename) names its own source:
`python scripts/port_to_org.py --org <next-org> --from <target-org>/<tool-repo>`.

## C. Install the command line on the Windows machine

The lines are PowerShell. They were written from the bash lines of `docs/quickstart.md`, which
ran on macOS; no line ran on Windows. `docs/known-gaps.md`, section 10, "Windows (N4)", says to
use Git Bash for the commands of the quickstart. If a PowerShell line fails for a reason of the
shell, run the bash line of the quickstart in Git Bash.

1. Check the machine.

   ```powershell
   python --version
   uv --version
   git --version
   az version
   gh --version
   gh auth status
   ```

   Check: Python 3.12 or later; git 2.24 or later; `gh auth status` names your account on the
   target GitHub.

2. Settings for this session and for git.

   ```powershell
   $env:PYTHONUTF8 = "1"
   git config --global core.longpaths true
   ```

   - `PYTHONUTF8`: some tests and `scripts/setup_repo.py` read text with the default encoding of
     the machine (`docs/known-gaps.md`, N4-15 and N4-17).
   - Long paths: git needs `core.longpaths`, and Windows needs `LongPathsEnabled` (N4-05). Check:
     `(Get-ItemProperty HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem).LongPathsEnabled`
     prints `1`. If it prints `0`, keep the clones in a short folder such as `C:\src`.
   - Line ends: leave `core.autocrlf` as it is. `.gitattributes` of the tool and of the template
     pins LF. Keep that file in every database repository: with CRLF files `verify` gives a false
     finding (N4-06).

3. Get the tool at the tag and install it with the locked versions.

   ```powershell
   git clone --branch v0.1.0 https://github.com/<target-org>/<tool-repo>.git C:\src\azsqlcd
   cd C:\src\azsqlcd
   uv sync --frozen --extra db
   $TOOL = "C:\src\azsqlcd"
   function azsqlcd { uv run --project $TOOL --no-sync azsqlcd @args }
   azsqlcd --version
   ```

   Check: `azsqlcd 0.1.0`. The function lasts for this PowerShell session; put the two lines
   into your profile to keep it. Check the line ends of the checkout:

   ```powershell
   git ls-files --eol | Where-Object { $_ -match '\sw/(crlf|mixed)\s' -and $_ -notmatch 'attr/-text' }
   ```

   prints nothing.

   Other way, from the design, not run for this project:
   `uv tool install "git+https://github.com/<target-org>/<tool-repo>@v0.1.0"`. It gives the command
   `azsqlcd` for `lint`, `gen` and `verify` with no driver, and it does not read `uv.lock`
   (assumption of the design). For a database command use the clone.

4. Sign in to Azure. The offline commands need no login. A database command that you run on
   this machine, and the scripts of section E, use the login of the Azure CLI.

   ```powershell
   az login
   az account show --query "{tenant:tenantId, user:user.name}"
   ```

   Check: the tenant is the target tenant.

5. Prove the install on the demo repository. No database, no network.

   ```powershell
   Copy-Item -Recurse -Force "$TOOL\examples\demo-db" C:\src\db-demo
   cd C:\src\db-demo
   Test-Path .gitattributes, .github\workflows\db.yml
   git init -b main
   git add -A
   git commit -m "Demo sales database"
   git update-ref refs/remotes/origin/main HEAD
   azsqlcd lint
   ```

   The folder `C:\src\db-demo` must not exist before the first line. Check: `Test-Path` prints
   `True` twice, and `lint` prints `0 error(s), 0 warning(s)`.
   Then do steps 3 to 8 of `docs/quickstart.md` in the same folder. The `azsqlcd` lines are the
   same in PowerShell, and the document shows the output of each one:

   ```powershell
   azsqlcd gen --name customer_loyalty_tier
   azsqlcd gen --resum
   azsqlcd verify --base "$(git merge-base origin/main HEAD)"
   ```

   Check: after the edits of the quickstart, `verify` prints `0 error(s), 0 warning(s)`, and
   after the wrong edit of its step 8 it prints `PRF001` and ends with exit 22.

## D. The pilot database

One database, one repository. Sections of `docs/setup.md` are named at each step; it holds the
detail and the rules. That document was not run step by step against GitHub and Azure; its
status line says what ran. If you can, do section E first: it is the cheaper place to find a
fault of the driver or of the wiring.

1. Create the repository `<target-org>/<db-repo>` (private) and fill it from the template
   (`docs/setup.md`, section 3).

   ```powershell
   git clone https://github.com/<target-org>/<db-repo>.git C:\src\<db-repo>
   cd C:\src\<db-repo>
   git switch -c chore/azsqlcd-setup
   Copy-Item -Recurse -Force "$TOOL\templates\db-repo\*" .
   Test-Path .gitattributes, .github\CODEOWNERS, .github\workflows\db.yml, azsqlcd.toml
   ```

   Check: `True` four times. In Git Bash: `cp -R "$TOOL/templates/db-repo/." .`

   `templates/db-repo` is a folder, not a repository. For the "Use this template" button of
   GitHub, make one repository from this folder and mark it as a template. One pilot does not
   need it.

2. Fill the files.

   - `azsqlcd.toml`: the project name, the tenant id, one target for each of the five
     environments (target id, server, database). Leave `table_model = false`. The six client ids
     come from step 3. The rules of the file: `docs/setup.md`, section 3, step 3.
   - `.github/CODEOWNERS`: `@<target-org>/<dba-team>` on every line. The team needs write access.
   - `.github/workflows/`: every `uses:` names `<target-org>/<tool-repo>` at `@v0.1.0` after
     section B. Change `runs-on` only if your runner groups have other names than
     `azsql-nonprod` and `azsql-prod`. `gated` of each stage in `db.yml` must equal `gated` in
     `azsqlcd.toml`.
   - `.gitignore`: add the line `.azsqlcd/` (`docs/triage.md`, section 1).

   ```powershell
   azsqlcd lint
   azsqlcd setup-sql --env dev --target <target-id-of-dev>
   ```

   Check: `lint` prints `0 error(s), 0 warning(s)`. `setup-sql` prints a script that names your
   database and the users `azsqlcd-<project>-dev-plan` and `azsqlcd-<project>-dev-deploy`; it
   connects to nothing. `CONFIG_INVALID` names the key that is wrong.

3. Identities and federated credentials (`docs/setup.md`, section 2). You do not need the right
   to create them: print the commands and give them to the person of row 5 of section A.

   ```powershell
   uv run --project $TOOL --no-sync python "$TOOL\scripts\setup_repo.py" --repo <target-org>/<db-repo> --print-azure | Out-File -Encoding utf8 C:\src\azure-commands.txt
   ```

   The command calls nothing. The file holds no secret. Its lines are bash text: they run in
   Azure Cloud Shell (bash) or in Git Bash, not in PowerShell. Copy the lines; do not run the
   file as a script (Windows PowerShell 5.1 starts it with a byte order mark). What to ask for:

   - Part 1 of the file: six identities, `<project>-nonprod-plan`, `-prod-plan`,
     `-nonprod-deploy`, `-test-deploy`, `-preprod-deploy`, `-prod-deploy`, in one resource group
     (`RG`, `LOCATION`). No Azure role for any of them.
   - Part 2: nine federated credentials with the short subject. None for environment `prod`.
   - Back to you: the six client ids and the tenant id. Put them into `azsqlcd.toml`.
   - Part 3 is blocking for prod (A29): the credentials with `job_workflow_ref`, then the `gh`
     command that changes the subject form of the repository, then the deletion of the
     credentials of part 2. Prove it on a scratch repository first (G6). Until then keep prod
     off the pipeline.
   - `PROJECT` in the file must equal `[project].name` of `azsqlcd.toml`. The default is the
     repository name without a leading `db-`.

   Check: `az identity list --resource-group <rg> --query "[].{name:name, clientId:clientId}" -o table`
   shows six rows.

4. Environments, approvers and rulesets (`docs/setup.md`, section 4). You need `gh` with admin
   rights on `<db-repo>`. Push the files to `main` before the rulesets exist, or by pull request
   after (section 3, step 9).

   ```powershell
   uv run --project $TOOL --no-sync python "$TOOL\scripts\setup_repo.py" --repo <target-org>/<db-repo> --dba-team <dba-team>
   uv run --project $TOOL --no-sync python "$TOOL\scripts\setup_repo.py" --repo <target-org>/<db-repo> --dba-team <dba-team> --check
   ```

   The first line makes ten environments (the five and a `-plan` twin of each), branch `main`
   only, `<dba-team>` as reviewers of preprod and prod, prevent self-review on prod, and the two
   rulesets. Then do each line that it prints as `manual:`. While it prints such a line, it ends
   with exit 1.

   Check: the second line exits 0 and prints `<target-org>/<db-repo> meets the policy`. It stays
   at exit 1 until the subject form holds `job_workflow_ref` (step 3, part 3). Open a pull
   request: the check must have the name `verify / verify` (W4).

5. The runner (`docs/setup.md`, section 5). Ask the platform team for: two runner groups,
   `azsql-nonprod` (dev, sandbox, test, preprod) and `azsql-prod` (prod only); one job for each
   machine; access of each group limited to the database repositories and to the workflows
   `<target-org>/<tool-repo>/.github/workflows/stage.yml@v0.1.0`, `resolve.yml@v0.1.0`,
   `drift.yml@v0.1.0` and `onboard.yml@v0.1.0`; the image (bash, gh, az, uv, Python 3.12,
   `libltdl7`, `libkrb5-3`, `libgssapi-krb5-2`); a private endpoint path to each server; egress to
   GitHub, `login.microsoftonline.com` and PyPI. Treat the workflows as Linux only (N4-12).

   Check: Settings > Actions > Runner groups shows both groups with `<db-repo>` and the four
   workflows. No script checks this.

6. `setup-sql` in the database of each environment (`docs/setup.md`, section 6). Print one script
   for each target, in the checkout of `<db-repo>`:

   ```powershell
   azsqlcd setup-sql --env dev --target <target-id> | Out-File -Encoding utf8 C:\src\setup-dev.sql
   ```

   The administrator of row 8 reads it and runs it once on that database. It makes schema
   `azsqlcd`, four state tables, the `meta` row and two users with their grants. Repeat for
   sandbox, test, preprod and prod.

   Check, in each database: `SELECT * FROM azsqlcd.meta;` gives one row with your project and
   the environment, and `SELECT name FROM sys.database_principals WHERE name LIKE 'azsqlcd-%';`
   gives the plan user and the deploy user.

7. Onboarding (`docs/setup.md`, section 7, "Existing database"). `<n>`, `<m>` and `<k>` are
   releases of `<db-repo>`; the first push to `main` that holds `azsqlcd.toml` makes the first.
   Until step 6 is done the stages of a release stop with `STATE_MISSING`, and for test, preprod
   and prod each stop opens an incident issue. That is expected.

   1. Export from prod, read only, with the plan identity:

      ```
      gh workflow run onboard.yml --ref main -f environment=prod -f target=<target> -f action=export -f release=r<n>
      gh run download <run-id> -n onboard-export-prod-<target> -D export
      ```

   2. Read `export/onboarding/prod/export.md`: each object that stays unmanaged, with its reason.
   3. Commit by one pull request: `onboarding/prod/` of the export; the list at the end of
      `export.md` as `[unmanaged] objects`; the line `baseline` as line 2 of
      `migrations/migrations.sum`; and, with `table_model = false`, the directories `views`,
      `functions`, `procedures` and `triggers` of `schema/`. The merge makes release r<m>.
   4. For dev, sandbox, test, preprod, prod, in this order, compare (read only):

      ```
      gh workflow run onboard.yml --ref main -f environment=<env> -f target=<target> -f action=baseline-report -f release=r<m> -f confirm_database=<database>
      ```

      Read `baseline-diff.md` of the artefact `onboard-baseline-report-<env>-<target>`.
   5. For each module that `differs`: the acknowledgement file of `docs/setup.md`, section 7,
      step 6. The merge makes release r<k>.
   6. For the same five, in the same order, record the database. This writes state rows only,
      behind the approval of the deploy environment:

      ```
      gh workflow run onboard.yml --ref main -f environment=<env> -f target=<target> -f action=baseline -f release=r<k> -f confirm_database=<database>
      ```

   7. `gh workflow run db.yml --ref main -f release=r<k>`

   Check: every stage of the last run ends green, and in each database
   `SELECT TOP (5) run_id, command, status, release_seq, note FROM azsqlcd.run ORDER BY run_id DESC;`
   shows a `baseline` row and a `deploy` row.

   Start with `table_model = false` (modules only). It leaves fewer paths that no live run
   covered: `docs/known-gaps.md`, section 9, "Still not proven". The switch to the table model
   is in `docs/setup.md`, section 7.

8. The first small change, through all five environments. Pick one view, procedure or function
   that no release of the application waits for.

   ```powershell
   cd C:\src\<db-repo>
   git switch main
   git pull
   git switch -c feat/<short-name>
   # edit one file under schema\
   azsqlcd lint
   git fetch origin
   azsqlcd verify --base "$(git merge-base origin/main HEAD)"
   ```

   Open a pull request. A code owner approves. Merge. The merge builds one release and runs
   dev > sandbox > test > preprod > prod; preprod and prod wait for an approval, and prod needs
   the second DBA. Open the compare link in the plan summary before you approve.

   Check: `gh run list --workflow db.yml --limit 3`, then `gh run view <run-id>`: five stages,
   all green. In the prod database the newest row of `azsqlcd.run` has the command `deploy` and
   the new release.

## E. What is proven and what is not

State on 2026-10-07. `docs/known-gaps.md` is the list of record and `docs/live-testing.md` holds
the results; if they differ from these five lines, they win.

1. Proven on one disposable Azure SQL Database, from macOS with Python 3.12: the spike (19 pass,
   1 inconclusive by design, 3 manual), the acceptance run (50 of 50 checks on the final commit;
   52 of 52 on an earlier commit, where one scenario had 2 more checks) and the table-model
   script (18 of 18). The read side ran once on a copy of a sample database.
2. Partly proven: GitHub. The workflows ran on GitHub Actions in one scratch demo repository
   of a user account: release create, verify (a pass and a refusal), the dispatch of an existing
   release, and the targets, record and incident jobs of a stage. Every job that needs Azure
   stopped at `azure/login`, because no tenant was wired. Not proven: every step after the login,
   the gate job, approvals, the OIDC subject against Entra, runner groups, a reference by tag.
   The checklist G1 to G7, the wiring W1 to W15 and the bypass tests B1 to B6 are open.
3. Not proven: Windows against a database (the unit tests pass on the Windows runners of
   `ci.yml`), Python 3.14 against a database, and the runner image.
4. Through the command line on two more disposable databases: `setup-sql`, `export`, `baseline`,
   `plan`, `deploy`, `drift` and `resolve` ran (one release walk, one onboarding of an existing
   schema). Not proven: the role path of `setup-sql` for environments that share one identity,
   and a `deploy` of an approved plan through a gated workflow.
5. The command-line runs of line 4 were made before the last fix wave and were not repeated on
   the final commit. The tree as it stands has not run against a database as a whole. The tool
   is not production-ready until the open tests pass.

Before the pilot, run the live scripts on the disposable database of row 12, from the machine
where you installed the tool. The driver, the token and the network path differ in each
environment, and these scripts are the only test of them. `docs/live-testing.md`, sections 1 to
3, has the guards and the order.

```powershell
cd $TOOL
az login
uv sync --frozen --extra db
uv run python scripts/live_spike.py --list
uv run --extra db python scripts/live_spike.py --server <server>.database.windows.net --database <db> --confirm-disposable-database <db>
uv run --extra db python scripts/live_acceptance.py --server <server>.database.windows.net --database <db> --confirm-disposable-database <db>
```

Check: each script ends with exit 0. Exit 22 = a guard refused and nothing was written; 24 = no
connection or no token; 1 = an item or a check failed. `scripts/live_tables.py` takes the same
three options and covers the table model; run it before the acceptance run. A result on Windows
is new: write it into `docs/known-gaps.md`. Then prove the GitHub wiring on a scratch database repository
(`docs/live-testing.md`, section 8) before a pipeline reaches a database that matters.

## F. When something fails

1. Read the last lines that the tool printed: `REASON_CODE: message`, then
   `azsqlcd <command>: exit N`. The exit code tells the state of the database: 21 rolled back,
   22 nothing was sent, 23 unknown until a human looks, 24 clean stop, 25 another run holds the
   lock. Any other code: the tool did not start.
2. `docs/runbook.md` has one row for each reason code under its exit code, with the command.
3. `docs/triage.md` tells how to make one zip of the log and the reports of the run, and what to
   paste for a person or an AI assistant who was not there:

   ```
   azsqlcd support-bundle --latest --log-dir <log directory> --out azsqlcd-support.zip
   ```

   The log holds no SQL text, no data value and no token. Read section 5 of that document before
   the zip leaves your organisation.
4. Never change a table of schema `azsqlcd` by hand, never run a migration file by hand, and
   never edit a merged migration (`docs/runbook.md`, "Rules with no exception").

## G. Rollout after the pilot

- One repository for each database. Each one repeats section D: six identities, ten
  environments, `setup-sql` on each target, onboarding.
- Order (advice, not a rule of the tool): databases with low risk first, and only after the
  pilot has passed several releases through all five environments, with one stop that you
  recovered by the runbook. Modules only first; the table model for a database after that.
- Before each new repository: add it to both runner groups (workflow access is for each group).
- A tool upgrade moves every repository: `docs/setup.md`, section 9. One identity holds at most
  20 federated credentials (not verified), so the credentials of two tool tags fit and those of
  three do not: delete the old ones.

What to watch:

| Watch | Where |
|---|---|
| Stops in test, preprod and prod | Issues with the label `azsqlcd-incident`; subscribe `<dba-team>` |
| The nightly drift report | The `db` workflow of each repository. A failed scheduled run notifies only the person who last changed the schedule |
| Repository settings that moved | `scripts/setup_repo.py --repo <target-org>/<db-repo> --dba-team <dba-team> --check` on a schedule; the script does not schedule itself |
| Approvals of older releases | Reject them in the Actions page |
| The time of `verify` on a large database | About 100 s on 2,000 tables (`docs/known-gaps.md`, N5-01) |
| Open findings of the tool | `docs/known-gaps.md`, section 10 |
