"""[env.<name>] not_managed: objects that one environment does not manage yet.

config.for_env puts the patterns into the scope of the commands of that environment, so there the
matching objects are outside the scope, as a pattern of [scope] exclude_objects puts them: not read,
not compared, not baselined, not listed as unmanaged, no drift, no module sent.

This module decides what happens to the pending migrations there. One that touches only objects
that the environment does not manage is set aside for good (set_aside.NOT_MANAGED): the deploy
records it as a migration step with the status not_applied and the note set_aside.skip_note(env),
and it never runs there, also after the pattern went. One that touches managed objects too is
refused (MIXED_NOT_MANAGED). What a migration touches is set_aside.footprint: the object of each
model operation, every name of the text of a data or raw batch.
"""

from __future__ import annotations

from collections.abc import Sequence

from azsqlcd import names, set_aside
from azsqlcd.chain import ChainEntry, Migration
from azsqlcd.config import Scope
from azsqlcd.errors import refused
from azsqlcd.set_aside import SetAside


def _shown(objects: Sequence[tuple[str, str]]) -> list[str]:
    return [f"{names.quote(schema)}.{names.quote(name)}" for schema, name in sorted(objects)]


def skips(pending: Sequence[tuple[ChainEntry, Migration]], scope: Scope | None) -> dict[str, SetAside]:
    """The pending migrations that the environment of the scope skips, by migration id, in the
    order given. Refused (MIXED_NOT_MANAGED): a migration that touches objects that the environment
    manages and objects that it does not. With no not_managed in the scope: nothing."""
    if scope is None or not scope.not_managed:
        return {}
    found: dict[str, SetAside] = {}
    for entry, migration in pending:
        touched = set_aside.footprint(migration).touched
        outside = [item for item in touched if scope.not_managed_of(*item) is not None]
        if not outside:
            continue
        inside = [item for item in touched if item not in outside]
        where = f"[env.{scope.env}] not_managed"
        if inside:
            raise refused(
                "MIXED_NOT_MANAGED",
                f"{entry.file} touches objects that {where} matches ({', '.join(_shown(outside))}) and "
                f"objects that this environment manages ({', '.join(_shown(inside))}). The tool cannot "
                "run a part of a migration. Split it into one migration for each side (withdraw and "
                "replace it), or change not_managed",
                migration=entry.file,
                not_managed=_shown(outside),
                managed=_shown(inside),
            )
        found[entry.file] = SetAside(
            entry.file,
            set_aside.NOT_MANAGED,
            f"it touches only objects that {where} matches: " + ", ".join(_shown(outside)),
        )
    return found
