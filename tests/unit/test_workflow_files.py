"""The GitHub wiring: action.yml, the reusable workflows and the database-repository template.

Part one reads the YAML and pins the rules that keep an identity away from unreviewed code.
Part two runs the shell scripts of the workflows with fake `uv`, `gh` and real `jq`, because the
decisions they take (argument passing, release creation, the gate, the approver list) are safety
decisions of the pipeline.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from azsqlcd.errors import Exit

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
TEMPLATE = REPO / "templates" / "db-repo"
REUSABLE = ("verify.yml", "release.yml", "stage.yml", "drift.yml", "resolve.yml", "onboard.yml")
YAML_FILES = [
    REPO / "action.yml",
    *sorted(WORKFLOWS.glob("*.yml")),
    *sorted((TEMPLATE / ".github" / "workflows").glob("*.yml")),
]
WORKFLOW_FILES = [p for p in YAML_FILES if p.name != "action.yml"]
OWN = "akaalholdings/azsqlcd"
VERSION = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
STAGES = ("dev", "sandbox", "test", "preprod", "prod")
LEVEL = {"none": 0, "read": 1, "write": 2}


def rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def triggers(doc: dict[str, Any]) -> dict[str, Any]:
    # YAML 1.1 reads the key `on` as the boolean true
    return doc.get("on", doc.get(True)) or {}


def jobs(path: Path) -> dict[str, dict[str, Any]]:
    return load(path).get("jobs", {})


def all_steps(path: Path) -> list[tuple[str, dict[str, Any]]]:
    """(job id, step) for every step of a workflow, or of the composite action."""
    doc = load(path)
    if path.name == "action.yml":
        return [("action", step) for step in doc["runs"]["steps"]]
    return [(job_id, step) for job_id, job in doc["jobs"].items() for step in job.get("steps", [])]


def all_uses(path: Path) -> list[str]:
    found = [step["uses"] for _, step in all_steps(path) if "uses" in step]
    if path.name != "action.yml":
        found += [job["uses"] for job in jobs(path).values() if "uses" in job]
    return found


def permissions(job: dict[str, Any]) -> dict[str, str]:
    return job.get("permissions") or {}


def called_file(uses: str) -> Path:
    """Local file of a reusable workflow of this repository that a job calls."""
    match = re.fullmatch(rf"{OWN}/\.github/workflows/([a-z]+\.yml)@\S+", uses)
    assert match, f"not a reusable workflow of this repository: {uses}"
    return WORKFLOWS / match.group(1)


# ----------------------------------------------------------------------------- structure


def test_expected_files_exist():
    assert {p.name for p in WORKFLOWS.glob("*.yml")} == {"ci.yml", *REUSABLE}
    assert {p.name for p in (TEMPLATE / ".github" / "workflows").glob("*.yml")} == {
        "db.yml",
        "resolve.yml",
        "onboard.yml",
    }


@pytest.mark.parametrize("path", YAML_FILES, ids=rel)
def test_every_yaml_file_parses(path: Path):
    assert isinstance(load(path), dict)


@pytest.mark.parametrize("name", REUSABLE)
def test_reusable_workflow_is_called_and_starts_with_no_permission(name: str):
    doc = load(WORKFLOWS / name)
    assert list(triggers(doc)) == ["workflow_call"]
    assert doc["permissions"] == {}


@pytest.mark.parametrize("path", sorted((TEMPLATE / ".github" / "workflows").glob("*.yml")), ids=rel)
def test_template_workflow_starts_with_no_permission(path: Path):
    assert load(path)["permissions"] == {}


def test_tool_ci_reads_contents_only_and_has_no_database_job():
    doc = load(WORKFLOWS / "ci.yml")
    assert doc["permissions"] == {"contents": "read"}
    job = doc["jobs"]["test"]
    assert job["strategy"]["matrix"] == {
        "os": ["ubuntu-latest", "windows-latest"],
        "python": ["3.12", "3.14"],
    }
    assert "environment" not in job and "permissions" not in job
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert "azure/login" not in text
    for command in ("uv sync --frozen", "ruff check", "ruff format --check", "pyright", "pytest"):
        assert command in text


def test_tool_ci_checks_out_with_lf_on_windows_and_proves_it_before_the_tests():
    """N4-01. Git for Windows and the Windows runners have core.autocrlf=true. The eol attribute of
    .gitattributes wins over it; without it 36 tests of test_modules.py fail on the Windows cells.
    The step gives one clear message when a file is not LF, in place of those test failures."""
    attributes = (REPO / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "* text=auto eol=lf" in attributes and "*.sql text eol=lf" in attributes
    steps = jobs(WORKFLOWS / "ci.yml")["test"]["steps"]
    position = {
        (step.get("uses") or step.get("run") or "").split("@")[0].strip(): n for n, step in enumerate(steps)
    }
    guard = next(step for step in steps if "git ls-files --eol" in step.get("run", ""))
    assert guard["shell"] == "bash"  # the default shell of a Windows runner is PowerShell
    assert "exit 1" in guard["run"] and "crlf" in guard["run"] and "mixed" in guard["run"]
    assert position["actions/checkout"] < steps.index(guard) < position["uv run --no-sync pytest -q"]
    # the checkout step sets nothing that could undo the attribute
    checkout = steps[position["actions/checkout"]]
    assert checkout["with"] == {"persist-credentials": False}


@pytest.mark.skipif(sys.platform == "win32", reason="the step is run with the bash of a POSIX machine")
@pytest.mark.parametrize(
    ("attribute", "line_end", "passes"),
    [
        ("* text=auto eol=lf", b"\n", True),
        ("* text=auto eol=lf", b"\r\n", False),
        ("*.sql text eol=lf", b"\r\n", False),
        ("# no attribute", b"\r\n", False),  # .gitattributes lost its lines: core.autocrlf decides again
        ("*.sql -text", b"\r\n", True),  # a file that is marked as not text keeps its bytes
    ],
    ids=["lf", "crlf", "crlf of a named kind", "crlf with no attribute", "crlf bytes of a binary file"],
)
def test_the_line_end_step_of_tool_ci_passes_for_lf_and_fails_for_crlf(tmp_path, attribute, line_end, passes):
    steps = jobs(WORKFLOWS / "ci.yml")["test"]["steps"]
    script = next(step["run"] for step in steps if "git ls-files --eol" in step.get("run", ""))
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, env=env, check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / ".gitattributes").write_bytes(attribute.encode() + b"\n")
    (tmp_path / "a.sql").write_bytes(b"SELECT 1;\nSELECT 2;\n")
    git("add", ".gitattributes", "a.sql")
    (tmp_path / "a.sql").write_bytes(b"SELECT 1;" + line_end + b"SELECT 2;" + line_end)
    ran = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=False,
    )
    assert (ran.returncode == 0) is passes, ran.stderr.decode("utf-8", "replace")
    if not passes:
        assert b"a.sql" in ran.stdout + ran.stderr and b".gitattributes" in ran.stderr


def test_tool_ci_runs_pyright_over_the_package_and_the_scripts():
    # scripts/ uses names of the package: a rename must fail in CI, not in the owner's live run
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert {"src", "scripts"} <= set(project["tool"]["pyright"]["include"])
    runs = [step.get("run", "") for step in jobs(WORKFLOWS / "ci.yml")["test"]["steps"]]
    # no path argument: a path on the command line replaces the include list of pyproject.toml
    assert "uv run --no-sync pyright" in runs
    assert sorted(p.name for p in (REPO / "scripts").glob("*.py"))


# ----------------------------------------------------------------------------- identity rules


def test_pull_request_job_has_no_token_no_environment_and_no_driver():
    for job_id, job in jobs(WORKFLOWS / "verify.yml").items():
        assert permissions(job) == {"contents": "read"}, job_id
        assert "environment" not in job, job_id
    for _, step in all_steps(WORKFLOWS / "verify.yml"):
        assert step.get("with", {}).get("db", "false") in ("false", False)
        assert "azure/login" not in step.get("uses", "")
    assert load(WORKFLOWS / "verify.yml")["permissions"] == {}


def test_pull_request_checkout_keeps_no_credential_and_has_full_history():
    (checkout,) = [
        s for _, s in all_steps(WORKFLOWS / "verify.yml") if s.get("uses", "").startswith("actions/checkout@")
    ]
    assert checkout["with"] == {"fetch-depth": 0, "persist-credentials": False}


@pytest.mark.parametrize("path", YAML_FILES, ids=rel)
def test_no_checkout_keeps_the_token_on_disk(path: Path):
    for job_id, step in all_steps(path):
        if step.get("uses", "").startswith("actions/checkout@"):
            assert step.get("with", {}).get("persist-credentials") is False, job_id


@pytest.mark.parametrize("path", YAML_FILES, ids=rel)
def test_no_file_uses_pull_request_target(path: Path):
    assert "pull_request_target" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=rel)
def test_a_job_that_can_get_a_token_names_an_environment(path: Path):
    for job_id, job in jobs(path).items():
        if permissions(job).get("id-token") != "write":
            continue
        if "uses" in job:
            # a caller only sets the upper limit; the rule is checked on the jobs of the called file
            assert called_file(job["uses"]).exists(), job_id
        else:
            assert job.get("environment"), (
                f"{rel(path)}: job {job_id} can mint a token without an environment"
            )


@pytest.mark.parametrize("name", REUSABLE)
def test_token_jobs_use_only_the_plan_or_the_deploy_environment(name: str):
    for job_id, job in jobs(WORKFLOWS / name).items():
        if "environment" in job:
            assert job["environment"] in ("${{ inputs.environment }}", "${{ inputs.environment }}-plan"), (
                job_id
            )
            assert permissions(job).get("id-token") == "write", job_id


def test_jobs_without_a_database_identity_run_on_the_light_runner():
    for name in ("stage.yml", "drift.yml", "resolve.yml", "onboard.yml"):
        for job_id, job in jobs(WORKFLOWS / name).items():
            runner = "runs-on" if "environment" in job else "runs-on-light"
            assert job["runs-on"] == f"${{{{ fromJSON(inputs.{runner}) }}}}", f"{name}: {job_id}"


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=rel)
def test_caller_grants_every_permission_that_the_called_jobs_declare(path: Path):
    # GitHub refuses the whole run when a called job asks for more than its caller grants.
    for job_id, job in jobs(path).items():
        if "uses" not in job:
            continue
        granted = permissions(job)
        for called_id, called in jobs(called_file(job["uses"])).items():
            for scope, level in permissions(called).items():
                have = granted.get(scope, "none")
                assert LEVEL[have] >= LEVEL[level], (
                    f"{rel(path)}: {job_id} grants {scope}={have}, {called_id}: {level}"
                )


def test_stage_jobs_hold_least_privilege():
    stage = jobs(WORKFLOWS / "stage.yml")
    assert {job_id: permissions(job) for job_id, job in stage.items()} == {
        "targets": {"contents": "read"},
        "plan": {"contents": "read", "id-token": "write"},
        "gate": {},
        "deploy": {"contents": "read", "id-token": "write", "actions": "read"},
        "record": {"contents": "write"},
        "incident": {"issues": "write"},
    }


def test_no_concurrency_group_can_cancel_or_hold_a_deploy():
    # an Actions concurrency group can drop a pending run; it is not a safety control (A30)
    for path in WORKFLOW_FILES:
        assert "concurrency" not in load(path), rel(path)
        assert all("concurrency" not in job for job in jobs(path).values()), rel(path)


# ----------------------------------------------------------------------------- pinning


@pytest.mark.parametrize("path", YAML_FILES, ids=rel)
def test_third_party_actions_are_pinned_by_commit_and_own_references_by_the_tool_version(path: Path):
    for uses in all_uses(path):
        name, _, ref = uses.partition("@")
        if name == OWN or name.startswith(OWN + "/"):
            assert ref == f"v{VERSION}", f"{rel(path)}: {uses}"
        else:
            assert re.fullmatch(r"[0-9a-f]{40}", ref), f"{rel(path)}: {uses} is not pinned by a commit SHA"


@pytest.mark.parametrize("path", YAML_FILES, ids=rel)
def test_a_pinned_commit_names_its_version_in_a_comment(path: Path):
    for line in path.read_text(encoding="utf-8").splitlines():
        if re.search(r"uses:\s*\S+@[0-9a-f]{40}", line):
            assert re.search(r"@[0-9a-f]{40} # v\d+\.\d+\.\d+$", line), f"{rel(path)}: {line.strip()}"


def test_one_commit_per_third_party_action_across_all_files():
    pins: dict[str, set[str]] = {}
    for path in YAML_FILES:
        for uses in all_uses(path):
            name, _, ref = uses.partition("@")
            if not name.startswith(OWN):
                pins.setdefault(name, set()).add(ref)
    assert set(pins) == {
        "actions/checkout",
        "actions/upload-artifact",
        "actions/download-artifact",
        "azure/login",
        "astral-sh/setup-uv",
    }
    assert all(len(refs) == 1 for refs in pins.values()), pins


def test_every_written_tool_reference_carries_the_pyproject_version():
    files = [*YAML_FILES, *(p for p in TEMPLATE.rglob("*") if p.is_file()), *(REPO / "docs").glob("*.md")]
    files = [p for p in files if p.name != "design.md"]
    found = 0
    for path in files:
        for ref in re.findall(
            rf"{OWN}[\w./-]*@(?:refs/tags/)?(v[0-9][\w.]*)", path.read_text(encoding="utf-8")
        ):
            found += 1
            assert ref == f"v{VERSION}", f"{rel(path)}: {ref}"
    assert found > 20


# ----------------------------------------------------------------------------- shell injection


@pytest.mark.parametrize("path", YAML_FILES, ids=rel)
def test_no_script_interpolates_an_expression(path: Path):
    # inputs, matrix values and event data reach a shell only as environment variables
    for job_id, step in all_steps(path):
        assert "${{" not in step.get("run", ""), f"{rel(path)}: {job_id}: {step.get('name', step.get('id'))}"


@pytest.mark.parametrize("path", YAML_FILES, ids=rel)
def test_every_script_names_its_shell_in_a_reusable_file(path: Path):
    if path.name == "ci.yml":
        return
    for job_id, step in all_steps(path):
        if "run" in step:
            assert step.get("shell") == "bash", f"{rel(path)}: {job_id}"


# ----------------------------------------------------------------------------- release.yml (A23)


def test_only_the_push_job_can_create_a_release():
    release = jobs(WORKFLOWS / "release.yml")
    creators = [job_id for job_id, job in release.items() if "gh release create" in json.dumps(job)]
    assert creators == ["create"]
    assert release["create"]["if"] == "${{ github.event_name == 'push' && inputs.release == '' }}"
    assert permissions(release["create"]) == {"contents": "write"}


def test_the_dispatch_path_never_creates_a_release_and_cannot_write():
    existing = jobs(WORKFLOWS / "release.yml")["existing"]
    assert existing["if"] == "${{ inputs.release != '' }}"
    assert "gh release create" not in json.dumps(existing)
    assert "gh release upload" not in json.dumps(existing)
    assert permissions(existing) == {"contents": "read"}


def test_the_dispatch_path_checks_the_name_before_it_checks_out_the_tag():
    steps = jobs(WORKFLOWS / "release.yml")["existing"]["steps"]
    assert steps[0]["id"] == "name" and "^r[0-9]+$" in steps[0]["run"]
    assert steps[1]["uses"].startswith("actions/checkout@")
    assert steps[1]["with"]["ref"] == "refs/tags/${{ inputs.release }}"


def test_no_workflow_but_the_release_and_record_jobs_can_write_to_a_release():
    writers = set()
    for path in WORKFLOW_FILES:
        for job_id, job in jobs(path).items():
            if "steps" in job and re.search(r"gh release (create|upload|edit|delete)", json.dumps(job)):
                writers.add((path.name, job_id))
    assert writers == {("release.yml", "create"), ("stage.yml", "record")}


# ----------------------------------------------------------------------------- command lines

COMMANDS = {
    # command -> flags that take a value, flags that take none (docs/design.md, "Command line")
    "verify": ({"--base", "--ci"}, set()),
    "build": ({"--commit", "--out", "--ci"}, set()),
    "targets": ({"--bundle", "--digest", "--env", "--ci"}, set()),
    "plan": ({"--bundle", "--digest", "--env", "--target", "--out", "--ci"}, set()),
    "deploy": (
        {
            "--bundle",
            "--digest",
            "--env",
            "--target",
            "--out",
            "--ci",
            "--expect-plan-file",
            "--approved-by",
            "--approved-utc",
            "--triggering-actor",
            "--ci-run-url",
        },
        {"--inline-plan"},
    ),
    "drift": ({"--bundle", "--digest", "--env", "--target", "--ci"}, set()),
    "export": ({"--root", "--env", "--target", "--out", "--ci"}, set()),
    "baseline": (
        {"--bundle", "--digest", "--env", "--target", "--confirm-database", "--ci"},
        {"--report-only"},
    ),
    "resolve": (
        {
            "--bundle",
            "--digest",
            "--env",
            "--target",
            "--confirm-database",
            "--reason",
            "--ci",
            "--${{ inputs.action }}",
        },
        {"--force-no-readback"},
    ),
}


def tool_calls() -> list[tuple[str, list[str]]]:
    calls = []
    for path in WORKFLOW_FILES:
        for job_id, step in all_steps(path):
            if step.get("uses", "").startswith(OWN + "@"):
                calls.append((f"{rel(path)}: {job_id}", step["with"]["args"].splitlines()))
    return calls


def test_workflows_call_only_commands_and_flags_of_the_command_line_contract():
    calls = tool_calls()
    assert {lines[0] for _, lines in calls} == set(COMMANDS)
    for where, lines in calls:
        allowed = set().union(*COMMANDS[lines[0]])
        for line in lines[1:]:
            # a flag is a line that starts with --, or a quoted flag inside an expression
            flags = [line] if line.startswith("--") else re.findall(r"'(--[a-z-]+)'", line)
            assert set(flags) <= allowed, f"{where}: {line}"


def test_every_tool_call_writes_ci_outputs_and_database_commands_install_the_driver():
    offline = {"verify", "build", "targets"}
    for path in WORKFLOW_FILES:
        for job_id, step in all_steps(path):
            if not step.get("uses", "").startswith(OWN + "@"):
                continue
            lines = step["with"]["args"].splitlines()
            assert lines[-2:] == ["--ci", "github"], f"{rel(path)}: {job_id}"
            assert (step["with"].get("db") is True) == (lines[0] not in offline), f"{rel(path)}: {job_id}"


def test_gated_deploy_uses_the_approved_plan_and_ungated_deploy_plans_under_the_lock():
    (deploy,) = [lines for _, lines in tool_calls() if lines[0] == "deploy"]
    assert "${{ inputs.gated && '--expect-plan-file' || '--inline-plan' }}" in deploy
    plan_file = "format('plan/plan-{0}-{1}/plan.json', inputs.environment, matrix.target.id)"
    assert f"${{{{ inputs.gated && {plan_file} || '' }}}}" in deploy
    for flag in ("--approved-by", "--approved-utc", "--triggering-actor", "--ci-run-url"):
        assert any(flag in line for line in deploy), flag


def result_uploads(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The upload steps of a job that carry the result of the tool; the triage log is another artefact."""
    return [
        step
        for step in steps
        if step.get("uses", "").startswith("actions/upload-artifact@")
        and not str(step["with"]["name"]).startswith("azsqlcd-logs-")
    ]


def test_an_artefact_holds_one_directory_of_its_name_and_a_download_by_pattern_merges():
    # download-artifact puts one matching artefact into `path` itself and several into
    # `path`/<name>/. The gate, record and incident scripts read `path`/<name>/, and most
    # environments have one target. So the directory is a part of the artefact and each download
    # by pattern merges: one layout for one target and for several.
    stage = jobs(WORKFLOWS / "stage.yml")
    for job_id, kind in (("plan", "plan"), ("deploy", "report")):
        steps = stage[job_id]["steps"]
        name = f"{kind}-${{{{ inputs.environment }}}}-${{{{ matrix.target.id }}}}"
        (upload,) = result_uploads(steps)
        assert upload["with"]["name"] == name and upload["with"]["path"] == kind
        (tool,) = [s for s in steps if s.get("id") == job_id]
        lines = tool["with"]["args"].splitlines()
        assert lines[lines.index("--out") + 1] == f"{kind}/{name}"
        (outcome,) = [s for s in steps if s.get("id") == "outcome"]
        assert outcome["env"]["DIR"] == f"{kind}/{name}"
    downloads = [
        step["with"]
        for path in WORKFLOW_FILES
        for _, step in all_steps(path)
        if step.get("uses", "").startswith("actions/download-artifact@")
    ]
    by_pattern = [values for values in downloads if "pattern" in values]
    assert len(by_pattern) == 5
    assert all(values.get("merge-multiple") is True and "name" not in values for values in by_pattern)
    # a download by name is one artefact: its content goes into `path`, so the plan of the deploy
    # job is plan/<artefact name>/plan.json
    (approved,) = [values for values in downloads if values.get("path") == "plan"]
    assert approved["name"] == "plan-${{ inputs.environment }}-${{ matrix.target.id }}"
    assert all("name" in values for values in downloads if "pattern" not in values)


def test_action_outputs_are_the_keys_that_the_tool_writes():
    action = load(REPO / "action.yml")
    assert set(action["inputs"]) == {"args", "db"}
    assert set(action["outputs"]) == {
        "matrix",
        "timeout",
        "release",
        "digest",
        "plan_sha256",
        "pending",
        "exit_code",
        "reason_code",
    }
    for key, output in action["outputs"].items():
        assert output["value"] == f"${{{{ steps.run.outputs.{key} }}}}"


def test_action_knows_exactly_the_exit_codes_of_the_tool():
    script = step_script(REPO / "action.yml", "action", "run")
    (codes,) = re.findall(r"^\s*([0-9|]+)\) ;;$", script, flags=re.MULTILINE)
    assert {int(code) for code in codes.split("|")} == {int(code) for code in Exit}


# ----------------------------------------------------------------------------- template


def template_text(name: str) -> str:
    return (TEMPLATE / name).read_text(encoding="utf-8")


def test_template_config_is_accepted_by_the_tool_and_holds_every_key():
    from azsqlcd.config import ENVIRONMENTS, load_config, targets_matrix

    raw = tomllib.loads(template_text("azsqlcd.toml"))
    assert set(raw) == {"project", "identities", "env", "unmanaged", "ack"}
    assert set(raw["project"]) == {"name", "tenant_id", "table_model", "module_chunk", "min_token_minutes"}
    assert set(raw["identities"]) == {
        "nonprod_plan",
        "prod_plan",
        "nonprod_deploy",
        "test_deploy",
        "preprod_deploy",
        "prod_deploy",
    }
    assert tuple(raw["env"]) == STAGES
    for name, env in raw["env"].items():
        assert set(env) == {
            "plan_identity",
            "deploy_identity",
            "drift",
            "lock_timeout_ms",
            "applock_wait_s",
            "job_timeout_minutes",
            "gated",
            "targets",
        }, name
    assert set(raw["unmanaged"]) == {"objects"} and set(raw["ack"]) == {"unmanaged_dependants"}
    config = load_config(template_text("azsqlcd.toml"))
    assert set(STAGES) < set(ENVIRONMENTS)
    assert all(len(targets_matrix(config, stage)) == 1 for stage in STAGES)


def test_template_gives_prod_its_own_identities():
    env = tomllib.loads(template_text("azsqlcd.toml"))["env"]
    assert env["prod"]["deploy_identity"] not in {env[s]["deploy_identity"] for s in STAGES if s != "prod"}
    assert env["prod"]["plan_identity"] not in {env[s]["plan_identity"] for s in ("dev", "sandbox", "test")}


def test_template_stages_run_in_promotion_order_and_agree_with_the_config_on_gated():
    db = jobs(TEMPLATE / ".github" / "workflows" / "db.yml")
    env = tomllib.loads(template_text("azsqlcd.toml"))["env"]
    previous = None
    for stage in STAGES:
        job = db[stage]
        assert called_file(job["uses"]).name == "stage.yml"
        assert job["needs"] == ("release" if previous is None else ["release", previous])
        assert "if" not in job  # a failed or skipped earlier stage stops the promotion
        assert job["with"]["environment"] == stage
        assert job["with"]["gated"] is env[stage]["gated"]
        assert job["with"]["release"] == "${{ needs.release.outputs.release }}"
        assert job["with"]["digest"] == "${{ needs.release.outputs.digest }}"
        previous = stage


def test_template_from_stage_makes_each_earlier_stage_check_only():
    db = jobs(TEMPLATE / ".github" / "workflows" / "db.yml")
    for index, stage in enumerate(STAGES):
        later = list(STAGES[index + 1 :])
        expression = db[stage]["with"].get("check-only")
        if not later:
            assert expression is None
        elif len(later) == 1:
            assert expression == f"${{{{ inputs.from_stage == '{later[0]}' }}}}"
        else:
            names = json.dumps(later, separators=(",", ":"))
            assert expression == f"${{{{ contains(fromJSON('{names}'), inputs.from_stage) }}}}"


def test_template_keeps_prod_on_its_own_runner_group():
    db = jobs(TEMPLATE / ".github" / "workflows" / "db.yml")
    runners = {stage: json.loads(db[stage]["with"]["runs-on"]) for stage in STAGES}
    assert runners["prod"] == {"group": "azsql-prod"}
    assert all(runners[stage] == {"group": "azsql-nonprod"} for stage in STAGES if stage != "prod")
    drift = {
        row["environment"]: json.loads(row["runs-on"]) for row in db["drift"]["strategy"]["matrix"]["include"]
    }
    assert drift == runners


def test_template_triggers():
    doc = load(TEMPLATE / ".github" / "workflows" / "db.yml")
    on = triggers(doc)
    assert set(on) == {"pull_request", "push", "workflow_dispatch", "schedule"}
    assert on["pull_request"] is None  # no paths filter: the required check always reports
    assert on["push"]["branches"] == ["main"]
    # every path that a release bundle holds; a change to one of them must give a new release
    assert on["push"]["paths"] == ["schema/**", "migrations/**", "onboarding/**", "azsqlcd.toml"]
    assert set(on["workflow_dispatch"]["inputs"]) == {"release", "from_stage"}
    assert on["workflow_dispatch"]["inputs"]["from_stage"]["options"] == list(STAGES)
    job = doc["jobs"]
    assert job["verify"]["if"] == "${{ github.event_name == 'pull_request' }}"
    assert permissions(job["verify"]) == {"contents": "read"}
    assert "github.ref == 'refs/heads/main'" in job["release"]["if"]
    assert job["drift"]["if"] == "${{ github.event_name == 'schedule' }}"


def test_template_dispatch_forms_offer_only_the_closed_action_lists():
    resolve = triggers(load(TEMPLATE / ".github" / "workflows" / "resolve.yml"))["workflow_dispatch"][
        "inputs"
    ]
    assert resolve["action"]["options"] == [
        "mark-applied",
        "mark-not-applied",
        "accept-drift",
        "adopt-module",
        "clear-run",
        "rebind-environment",
    ]
    onboard = triggers(load(TEMPLATE / ".github" / "workflows" / "onboard.yml"))["workflow_dispatch"][
        "inputs"
    ]
    assert onboard["action"]["options"] == ["export", "baseline-report", "baseline"]
    for name, options in (
        ("resolve.yml", resolve["action"]["options"]),
        ("onboard.yml", onboard["action"]["options"]),
    ):
        script = step_script(WORKFLOWS / name, "targets", "inputs")
        for option in options:
            assert re.search(rf"(^|[ |]){re.escape(option)}[|)]", script, flags=re.MULTILINE), option


def test_template_codeowners_cover_every_path_that_reaches_a_database():
    owned = [
        line.split()[0]
        for line in template_text(".github/CODEOWNERS").splitlines()
        if line and line[0] != "#"
    ]
    assert owned == ["/schema/**", "/migrations/**", "/onboarding/**", "/azsqlcd.toml", "/.github/**"]


def test_template_small_files():
    assert template_text("migrations/migrations.sum").splitlines()[0] == "azsqlcd-sum 1"
    assert "*.sql text eol=lf" in template_text(".gitattributes").splitlines()
    assert tomllib.loads(template_text("schema/_tombstones.toml")) == {}
    assert len(template_text("README.md").splitlines()) <= 40


# ----------------------------------------------------------------------------- scripts

needs_bash = pytest.mark.skipif(
    sys.platform == "win32", reason="the scripts run on Linux runners; fakes need a POSIX shell"
)
needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")

FAKE_UV = """#!/bin/bash
{
  echo "--call--"
  printf '%s\\n' "$@"
  echo "PYTHONPATH=${PYTHONPATH:-}"
  echo "UV_OFFLINE=${UV_OFFLINE:-}"
} >> "$LOG"
case "$1" in
  sync) exit "${FAKE_SYNC_EXIT:-0}" ;;
  run) exit "${FAKE_RUN_EXIT:-0}" ;;
esac
"""

# gh: logs each call; `api` and `issue list` answer like the real `--jq` (raw strings)
FAKE_GH = """#!/bin/bash
printf '%s\\n' "gh $*" >> "$LOG"
filter=
previous=
for argument in "$@"; do
  if [ "$previous" = "--jq" ]; then filter="$argument"; fi
  previous="$argument"
done
case "$1 $2" in
  "api "*) jq -r "$filter" "$FAKE_API" ;;
  "issue list") jq -r "$filter" "$FAKE_ISSUES" ;;
  "release view") exit "${FAKE_RELEASE_EXISTS:-1}" ;;
  "release download") mkdir -p stored && cp "$FAKE_STORED" stored/manifest.json ;;
esac
"""


def step_script(path: Path, job_id: str, step_id: str) -> str:
    (script,) = [step["run"] for job, step in all_steps(path) if job == job_id and step.get("id") == step_id]
    return script


def run_step(
    script: str, tmp_path: Path, env: dict[str, str], fakes: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a step the way the runner does: bash -e -o pipefail on a script file."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (fakes or {}).items():
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    (tmp_path / "step.sh").write_text(script, encoding="utf-8")
    full_env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "LOG": str(tmp_path / "log.txt"),
        "GITHUB_OUTPUT": str(tmp_path / "output.txt"),
        "GITHUB_REPOSITORY": "akaalholdings/db-sales",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": "900",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_SHA": "a" * 40,
        "RUNNER_TEMP": str(tmp_path),
        **env,
    }
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", str(tmp_path / "step.sh")],
        cwd=tmp_path,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )


def log(tmp_path: Path) -> str:
    path = tmp_path / "log.txt"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def outputs(tmp_path: Path) -> dict[str, str]:
    path = tmp_path / "output.txt"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return dict(line.split("=", 1) for line in lines)


def run_action(tmp_path: Path, args: str, **env: str) -> subprocess.CompletedProcess[str]:
    script = step_script(REPO / "action.yml", "action", "run")
    base = {"AZSQLCD_ARGS": args, "AZSQLCD_DB": "false", "GITHUB_ACTION_PATH": "/opt/tool"}
    return run_step(script, tmp_path, {**base, **env}, {"uv": FAKE_UV})


def uv_calls(tmp_path: Path) -> list[list[str]]:
    return [call.splitlines() for call in log(tmp_path).split("--call--\n")[1:]]


@needs_bash
def test_action_passes_each_line_as_one_argument_and_expands_nothing(tmp_path: Path):
    hostile = [
        "[sales].[Order Lines]",
        "it's $(touch pwned) `touch pwned` $HOME * ; touch pwned",
        '"quoted" \\n',
        "-- x",
    ]
    block = "resolve\n--reason\n" + "\n\n".join(hostile) + "\r\n\n"
    done = run_action(tmp_path, block)
    assert done.returncode == 0, done.stderr
    sync, run = uv_calls(tmp_path)
    assert sync[:7] == [
        "sync",
        "--project",
        "/opt/tool",
        "--frozen",
        "--no-dev",
        "--no-install-project",
        "PYTHONPATH=",
    ]
    assert run[:9] == [
        "run",
        "--project",
        "/opt/tool",
        "--no-sync",
        "python",
        "-P",
        "-m",
        "azsqlcd",
        "resolve",
    ]
    assert run[9:-2] == ["--reason", *hostile]
    assert run[-2] == "PYTHONPATH=/opt/tool/src"
    assert not (tmp_path / "pwned").exists()


# uv that runs the interpreter of this test run with the arguments that the action gives to `python`
UV_THAT_RUNS_PYTHON = """#!/bin/bash
case "$1" in
  sync) exit 0 ;;
  run)
    while [ "$1" != "python" ]; do shift; done
    shift
    exec "$REAL_PYTHON" "$@" ;;
esac
"""


@needs_bash
def test_action_runs_the_tool_and_never_a_module_of_the_checkout_that_it_reads(tmp_path: Path):
    # verify and build run in the checkout of the database repository, and for verify that is the
    # code of a pull request. `python -m` puts the working directory first on the module path: a
    # directory azsqlcd/ in the repository would be the tool, and could answer exit 0 for anything.
    (tmp_path / "azsqlcd").mkdir()
    (tmp_path / "azsqlcd" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "azsqlcd" / "__main__.py").write_text(
        "print('code of the repository ran')\nraise SystemExit(0)\n", encoding="utf-8"
    )
    hostile = "raise SystemExit('code of the repository ran')\n"
    (tmp_path / "argparse.py").write_text(hostile, encoding="utf-8")
    script = step_script(REPO / "action.yml", "action", "run")
    env = {
        "AZSQLCD_ARGS": "--version\n",
        "AZSQLCD_DB": "false",
        "GITHUB_ACTION_PATH": str(REPO),
        "REAL_PYTHON": sys.executable,
    }

    done = run_step(script, tmp_path, env, {"uv": UV_THAT_RUNS_PYTHON})

    assert done.stdout.strip() == f"azsqlcd {VERSION}", done.stdout + done.stderr
    assert done.returncode == 0 and "code of the repository ran" not in done.stdout + done.stderr


@needs_bash
def test_action_shows_the_output_of_the_tool_in_the_order_in_which_it_was_written(tmp_path: Path):
    # The findings go to stdout and the last line (reason code, exit code) to stderr. The runner
    # reads two pipes in no fixed order, and Python holds back stdout of a pipe: on GitHub the
    # last line came before the finding that it sums up. One stream, not buffered.
    fake = """#!/bin/bash
if [ "$1" = "run" ]; then
  echo "finding"
  echo "last line PYTHONUNBUFFERED=${PYTHONUNBUFFERED:-}" >&2
  echo "count"
  exit 22
fi
"""
    script = step_script(REPO / "action.yml", "action", "run")
    env = {"AZSQLCD_ARGS": "verify", "AZSQLCD_DB": "false", "GITHUB_ACTION_PATH": "/opt/tool"}
    env["PYTHONUNBUFFERED"] = load(REPO / "action.yml")["runs"]["steps"][-1]["env"]["PYTHONUNBUFFERED"]
    done = run_step(script, tmp_path, env, {"uv": fake})
    assert done.returncode == 22
    assert done.stdout.splitlines() == ["finding", "last line PYTHONUNBUFFERED=1", "count"]
    assert done.stderr == ""


@needs_bash
def test_action_installs_the_driver_only_for_database_commands(tmp_path: Path):
    run_action(tmp_path, "plan\n", AZSQLCD_DB="true")
    assert uv_calls(tmp_path)[0][6:8] == ["--extra", "db"]


@needs_bash
def test_action_on_a_private_runner_uses_no_network(tmp_path: Path):
    run_action(tmp_path, "plan\n", AZSQLCD_OFFLINE="1")
    assert all(call[-1] == "UV_OFFLINE=1" for call in uv_calls(tmp_path))
    mode = step_script(REPO / "action.yml", "action", "mode")
    assert outputs_of(run_step(mode, tmp_path, {"AZSQLCD_OFFLINE": "1"}), tmp_path) == {"offline": "1"}


def outputs_of(done: subprocess.CompletedProcess[str], tmp_path: Path) -> dict[str, str]:
    assert done.returncode == 0, done.stderr
    return outputs(tmp_path)


@needs_bash
@pytest.mark.parametrize("code", sorted(int(code) for code in Exit))
def test_action_returns_a_tool_exit_code_unchanged(tmp_path: Path, code: int):
    done = run_action(tmp_path, "deploy\n", FAKE_RUN_EXIT=str(code))
    assert done.returncode == code
    assert "tool did not start" not in done.stdout


@needs_bash
@pytest.mark.parametrize("code", [1, 2, 127, 137])
def test_action_says_the_tool_did_not_start_for_any_other_exit_code(tmp_path: Path, code: int):
    done = run_action(tmp_path, "deploy\n", FAKE_RUN_EXIT=str(code))
    assert done.returncode == code
    assert "tool did not start" in done.stdout


@needs_bash
def test_action_does_not_run_the_tool_when_the_environment_cannot_be_built(tmp_path: Path):
    done = run_action(tmp_path, "deploy\n", FAKE_SYNC_EXIT="2")
    assert done.returncode == 2
    assert "tool did not start" in done.stdout
    assert [call[0] for call in uv_calls(tmp_path)] == ["sync"]


@needs_bash
def test_action_refuses_an_empty_argument_list(tmp_path: Path):
    done = run_action(tmp_path, "\n\n")
    assert done.returncode == 2 and "tool did not start" in done.stdout
    assert uv_calls(tmp_path) == []


MANIFEST = {
    "commit": "a" * 40,
    "release_seq": 57,
    "files": [["azsqlcd.toml", "0" * 64]],
    "chain_added_in": {},
}


def canonical(manifest: Any) -> bytes:
    # manifest.json is canonical JSON, so one release has one byte sequence (release.manifest_json)
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def release_step(tmp_path: Path, job_id: str, step_id: str, stored: dict[str, Any] | None, **env: str):
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "manifest.json").write_bytes(canonical(MANIFEST))
    (tmp_path / "dist" / "bundle.tar").write_bytes(b"")
    (tmp_path / "stored.json").write_bytes(canonical(stored))
    base = {
        "RELEASE": "r57",
        "DIGEST": "d" * 64,
        "FAKE_STORED": str(tmp_path / "stored.json"),
        "FAKE_RELEASE_EXISTS": "1" if stored is None else "0",
    }
    script = step_script(WORKFLOWS / "release.yml", job_id, step_id)
    return run_step(script, tmp_path, {**base, **env}, {"gh": FAKE_GH})


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def repository_with_commit(tmp_path: Path) -> str:
    git(tmp_path, "init", "-q")
    git(
        tmp_path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "c",
    )
    return git(tmp_path, "rev-parse", "HEAD")


@needs_bash
def test_push_creates_the_release_named_by_the_tool_at_the_pushed_commit(tmp_path: Path):
    sha = repository_with_commit(tmp_path)
    done = release_step(tmp_path, "create", "publish", None, GITHUB_SHA=sha)
    assert done.returncode == 0, done.stderr
    (create,) = [line for line in log(tmp_path).splitlines() if line.startswith("gh release create")]
    assert create == (
        "gh release create r57 dist/bundle.tar dist/manifest.json -R akaalholdings/db-sales "
        f"--target {sha} --title r57 --notes digest {'d' * 64}"
    )


@needs_bash
@pytest.mark.parametrize(
    "release, digest", [("", "d" * 64), ("r57; id", "d" * 64), ("main", "d" * 64), ("r57", "")]
)
def test_push_creates_nothing_without_a_well_formed_name_and_digest(
    tmp_path: Path, release: str, digest: str
):
    sha = repository_with_commit(tmp_path)
    done = release_step(tmp_path, "create", "publish", None, GITHUB_SHA=sha, RELEASE=release, DIGEST=digest)
    assert done.returncode != 0
    assert log(tmp_path) == ""


@needs_bash
def test_second_run_for_a_commit_creates_nothing_when_the_stored_release_matches(tmp_path: Path):
    sha = repository_with_commit(tmp_path)
    done = release_step(tmp_path, "create", "publish", MANIFEST, GITHUB_SHA=sha)
    assert done.returncode == 0, done.stderr
    assert "gh release create" not in log(tmp_path)


@needs_bash
def test_second_run_fails_when_the_stored_release_differs(tmp_path: Path):
    sha = repository_with_commit(tmp_path)
    stored = {**MANIFEST, "files": [["azsqlcd.toml", "1" * 64]]}
    done = release_step(tmp_path, "create", "publish", stored, GITHUB_SHA=sha)
    assert done.returncode != 0
    assert "gh release create" not in log(tmp_path)


@needs_bash
def test_push_refuses_a_release_name_whose_tag_sits_on_another_commit(tmp_path: Path):
    sha = repository_with_commit(tmp_path)
    git(tmp_path, "tag", "r57")
    done = release_step(tmp_path, "create", "publish", None, GITHUB_SHA="b" * 40)
    assert done.returncode != 0 and "another commit" in done.stdout
    assert "gh release create" not in log(tmp_path)
    assert sha != "b" * 40


@needs_bash
@pytest.mark.parametrize("name", ["main", "r57x", "refs/heads/r57", "r57\nr58", "", "v0.1.0", "R57", "r"])
def test_dispatch_refuses_a_release_input_that_is_not_a_release_name(tmp_path: Path, name: str):
    done = run_step(step_script(WORKFLOWS / "release.yml", "existing", "name"), tmp_path, {"REQUESTED": name})
    assert done.returncode != 0


@needs_bash
def test_dispatch_accepts_a_release_name(tmp_path: Path):
    done = run_step(
        step_script(WORKFLOWS / "release.yml", "existing", "name"), tmp_path, {"REQUESTED": "r57"}
    )
    assert done.returncode == 0, done.stderr


@needs_bash
@pytest.mark.parametrize(
    "requested, stored, ok",
    [
        ("r57", MANIFEST, True),
        ("r58", MANIFEST, False),  # the tag builds another release than the one that was asked for
        ("r57", {**MANIFEST, "commit": "c" * 40}, False),
        ("r57", {**MANIFEST, "chain_added_in": {"0001__x.sql": 57}}, False),
        ("r57", {**MANIFEST, "files": []}, False),
    ],
)
def test_dispatch_compares_the_build_of_the_tag_with_the_stored_release(
    tmp_path: Path, requested: str, stored, ok: bool
):
    done = release_step(tmp_path, "existing", "compare", stored, REQUESTED=requested)
    assert (done.returncode == 0) is ok, done.stdout + done.stderr
    assert "gh release create" not in log(tmp_path)


def plan_results(tmp_path: Path, **pending: str) -> None:
    for target, value in pending.items():
        directory = tmp_path / "plans" / f"plan-prod-{target}"
        directory.mkdir(parents=True)
        (directory / "outcome.txt").write_text(
            f"job=plan\ntarget={target}\noutcome=success\npending={value}\n"
        )


def run_gate(tmp_path: Path, check_only: str = "false") -> subprocess.CompletedProcess[str]:
    script = step_script(WORKFLOWS / "stage.yml", "gate", "gate")
    return run_step(script, tmp_path, {"ENVIRONMENT": "prod", "CHECK_ONLY": check_only})


@needs_bash
@pytest.mark.parametrize(
    "pending, expected",
    [({"a": "false"}, "false"), ({"a": "true"}, "true"), ({"a": "false", "b": "true"}, "true")],
)
def test_gate_asks_for_a_deploy_only_when_a_target_has_pending_work(tmp_path: Path, pending, expected: str):
    plan_results(tmp_path, **pending)
    assert outputs_of(run_gate(tmp_path), tmp_path) == {"pending": expected}


@needs_bash
@pytest.mark.parametrize("pending", [{}, {"a": "none"}, {"a": "false", "b": ""}])
def test_gate_fails_when_a_plan_result_is_missing_or_says_nothing(tmp_path: Path, pending):
    plan_results(tmp_path, **pending)
    assert run_gate(tmp_path).returncode != 0
    assert outputs(tmp_path) == {}


@needs_bash
def test_stage_before_from_stage_fails_when_it_does_not_hold_the_release(tmp_path: Path):
    plan_results(tmp_path, a="true")
    done = run_gate(tmp_path, check_only="true")
    assert done.returncode != 0 and "before from_stage" in done.stdout


@needs_bash
def test_stage_before_from_stage_passes_when_nothing_is_pending(tmp_path: Path):
    plan_results(tmp_path, a="false")
    assert outputs_of(run_gate(tmp_path, check_only="true"), tmp_path) == {"pending": "false"}


def test_deploy_runs_only_after_the_gate_or_with_an_inline_plan_and_never_in_check_only():
    deploy = jobs(WORKFLOWS / "stage.yml")["deploy"]
    condition = " ".join(deploy["if"].split())
    assert condition == (
        "${{ !cancelled() && !inputs.check-only && needs.targets.result == 'success' && "
        "((inputs.gated && needs.gate.result == 'success' && needs.gate.outputs.pending == 'true') || "
        "(!inputs.gated && needs.gate.result == 'skipped')) }}"
    )
    stage = jobs(WORKFLOWS / "stage.yml")
    assert stage["plan"]["if"] == "${{ inputs.gated || inputs.check-only }}"
    assert stage["gate"]["needs"] == "plan" and "if" not in stage["gate"]
    assert deploy["environment"] == "${{ inputs.environment }}"
    assert stage["plan"]["environment"] == "${{ inputs.environment }}-plan"


APPROVALS = [
    {"state": "approved", "user": {"login": "dba-two"}, "environments": [{"name": "prod"}]},
    {
        "state": "approved",
        "user": {"login": "dba-one"},
        "environments": [{"name": "prod"}, {"name": "preprod"}],
    },
    {"state": "approved", "user": {"login": "dba-one"}, "environments": [{"name": "prod"}]},
    {"state": "rejected", "user": {"login": "dba-three"}, "environments": [{"name": "prod"}]},
    {"state": "approved", "user": {"login": "dev-four"}, "environments": [{"name": "preprod"}]},
    {"state": "approved", "user": {"login": "dev-five"}, "environments": [{"name": "prod-plan"}]},
]


def run_audit(
    tmp_path: Path, environment: str, approvals: list[dict[str, Any]]
) -> subprocess.CompletedProcess[str]:
    (tmp_path / "api.json").write_text(json.dumps(approvals), encoding="utf-8")
    script = step_script(WORKFLOWS / "stage.yml", "deploy", "audit")
    env = {"ENVIRONMENT": environment, "FAKE_API": str(tmp_path / "api.json")}
    return run_step(script, tmp_path, env, {"gh": FAKE_GH})


@needs_bash
@needs_jq
@pytest.mark.parametrize(
    "environment, approved_by",
    [("prod", "dba-one,dba-two"), ("preprod", "dba-one,dev-four"), ("dev", "")],
)
def test_audit_names_only_the_people_who_approved_this_environment(
    tmp_path: Path, environment: str, approved_by: str
):
    found = outputs_of(run_audit(tmp_path, environment, APPROVALS), tmp_path)
    assert found["approved_by"] == approved_by
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", found["approved_utc"])
    assert "repos/akaalholdings/db-sales/actions/runs/900/approvals" in log(tmp_path)


@needs_bash
@needs_jq
def test_audit_refuses_a_login_that_could_add_an_argument(tmp_path: Path):
    approvals = [
        {"state": "approved", "user": {"login": "x --inline-plan"}, "environments": [{"name": "prod"}]}
    ]
    assert run_audit(tmp_path, "prod", approvals).returncode != 0
    assert outputs(tmp_path) == {}


MATRIX = [
    {"id": "sales-a", "plan_client_id": "p", "deploy_client_id": "d", "tenant_id": "t", "gated": True},
    {"id": "sales-b", "plan_client_id": "p", "deploy_client_id": "d", "tenant_id": "t", "gated": True},
]


@needs_bash
@needs_jq
@pytest.mark.parametrize("gated, ok", [("true", True), ("false", False)])
def test_stage_stops_when_its_gated_input_differs_from_the_config(tmp_path: Path, gated: str, ok: bool):
    script = step_script(WORKFLOWS / "stage.yml", "targets", "agree")
    done = run_step(script, tmp_path, {"MATRIX": json.dumps(MATRIX), "GATED": gated})
    assert (done.returncode == 0) is ok, done.stdout + done.stderr


@needs_bash
@needs_jq
@pytest.mark.parametrize("name", ["resolve.yml", "onboard.yml"])
@pytest.mark.parametrize("target, ok", [("sales-b", True), ("sales-c", False), ("sales", False), ("", False)])
def test_single_target_workflows_select_exactly_the_named_target(
    tmp_path: Path, name: str, target: str, ok: bool
):
    script = step_script(WORKFLOWS / name, "targets", "select")
    done = run_step(script, tmp_path, {"MATRIX": json.dumps(MATRIX), "TARGET": target})
    assert (done.returncode == 0) is ok
    if ok:
        assert json.loads(outputs(tmp_path)["target"]) == MATRIX[1]


RESOLVE_INPUTS = {
    "ACTION": "mark-applied",
    "RELEASE": "r57",
    "TARGET": "sales-prod",
    "SUBJECT": "0002__ix_order_status.sql",
    "CONFIRM_DATABASE": "sales",
    "REASON": "index build finished; checked sys.index_resumable_operations",
}


@needs_bash
@pytest.mark.parametrize(
    "change, ok",
    [
        ({}, True),
        ({"ACTION": "rebind-environment", "SUBJECT": "sandbox"}, True),
        ({"ACTION": "align"}, False),
        ({"ACTION": "mark-applied --force-no-readback"}, False),
        ({"RELEASE": "main"}, False),
        ({"REASON": ""}, False),
        ({"REASON": "ok\n--force-no-readback"}, False),
        ({"SUBJECT": "x\r\n--clear-run"}, False),
    ],
)
def test_resolve_refuses_an_unknown_action_and_an_input_that_could_add_an_argument(
    tmp_path: Path, change, ok: bool
):
    script = step_script(WORKFLOWS / "resolve.yml", "targets", "inputs")
    done = run_step(script, tmp_path, {**RESOLVE_INPUTS, **change})
    assert (done.returncode == 0) is ok, done.stdout + done.stderr


@needs_bash
@pytest.mark.parametrize(
    "action, confirm, ok",
    [
        ("export", "", True),
        ("baseline-report", "sales", True),
        ("baseline", "sales", True),
        ("baseline", "", False),
        ("init", "sales", False),
    ],
)
def test_onboard_accepts_only_its_three_actions_and_baseline_needs_the_database_name(
    tmp_path: Path, action: str, confirm: str, ok: bool
):
    script = step_script(WORKFLOWS / "onboard.yml", "targets", "inputs")
    env = {"ACTION": action, "RELEASE": "r3", "TARGET": "sales-dev", "CONFIRM_DATABASE": confirm}
    assert (run_step(script, tmp_path, env).returncode == 0) is ok


def test_onboard_reads_with_the_plan_identity_and_writes_only_behind_the_deploy_environment():
    onboard = jobs(WORKFLOWS / "onboard.yml")
    assert onboard["read"]["environment"] == "${{ inputs.environment }}-plan"
    assert onboard["read"]["if"] == "${{ inputs.action == 'export' || inputs.action == 'baseline-report' }}"
    assert "plan_client_id" in json.dumps(onboard["read"]) and "deploy_client_id" not in json.dumps(
        onboard["read"]
    )
    assert onboard["baseline"]["environment"] == "${{ inputs.environment }}"
    assert onboard["baseline"]["if"] == "${{ inputs.action == 'baseline' }}"
    assert "--report-only" not in json.dumps(onboard["baseline"])
    resolve = jobs(WORKFLOWS / "resolve.yml")["resolve"]
    assert resolve["environment"] == "${{ inputs.environment }}"


def test_onboard_uploads_the_result_after_a_tool_failure_and_not_after_a_failed_login():
    # The upload fails when it finds no file. After a failed login the tool did not run: the job
    # then showed "No files were found" as a second error under the login error.
    steps = jobs(WORKFLOWS / "onboard.yml")["read"]["steps"]
    (login,) = [s for s in steps if s.get("uses", "").startswith("azure/login@")]
    (upload,) = result_uploads(steps)
    assert upload["with"]["if-no-files-found"] == "error"
    # 'skipped' with a managed identity: no login step runs, and the tool signs in itself
    outcome, row = f"steps.{login['id']}.outcome", "fromJSON(needs.targets.outputs.target)"
    assert " ".join(upload["if"].split()) == (
        f"${{{{ always() && ({outcome} == 'success' || "
        f"({outcome} == 'skipped' && {row}.auth == 'managed-identity')) }}}}"
    )


def test_drift_uses_the_plan_identity_and_cannot_write_to_the_repository():
    drift = jobs(WORKFLOWS / "drift.yml")["drift"]
    assert drift["environment"] == "${{ inputs.environment }}-plan"
    assert permissions(drift) == {"contents": "read", "id-token": "write"}
    assert "plan_client_id" in json.dumps(drift) and "deploy_client_id" not in json.dumps(drift)
    # exit 30 must fail the job: no step may swallow the exit code of the tool
    assert "continue-on-error" not in (WORKFLOWS / "drift.yml").read_text(encoding="utf-8")


def outcome_env(**change: str) -> dict[str, str]:
    base = {
        "JOB": "deploy",
        "DIR": "report",
        "TARGET": "sales-prod",
        "OUTCOME": "failure",
        "EXIT_CODE": "23",
        "REASON_CODE": "TOOL_DEFECT_AFTER_DISPATCH",
        "PENDING": "",
    }
    return {**base, **change}


@needs_bash
@pytest.mark.parametrize("job_id", ["plan", "deploy"])
def test_outcome_file_holds_only_checked_values(tmp_path: Path, job_id: str):
    script = step_script(WORKFLOWS / "stage.yml", job_id, "outcome")
    hostile = outcome_env(
        REASON_CODE="X\nexit_code=0", EXIT_CODE="0; rm -rf /", OUTCOME="success\noutcome=success"
    )
    assert run_step(script, tmp_path, hostile).returncode == 0
    assert (tmp_path / "report" / "outcome.txt").read_text().splitlines() == [
        "job=deploy",
        "target=sales-prod",
        "outcome=none",
        "exit_code=none",
        "reason_code=none",
        "pending=none",
    ]
    assert run_step(script, tmp_path, outcome_env(TARGET="a/../b")).returncode != 0


def run_incident(tmp_path: Path, issues: list[dict[str, Any]]) -> subprocess.CompletedProcess[str]:
    (tmp_path / "issues.json").write_text(json.dumps(issues), encoding="utf-8")
    script = step_script(WORKFLOWS / "stage.yml", "incident", "issue")
    env = {"ENVIRONMENT": "prod", "RELEASE": "r57", "FAKE_ISSUES": str(tmp_path / "issues.json")}
    return run_step(script, tmp_path, env, {"gh": FAKE_GH})


def write_outcome(tmp_path: Path, artefact: str, env: dict[str, str]) -> None:
    script = step_script(WORKFLOWS / "stage.yml", "deploy", "outcome")
    assert run_step(script, tmp_path, env).returncode == 0
    (tmp_path / "out").mkdir(exist_ok=True)
    shutil.move(str(tmp_path / env["DIR"]), str(tmp_path / "out" / artefact))


@needs_bash
@needs_jq
def test_incident_opens_one_issue_for_the_failed_target_with_codes_and_no_issue_for_a_good_one(
    tmp_path: Path,
):
    write_outcome(tmp_path, "report-prod-sales-prod", outcome_env())
    write_outcome(
        tmp_path, "report-prod-sales-eu", outcome_env(TARGET="sales-eu", OUTCOME="success", EXIT_CODE="0")
    )
    done = run_incident(tmp_path, [{"number": 4, "title": "azsqlcd incident: prod / other"}])
    assert done.returncode == 0, done.stderr
    calls = log(tmp_path)
    (create,) = re.findall(r"^gh issue create .*$", calls, flags=re.MULTILINE)
    assert "--title azsqlcd incident: prod / sales-prod --label azsqlcd-incident" in create
    for fact in ("Exit code: 23", "Reason code: TOOL_DEFECT_AFTER_DISPATCH", "Release: r57", "Job: deploy"):
        assert fact in calls
    assert "actions/runs/900/attempts/2" in calls
    assert "sales-eu" not in calls and "gh issue comment" not in calls


@needs_bash
@needs_jq
def test_incident_adds_a_comment_when_the_target_has_an_open_issue(tmp_path: Path):
    write_outcome(tmp_path, "report-prod-sales-prod", outcome_env())
    done = run_incident(tmp_path, [{"number": 7, "title": "azsqlcd incident: prod / sales-prod"}])
    assert done.returncode == 0, done.stderr
    assert "gh issue comment 7 " in log(tmp_path) and "gh issue create" not in log(tmp_path)


@needs_bash
@needs_jq
def test_incident_is_raised_when_a_job_failed_before_the_tool_reported(tmp_path: Path):
    done = run_incident(tmp_path, [])
    assert done.returncode == 0, done.stderr
    assert "--title azsqlcd incident: prod / no tool report" in log(tmp_path)


def test_incident_job_runs_only_for_a_failure_in_a_gated_stage():
    incident = jobs(WORKFLOWS / "stage.yml")["incident"]
    assert incident["if"] == (
        "${{ always() && inputs.gated && !inputs.check-only && contains(needs.*.result, 'failure') }}"
    )
    assert incident["needs"] == ["targets", "plan", "gate", "deploy"]


@needs_bash
def test_record_attaches_plan_and_report_under_names_that_cannot_collide(tmp_path: Path):
    for artefact, files in (
        ("plan-prod-sales-prod", ["plan.json"]),
        ("report-prod-sales-prod", ["report.json", "outcome.txt"]),
    ):
        (tmp_path / "out" / artefact).mkdir(parents=True)
        for name in files:
            (tmp_path / "out" / artefact / name).write_text("{}")
    script = step_script(WORKFLOWS / "stage.yml", "record", "attach")
    done = run_step(script, tmp_path, {"RELEASE": "r57"}, {"gh": FAKE_GH})
    assert done.returncode == 0, done.stderr
    assert log(tmp_path).strip() == (
        "gh release upload r57 -R akaalholdings/db-sales "
        "assets/plan-prod-sales-prod-900-2.json assets/report-prod-sales-prod-900-2.json"
    )


@needs_bash
def test_record_never_replaces_an_asset_of_the_release(tmp_path: Path):
    script = step_script(WORKFLOWS / "stage.yml", "record", "attach")
    assert "--clobber" not in script
    done = run_step(script, tmp_path, {"RELEASE": "r57"}, {"gh": FAKE_GH})
    assert done.returncode == 0 and log(tmp_path) == ""  # nothing to attach is not an error


# ----------------------------------------------------------------------------- operator documents


def test_runbook_has_one_section_per_exit_code():
    runbook = (REPO / "docs" / "runbook.md").read_text(encoding="utf-8")
    sections = [int(code) for code in re.findall(r"^## Exit (\d+)\b", runbook, flags=re.MULTILINE)]
    assert sorted(sections) == sorted(int(code) for code in Exit)


CONSTRUCTOR_EXIT = {
    "failed": Exit.FAILED_ROLLED_BACK,
    "refused": Exit.REFUSED,
    "unknown": Exit.UNKNOWN,
    "retry_safe": Exit.RETRY_SAFE,
    "locked": Exit.LOCKED,
}


def reason_codes_in_source() -> set[tuple[int, str]]:
    """(exit code, reason code) for every ToolError that the source builds with a literal code."""
    pattern = re.compile(
        r"\b(failed|refused|unknown|retry_safe|locked)\(\s*\"([A-Z][A-Z0-9_]+)\""
        r"|ToolError\(\s*Exit\.([A-Z_]+),\s*\"([A-Z][A-Z0-9_]+)\""
    )
    found: set[tuple[int, str]] = set()
    for path in (REPO / "src" / "azsqlcd").glob("*.py"):
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            if match.group(1):
                found.add((int(CONSTRUCTOR_EXIT[match.group(1)]), match.group(2)))
            else:
                found.add((int(Exit[match.group(3)]), match.group(4)))
    return found


def reason_codes_in_runbook() -> set[tuple[int, str]]:
    runbook = (REPO / "docs" / "runbook.md").read_text(encoding="utf-8")
    found: set[tuple[int, str]] = set()
    for section in re.split(r"^## ", runbook, flags=re.MULTILINE):
        title = re.match(r"Exit (\d+)\n", section)
        if title:
            for code in re.findall(r"^\| `([A-Z][A-Z0-9_]+)` \|", section, flags=re.MULTILINE):
                found.add((int(title.group(1)), code))
    return found


def test_runbook_has_a_row_for_every_reason_code_under_the_exit_code_that_the_tool_gives():
    # A new reason code in src/azsqlcd needs a row in docs/runbook.md, in the section of its exit
    # code: | `CODE` | meaning | state of the database | what to do, with the exact command |
    missing = sorted(reason_codes_in_source() - reason_codes_in_runbook())
    assert not missing, f"docs/runbook.md has no row for (exit, reason code): {missing}"


def test_runbook_rows_of_part_1_reason_codes_sit_under_their_exit_code():
    documented = reason_codes_in_runbook()
    part_1 = {
        (0, "ALREADY_PAST"),
        (21, "DEPENDANT_BROKEN"),
        (22, "TOOL_DEFECT"),
        (22, "STATE_MISSING"),
        (22, "RUN_UNKNOWN"),
        (22, "NONTX_NOT_ALONE"),
        (22, "CATCHUP_REQUIRED"),
        (22, "CHAIN_DIVERGED"),
        (22, "NAME_COLLISION"),
        (22, "NOT_ON_MAIN"),
        (22, "STALE_PLAN"),
        (23, "TOOL_DEFECT_AFTER_DISPATCH"),
        (25, "RUN_LIVE"),
    }
    assert part_1 <= documented, sorted(part_1 - documented)


@pytest.mark.parametrize(
    "case",
    [
        "Job cancelled or runner lost",
        "Lock timeout in prod at night",
        "Stale plan",
        "Drift on a touched object",
        "Out-of-band change by a DBA",
        "Refresh of an environment from prod",
        "Unknown run",
    ],
)
def test_runbook_has_the_named_cases(case: str):
    runbook = (REPO / "docs" / "runbook.md").read_text(encoding="utf-8")
    assert f"\n### {case}\n" in runbook


def test_runbook_commands_use_only_inputs_that_the_dispatch_forms_have():
    runbook = (REPO / "docs" / "runbook.md").read_text(encoding="utf-8")
    setup = (REPO / "docs" / "setup.md").read_text(encoding="utf-8")
    seen = 0
    for name in ("db.yml", "resolve.yml", "onboard.yml"):
        form = triggers(load(TEMPLATE / ".github" / "workflows" / name))["workflow_dispatch"]["inputs"]
        for command in re.findall(rf"gh workflow run {re.escape(name)}[^`\n]*", runbook + setup):
            seen += 1
            assert set(re.findall(r"-f (\w+)=", command)) <= set(form), command
            assert "--ref main" in command, command
    assert seen >= 10
    # fragments of a resolve command name the same inputs
    assert set(re.findall(r"`-f (\w+)=", runbook)) <= set(
        triggers(load(TEMPLATE / ".github" / "workflows" / "resolve.yml"))["workflow_dispatch"]["inputs"]
    )


def test_known_gaps_lists_every_spike_of_the_blueprint():
    gaps = (REPO / "docs" / "known-gaps.md").read_text(encoding="utf-8")
    spikes = [f"L{n}" for n in range(1, 18)] + [f"G{n}" for n in range(1, 8)]
    for spike in spikes:
        assert re.search(rf"^\| {spike} \|", gaps, flags=re.MULTILINE), spike


# ----------------------------------------------------------------------------- TQ-08: wiring that had no test
def login_steps() -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    found = []
    for name in REUSABLE:
        for job_id, job in jobs(WORKFLOWS / name).items():
            for step in job.get("steps", []):
                if step.get("uses", "").startswith("azure/login@"):
                    found.append((f"{name}: {job_id}", job, step))
    return found


def test_a_plan_environment_signs_in_with_the_plan_identity_and_a_deploy_environment_with_its_own():
    # the plan job runs before the approval: it must never hold the identity that can write
    seen = login_steps()
    assert {where for where, _, _ in seen} == {
        "stage.yml: plan",
        "stage.yml: deploy",
        "drift.yml: drift",
        "resolve.yml: resolve",
        "onboard.yml: read",
        "onboard.yml: baseline",
    }
    for where, job, step in seen:
        identity = "plan_client_id" if job["environment"].endswith("-plan") else "deploy_client_id"
        client = step["with"]["client-id"]
        assert re.fullmatch(r"\$\{\{ [A-Za-z.()]+\." + identity + r" \}\}", client), f"{where}: {client}"
        assert "tenant_id }}" in step["with"]["tenant-id"], where
    stage = jobs(WORKFLOWS / "stage.yml")
    (plan_login,) = [s for s in stage["plan"]["steps"] if s.get("uses", "").startswith("azure/login@")]
    (deploy_login,) = [s for s in stage["deploy"]["steps"] if s.get("uses", "").startswith("azure/login@")]
    assert plan_login["with"]["client-id"] == "${{ matrix.target.plan_client_id }}"
    assert deploy_login["with"]["client-id"] == "${{ matrix.target.deploy_client_id }}"


def test_every_call_that_reads_a_release_checks_it_against_the_digest_of_the_release_job():
    checked = 0
    for where, lines in tool_calls():
        if "--bundle" not in lines:
            continue
        checked += 1
        digest = lines[lines.index("--digest") + 1]
        if where.startswith(".github/workflows/drift.yml"):
            # the bundle of a drift run is built in the run itself
            built = ("${{ steps.build.outputs.digest }}", "${{ needs.targets.outputs.digest }}")
            assert digest in built, where
        else:
            assert digest == "${{ inputs.digest }}", f"{where}: {digest}"
    assert checked >= 10
    for name in ("db.yml", "resolve.yml", "onboard.yml"):
        for job_id, job in jobs(TEMPLATE / ".github" / "workflows" / name).items():
            if "digest" in job.get("with", {}):
                assert job["with"]["digest"] == "${{ needs.release.outputs.digest }}", f"{name}: {job_id}"
                assert job["with"]["release"] == "${{ needs.release.outputs.release }}", f"{name}: {job_id}"


RELEASE_INPUT = "${{ inputs.release }}"
NAME_CHECK = re.compile(r"name='\^r\[0-9\]\+\$'\n(.*\n)*?.*\[\[ \"\$(RELEASE|REQUESTED)\" =~ \$name")


@pytest.mark.parametrize("name", ["release.yml", "stage.yml", "resolve.yml", "onboard.yml"])
def test_every_job_checks_the_release_name_before_it_uses_it(name: str):
    seen = 0
    for job_id, job in jobs(WORKFLOWS / name).items():
        checked = False
        for step in job.get("steps", []):
            uses_input = RELEASE_INPUT in (step.get("env") or {}).values()
            if uses_input and NAME_CHECK.search(step["run"]):
                checked = True
            if uses_input:
                seen += 1
                assert checked, f"{name}: {job_id}: a script reads the release input before the name check"
            if RELEASE_INPUT in json.dumps(step.get("with") or {}):
                # a checkout of the tag: this job, or the job that it waits for, checked the name
                first = jobs(WORKFLOWS / name)[job.get("needs", job_id)]["steps"][0] if not checked else step
                assert checked or NAME_CHECK.search(first.get("run", "")), f"{name}: {job_id}"
    assert seen >= 2


@needs_bash
@needs_jq
def test_incident_never_writes_a_release_input_that_is_not_a_release_name_into_an_issue(tmp_path: Path):
    (tmp_path / "issues.json").write_text("[]", encoding="utf-8")
    script = step_script(WORKFLOWS / "stage.yml", "incident", "issue")
    hostile = "r57 [approve here](https://evil.example) @everyone"
    env = {"ENVIRONMENT": "prod", "RELEASE": hostile, "FAKE_ISSUES": str(tmp_path / "issues.json")}

    done = run_step(script, tmp_path, env, {"gh": FAKE_GH})

    assert done.returncode == 0, done.stderr
    assert "evil.example" not in log(tmp_path) and "Release: (not a release name)" in log(tmp_path)


def test_a_build_checks_out_the_full_history():
    # the release number is the count of first-parent commits: a shallow clone is SHALLOW_REPOSITORY
    builds = 0
    for path in WORKFLOW_FILES:
        for job_id, job in jobs(path).items():
            steps = job.get("steps", [])
            for at, step in enumerate(steps):
                if not step.get("uses", "").startswith(OWN + "@"):
                    continue
                if step["with"]["args"].splitlines()[0] not in ("build", "verify"):
                    continue
                builds += 1
                checkouts = [s for s in steps[:at] if s.get("uses", "").startswith("actions/checkout@")]
                assert len(checkouts) == 1, f"{rel(path)}: {job_id}"
                assert checkouts[0]["with"]["fetch-depth"] == 0, f"{rel(path)}: {job_id}"
    assert builds == 4  # release create, release existing, drift targets, verify


# ----------------------------------------------------------------------------- the triage log
LOG_DIR = "azsqlcd-logs"
LOG_TARGET = {"drift": "${{ matrix.target.id }}", "plan": "${{ matrix.target.id }}"} | {
    "deploy": "${{ matrix.target.id }}",
    "read": "${{ inputs.target }}",
    "baseline": "${{ inputs.target }}",
    "resolve": "${{ inputs.target }}",
}


def database_jobs() -> list[tuple[Path, str, dict[str, Any]]]:
    """Every job that runs a database command: a step of the tool with `db: true`."""
    found = []
    for path in WORKFLOW_FILES:
        if path.name == "action.yml":
            continue
        for job_id, job in jobs(path).items():
            steps = job.get("steps", [])
            if any(step.get("uses", "").startswith(OWN + "@") and step["with"].get("db") for step in steps):
                found.append((path, job_id, job))
    return found


def test_every_job_that_runs_a_database_command_uploads_its_triage_log_also_when_it_failed():
    # the owner sends this log when a run fails: a job that does not upload it loses it with the runner
    found = database_jobs()
    assert sorted(job_id for _, job_id, _ in found) == sorted(LOG_TARGET)
    for path, job_id, job in found:
        where = f"{rel(path)}: {job_id}"
        assert (job.get("env") or {}).get("AZSQLCD_LOG_DIR") == LOG_DIR, where
        steps = job["steps"]
        last_tool = max(at for at, step in enumerate(steps) if step.get("uses", "").startswith(OWN + "@"))
        uploads = [
            (at, step)
            for at, step in enumerate(steps)
            if step.get("uses", "").startswith("actions/upload-artifact@")
            and step["with"].get("path") == LOG_DIR
        ]
        assert len(uploads) == 1, where
        at, upload = uploads[0]
        assert at > last_tool, where
        assert "always()" in str(upload.get("if")), where
        name = f"azsqlcd-logs-${{{{ inputs.environment }}}}-{LOG_TARGET[job_id]}-{job_id}"
        assert upload["with"]["name"] == name, where
        # a job that failed before the tool started has no log: that must not hide the first failure
        assert upload["with"].get("if-no-files-found") == "ignore", where


def test_the_log_artefact_is_not_read_by_the_jobs_that_collect_plans_and_reports():
    # record and incident download plan-<env>-* and report-<env>-*: the log name must match neither
    for _, job_id, _ in database_jobs():
        name = f"azsqlcd-logs-prod-sales-{job_id}"
        assert not name.startswith(("plan-", "report-"))


# ----------------------------------------------------------------------------- OIDC or a managed identity
# The identity of each job that opens a database session. A job of a plan environment runs before
# the approval: it never gets the identity that can write.
SIGN_IN = {
    "stage.yml: plan": "plan_client_id",
    "stage.yml: deploy": "deploy_client_id",
    "drift.yml: drift": "plan_client_id",
    "onboard.yml: read": "plan_client_id",
    "onboard.yml: baseline": "deploy_client_id",
    "resolve.yml: resolve": "deploy_client_id",
}
ROW = re.compile(r"(?:matrix\.target|fromJSON\(needs\.targets\.outputs\.target\))\.([a-z_]+)")
PLAN_ID, DEPLOY_ID = "00000000-0000-0000-0000-00000000000a", "00000000-0000-0000-0000-00000000000b"


def matrix_row(auth: str | None) -> dict[str, Any]:
    row: dict[str, Any] = {"id": "sales-a", "plan_client_id": PLAN_ID, "deploy_client_id": DEPLOY_ID}
    return row | {"tenant_id": "t", "gated": True} | ({} if auth is None else {"auth": auth})


def value_of(expression: str, row: dict[str, Any], **steps: str) -> Any:
    """What an expression of these files gives for a matrix row. `&&` and `||` give one of their
    operands, as in Python; a key that the row does not hold is the empty string."""
    inner = re.fullmatch(r"\$\{\{ (.*) \}\}", expression)
    assert inner, expression
    text = ROW.sub(lambda found: repr(row.get(found.group(1), "")), inner.group(1))
    text = re.sub(r"steps\.([a-z]+)\.outcome", lambda found: repr(steps[found.group(1)]), text)
    text = text.replace("always()", "True").replace("&&", " and ").replace("||", " or ")
    assert re.fullmatch(r"[A-Za-z0-9 '()=!_-]*", text), text  # only literals and operators are left
    return eval(text, {"__builtins__": {}}, {})  # noqa: S307


def sign_in_jobs() -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """(where, job, its azure/login step) of every job that runs a database command."""
    found = []
    for path, job_id, job in database_jobs():
        (login,) = [step for step in job["steps"] if step.get("uses", "").startswith("azure/login@")]
        found.append((f"{path.name}: {job_id}", job, login))
    return found


def test_every_database_job_has_one_sign_in_and_the_table_of_this_file_names_its_identity():
    assert {where for where, _, _ in sign_in_jobs()} == set(SIGN_IN)
    for where, job, login in sign_in_jobs():
        plan_environment = job["environment"].endswith("-plan")
        assert SIGN_IN[where] == ("plan_client_id" if plan_environment else "deploy_client_id"), where
        assert ROW.findall(login["with"]["client-id"]) == [SIGN_IN[where]], where


@pytest.mark.parametrize("auth", ["oidc", None])
def test_with_oidc_the_azure_login_step_runs_and_the_tool_reads_the_azure_cli_session(auth):
    """None: a matrix row of a tool version that did not write the key. It signs in as before."""
    row = matrix_row(auth)
    for where, job, login in sign_in_jobs():
        assert value_of(login["if"], row) is True, where
        assert value_of(login["with"]["client-id"], row) == row[SIGN_IN[where]], where
        assert value_of(job["env"]["AZSQLCD_AUTH"], row) == "entra", where
        assert value_of(job["env"]["AZSQLCD_MANAGED_IDENTITY_CLIENT_ID"], row) == "", where


def test_with_a_managed_identity_no_azure_login_runs_and_the_tool_gets_the_identity_of_the_job():
    row = matrix_row("managed-identity")
    for where, job, login in sign_in_jobs():
        assert value_of(login["if"], row) is False, where
        assert value_of(job["env"]["AZSQLCD_AUTH"], row) == "managed-identity", where
        # the same identity that azure/login gets with OIDC: plan for a read, deploy for a write
        assert value_of(job["env"]["AZSQLCD_MANAGED_IDENTITY_CLIENT_ID"], row) == row[SIGN_IN[where]], where
        assert ROW.findall(job["env"]["AZSQLCD_MANAGED_IDENTITY_CLIENT_ID"]) == [
            "auth",
            *ROW.findall(login["with"]["client-id"]),
        ], where


def test_no_job_of_a_plan_environment_can_name_the_deploy_identity_in_either_way_to_sign_in():
    for where, job, _ in sign_in_jobs():
        if SIGN_IN[where] == "plan_client_id":
            assert "deploy_client_id" not in json.dumps(job), where
        else:
            assert "plan_client_id" not in json.dumps(job), where


def test_the_sign_in_of_a_job_is_set_for_the_whole_job_before_any_database_command():
    """The variables are in `env:` of the job, as AZSQLCD_LOG_DIR is: every step of the job gets
    them, also the steps of the composite action. No step sets them again, and no value of the
    matrix reaches a script or an argument of the tool."""
    from azsqlcd import config, session

    for where, job, login in sign_in_jobs():
        steps = job["steps"]
        first_tool = min(at for at, step in enumerate(steps) if step.get("uses", "").startswith(OWN + "@"))
        assert steps.index(login) < first_tool, where
        names = {session.AUTH_VARIABLE, session.MANAGED_IDENTITY_CLIENT_ID_VARIABLE}
        assert names < set(job["env"]), where
        for step in steps:
            assert not names & set(step.get("env") or {}), where
            assert "auth" not in ROW.findall(json.dumps(step.get("with") or {})), where
        # the words of the expressions are the ones of the tool
        text = job["env"][session.AUTH_VARIABLE] + login["if"]
        assert set(re.findall(r"'([a-z-]+)'", text)) == {config.AUTH_MANAGED_IDENTITY, session.AUTH_ENTRA}
        assert config.AUTH_MANAGED_IDENTITY == session.AUTH_MANAGED_IDENTITY
    # SQL authentication is for a workstation: no file of the pipeline can ask for it
    for path in YAML_FILES:
        text = path.read_text(encoding="utf-8")
        assert "AZSQLCD_SQL" not in text and "'sql'" not in text, rel(path)


def test_a_job_that_can_sign_in_keeps_the_permission_for_oidc():
    for where, job, _ in sign_in_jobs():
        assert permissions(job).get("id-token") == "write", where


@pytest.mark.parametrize(
    ("login", "auth", "uploads"),
    [
        ("success", "oidc", True),
        ("failure", "oidc", False),
        ("skipped", "oidc", False),  # a step before the login failed: the tool did not run
        ("skipped", "managed-identity", True),  # no login step runs: the tool signs in itself
    ],
)
def test_onboard_uploads_the_result_when_the_tool_can_have_run(login, auth, uploads):
    steps = jobs(WORKFLOWS / "onboard.yml")["read"]["steps"]
    (upload,) = result_uploads(steps)
    expression = " ".join(upload["if"].split())
    assert bool(value_of(expression, matrix_row(auth), login=login)) is uploads


def test_the_template_and_the_example_sign_in_with_oidc_and_their_workflows_hold_no_sign_in():
    from azsqlcd.config import load_config, targets_matrix

    for folder in (TEMPLATE, REPO / "examples" / "demo-db"):
        text = (folder / "azsqlcd.toml").read_text(encoding="utf-8")
        config = load_config(text)
        assert {environment.auth for environment in config.env.values()} == {"oidc"}, folder.name
        assert all(row["auth"] == "oidc" for stage in STAGES for row in targets_matrix(config, stage))
        assert "managed-identity" in text  # a comment says what the other value is
        for path in sorted((folder / ".github" / "workflows").glob("*.yml")):
            workflow = path.read_text(encoding="utf-8")
            assert "AZSQLCD_AUTH" not in workflow and "azure/login" not in workflow, rel(path)


@needs_bash
@pytest.mark.parametrize(
    ("env", "told"),
    [
        ({"AZSQLCD_AUTH": "not-a-sign-in"}, "AUTH_INVALID: AZSQLCD_AUTH must be one of"),
        (
            {"AZSQLCD_AUTH": "managed-identity", "AZSQLCD_MANAGED_IDENTITY_CLIENT_ID": "not-a-guid"},
            "AUTH_INVALID: AZSQLCD_MANAGED_IDENTITY_CLIENT_ID must be a GUID",
        ),
    ],
)
def test_a_variable_of_the_job_reaches_the_tool_through_the_composite_action(tmp_path: Path, env, told):
    """The action sets its own variables in `env:` of its step and passes the rest of the
    environment on. The tool is run for real here, and it stops on the value before it reads the
    release: so it read the variable."""
    script = step_script(REPO / "action.yml", "action", "run")
    args = "plan\n--bundle\nnone\n--digest\nd\n--env\ndev\n--target\nt\n--out\nplan\n--no-log\n"
    base = {
        "AZSQLCD_ARGS": args,
        "AZSQLCD_DB": "true",
        "GITHUB_ACTION_PATH": str(REPO),
        "REAL_PYTHON": sys.executable,
    }

    done = run_step(script, tmp_path, base | env, {"uv": UV_THAT_RUNS_PYTHON})

    assert done.returncode == 22, done.stdout + done.stderr
    assert told in done.stdout
