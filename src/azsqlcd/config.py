"""azsqlcd.toml: the project, its identities, its environments and their targets.

The file holds no secrets. Every key is known and checked: an unknown key is an error, so a typing
mistake cannot fall back to a default without a message. Error messages name the key path and
never print a value.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from azsqlcd import names
from azsqlcd.errors import ToolError, refused

# Promotion order, then the environment of the live tests.
ENVIRONMENTS = ("dev", "sandbox", "test", "preprod", "prod", "disposable")
_GATED_BY_DEFAULT = frozenset({"test", "preprod", "prod"})

_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_TARGET_ID = re.compile(r"[A-Za-z0-9_-]+")
_SERVER = re.compile(r"[A-Za-z0-9.-]+")
_SECRET_KEY = re.compile(r"password|secret|key", re.IGNORECASE)
# The DNS suffixes of Azure SQL Database: the public cloud, US Government, China, and the retired
# German cloud. The access token of the plan and deploy identity is presented to the server of a
# target, so a server outside these names is refused unless [project] server_suffixes lists its suffix.
DEFAULT_SERVER_SUFFIXES = (
    ".database.windows.net",
    ".database.usgovcloudapi.net",
    ".database.chinacloudapi.cn",
    ".database.cloudapi.de",
)
_SUFFIX = re.compile(r"\.[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_INT32_MAX = 2**31 - 1


@dataclass(frozen=True)
class Project:
    name: str  # azsqlcd.meta.project
    tenant_id: str
    table_model: bool
    module_chunk: int
    min_token_minutes: int
    server_suffixes: tuple[str, ...] = DEFAULT_SERVER_SUFFIXES  # every target server ends with one
    # false: a '-- azsqlcd:data' batch in a migration is refused (lint DATA000). This version manages
    # structural changes; data batches are an option that the project switches on.
    data_batches: bool = False


@dataclass(frozen=True)
class Target:
    id: str
    server: str
    database: str


@dataclass(frozen=True)
class Environment:
    name: str
    plan_identity: str  # a name in Config.identities
    deploy_identity: str  # a name in Config.identities
    drift: str  # report | block
    lock_timeout_ms: int
    applock_wait_s: int
    job_timeout_minutes: int
    gated: bool
    targets: tuple[Target, ...]


@dataclass(frozen=True)
class Config:
    project: Project
    identities: dict[str, str]  # identity name -> client id
    env: dict[str, Environment]
    unmanaged_objects: tuple[str, ...]  # object keys
    unmanaged_dependants: tuple[str, ...]  # [ack] unmanaged_dependants, text as written


def _fail(path: str, problem: str) -> ToolError:
    return refused("CONFIG_INVALID", f"azsqlcd.toml: {path}: {problem}", key=path)


def _dict(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(path, "must be a table")
    return value


def _table(
    value: object, path: str, required: Collection[str], optional: Collection[str] = ()
) -> dict[str, Any]:
    """A table that holds every required key and no key outside required and optional."""
    table = _dict(value, path)
    prefix = f"{path}." if path else ""
    for key in table:
        if key not in required and key not in optional:
            raise _fail(prefix + key, "unknown key")
    for key in required:
        if key not in table:
            raise _fail(prefix + key, "is missing")
    return table


def _str(table: dict[str, Any], key: str, path: str) -> str:
    value = table[key]
    if not isinstance(value, str) or not value:
        raise _fail(f"{path}.{key}", "must be a string that is not empty")
    return value


def _printable(table: dict[str, Any], key: str, path: str) -> str:
    """A name that goes into batch text, the setup script and the connection string.

    A line break could start a batch of its own in a client tool that splits a script at GO lines,
    and no name of a project or a database needs a control character.
    """
    value = _str(table, key, path)
    if not value.isprintable():
        raise _fail(f"{path}.{key}", "must not hold a line break or a control character")
    return value


def _bool(table: dict[str, Any], key: str, path: str) -> bool:
    value = table[key]
    if not isinstance(value, bool):
        raise _fail(f"{path}.{key}", "must be true or false")
    return value


def _int(table: dict[str, Any], key: str, path: str, low: int, high: int | None = None) -> int:
    value = table[key]
    # bool is a subclass of int; TOML keeps them apart and so does this check
    if type(value) is not int or value < low or (high is not None and value > high):
        limit = f"{low} or higher" if high is None else f"from {low} to {high}"
        raise _fail(f"{path}.{key}", f"must be a whole number, {limit}")
    return value


def _guid(table: dict[str, Any], key: str, path: str) -> str:
    value = _str(table, key, path)
    if not _GUID.fullmatch(value):
        raise _fail(f"{path}.{key}", "must be a GUID")
    return value


def _strings(table: dict[str, Any], key: str, path: str) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise _fail(f"{path}.{key}", "must be a list of strings that are not empty")
    return tuple(value)


def _reject_secret_keys(value: object, path: str) -> None:
    if isinstance(value, dict):
        for key, inner in value.items():
            here = f"{path}.{key}" if path else key
            if _SECRET_KEY.search(key):
                raise _fail(
                    here,
                    "a key name with 'password', 'secret' or 'key' is refused; the file holds no secrets",
                )
            _reject_secret_keys(inner, here)
    elif isinstance(value, list):
        for i, inner in enumerate(value):
            _reject_secret_keys(inner, f"{path}[{i}]")


def _environment(
    name: str, raw: object, identities: dict[str, str], seen_ids: set[str], suffixes: tuple[str, ...]
) -> Environment:
    path = f"env.{name}"
    required = (
        "plan_identity",
        "deploy_identity",
        "drift",
        "lock_timeout_ms",
        "applock_wait_s",
        "job_timeout_minutes",
        "targets",
    )
    table = _table(raw, path, required, ("gated",))
    for key in ("plan_identity", "deploy_identity"):
        if _str(table, key, path) not in identities:
            raise _fail(f"{path}.{key}", "names an identity that is not in [identities]")
    if _str(table, "drift", path) not in ("report", "block"):
        raise _fail(f"{path}.drift", "must be 'report' or 'block'")
    raw_targets = table["targets"]
    if not isinstance(raw_targets, list) or not raw_targets:
        raise _fail(f"{path}.targets", "must be a list with at least one target")
    targets: list[Target] = []
    for i, item in enumerate(raw_targets):
        tpath = f"{path}.targets[{i}]"
        t = _table(item, tpath, ("id", "server", "database"))
        target = Target(_str(t, "id", tpath), _str(t, "server", tpath), _printable(t, "database", tpath))
        if not _TARGET_ID.fullmatch(target.id):
            raise _fail(f"{tpath}.id", "may hold only letters, digits, '_' and '-'")
        if target.id in seen_ids:
            raise _fail(f"{tpath}.id", f"target id {target.id!r} is used more than once in the file")
        if not _SERVER.fullmatch(target.server):
            raise _fail(f"{tpath}.server", "may hold only letters, digits, '.' and '-'")
        host = target.server.lower()
        if not any(host.endswith(suffix) and len(host) > len(suffix) for suffix in suffixes):
            raise _fail(
                f"{tpath}.server",
                "must end with one of project.server_suffixes (default: the Azure SQL Database names, "
                f"for example {DEFAULT_SERVER_SUFFIXES[0]}); the access token is sent to this server",
            )
        seen_ids.add(target.id)
        targets.append(target)
    return Environment(
        name=name,
        plan_identity=table["plan_identity"],
        deploy_identity=table["deploy_identity"],
        drift=table["drift"],
        # the three limits are formatted into batch text as integers, so they are bounded here
        lock_timeout_ms=_int(table, "lock_timeout_ms", path, 0, _INT32_MAX),
        applock_wait_s=_int(table, "applock_wait_s", path, 0, _INT32_MAX // 1000),
        job_timeout_minutes=_int(table, "job_timeout_minutes", path, 1),
        gated=_bool(table, "gated", path) if "gated" in table else name in _GATED_BY_DEFAULT,
        targets=tuple(targets),
    )


def load_config(text: str) -> Config:
    """Parse and check the text of azsqlcd.toml. Raises ToolError REFUSED CONFIG_INVALID."""
    try:
        # one leading BOM is no content (Windows PowerShell 5.1 and Notepad write it); tomllib refuses it
        doc = tomllib.loads(text.removeprefix("\ufeff"))
    except tomllib.TOMLDecodeError as e:
        raise refused("CONFIG_INVALID", f"azsqlcd.toml is not valid TOML: {e}") from None
    _reject_secret_keys(doc, "")
    _table(doc, "", ("project", "identities", "env"), ("unmanaged", "ack"))

    p = _table(
        doc["project"],
        "project",
        ("name", "tenant_id", "table_model", "module_chunk", "min_token_minutes"),
        ("server_suffixes", "data_batches"),
    )
    suffixes = DEFAULT_SERVER_SUFFIXES
    if "server_suffixes" in p:
        suffixes = tuple(item.lower() for item in _strings(p, "server_suffixes", "project"))
        if not suffixes or not all(_SUFFIX.fullmatch(item) for item in suffixes):
            raise _fail(
                "project.server_suffixes",
                "must be a list of DNS suffixes that start with '.' and hold at least two labels, "
                f"for example {DEFAULT_SERVER_SUFFIXES[0]}",
            )
    project = Project(
        name=_printable(p, "name", "project"),
        tenant_id=_guid(p, "tenant_id", "project"),
        table_model=_bool(p, "table_model", "project"),
        module_chunk=_int(p, "module_chunk", "project", 1),
        min_token_minutes=_int(p, "min_token_minutes", "project", 0),
        server_suffixes=suffixes,
        data_batches=_bool(p, "data_batches", "project") if "data_batches" in p else False,
    )
    if len(project.name) > 128:  # azsqlcd.meta.project is nvarchar(128)
        raise _fail("project.name", "must be at most 128 characters")

    raw_identities = _dict(doc["identities"], "identities")
    identities = {name: _guid(raw_identities, name, "identities") for name in raw_identities}

    env: dict[str, Environment] = {}
    seen_ids: set[str] = set()
    for name, raw in _dict(doc["env"], "env").items():
        if name not in ENVIRONMENTS:
            raise _fail(f"env.{name}", f"an environment name is one of: {', '.join(ENVIRONMENTS)}")
        env[name] = _environment(name, raw, identities, seen_ids, suffixes)

    unmanaged = _strings(
        _table(doc.get("unmanaged", {}), "unmanaged", (), ("objects",)), "objects", "unmanaged"
    )
    for i, key in enumerate(unmanaged):
        try:
            names.parse_object_key(key)
        except ValueError:
            raise _fail(
                f"unmanaged.objects[{i}]", "must be an object key, for example TABLE:[audit].[Log]"
            ) from None
        if key in unmanaged[:i]:
            raise _fail(f"unmanaged.objects[{i}]", "is listed more than once")
    ack = _table(doc.get("ack", {}), "ack", (), ("unmanaged_dependants",))
    return Config(
        project=project,
        identities=identities,
        env=env,
        unmanaged_objects=unmanaged,
        unmanaged_dependants=_strings(ack, "unmanaged_dependants", "ack"),
    )


def resolve_target(config: Config, env: str, target_id: str) -> tuple[Environment, Target]:
    """The environment and the target that a command names with --env and --target.

    Raises ToolError REFUSED: ENV_NOT_CONFIGURED (azsqlcd.toml has no [env.<env>]),
    TARGET_NOT_CONFIGURED (that environment has no target with the id).
    """
    environment = config.env.get(env)
    if environment is None:
        raise refused("ENV_NOT_CONFIGURED", f"azsqlcd.toml has no [env.{env}]", environment=env)
    target = next((t for t in environment.targets if t.id == target_id), None)
    if target is None:
        raise refused(
            "TARGET_NOT_CONFIGURED",
            f"azsqlcd.toml: [env.{env}] has no target with the id {target_id!r}",
            environment=env,
            target=target_id,
        )
    return environment, target


def targets_matrix(config: Config, env: str) -> list[dict[str, str | bool]]:
    """One row per target of the environment, for the workflow matrix. Client ids are resolved."""
    environment = config.env.get(env)
    if environment is None:
        raise refused("ENV_NOT_CONFIGURED", f"azsqlcd.toml has no [env.{env}]", environment=env)
    return [
        {
            "id": target.id,
            "server": target.server,
            "database": target.database,
            "plan_client_id": config.identities[environment.plan_identity],
            "deploy_client_id": config.identities[environment.deploy_identity],
            "tenant_id": config.project.tenant_id,
            "gated": environment.gated,
        }
        for target in environment.targets
    ]
