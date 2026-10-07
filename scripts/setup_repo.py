"""Set a database repository to the azsqlcd policy, or compare it with the policy.

Owner-run, with the gh command line and the login of the person who runs it. Idempotent: the
script reads the repository, computes the difference and sends only that. A second run sends no
write.

    setup_repo.py --repo OWNER/NAME --dba-team SLUG            apply the policy
    setup_repo.py --repo OWNER/NAME --dba-team SLUG --check    compare only; exit 1 on a difference
    setup_repo.py --repo OWNER/NAME --print-azure              print the az commands; no call is made

Policy (docs/design.md, section (i) and amendment A29):
- ten environments: dev, sandbox, test, preprod, prod and one `-plan` twin of each;
- every environment deploys from branch `main` only;
- preprod and prod need a review by the DBA team; prod also prevents self-review;
- branch ruleset on main: pull request, one approval, code-owner review, stale approvals dismissed,
  approval of the most recent push, squash merge only, the verify check, no bypass actors;
- tag ruleset on r* and v*: no update, no deletion, no bypass actors.

Two items cannot be set safely by this script. It reports them and `--check` fails on them:
- "allow administrators to bypass" of a reviewed environment (repository settings page);
- the OIDC subject form with job_workflow_ref, which is blocking for prod (see --print-azure).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TOOL_REPO = "akaalholdings/azsqlcd"
STAGES = ("dev", "sandbox", "test", "preprod", "prod")
REVIEWED = ("preprod", "prod")
SELF_REVIEW_PREVENTED = ("prod",)
DEPLOY_BRANCHES = ("main",)
# Check context of the job `verify` in verify.yml, called from the job `verify` in db.yml.
REQUIRED_CHECK = "verify / verify"
BRANCH_RULESET = "azsqlcd-main"
TAG_RULESET = "azsqlcd-tags"

ISSUER = "https://token.actions.githubusercontent.com"
AUDIENCE = "api://AzureADTokenExchange"
OIDC_CLAIMS = ("repo", "context", "job_workflow_ref")
# identity suffix -> environments whose jobs may use it
IDENTITIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("nonprod-plan", ("dev-plan", "sandbox-plan", "test-plan")),
    ("prod-plan", ("preprod-plan", "prod-plan")),
    ("nonprod-deploy", ("dev", "sandbox")),
    ("test-deploy", ("test",)),
    ("preprod-deploy", ("preprod",)),
    ("prod-deploy", ("prod",)),
)
# reusable workflows that hold a job in a `-plan` environment, and in a deploy environment
PLAN_WORKFLOWS = ("stage.yml", "drift.yml", "onboard.yml")
DEPLOY_WORKFLOWS = ("stage.yml", "resolve.yml", "onboard.yml")

# (method, path, body) -> parsed JSON answer, or None when the answer has no body
Gh = Callable[[str, str, Mapping[str, Any] | None], Any]


class SetupError(Exception):
    pass


def gh(method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
    """One GitHub API call through the gh command line. The only place that starts a process."""
    command = ["gh", "api", "--method", method, "-H", "Accept: application/vnd.github+json", path]
    if body is not None:
        command += ["--input", "-"]
    done = subprocess.run(
        command,
        input=None if body is None else json.dumps(body),
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise SetupError(f"gh api {method} {path} failed: {done.stderr.strip()}")
    return json.loads(done.stdout) if done.stdout.strip() else None


# --------------------------------------------------------------------------- desired state


@dataclass(frozen=True)
class EnvironmentPolicy:
    name: str
    reviewers: tuple[str, ...]  # "Team:<id>"
    prevent_self_review: bool
    branches: tuple[str, ...]


def environment_names() -> tuple[str, ...]:
    """The ten environments, in promotion order, each stage before its plan twin."""
    return tuple(name for stage in STAGES for name in (stage, f"{stage}-plan"))


def desired_environments(dba_team_id: int) -> tuple[EnvironmentPolicy, ...]:
    return tuple(
        EnvironmentPolicy(
            name=name,
            reviewers=(f"Team:{dba_team_id}",) if name in REVIEWED else (),
            prevent_self_review=name in SELF_REVIEW_PREVENTED,
            branches=DEPLOY_BRANCHES,
        )
        for name in environment_names()
    )


def desired_rulesets() -> tuple[dict[str, Any], ...]:
    branch = {
        "name": BRANCH_RULESET,
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 1,
                    "dismiss_stale_reviews_on_push": True,
                    "require_code_owner_review": True,
                    "require_last_push_approval": True,
                    "required_review_thread_resolution": False,
                    # Squash merge only (WP2-1). One push builds one release, for the head commit,
                    # and a release applies only the migrations that its own commit added. "Rebase
                    # and merge" of two commits puts the first migration in a commit that gets no
                    # release, and no database can take the release of the head (CATCHUP_REQUIRED).
                    # A merge commit is off too: one pull request is one commit on main.
                    "allowed_merge_methods": ["squash"],
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": False,
                    "required_status_checks": [{"context": REQUIRED_CHECK}],
                },
            },
        ],
    }
    tags = {
        "name": TAG_RULESET,
        "target": "tag",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": ["refs/tags/r*", "refs/tags/v*"], "exclude": []}},
        "rules": [{"type": "update"}, {"type": "deletion"}],
    }
    return (branch, tags)


# --------------------------------------------------------------------------- live state


@dataclass(frozen=True)
class EnvironmentState:
    reviewers: tuple[str, ...]  # "Team:<id>" or "User:<id>", sorted
    prevent_self_review: bool
    branch_rule: str  # custom | protected | any
    branches: tuple[tuple[str, int], ...]  # (name, id) of the custom branch policies
    admins_can_bypass: bool | None  # None: the API did not say


@dataclass(frozen=True)
class Live:
    environments: dict[str, EnvironmentState]
    rulesets: dict[str, dict[str, Any]]  # name -> full ruleset JSON, with its id
    oidc_claims: tuple[str, ...]  # () = the default subject (repo + context)
    subject_prefix: str | None = None  # sub_claim_prefix of the API; None: the API did not say


def environment_state(
    environment: Mapping[str, Any], branch_policies: Sequence[Mapping[str, Any]]
) -> EnvironmentState:
    """Normal form of one environment as the REST API returns it."""
    reviewers: list[str] = []
    prevent_self_review = False
    for rule in environment.get("protection_rules") or []:
        if rule.get("type") == "required_reviewers":
            prevent_self_review = bool(rule.get("prevent_self_review"))
            reviewers = [f"{r['type']}:{r['reviewer']['id']}" for r in rule.get("reviewers") or []]
    policy = environment.get("deployment_branch_policy")
    if policy is None:
        branch_rule = "any"
    elif policy.get("custom_branch_policies"):
        branch_rule = "custom"
    else:
        branch_rule = "protected"
    return EnvironmentState(
        reviewers=tuple(sorted(reviewers)),
        prevent_self_review=prevent_self_review,
        branch_rule=branch_rule,
        branches=tuple(sorted((str(p["name"]), int(p["id"])) for p in branch_policies)),
        admins_can_bypass=environment.get("can_admins_bypass"),
    )


def read_live(call: Gh, repo: str) -> Live:
    environments: dict[str, EnvironmentState] = {}
    listed = call("GET", f"repos/{repo}/environments?per_page=100", None) or {}
    for environment in listed.get("environments") or []:
        name = environment["name"]
        if name not in environment_names():
            continue
        policies: Sequence[Mapping[str, Any]] = []
        if (environment.get("deployment_branch_policy") or {}).get("custom_branch_policies"):
            path = f"repos/{repo}/environments/{name}/deployment-branch-policies?per_page=100"
            policies = (call("GET", path, None) or {}).get("branch_policies") or []
        environments[name] = environment_state(environment, policies)
    rulesets: dict[str, dict[str, Any]] = {}
    for item in call("GET", f"repos/{repo}/rulesets?includes_parents=false&per_page=100", None) or []:
        if item["name"] in (BRANCH_RULESET, TAG_RULESET):
            rulesets[item["name"]] = call("GET", f"repos/{repo}/rulesets/{item['id']}", None)
    subject = call("GET", f"repos/{repo}/actions/oidc/customization/sub", None) or {}
    claims = () if subject.get("use_default", True) else tuple(subject.get("include_claim_keys") or ())
    prefix = subject.get("sub_claim_prefix")
    return Live(
        environments=environments,
        rulesets=rulesets,
        oidc_claims=claims,
        subject_prefix=prefix if isinstance(prefix, str) and prefix else None,
    )


# --------------------------------------------------------------------------- difference


@dataclass(frozen=True)
class Change:
    summary: str
    method: str
    path: str
    body: dict[str, Any] | None


def environment_changes(repo: str, policy: EnvironmentPolicy, state: EnvironmentState | None) -> list[Change]:
    base = f"repos/{repo}/environments/{policy.name}"
    problems: list[str] = []
    if state is None:
        problems.append("does not exist")
    else:
        if state.reviewers != policy.reviewers:
            problems.append(f"reviewers are {list(state.reviewers)}, policy {list(policy.reviewers)}")
        if state.prevent_self_review != policy.prevent_self_review:
            problems.append(
                f"prevent self-review is {state.prevent_self_review}, policy {policy.prevent_self_review}"
            )
        if state.branch_rule != "custom":
            problems.append(f"deployment branches: {state.branch_rule}, policy: named branches only")
    changes: list[Change] = []
    if problems:
        body = {
            "prevent_self_review": policy.prevent_self_review,
            "reviewers": [{"type": r.split(":")[0], "id": int(r.split(":")[1])} for r in policy.reviewers]
            or None,
            "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
        }
        changes.append(Change(f"environment {policy.name}: {'; '.join(problems)}", "PUT", base, body))
    live_branches = dict(state.branches) if state is not None and state.branch_rule == "custom" else {}
    for branch in policy.branches:
        if branch not in live_branches:
            summary = f"environment {policy.name}: branch {branch} may not deploy"
            body = {"name": branch, "type": "branch"}
            changes.append(Change(summary, "POST", f"{base}/deployment-branch-policies", body))
    for branch, policy_id in live_branches.items():
        if branch not in policy.branches:
            summary = f"environment {policy.name}: branch pattern {branch} may deploy"
            changes.append(Change(summary, "DELETE", f"{base}/deployment-branch-policies/{policy_id}", None))
    return changes


def ruleset_problems(desired: Mapping[str, Any], live: Mapping[str, Any]) -> list[str]:
    """Where a live ruleset is weaker than the policy. Rules and parameters outside the policy are ignored."""
    problems: list[str] = []
    for key in ("target", "enforcement"):
        if live.get(key) != desired[key]:
            problems.append(f"{key} is {live.get(key)!r}, policy {desired[key]!r}")
    if live.get("bypass_actors") is None:
        problems.append("bypass actors cannot be read with this login")
    elif live["bypass_actors"]:
        problems.append(f"{len(live['bypass_actors'])} bypass actor(s), policy none")
    live_refs = (live.get("conditions") or {}).get("ref_name") or {}
    for key in ("include", "exclude"):
        want = sorted(desired["conditions"]["ref_name"][key])
        if sorted(live_refs.get(key) or []) != want:
            problems.append(f"{key} is {sorted(live_refs.get(key) or [])}, policy {want}")
    live_rules = {rule["type"]: rule.get("parameters") or {} for rule in live.get("rules") or []}
    for rule in desired["rules"]:
        if rule["type"] not in live_rules:
            problems.append(f"rule {rule['type']} is missing")
            continue
        for name, want in (rule.get("parameters") or {}).items():
            have = live_rules[rule["type"]].get(name)
            if name == "required_status_checks":
                want = sorted(check["context"] for check in want)
                have = sorted(check["context"] for check in have or [])
            elif name == "allowed_merge_methods":
                # not set: GitHub allows merge, squash and rebase
                want, have = sorted(want), sorted(have) if have is not None else None
            if have != want:
                problems.append(f"rule {rule['type']}: {name} is {have!r}, policy {want!r}")
    return problems


def ruleset_changes(repo: str, desired: Mapping[str, Any], live: Mapping[str, Any] | None) -> list[Change]:
    name = desired["name"]
    if live is None:
        return [Change(f"ruleset {name}: does not exist", "POST", f"repos/{repo}/rulesets", dict(desired))]
    problems = ruleset_problems(desired, live)
    if not problems:
        return []
    path = f"repos/{repo}/rulesets/{live['id']}"
    return [Change(f"ruleset {name}: {'; '.join(problems)}", "PUT", path, dict(desired))]


def plan_changes(repo: str, dba_team_id: int, live: Live) -> list[Change]:
    """Every write that brings the repository to the policy. Empty when it is there already."""
    changes: list[Change] = []
    for policy in desired_environments(dba_team_id):
        changes += environment_changes(repo, policy, live.environments.get(policy.name))
    for ruleset in desired_rulesets():
        changes += ruleset_changes(repo, ruleset, live.rulesets.get(ruleset["name"]))
    return changes


def manual_items(repo: str, live: Live) -> list[str]:
    """Policy items that this script does not set. Each one is a difference for --check."""
    items: list[str] = []
    for name in REVIEWED:
        state = live.environments.get(name)
        if state is None or state.admins_can_bypass is not False:
            items.append(
                f"environment {name}: turn off 'Allow administrators to bypass configured protection rules' "
                f"(https://github.com/{repo}/settings/environments)"
            )
    if "job_workflow_ref" not in live.oidc_claims:
        items.append(
            "OIDC subject: job_workflow_ref is not in the subject. Blocking for prod (A29). "
            "Follow the order that --print-azure prints."
        )
    return items


# --------------------------------------------------------------------------- Azure commands


def project_name(repo: str) -> str:
    """Default project name: the repository name without a leading 'db-'."""
    name = repo.split("/", 1)[1]
    return name[3:] if name.startswith("db-") else name


SUBJECT_PREFIX = re.compile(r"repo:[A-Za-z0-9._@/-]+")


def subject(
    repo: str,
    environment: str,
    workflow: str | None = None,
    tool_tag: str | None = None,
    prefix: str | None = None,
) -> str:
    """Federated-credential subject. With a workflow: the form that holds job_workflow_ref.

    prefix is the repository part that GitHub sends (sub_claim_prefix of the OIDC customization
    API). A repository with the immutable subject form sends repo:OWNER@ID/NAME@ID, not
    repo:OWNER/NAME (seen in the azure/login log of a real run). Entra compares the whole text.
    """
    if prefix is not None and not SUBJECT_PREFIX.fullmatch(prefix):
        raise ValueError(f"not a subject prefix: {prefix!r}")
    short = f"{prefix or f'repo:{repo}'}:environment:{environment}"
    if workflow is None:
        return short
    return f"{short}:job_workflow_ref:{TOOL_REPO}/.github/workflows/{workflow}@refs/tags/{tool_tag}"


def _credential(identity: str, name: str, subject_text: str) -> str:
    return (
        f'az identity federated-credential create --name "{name}" --identity-name "$PROJECT-{identity}" '
        f'--resource-group "$RG" --issuer "{ISSUER}" --subject "{subject_text}" --audiences "{AUDIENCE}"'
    )


def azure_commands(repo: str, tool_version: str, prefix: str | None = None) -> str:
    """Shell text for a human to review and run. Nothing in it is a secret."""
    tag = f"v{tool_version}"
    slug = tag.replace(".", "-")
    shown = prefix or f"repo:{repo}"
    lines = [
        f"# azsqlcd: identities and federated credentials for {repo}",
        "# Review, set RG and LOCATION, then run in a shell where `az login` is done.",
        "# No identity gets an Azure role. Database rights come from `azsqlcd setup-sql`.",
        f"# Subject prefix of every credential below: {shown}",
        f"#   Check it first: gh api repos/{repo}/actions/oidc/customization/sub",
        "#   When sub_claim_prefix in the answer is another text (the immutable form",
        "#   repo:OWNER@ID/NAME@ID), run this script again with --subject-prefix '<that text>'.",
        "#   Entra refuses a credential whose subject is not equal to the subject that GitHub sends.",
        'RG="<resource group>"',
        'LOCATION="<azure region>"',
        f'PROJECT="{project_name(repo)}"  # must equal [project].name in azsqlcd.toml',
        "",
        "# 1. Identities. Put each printed clientId into [identities] of azsqlcd.toml.",
    ]
    lines += [
        f'az identity create --name "$PROJECT-{identity}" --resource-group "$RG" --location "$LOCATION"'
        for identity, _ in IDENTITIES
    ]
    lines += [
        "",
        "# 2. Federated credentials with the default subject (repository + environment).",
        "#    Use them to bring up dev, sandbox, test and preprod.",
    ]
    for identity, environments in IDENTITIES:
        for environment in environments:
            if environment == "prod":
                continue
            lines.append(_credential(identity, environment, subject(repo, environment, prefix=prefix)))
    lines += [
        "",
        "# 3. BLOCKING FOR PROD (A29). The subject of the prod deploy identity must hold job_workflow_ref,",
        f"#    so that only the reusable workflows of {TOOL_REPO} at tag {tag} can get its token.",
        f"#    Section 2 has no credential for environment prod: never create {shown}:environment:prod.",
        "#    GitHub sets the subject form for the whole repository, not for one environment:",
        "#      a. create the credentials below;",
        "#      b. run the gh command; from then on every environment sends the long subject;",
        "#      c. delete the credentials of section 2, which no longer match.",
        "#    A tool upgrade changes the tag in the subject. Create the credentials for the new tag",
        "#    before the pull request that changes the tag in the workflows; delete the old ones after.",
    ]
    for identity, environments in IDENTITIES:
        for environment in environments:
            workflows = PLAN_WORKFLOWS if environment.endswith("-plan") else DEPLOY_WORKFLOWS
            for workflow in workflows:
                name = f"{environment}-{workflow.removesuffix('.yml')}-{slug}"
                lines.append(_credential(identity, name, subject(repo, environment, workflow, tag, prefix)))
    claims = " ".join(f'-f "include_claim_keys[]={claim}"' for claim in OIDC_CLAIMS)
    lines.append(
        f"gh api --method PUT repos/{repo}/actions/oidc/customization/sub -F use_default=false {claims}"
    )
    return "\n".join(lines) + "\n"


def tool_version() -> str:
    """Version of the tool that this script ships with (pyproject.toml of this checkout)."""
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]


# --------------------------------------------------------------------------- command line


def run(call: Gh, repo: str, dba_team: str, check: bool, out: Callable[[str], None]) -> int:
    """Compare, and apply unless `check`. Returns the exit code: 0 = the repository meets the policy."""
    org = repo.split("/", 1)[0]
    team_id = int(call("GET", f"orgs/{org}/teams/{dba_team}", None)["id"])
    live = read_live(call, repo)
    changes = plan_changes(repo, team_id, live)
    for change in changes:
        if not check:
            call(change.method, change.path, change.body)
        out(f"{'differs' if check else 'applied'}: {change.summary}")
    manual = manual_items(repo, live)
    for item in manual:
        out(f"manual: {item}")
    if live.subject_prefix and live.subject_prefix != f"repo:{repo}":
        # not a difference from the policy: the az commands must use the text that GitHub sends
        out(
            f"note: GitHub sends the immutable OIDC subject form for this repository. Entra refuses a "
            f"credential with repo:{repo}. Print the az commands with "
            f"--print-azure --subject-prefix '{live.subject_prefix}'"
        )
    if manual or (check and changes):
        return 1
    out(f"{repo} meets the policy")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set a database repository to the azsqlcd policy.")
    parser.add_argument("--repo", required=True, metavar="OWNER/NAME")
    parser.add_argument("--dba-team", metavar="SLUG", help="team that reviews preprod and prod")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="compare only; exit 1 on a difference")
    mode.add_argument("--print-azure", action="store_true", help="print the az commands; call nothing")
    parser.add_argument(
        "--subject-prefix",
        metavar="TEXT",
        help="with --print-azure: the sub_claim_prefix that "
        "`gh api repos/OWNER/NAME/actions/oidc/customization/sub` returns, when it is not repo:OWNER/NAME "
        "(the immutable form repo:OWNER@ID/NAME@ID)",
    )
    args = parser.parse_args(argv)
    owner, _, name = args.repo.partition("/")
    if not owner or not name or "/" in name:
        parser.error("--repo must be OWNER/NAME")
    if args.subject_prefix is not None and not args.print_azure:
        parser.error("--subject-prefix is an option of --print-azure")
    if args.subject_prefix is not None and not SUBJECT_PREFIX.fullmatch(args.subject_prefix):
        parser.error("--subject-prefix must be the text of sub_claim_prefix, for example repo:OWNER@1/NAME@2")
    if args.print_azure:
        sys.stdout.write(azure_commands(args.repo, tool_version(), args.subject_prefix))
        return 0
    if not args.dba_team:
        parser.error("--dba-team is required")
    try:
        return run(gh, args.repo, args.dba_team, args.check, print)
    except SetupError as error:
        print(f"setup_repo: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
