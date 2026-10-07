"""azsqlcd.toml: every key is known and checked, and the file holds no secrets."""

from pathlib import Path

import pytest

from azsqlcd.config import (
    DEFAULT_SERVER_SUFFIXES,
    ENVIRONMENTS,
    Target,
    load_config,
    resolve_target,
    targets_matrix,
)
from azsqlcd.errors import Exit, ToolError

TENANT = "11111111-2222-3333-4444-555555555555"
NONPROD_PLAN = "aaaaaaaa-0000-0000-0000-000000000001"
NONPROD_DEPLOY = "aaaaaaaa-0000-0000-0000-000000000002"
PROD_PLAN = "bbbbbbbb-0000-0000-0000-000000000001"
PROD_DEPLOY = "bbbbbbbb-0000-0000-0000-000000000002"

VALID = f"""
[project]
name = "sales"
tenant_id = "{TENANT}"
table_model = false
module_chunk = 100
min_token_minutes = 20

[identities]
nonprod_plan = "{NONPROD_PLAN}"
nonprod_deploy = "{NONPROD_DEPLOY}"
prod_plan = "{PROD_PLAN}"
prod_deploy = "{PROD_DEPLOY}"

[env.dev]
plan_identity = "nonprod_plan"
deploy_identity = "nonprod_deploy"
drift = "report"
lock_timeout_ms = 30000
applock_wait_s = 600
job_timeout_minutes = 120
targets = [{{ id = "sales-dev", server = "sql-sales-dev.database.windows.net", database = "sales" }}]

[env.prod]
plan_identity = "prod_plan"
deploy_identity = "prod_deploy"
drift = "block"
lock_timeout_ms = 10000
applock_wait_s = 600
job_timeout_minutes = 120
targets = [
  {{ id = "sales-prod", server = "sql-sales-prod.database.windows.net", database = "sales" }},
  {{ id = "sales-prod-eu", server = "sql-sales-prod-eu.database.windows.net", database = "sales_eu" }},
]

[unmanaged]
objects = ["TABLE:[audit].[Log]"]

[ack]
unmanaged_dependants = ["[rpt].[vMargin] -> [sales].[Order].[LegacyCode]"]
"""


def refused_key(text: str) -> str:
    """The key path that load_config refuses the text for."""
    with pytest.raises(ToolError) as e:
        load_config(text)
    assert (e.value.exit_code, e.value.reason_code) == (Exit.REFUSED, "CONFIG_INVALID")
    return e.value.detail.get("key", "")


def env_block(name: str, target_id: str) -> str:
    return f"""
[env.{name}]
plan_identity = "nonprod_plan"
deploy_identity = "nonprod_deploy"
drift = "report"
lock_timeout_ms = 30000
applock_wait_s = 600
job_timeout_minutes = 120
targets = [{{ id = "{target_id}", server = "s.database.windows.net", database = "sales" }}]
"""


def test_every_documented_key_is_read_into_the_config():
    c = load_config(VALID)
    assert (c.project.name, c.project.tenant_id) == ("sales", TENANT)
    assert (c.project.table_model, c.project.module_chunk, c.project.min_token_minutes) == (False, 100, 20)
    assert c.identities["prod_deploy"] == PROD_DEPLOY
    prod = c.env["prod"]
    assert (prod.name, prod.plan_identity, prod.deploy_identity, prod.drift) == (
        "prod",
        "prod_plan",
        "prod_deploy",
        "block",
    )
    assert (prod.lock_timeout_ms, prod.applock_wait_s, prod.job_timeout_minutes) == (10000, 600, 120)
    assert prod.targets[1] == Target("sales-prod-eu", "sql-sales-prod-eu.database.windows.net", "sales_eu")
    assert c.unmanaged_objects == ("TABLE:[audit].[Log]",)
    assert c.unmanaged_dependants == ("[rpt].[vMargin] -> [sales].[Order].[LegacyCode]",)


def test_unmanaged_and_ack_sections_are_optional_and_default_to_empty():
    text = VALID[: VALID.index("[unmanaged]")]
    c = load_config(text)
    assert (c.unmanaged_objects, c.unmanaged_dependants) == ((), ())


@pytest.mark.parametrize(
    ("old", "new", "key"),
    [
        ("[identities]", "colour = 1\n[identities]", "project.colour"),
        ('drift = "block"', 'drift = "block"\nretries = 3', "env.prod.retries"),
        ('database = "sales_eu"', 'database = "sales_eu", port = 1433', "env.prod.targets[1].port"),
        ("[unmanaged]", "[extra]\nx = 1\n[unmanaged]", "extra"),
        ('objects = ["TABLE:[audit].[Log]"]', "tables = []", "unmanaged.tables"),
        ("unmanaged_dependants =", "dependants =", "ack.dependants"),
    ],
)
def test_an_unknown_key_is_an_error_at_every_level(old, new, key):
    assert old in VALID
    assert refused_key(VALID.replace(old, new)) == key


@pytest.mark.parametrize(
    ("old", "key"),
    [
        ("table_model = false\n", "project.table_model"),
        ("applock_wait_s = 600\n", "env.dev.applock_wait_s"),
        ('server = "sql-sales-dev.database.windows.net", ', "env.dev.targets[0].server"),
    ],
)
def test_a_missing_key_is_an_error_and_never_a_silent_default(old, key):
    assert old in VALID
    assert refused_key(VALID.replace(old, "", 1)) == key


@pytest.mark.parametrize("name", ["staging", "production", "DEV", "dev-plan"])
def test_an_environment_name_outside_the_fixed_list_is_refused(name):
    assert refused_key(VALID + env_block(name, "t1")) == f"env.{name}"


def test_the_fixed_environment_list_is_the_promotion_order_plus_the_live_test_environment():
    assert ENVIRONMENTS == ("dev", "sandbox", "test", "preprod", "prod", "disposable")
    text = VALID
    for name in ("sandbox", "test", "preprod", "disposable"):
        text += env_block(name, f"t-{name}")
    assert list(load_config(text).env) == ["dev", "prod", "sandbox", "test", "preprod", "disposable"]


def test_a_target_id_is_unique_in_the_whole_file_not_only_in_its_environment():
    assert refused_key(VALID + env_block("test", "sales-dev")) == "env.test.targets[0].id"
    assert refused_key(VALID.replace('id = "sales-prod-eu"', 'id = "sales-prod"')) == "env.prod.targets[1].id"


@pytest.mark.parametrize("bad_id", ["sales dev", "sales/dev", "sales.dev", "", "sales-dev\\n"])
def test_a_target_id_holds_only_characters_that_are_safe_in_a_workflow_matrix(bad_id):
    assert refused_key(VALID.replace('id = "sales-dev"', f'id = "{bad_id}"')) == "env.dev.targets[0].id"


@pytest.mark.parametrize(
    "bad_server", ["tcp:sql.database.windows.net", "sql.database.windows.net,1433", "sql;Encrypt=no", "a b"]
)
def test_a_server_name_cannot_carry_connection_string_syntax(bad_server):
    text = VALID.replace("sql-sales-dev.database.windows.net", bad_server)
    assert refused_key(text) == "env.dev.targets[0].server"


@pytest.mark.parametrize("escape", ["\\n", "\\r\\n", "\\t", "\\u0000", "\\u001B", "\\u0085", "\\u2028"])
@pytest.mark.parametrize(
    ("old", "key"),
    [
        ('name = "sales"', "project.name"),
        ('database = "sales_eu"', "env.prod.targets[1].database"),
        ("sql-sales-dev.database.windows.net", "env.dev.targets[0].server"),
    ],
)
def test_a_project_database_or_server_name_holds_no_control_character(old, key, escape):
    """These names go into batch text, the setup script and the connection string. A line break
    could start a batch of its own where a client tool splits a script at GO lines."""
    assert refused_key(VALID.replace(old, old.replace("sales", f"sa{escape}les"))) == key


def test_a_name_with_a_space_a_quote_or_a_letter_outside_ascii_is_still_a_name():
    text = VALID.replace('name = "sales"', 'name = "Sales \u00e9t\u00e9 \'24"')
    text = text.replace('database = "sales_eu"', 'database = "sales eu; [x]"')
    loaded = load_config(text)
    assert loaded.project.name == "Sales été '24"
    assert loaded.env["prod"].targets[1].database == "sales eu; [x]"


def test_an_identity_reference_must_resolve():
    assert refused_key(VALID.replace('plan_identity = "prod_plan"', 'plan_identity = "nobody"')) == (
        "env.prod.plan_identity"
    )
    assert refused_key(VALID.replace('deploy_identity = "prod_deploy"', 'deploy_identity = "x"')) == (
        "env.prod.deploy_identity"
    )


@pytest.mark.parametrize(
    "not_a_guid", ["<client id>", TENANT[:-1], TENANT + "0", "{" + TENANT + "}", TENANT + "\\n"]
)
def test_client_ids_and_the_tenant_id_are_guids(not_a_guid):
    assert refused_key(VALID.replace(TENANT, not_a_guid)) == "project.tenant_id"
    assert refused_key(VALID.replace(PROD_PLAN, not_a_guid)) == "identities.prod_plan"


@pytest.mark.parametrize(
    ("old", "new", "key"),
    [
        ("[identities]", '[identities]\ndeploy_password = "hunter2-value"', "identities.deploy_password"),
        ("[identities]", 'api_key = "hunter2-value"\n[identities]', "project.api_key"),
        ('database = "sales_eu"', 'database = "sales_eu", Client_Secret = "hunter2-value"', None),
        ("[unmanaged]", '[vault]\nSECRET = "hunter2-value"\n[unmanaged]', "vault.SECRET"),
        ("[unmanaged]", '[env.dev.keys]\na = "hunter2-value"\n[unmanaged]', "env.dev.keys"),
    ],
)
def test_a_secret_looking_key_is_refused_and_its_value_is_never_printed(old, new, key):
    with pytest.raises(ToolError) as e:
        load_config(VALID.replace(old, new))
    assert e.value.reason_code == "CONFIG_INVALID"
    assert e.value.detail["key"] == (key or "env.prod.targets[1].Client_Secret")
    assert "no secrets" in e.value.message
    assert "hunter2" not in str(e.value) + repr(e.value.detail)


def test_a_bad_value_is_named_by_key_path_and_not_echoed():
    with pytest.raises(ToolError) as e:
        load_config(VALID.replace(TENANT, "sv=2024&sig=LooksLikeACredential"))
    assert "LooksLikeACredential" not in str(e.value) + repr(e.value.detail)


@pytest.mark.parametrize(
    ("old", "new", "key"),
    [
        ("module_chunk = 100", 'module_chunk = "100"', "project.module_chunk"),
        ("module_chunk = 100", "module_chunk = 0", "project.module_chunk"),
        ("module_chunk = 100", "module_chunk = true", "project.module_chunk"),
        ("min_token_minutes = 20", "min_token_minutes = -1", "project.min_token_minutes"),
        ("table_model = false", "table_model = 0", "project.table_model"),
        ("lock_timeout_ms = 10000", "lock_timeout_ms = -1", "env.prod.lock_timeout_ms"),
        ("lock_timeout_ms = 10000", "lock_timeout_ms = 2147483648", "env.prod.lock_timeout_ms"),
        ("lock_timeout_ms = 10000", "lock_timeout_ms = 10.5", "env.prod.lock_timeout_ms"),
        ("applock_wait_s = 600", "applock_wait_s = 2147484", "env.dev.applock_wait_s"),
        ('drift = "block"', 'drift = "ignore"', "env.prod.drift"),
        ('name = "sales"', 'name = ""', "project.name"),
        ('name = "sales"', f'name = "{"x" * 129}"', "project.name"),
        ('objects = ["TABLE:[audit].[Log]"]', 'objects = ["audit.Log"]', "unmanaged.objects[0]"),
        (
            'objects = ["TABLE:[audit].[Log]"]',
            'objects = ["TABLE:[a].[b]", "TABLE:[a].[b]"]',
            "unmanaged.objects[1]",
        ),
        ('objects = ["TABLE:[audit].[Log]"]', 'objects = "TABLE:[a].[b]"', "unmanaged.objects"),
    ],
)
def test_a_value_of_the_wrong_type_or_range_is_refused(old, new, key):
    assert old in VALID
    assert refused_key(VALID.replace(old, new)) == key


def test_an_environment_needs_at_least_one_target():
    start, end = VALID.index("targets = [\n"), VALID.index("[unmanaged]")
    assert refused_key(VALID[:start] + "targets = []\n" + VALID[end:]) == "env.prod.targets"


def test_text_that_is_not_toml_is_refused_as_a_config_error():
    with pytest.raises(ToolError) as e:
        load_config("[project\nname = ")
    assert (e.value.exit_code, e.value.reason_code) == (Exit.REFUSED, "CONFIG_INVALID")


def test_gated_defaults_to_true_only_for_test_preprod_and_prod():
    text = VALID
    for name in ("sandbox", "test", "preprod", "disposable"):
        text += env_block(name, f"t-{name}")
    gated = {name: e.gated for name, e in load_config(text).env.items()}
    assert gated == {
        "dev": False,
        "sandbox": False,
        "test": True,
        "preprod": True,
        "prod": True,
        "disposable": False,
    }


def test_gated_can_be_set_against_the_default_in_both_directions():
    text = VALID.replace('drift = "block"', 'drift = "block"\ngated = false')
    text = text.replace('drift = "report"', 'drift = "report"\ngated = true')
    env = load_config(text).env
    assert (env["dev"].gated, env["prod"].gated) == (True, False)
    assert refused_key(VALID.replace('drift = "block"', 'drift = "block"\ngated = "no"')) == "env.prod.gated"


def test_targets_matrix_gives_one_row_per_target_with_the_client_ids_resolved():
    assert targets_matrix(load_config(VALID), "prod") == [
        {
            "id": "sales-prod",
            "server": "sql-sales-prod.database.windows.net",
            "database": "sales",
            "plan_client_id": PROD_PLAN,
            "deploy_client_id": PROD_DEPLOY,
            "tenant_id": TENANT,
            "gated": True,
        },
        {
            "id": "sales-prod-eu",
            "server": "sql-sales-prod-eu.database.windows.net",
            "database": "sales_eu",
            "plan_client_id": PROD_PLAN,
            "deploy_client_id": PROD_DEPLOY,
            "tenant_id": TENANT,
            "gated": True,
        },
    ]
    dev = targets_matrix(load_config(VALID), "dev")
    assert [(row["id"], row["deploy_client_id"], row["gated"]) for row in dev] == [
        ("sales-dev", NONPROD_DEPLOY, False)
    ]


def test_targets_matrix_refuses_an_environment_that_the_file_does_not_define():
    with pytest.raises(ToolError) as e:
        targets_matrix(load_config(VALID), "sandbox")
    assert (e.value.exit_code, e.value.reason_code) == (Exit.REFUSED, "ENV_NOT_CONFIGURED")


def test_a_command_gets_the_environment_and_the_target_that_env_and_target_name():
    config = load_config(VALID)
    environment, target = resolve_target(config, "prod", "sales-prod-eu")
    assert environment is config.env["prod"]
    assert target == Target("sales-prod-eu", "sql-sales-prod-eu.database.windows.net", "sales_eu")


@pytest.mark.parametrize(
    ("env", "target_id", "reason_code", "detail"),
    [
        ("sandbox", "sales-dev", "ENV_NOT_CONFIGURED", {"environment": "sandbox"}),
        # a target of another environment is not a target of this one
        ("prod", "sales-dev", "TARGET_NOT_CONFIGURED", {"environment": "prod", "target": "sales-dev"}),
        ("dev", "SALES-DEV", "TARGET_NOT_CONFIGURED", {"environment": "dev", "target": "SALES-DEV"}),
    ],
)
def test_an_environment_or_a_target_that_the_file_does_not_hold_is_refused(
    env, target_id, reason_code, detail
):
    with pytest.raises(ToolError) as e:
        resolve_target(load_config(VALID), env, target_id)
    assert (e.value.exit_code, e.value.reason_code, e.value.detail) == (Exit.REFUSED, reason_code, detail)


@pytest.mark.parametrize(
    "server",
    [
        "evil.example.com",
        "x.database.windows.net.evil.com",
        "database.windows.net",
        ".database.windows.net",
        "sql-sales-dev",
        "localhost",
        "xdatabase.windows.net",
    ],
)
def test_config_refuses_server_outside_allowed_suffixes(server):
    """LB-13. The access token of the plan and deploy identity is presented to the server of a
    target, so the name must be an Azure SQL Database name unless the file lists another suffix."""
    assert (
        refused_key(VALID.replace("sql-sales-dev.database.windows.net", server))
        == "env.dev.targets[0].server"
    )


@pytest.mark.parametrize(
    "server",
    [
        "sql-sales-dev.database.windows.net",
        "SQL-Sales-Dev.Database.Windows.NET",
        "sql-sales-dev.privatelink.database.windows.net",
        "sql-sales-dev.database.usgovcloudapi.net",
        "sql-sales-dev.database.chinacloudapi.cn",
    ],
)
def test_the_default_server_suffixes_are_the_azure_sql_database_names(server):
    config = load_config(VALID.replace("sql-sales-dev.database.windows.net", server))
    assert config.env["dev"].targets[0].server == server
    assert config.project.server_suffixes == DEFAULT_SERVER_SUFFIXES


def test_server_suffixes_of_the_project_replace_the_default_list():
    listed = VALID.replace(
        "min_token_minutes = 20", 'min_token_minutes = 20\nserver_suffixes = [".SQL.corp.example"]'
    )
    assert refused_key(listed) == "env.dev.targets[0].server"  # the Azure names are no longer in the list
    own = listed.replace(".database.windows.net", ".sql.corp.example")
    assert load_config(own).project.server_suffixes == (".sql.corp.example",)


@pytest.mark.parametrize(
    "value", ["[]", '[""]', '["example.net"]', '[".net"]', '[".a b.net"]', '".database.windows.net"', "[1]"]
)
def test_a_server_suffix_starts_with_a_dot_and_holds_two_labels(value):
    text = VALID.replace("min_token_minutes = 20", f"min_token_minutes = 20\nserver_suffixes = {value}")
    assert refused_key(text) == "project.server_suffixes"


def test_data_batches_are_off_unless_the_project_switches_them_on():
    """Owner decision: this version manages structural changes. The key is optional and false."""
    assert load_config(VALID).project.data_batches is False
    on = VALID.replace("table_model = false", "table_model = false\ndata_batches = true")
    assert load_config(on).project.data_batches is True
    off = VALID.replace("table_model = false", "table_model = false\ndata_batches = false")
    assert load_config(off).project.data_batches is False
    for value in ('"true"', "1", "0", '"on"'):
        bad = VALID.replace("table_model = false", f"table_model = false\ndata_batches = {value}")
        assert refused_key(bad) == "project.data_batches"


def _without(section: str, until: str | None) -> str:
    """VALID with the section replaced by a scalar of its name, at the top of the file."""
    start = VALID.index(f"[{section}]")
    end = len(VALID) if until is None else VALID.index(f"[{until}]")
    return f"{section} = 1\n" + VALID[:start] + VALID[end:]


@pytest.mark.parametrize(
    ("section", "until"),
    [("project", "identities"), ("identities", "env.dev"), ("unmanaged", "ack"), ("ack", None)],
)
def test_a_section_that_is_not_a_table_is_refused_by_its_key_path(section, until):
    """TQ-11: the branch 'must be a table' had no test. Without it the next line would fail with a
    TypeError or an AttributeError and no key path."""
    with pytest.raises(ToolError) as e:
        load_config(_without(section, until))
    assert (e.value.reason_code, e.value.detail.get("key")) == ("CONFIG_INVALID", section)
    assert "must be a table" in e.value.message


def test_an_environment_or_a_target_that_is_not_a_table_is_refused_by_its_key_path():
    not_a_table = VALID[: VALID.index("[env.dev]")] + "[env]\ndev = 5\n"
    assert refused_key(not_a_table) == "env.dev"
    target = VALID.replace(
        '[{ id = "sales-dev", server = "sql-sales-dev.database.windows.net", database = "sales" }]',
        '["sales-dev"]',
    )
    assert target != VALID
    assert refused_key(target) == "env.dev.targets[0]"


def test_the_template_of_a_database_repository_shows_the_data_switch_and_leaves_it_off():
    template = Path(__file__).resolve().parents[2] / "templates" / "db-repo" / "azsqlcd.toml"
    text = template.read_text(encoding="utf-8")
    assert "\n# data_batches = false\n" in text  # the key is shown, commented, with its default
    assert load_config(text).project.data_batches is False
    switched_on = text.replace("\n# data_batches = false\n", "\ndata_batches = true\n")
    assert load_config(switched_on).project.data_batches is True


def test_a_file_with_a_utf8_bom_or_crlf_line_ends_is_the_same_config():
    """N4-07. Windows PowerShell 5.1 (Set-Content -Encoding UTF8) and older Notepad write a BOM.
    tomllib refuses it with 'Invalid statement (at line 1, column 1)', which names no cause."""
    plain = load_config(VALID)
    assert load_config("﻿" + VALID) == plain
    assert load_config("﻿" + VALID.replace("\n", "\r\n")) == plain
    assert load_config(b"\xef\xbb\xbf".decode("utf-8") + VALID.lstrip("\n")) == plain


def test_only_one_bom_at_the_start_is_removed():
    for text in ("﻿﻿" + VALID, VALID.replace("[project]", "﻿[project]")):
        with pytest.raises(ToolError) as e:
            load_config(text)
        assert (e.value.exit_code, e.value.reason_code) == (Exit.REFUSED, "CONFIG_INVALID")
