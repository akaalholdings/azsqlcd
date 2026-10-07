"""scripts/setup_repo.py: the repository policy and the difference to a live repository.

No test starts a process. `FakeGitHub` is an in-memory stand-in for the REST endpoints; it answers
in the shape of the GitHub REST API, with the extra fields that the API adds.
"""

from __future__ import annotations

import copy
import importlib.util
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "setup_repo.py"
REPO = "akaalholdings/db-sales"
TEAM_ID = 42


def _load():
    spec = importlib.util.spec_from_file_location("setup_repo", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


setup_repo = _load()


@pytest.fixture(autouse=True)
def no_process(monkeypatch: pytest.MonkeyPatch):
    def refuse(*args: object, **kwargs: object):
        raise AssertionError("a unit test must not start a process")

    monkeypatch.setattr(setup_repo.subprocess, "run", refuse)


class FakeGitHub:
    def __init__(self) -> None:
        self.environments: dict[str, dict[str, Any]] = {}
        self.branch_policies: dict[str, dict[int, str]] = {}
        self.rulesets: dict[int, dict[str, Any]] = {}
        self.oidc: dict[str, Any] = {"use_default": True}
        self.writes: list[tuple[str, str]] = []
        self._next_id = 100

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _environment(self, name: str) -> dict[str, Any]:
        stored = self.environments[name]
        rules: list[dict[str, Any]] = []
        if stored["reviewers"]:
            reviewers = [
                {"type": r["type"], "reviewer": {"id": r["id"], "slug": "dba"}} for r in stored["reviewers"]
            ]
            rules.append(
                {
                    "id": 1,
                    "type": "required_reviewers",
                    "prevent_self_review": stored["prevent_self_review"],
                    "reviewers": reviewers,
                }
            )
        if stored["deployment_branch_policy"]:
            rules.append({"id": 2, "type": "branch_policy"})
        return {
            "id": 7,
            "name": name,
            "protection_rules": rules,
            "deployment_branch_policy": stored["deployment_branch_policy"],
            "can_admins_bypass": stored["can_admins_bypass"],
        }

    def _ruleset(self, ruleset_id: int) -> dict[str, Any]:
        # the API adds defaults and ids that the caller did not send
        answer = copy.deepcopy(self.rulesets[ruleset_id])
        for rule in answer["rules"]:
            if rule["type"] == "pull_request":
                # a ruleset that names no merge method allows all three
                rule["parameters"].setdefault("allowed_merge_methods", ["merge", "squash", "rebase"])
            if rule["type"] == "required_status_checks":
                for check in rule["parameters"]["required_status_checks"]:
                    check["integration_id"] = 15368
        return {"id": ruleset_id, "source_type": "Repository", "source": REPO, **answer}

    def __call__(self, method: str, path: str, body: Mapping[str, Any] | None) -> Any:
        path = path.split("?")[0]
        if method != "GET":
            self.writes.append((method, path))
        parts = path.split("/")
        if parts[0] == "orgs":
            assert parts[1:] == ["akaalholdings", "teams", "dba"]
            return {"id": TEAM_ID, "slug": "dba"}
        assert "/".join(parts[:3]) == f"repos/{REPO}"
        match method, parts[3:]:
            case "GET", ["environments"]:
                return {"environments": [self._environment(name) for name in self.environments]}
            case "PUT", ["environments", name]:
                assert body is not None
                previous = self.environments.get(name, {})
                self.environments[name] = {
                    "reviewers": body["reviewers"] or [],
                    "prevent_self_review": body["prevent_self_review"],
                    "deployment_branch_policy": body["deployment_branch_policy"],
                    "can_admins_bypass": previous.get("can_admins_bypass", True),
                }
                self.branch_policies.setdefault(name, {})
                return self._environment(name)
            case "GET", ["environments", name, "deployment-branch-policies"]:
                policies = [
                    {"id": i, "name": n, "type": "branch"} for i, n in self.branch_policies[name].items()
                ]
                return {"total_count": len(policies), "branch_policies": policies}
            case "POST", ["environments", name, "deployment-branch-policies"]:
                assert (
                    body is not None
                    and self.environments[name]["deployment_branch_policy"]["custom_branch_policies"]
                )
                self.branch_policies[name][self._id()] = body["name"]
                return None
            case "DELETE", ["environments", name, "deployment-branch-policies", policy_id]:
                del self.branch_policies[name][int(policy_id)]
                return None
            case "GET", ["rulesets"]:
                return [{"id": i, "name": r["name"], "target": r["target"]} for i, r in self.rulesets.items()]
            case "GET", ["rulesets", ruleset_id]:
                return self._ruleset(int(ruleset_id))
            case "POST", ["rulesets"]:
                assert body is not None
                self.rulesets[self._id()] = copy.deepcopy(dict(body))
                return None
            case "PUT", ["rulesets", ruleset_id]:
                assert body is not None and int(ruleset_id) in self.rulesets
                self.rulesets[int(ruleset_id)] = copy.deepcopy(dict(body))
                return None
            case "GET", ["actions", "oidc", "customization", "sub"]:
                return self.oidc
            case _:
                raise AssertionError(f"unexpected call {method} {path}")


def apply(github: FakeGitHub) -> tuple[int, list[str]]:
    lines: list[str] = []
    return setup_repo.run(github, REPO, "dba", False, lines.append), lines


def check(github: FakeGitHub) -> tuple[int, list[str]]:
    lines: list[str] = []
    return setup_repo.run(github, REPO, "dba", True, lines.append), lines


def at_policy() -> FakeGitHub:
    """A repository after one apply and after the two manual items."""
    github = FakeGitHub()
    apply(github)
    for name in ("preprod", "prod"):
        github.environments[name]["can_admins_bypass"] = False
    github.oidc = {"use_default": False, "include_claim_keys": ["repo", "context", "job_workflow_ref"]}
    github.writes.clear()
    return github


def ruleset(github: FakeGitHub, name: str) -> dict[str, Any]:
    (found,) = [r for r in github.rulesets.values() if r["name"] == name]
    return found


def rule(body: Mapping[str, Any], rule_type: str) -> dict[str, Any]:
    (found,) = [r for r in body["rules"] if r["type"] == rule_type]
    return found


# ----------------------------------------------------------------------------- the policy


def test_policy_has_ten_environments_and_each_deploys_from_main_only():
    policies = setup_repo.desired_environments(TEAM_ID)
    assert [p.name for p in policies] == [
        "dev",
        "dev-plan",
        "sandbox",
        "sandbox-plan",
        "test",
        "test-plan",
        "preprod",
        "preprod-plan",
        "prod",
        "prod-plan",
    ]
    assert all(p.branches == ("main",) for p in policies)


def test_only_preprod_and_prod_need_a_review_and_only_prod_prevents_self_review():
    policies = {p.name: p for p in setup_repo.desired_environments(TEAM_ID)}
    assert {name for name, p in policies.items() if p.reviewers} == {"preprod", "prod"}
    assert policies["prod"].reviewers == policies["preprod"].reviewers == (f"Team:{TEAM_ID}",)
    assert {name for name, p in policies.items() if p.prevent_self_review} == {"prod"}


def test_plan_environments_never_wait_for_a_reviewer():
    assert all(not p.reviewers for p in setup_repo.desired_environments(TEAM_ID) if p.name.endswith("-plan"))


def test_branch_ruleset_is_the_a29_rule_set_with_no_bypass():
    branch, _ = setup_repo.desired_rulesets()
    assert branch["target"] == "branch" and branch["enforcement"] == "active"
    assert branch["bypass_actors"] == []
    assert branch["conditions"]["ref_name"] == {"include": ["refs/heads/main"], "exclude": []}
    assert rule(branch, "pull_request")["parameters"] == {
        "required_approving_review_count": 1,
        "dismiss_stale_reviews_on_push": True,
        "require_code_owner_review": True,
        "require_last_push_approval": True,
        "required_review_thread_resolution": False,
        # WP2-1: one pull request = one commit on main = one release
        "allowed_merge_methods": ["squash"],
    }
    checks = rule(branch, "required_status_checks")["parameters"]["required_status_checks"]
    assert checks == [{"context": "verify / verify"}]
    assert {r["type"] for r in branch["rules"]} >= {"deletion", "non_fast_forward"}


def test_required_check_is_the_verify_job_of_the_template():
    db = yaml.safe_load(
        (REPO_ROOT / "templates/db-repo/.github/workflows/db.yml").read_text(encoding="utf-8")
    )
    called = yaml.safe_load((REPO_ROOT / ".github/workflows/verify.yml").read_text(encoding="utf-8"))
    caller_job, called_job = setup_repo.REQUIRED_CHECK.split(" / ")
    assert db["jobs"][caller_job]["uses"].endswith("/verify.yml@v" + setup_repo.tool_version())
    assert called_job in called["jobs"]


def test_tag_ruleset_stops_a_release_or_tool_tag_from_moving_or_vanishing():
    _, tags = setup_repo.desired_rulesets()
    assert tags["target"] == "tag" and tags["enforcement"] == "active" and tags["bypass_actors"] == []
    assert tags["conditions"]["ref_name"]["include"] == ["refs/tags/r*", "refs/tags/v*"]
    assert {r["type"] for r in tags["rules"]} == {"update", "deletion"}


# ----------------------------------------------------------------------------- live state


def test_environment_state_reads_the_rest_shape():
    environment = {
        "name": "prod",
        "protection_rules": [
            {"id": 1, "type": "wait_timer", "wait_timer": 30},
            {
                "id": 2,
                "type": "required_reviewers",
                "prevent_self_review": True,
                "reviewers": [
                    {"type": "User", "reviewer": {"id": 9, "login": "x"}},
                    {"type": "Team", "reviewer": {"id": 42, "slug": "dba"}},
                ],
            },
            {"id": 3, "type": "branch_policy"},
        ],
        "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
        "can_admins_bypass": False,
    }
    policies = [{"id": 5, "name": "main", "type": "branch"}]
    assert setup_repo.environment_state(environment, policies) == setup_repo.EnvironmentState(
        reviewers=("Team:42", "User:9"),
        prevent_self_review=True,
        branch_rule="custom",
        branches=(("main", 5),),
        admins_can_bypass=False,
    )


@pytest.mark.parametrize(
    "policy, expected",
    [(None, "any"), ({"protected_branches": True, "custom_branch_policies": False}, "protected")],
)
def test_environment_state_tells_an_open_environment_from_a_limited_one(policy, expected: str):
    state = setup_repo.environment_state(
        {"name": "dev", "protection_rules": [], "deployment_branch_policy": policy}, []
    )
    assert (state.branch_rule, state.reviewers, state.admins_can_bypass) == (expected, (), None)


# ----------------------------------------------------------------------------- difference and apply


def test_empty_repository_gets_every_environment_and_both_rulesets():
    github = FakeGitHub()
    code, lines = apply(github)
    assert sorted(github.environments) == sorted(setup_repo.environment_names())
    assert all(list(policies.values()) == ["main"] for policies in github.branch_policies.values())
    assert github.environments["prod"]["reviewers"] == [{"type": "Team", "id": TEAM_ID}]
    assert github.environments["prod"]["prevent_self_review"] is True
    assert github.environments["preprod"]["prevent_self_review"] is False
    assert github.environments["dev"]["reviewers"] == []
    assert sorted(r["name"] for r in github.rulesets.values()) == ["azsqlcd-main", "azsqlcd-tags"]
    assert len(github.writes) == 22 and len([line for line in lines if line.startswith("applied: ")]) == 22
    assert code == 1  # the manual items are open
    assert len([line for line in lines if line.startswith("manual: ")]) == 3


def test_second_apply_sends_no_write():
    github = FakeGitHub()
    apply(github)
    github.writes.clear()
    apply(github)
    assert github.writes == []


def test_repository_at_policy_passes_the_check_with_no_write():
    github = at_policy()
    assert check(github) == (0, [f"{REPO} meets the policy"])
    assert github.writes == []


def test_check_never_writes_and_fails_on_a_difference():
    github = FakeGitHub()
    code, lines = check(github)
    assert code == 1 and github.writes == [] and github.environments == {}
    assert len([line for line in lines if line.startswith("differs: ")]) == 22


def weaken_reviewers(github: FakeGitHub) -> None:
    github.environments["prod"]["reviewers"] = []


def weaken_self_review(github: FakeGitHub) -> None:
    github.environments["prod"]["prevent_self_review"] = False


def add_reviewer_to_dev_plan(github: FakeGitHub) -> None:
    github.environments["dev-plan"]["reviewers"] = [{"type": "User", "id": 9}]


def open_branches(github: FakeGitHub) -> None:
    github.environments["prod"]["deployment_branch_policy"] = None
    github.branch_policies["prod"] = {}


def protected_branches(github: FakeGitHub) -> None:
    github.environments["test"]["deployment_branch_policy"] = {
        "protected_branches": True,
        "custom_branch_policies": False,
    }
    github.branch_policies["test"] = {}


def extra_branch(github: FakeGitHub) -> None:
    github.branch_policies["prod"][999] = "hotfix/*"


def bypass_actor(github: FakeGitHub) -> None:
    ruleset(github, "azsqlcd-main")["bypass_actors"] = [{"actor_id": 5, "actor_type": "RepositoryRole"}]


def tag_bypass_actor(github: FakeGitHub) -> None:
    ruleset(github, "azsqlcd-tags")["bypass_actors"] = [{"actor_id": 1, "actor_type": "OrganizationAdmin"}]


def no_approval(github: FakeGitHub) -> None:
    rule(ruleset(github, "azsqlcd-main"), "pull_request")["parameters"]["required_approving_review_count"] = 0


def stale_approvals_stay(github: FakeGitHub) -> None:
    rule(ruleset(github, "azsqlcd-main"), "pull_request")["parameters"]["dismiss_stale_reviews_on_push"] = (
        False
    )


def last_push_not_approved(github: FakeGitHub) -> None:
    rule(ruleset(github, "azsqlcd-main"), "pull_request")["parameters"]["require_last_push_approval"] = False


def no_code_owner(github: FakeGitHub) -> None:
    rule(ruleset(github, "azsqlcd-main"), "pull_request")["parameters"]["require_code_owner_review"] = False


def rebase_merge_allowed(github: FakeGitHub) -> None:
    # "Rebase and merge" of two commits puts a migration in a commit that gets no release (WP2-1)
    parameters = rule(ruleset(github, "azsqlcd-main"), "pull_request")["parameters"]
    parameters["allowed_merge_methods"] = ["squash", "rebase"]


def merge_commit_allowed(github: FakeGitHub) -> None:
    parameters = rule(ruleset(github, "azsqlcd-main"), "pull_request")["parameters"]
    parameters["allowed_merge_methods"] = ["merge", "squash"]


def merge_methods_not_set(github: FakeGitHub) -> None:
    # a ruleset that an older version of this script made: GitHub then allows all three methods
    del rule(ruleset(github, "azsqlcd-main"), "pull_request")["parameters"]["allowed_merge_methods"]


def other_check(github: FakeGitHub) -> None:
    parameters = rule(ruleset(github, "azsqlcd-main"), "required_status_checks")["parameters"]
    parameters["required_status_checks"] = [{"context": "lint"}]


def not_enforced(github: FakeGitHub) -> None:
    ruleset(github, "azsqlcd-main")["enforcement"] = "evaluate"


def tags_may_be_deleted(github: FakeGitHub) -> None:
    body = ruleset(github, "azsqlcd-tags")
    body["rules"] = [r for r in body["rules"] if r["type"] != "deletion"]


def release_tags_not_covered(github: FakeGitHub) -> None:
    ruleset(github, "azsqlcd-tags")["conditions"]["ref_name"]["include"] = ["refs/tags/v*"]


def main_excluded(github: FakeGitHub) -> None:
    ruleset(github, "azsqlcd-main")["conditions"]["ref_name"]["exclude"] = ["refs/heads/main"]


def environment_deleted(github: FakeGitHub) -> None:
    del github.environments["prod-plan"]
    del github.branch_policies["prod-plan"]


WEAKENED = [
    (weaken_reviewers, [("PUT", "environments/prod")]),
    (weaken_self_review, [("PUT", "environments/prod")]),
    (add_reviewer_to_dev_plan, [("PUT", "environments/dev-plan")]),
    (open_branches, [("PUT", "environments/prod"), ("POST", "environments/prod/deployment-branch-policies")]),
    (
        protected_branches,
        [("PUT", "environments/test"), ("POST", "environments/test/deployment-branch-policies")],
    ),
    (extra_branch, [("DELETE", "environments/prod/deployment-branch-policies/999")]),
    (
        environment_deleted,
        [("PUT", "environments/prod-plan"), ("POST", "environments/prod-plan/deployment-branch-policies")],
    ),
    (bypass_actor, [("PUT", "rulesets/<main>")]),
    (tag_bypass_actor, [("PUT", "rulesets/<tags>")]),
    (no_approval, [("PUT", "rulesets/<main>")]),
    (stale_approvals_stay, [("PUT", "rulesets/<main>")]),
    (last_push_not_approved, [("PUT", "rulesets/<main>")]),
    (no_code_owner, [("PUT", "rulesets/<main>")]),
    (rebase_merge_allowed, [("PUT", "rulesets/<main>")]),
    (merge_commit_allowed, [("PUT", "rulesets/<main>")]),
    (merge_methods_not_set, [("PUT", "rulesets/<main>")]),
    (other_check, [("PUT", "rulesets/<main>")]),
    (not_enforced, [("PUT", "rulesets/<main>")]),
    (main_excluded, [("PUT", "rulesets/<main>")]),
    (tags_may_be_deleted, [("PUT", "rulesets/<tags>")]),
    (release_tags_not_covered, [("PUT", "rulesets/<tags>")]),
]


@pytest.mark.parametrize("weaken, expected", WEAKENED, ids=[w.__name__ for w, _ in WEAKENED])
def test_a_weakened_setting_fails_the_check_and_apply_repairs_exactly_that(weaken, expected):
    github = at_policy()
    ids = {"<main>": None, "<tags>": None}
    for ruleset_id, body in github.rulesets.items():
        ids["<main>" if body["name"] == "azsqlcd-main" else "<tags>"] = ruleset_id
    weaken(github)

    code, lines = check(github)
    assert code == 1 and github.writes == []
    assert len([line for line in lines if line.startswith("differs: ")]) == len(expected)

    apply(github)
    wanted = [(method, f"repos/{REPO}/{path}") for method, path in expected]
    wanted = [
        (m, p.replace("<main>", str(ids["<main>"])).replace("<tags>", str(ids["<tags>"]))) for m, p in wanted
    ]
    assert github.writes == wanted
    assert check(github)[0] == 0


def test_a_stricter_live_ruleset_is_left_alone():
    github = at_policy()
    body = ruleset(github, "azsqlcd-main")
    body["rules"].append({"type": "required_linear_history"})
    rule(body, "pull_request")["parameters"]["required_review_thread_resolution"] = False
    assert check(github)[0] == 0


def test_ruleset_whose_bypass_list_cannot_be_read_is_a_difference():
    desired, _ = setup_repo.desired_rulesets()
    live = {**copy.deepcopy(desired), "id": 1}
    del live["bypass_actors"]
    assert setup_repo.ruleset_problems(desired, live) == ["bypass actors cannot be read with this login"]


def test_other_environments_and_rulesets_of_the_repository_are_not_touched():
    github = at_policy()
    github.environments["docs-preview"] = {
        "reviewers": [],
        "prevent_self_review": False,
        "deployment_branch_policy": None,
        "can_admins_bypass": True,
    }
    github.rulesets[900] = {"name": "team-rule", "target": "branch", "enforcement": "disabled", "rules": []}
    assert apply(github)[0] == 0 and github.writes == []


# ----------------------------------------------------------------------------- manual items


def test_admin_bypass_on_a_reviewed_environment_keeps_the_check_red():
    github = at_policy()
    github.environments["prod"]["can_admins_bypass"] = True
    code, lines = check(github)
    assert code == 1 and len(lines) == 1
    assert lines[0].startswith("manual: environment prod: turn off 'Allow administrators to bypass")


@pytest.mark.parametrize(
    "oidc",
    [
        {"use_default": True},
        {"use_default": False, "include_claim_keys": ["repo", "context"]},
        {"use_default": True, "include_claim_keys": ["job_workflow_ref"]},
    ],
)
def test_subject_without_job_workflow_ref_keeps_the_check_red(oidc):
    github = at_policy()
    github.oidc = oidc
    code, lines = check(github)
    assert (
        code == 1 and len(lines) == 1 and "job_workflow_ref" in lines[0] and "Blocking for prod" in lines[0]
    )


def test_apply_does_not_change_the_subject_form():
    # the change breaks every credential of the default form, so a human does it in the printed order
    github = FakeGitHub()
    apply(github)
    assert github.oidc == {"use_default": True}
    assert not any("oidc" in path for _, path in github.writes)


# ----------------------------------------------------------------------------- Azure commands


def azure(repo: str = REPO, version: str = "0.1.0") -> list[str]:
    return setup_repo.azure_commands(repo, version).splitlines()


def credentials(lines: list[str]) -> list[tuple[str, str]]:
    found = []
    for line in lines:
        match = re.search(r'--identity-name "\$PROJECT-([a-z-]+)" .* --subject "([^"]+)"', line)
        if match:
            found.append((match.group(1), match.group(2)))
    return found


def test_six_identities_are_created_and_none_gets_an_azure_role():
    lines = azure()
    created = [
        re.search(r'--name "\$PROJECT-([a-z-]+)"', line).group(1)
        for line in lines
        if line.startswith("az identity create")
    ]
    assert created == [
        "nonprod-plan",
        "prod-plan",
        "nonprod-deploy",
        "test-deploy",
        "preprod-deploy",
        "prod-deploy",
    ]
    assert not any(line.startswith("az role") for line in lines)
    assert 'PROJECT="sales"  # must equal [project].name in azsqlcd.toml' in lines


def test_default_subjects_cover_nine_environments_and_never_prod():
    short = {(identity, s) for identity, s in credentials(azure()) if "job_workflow_ref" not in s}
    assert short == {
        ("nonprod-plan", f"repo:{REPO}:environment:dev-plan"),
        ("nonprod-plan", f"repo:{REPO}:environment:sandbox-plan"),
        ("nonprod-plan", f"repo:{REPO}:environment:test-plan"),
        ("prod-plan", f"repo:{REPO}:environment:preprod-plan"),
        ("prod-plan", f"repo:{REPO}:environment:prod-plan"),
        ("nonprod-deploy", f"repo:{REPO}:environment:dev"),
        ("nonprod-deploy", f"repo:{REPO}:environment:sandbox"),
        ("test-deploy", f"repo:{REPO}:environment:test"),
        ("preprod-deploy", f"repo:{REPO}:environment:preprod"),
    }


def test_prod_deploy_identity_trusts_only_the_tool_workflows_at_the_tool_tag():
    prod = sorted(s for identity, s in credentials(azure(version="0.3.1")) if identity == "prod-deploy")
    prefix = f"repo:{REPO}:environment:prod:job_workflow_ref:akaalholdings/azsqlcd/.github/workflows/"
    assert prod == [
        prefix + f"{name}@refs/tags/v0.3.1" for name in ("onboard.yml", "resolve.yml", "stage.yml")
    ]


def test_long_subjects_exist_for_all_ten_environments_and_each_identity_keeps_its_environments():
    long = [(identity, s) for identity, s in credentials(azure()) if "job_workflow_ref" in s]
    by_environment: dict[str, set[str]] = {}
    for identity, text in long:
        environment = text.split(":environment:")[1].split(":job_workflow_ref:")[0]
        by_environment.setdefault(environment, set()).add(identity)
    assert sorted(by_environment) == sorted(setup_repo.environment_names())
    assert all(len(identities) == 1 for identities in by_environment.values())
    assert by_environment["dev"] == by_environment["sandbox"] == {"nonprod-deploy"}
    assert by_environment["prod-plan"] == by_environment["preprod-plan"] == {"prod-plan"}
    assert len(long) == 30


def test_credential_names_are_unique_inside_an_identity():
    names = [
        (
            re.search(r'--identity-name "([^"]+)"', line).group(1),
            re.search(r'create --name "([^"]+)"', line).group(1),
        )
        for line in azure()
        if line.startswith("az identity federated-credential create")
    ]
    assert len(names) == len(set(names)) == 39
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{3,120}", name) for _, name in names)


def test_output_says_that_job_workflow_ref_blocks_prod_and_gives_the_order():
    text = setup_repo.azure_commands(REPO, "0.1.0")
    assert "BLOCKING FOR PROD (A29)" in text and "job_workflow_ref" in text
    assert f"never create repo:{REPO}:environment:prod." in text
    switch = text.index(f"gh api --method PUT repos/{REPO}/actions/oidc/customization/sub")
    assert switch > text.rindex("az identity federated-credential create")
    assert "include_claim_keys[]=job_workflow_ref" in text[switch:]


def test_every_credential_uses_the_github_issuer_and_the_entra_audience():
    lines = [line for line in azure() if line.startswith("az identity federated-credential create")]
    assert all('--issuer "https://token.actions.githubusercontent.com"' in line for line in lines)
    assert all('--audiences "api://AzureADTokenExchange"' in line for line in lines)


def test_workflow_lists_match_the_jobs_that_name_an_environment():
    plan, deploy = set(), set()
    for path in (REPO_ROOT / ".github" / "workflows").glob("*.yml"):
        for job in (yaml.safe_load(path.read_text(encoding="utf-8")).get("jobs") or {}).values():
            if job.get("environment") == "${{ inputs.environment }}-plan":
                plan.add(path.name)
            elif "environment" in job:
                deploy.add(path.name)
    assert plan == set(setup_repo.PLAN_WORKFLOWS)
    assert deploy == set(setup_repo.DEPLOY_WORKFLOWS)


def test_project_name_drops_only_a_leading_db_prefix():
    assert setup_repo.project_name("akaalholdings/db-sales") == "sales"
    assert setup_repo.project_name("akaalholdings/billing-db-core") == "billing-db-core"


# ----------------------------------------------------------------------------- command line


def test_print_azure_calls_nothing(capsys: pytest.CaptureFixture[str]):
    assert setup_repo.main(["--repo", REPO, "--print-azure"]) == 0
    printed = capsys.readouterr().out
    assert printed == setup_repo.azure_commands(REPO, setup_repo.tool_version())


@pytest.mark.parametrize(
    "argv",
    [
        ["--repo", "db-sales", "--check"],
        ["--repo", "a/b/c", "--check"],
        ["--repo", REPO],
        ["--repo", REPO, "--check", "--print-azure", "--dba-team", "dba"],
        [],
    ],
)
def test_wrong_arguments_stop_before_any_call(argv):
    with pytest.raises(SystemExit) as stop:
        setup_repo.main(argv)
    assert stop.value.code == 2


def test_a_failed_call_stops_the_run_and_gives_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    def broken(method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        raise setup_repo.SetupError(f"gh api {method} {path} failed: HTTP 403")

    monkeypatch.setattr(setup_repo, "gh", broken)
    assert setup_repo.main(["--repo", REPO, "--dba-team", "dba"]) == 1
    assert "HTTP 403" in capsys.readouterr().err


def test_one_function_starts_processes():
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.count("subprocess.") == 1
    assert not re.search(r"\bos\.(system|popen|exec)|Popen", source)
    start = source.index("def gh(")
    assert start < source.index("subprocess.run(") < source.index("\ndef ", start + 1)


def test_the_check_names_the_merge_methods_that_the_repository_allows_beyond_squash():
    github = at_policy()
    rebase_merge_allowed(github)
    code, lines = check(github)
    assert code == 1
    [line] = [line for line in lines if line.startswith("differs: ")]
    assert "allowed_merge_methods is ['rebase', 'squash'], policy ['squash']" in line


def test_the_setup_document_says_why_only_squash_merge_is_allowed():
    text = " ".join((REPO_ROOT / "docs" / "setup.md").read_text(encoding="utf-8").split())
    assert "squash merge only" in text and "Rebase and merge" in text and "CATCHUP_REQUIRED" in text


# ------------------------------------------------------------------ the subject that GitHub sends (proof D4)
IMMUTABLE = "repo:akaal@1001/db-sales@2002"


def test_the_default_subject_is_the_repository_name_and_a_prefix_replaces_it():
    assert setup_repo.subject(REPO, "dev") == f"repo:{REPO}:environment:dev"
    # seen in the azure/login log of a real run: the immutable form holds the owner id and the repository id
    assert setup_repo.subject(REPO, "dev", prefix=IMMUTABLE) == f"{IMMUTABLE}:environment:dev"
    long = setup_repo.subject(REPO, "prod", "stage.yml", "v0.1.0", IMMUTABLE)
    assert long.startswith(f"{IMMUTABLE}:environment:prod:job_workflow_ref:") and f"repo:{REPO}" not in long


@pytest.mark.parametrize(
    "bad", ["", "akaal/db-sales", 'repo:a/b" --evil "', "repo:a/b:environment:prod", "repo:a b"]
)
def test_a_text_that_is_no_subject_prefix_is_refused(bad):
    with pytest.raises(ValueError):
        setup_repo.subject(REPO, "dev", prefix=bad)


def test_print_azure_with_a_subject_prefix_writes_no_credential_with_the_repository_name(capsys):
    assert setup_repo.main(["--repo", REPO, "--print-azure", "--subject-prefix", IMMUTABLE]) == 0
    printed = capsys.readouterr().out
    subjects = [
        line.split('--subject "')[1].split('"')[0] for line in printed.splitlines() if "--subject " in line
    ]
    assert subjects and all(text.startswith(IMMUTABLE + ":environment:") for text in subjects)
    # and the text without the option says how to find out that the option is needed
    plain = setup_repo.azure_commands(REPO, "0.1.0")
    assert f"gh api repos/{REPO}/actions/oidc/customization/sub" in plain and "--subject-prefix" in plain


def test_subject_prefix_is_an_option_of_print_azure_only_and_is_checked():
    with pytest.raises(SystemExit):
        setup_repo.main(["--repo", REPO, "--check", "--dba-team", "dba", "--subject-prefix", IMMUTABLE])
    with pytest.raises(SystemExit):
        setup_repo.main(["--repo", REPO, "--print-azure", "--subject-prefix", "repo:a/b:environment:prod"])
