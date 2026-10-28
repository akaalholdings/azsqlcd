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

from collections.abc import Collection, Sequence

from azsqlcd import modules, names, set_aside
from azsqlcd.chain import ChainEntry, Migration
from azsqlcd.config import Scope
from azsqlcd.errors import refused
from azsqlcd.model import fold
from azsqlcd.modules import ModuleFile
from azsqlcd.set_aside import SetAside


def _shown(objects: Sequence[tuple[str, str]]) -> list[str]:
    return [f"{names.quote(schema)}.{names.quote(name)}" for schema, name in sorted(objects)]


def _touched(migration: Migration) -> list[tuple[str, str]]:
    """set_aside.footprint, and the object that a raw batch names in its directive."""
    found = set(set_aside.footprint(migration).touched)
    for batch in migration.batches:
        if batch.kind == "raw" and batch.raw_object:
            _, schema, name = names.parse_object_key(batch.raw_object)
            if schema is not None:
                found.add((fold(schema), fold(name)))
    return sorted(found)


def only_not_managed(migration: Migration, scope: Scope | None) -> bool:
    """The migration touches objects, and the patterns of [env.<name>] not_managed match each one:
    skips() skips it in that environment."""
    if scope is None or not scope.not_managed:
        return False
    touched = _touched(migration)
    return bool(touched) and all(scope.not_managed_of(*item) is not None for item in touched)


def skips(
    pending: Sequence[tuple[ChainEntry, Migration]], scope: Scope | None, held: Collection[str] = ()
) -> dict[str, SetAside]:
    """The pending migrations that the environment of the scope skips, by migration id, in the
    order given. Refused (MIXED_NOT_MANAGED): a migration that touches objects that the environment
    manages and objects that it does not. held: the ids of the migrations that a product holds here.
    Such a migration does not run here, so it is not refused while it is held; when it touches only
    objects that the environment does not manage, it is skipped for good all the same. With no
    not_managed in the scope: nothing."""
    if scope is None or not scope.not_managed:
        return {}
    found: dict[str, SetAside] = {}
    for entry, migration in pending:
        touched = _touched(migration)
        outside = [item for item in touched if scope.not_managed_of(*item) is not None]
        if not outside:
            continue
        inside = [item for item in touched if item not in outside]
        where = f"[env.{scope.env}] not_managed"
        if inside and entry.file in held:
            continue
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


def hidden_schemas(scope: Scope | None, keys: Collection[str]) -> frozenset[str]:
    """The case-folded schemas of which the patterns of [env.<name>] not_managed match every object
    of the keys (the objects of the release), when there is one. Such a schema is not managed in the
    environment either: the migration that creates it touches only its objects and is skipped there,
    so the schema is not asked for by the plan or by baseline. With no not_managed: none."""
    if scope is None or not scope.not_managed:
        return frozenset()
    matched: dict[str, bool] = {}
    for key in keys:
        _, schema, name = names.parse_object_key(key)
        if schema is not None:
            folded = fold(schema)
            matched[folded] = matched.get(folded, True) and scope.not_managed_of(schema, name) is not None
    return frozenset(schema for schema, every in matched.items() if every)


def notes(pending: Sequence[Migration], sent: Sequence[ModuleFile], scope: Scope | None) -> list[str]:
    """Plan notes of what runs in the environment and that the patterns do not stop: a data or raw
    batch, whose names in dynamic SQL the tool cannot see, and a migration or a module to send that
    names an object that the patterns match (a view of it, a foreign key to it). The tool does not
    know such an object here, so the statement can fail at deploy. With no not_managed: none."""
    if scope is None or not scope.not_managed:
        return []
    where = f"[env.{scope.env}] not_managed"
    found: list[str] = []
    for migration in pending:
        for kind in dict.fromkeys(set_aside.footprint(migration).unproven):
            if kind != "model":
                found.append(
                    f"not_managed not proven for a {kind} batch: {migration.file} (only the names of its "
                    f"text are compared with {where})"
                )
    named = [(m.file, set_aside.footprint(m).named) for m in pending]
    named += [(module.key, frozenset(modules.named_objects(module.text))) for module in sent]
    for what, objects in named:
        outside = [item for item in sorted(objects) if scope.not_managed_of(*item) is not None]
        if outside:
            found.append(
                f"not_managed: {what} names {', '.join(_shown(outside))}, which {where} matches: the tool "
                "does not know that object here"
            )
    return found
