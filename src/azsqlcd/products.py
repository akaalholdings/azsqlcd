"""Products: named groups of objects of one database, and the environments that their changes reach.

[products.<name>] of azsqlcd.toml names the objects of a product with patterns schema.name, as
[scope] exclude_objects does. An object belongs to at most one product (lint PRODUCT_OVERLAP); an
object that no product names is shared. A product with promote_to is held in every environment
that comes after promote_to in the order of config.ENVIRONMENTS.

In an environment where a product is held, a pending migration that touches an object of the
product is set aside with the reason `held` (set_aside.py: it does not run, the releases after it
deploy past it, and what runs must be independent of it). A module of the product is not sent and
not dropped there. When a later release raises or removes promote_to, the held migrations are
pending again and run in chain order with that release (the catch-up past set-aside migrations).

Nothing of this is stored: a hold is a fact of azsqlcd.toml of the release and the environment.
Opt-in: without [products], hold() returns None and the planner is as before.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from azsqlcd import chain, modules, names, set_aside
from azsqlcd.chain import Migration
from azsqlcd.config import ENVIRONMENTS, Config, Product
from azsqlcd.errors import ToolError, refused
from azsqlcd.modules import ModuleFile
from azsqlcd.set_aside import Aside, SetAside

_MIGRATIONS_DIR = "migrations/"


def product_of(config: Config, schema: str, name: str) -> Product | None:
    """The product of an object: the first one, in name order, whose patterns match it."""
    return next((p for p in config.products if p.matches(schema, name)), None)


def product_of_key(config: Config, key: str) -> Product | None:
    """The product of an object key; None for a schema key and for a shared object."""
    _, schema, name = names.parse_object_key(key)
    return None if schema is None else product_of(config, schema, name)


def overlapping(config: Config, schema: str, name: str) -> list[str]:
    """The names of every product that matches the object; more than one is PRODUCT_OVERLAP."""
    return [p.name for p in config.products if p.matches(schema, name)]


def is_held(product: Product, env: str) -> bool:
    """The changes of the product do not reach env: env comes after promote_to."""
    if product.promote_to is None or env not in ENVIRONMENTS:
        return False
    return ENVIRONMENTS.index(env) > ENVIRONMENTS.index(product.promote_to)


def touched_products(config: Config, migration: Migration) -> tuple[list[str], list[tuple[str, str]]]:
    """(the products that the migration touches, in name order; the shared objects it touches).
    What a migration touches is set_aside.footprint: the same rule as the independence check."""
    found: set[str] = set()
    shared: list[tuple[str, str]] = []
    for schema, name in sorted(set_aside.footprint(migration).touched):
        product = product_of(config, schema, name)
        if product is None:
            shared.append((schema, name))
        else:
            found.add(product.name)
    return sorted(found), shared


def mixed_hold(config: Config, migration: Migration) -> str | None:
    """Why a new migration mixes the objects of a product with promote_to and other objects, so that
    it would be held, or run, as a whole where only a part should be; None when it does not."""
    touched, shared = touched_products(config, migration)
    by_name = {p.name: p for p in config.products}
    for name in touched:
        if by_name[name].promote_to is None:
            continue
        others = [f"product {other}" for other in touched if other != name]
        others += [f"{names.quote(s)}.{names.quote(n)}" for s, n in shared]
        if others:
            return (
                f"{migration.file} touches objects of the product {name}, which has promote_to = "
                f'"{by_name[name].promote_to}", and also {", ".join(others)}. A held migration is held '
                "as a whole: put the changes of the product in a migration of their own (gen in two steps, "
                "or two pull requests)"
            )
    return None


@dataclass(frozen=True)
class Hold:
    """The holds of one environment, from azsqlcd.toml of the release and the files of the release."""

    config: Config
    env: str
    held: frozenset[str]  # the names of the held products
    migration_product: Mapping[str, str] = field(default_factory=dict)  # chain migration -> product
    held_migrations: Mapping[str, Migration] = field(default_factory=dict)  # migration id -> parsed file

    def holds(self, key: str) -> Product | None:
        """The held product of an object key; None when the object is shared or its product is not held."""
        product = product_of_key(self.config, key)
        return product if product is not None and product.name in self.held else None

    def merge(self, aside: Aside) -> Aside:
        """The set-aside facts of the database with the held migrations of this environment. A
        migration of a product that is not held here (any more) counts as set aside before
        (Aside.released), so it runs at its place in the chain; not so in the first environment,
        where nothing is ever held."""
        active: dict[str, SetAside] = dict(aside.active)
        by_name = {p.name: p for p in self.config.products}
        for file in self.held_migrations:
            product = by_name[self.migration_product[file]]
            why = f'product {product.name}, promote_to = "{product.promote_to}"'
            active.setdefault(file, SetAside(file, set_aside.HELD, why))
        released = set(aside.released)
        if self.env != ENVIRONMENTS[0]:
            released |= {file for file in self.migration_product if file not in self.held_migrations}
        return Aside(active, frozenset(released))

    def notes(
        self,
        pending: Sequence[Migration],
        held: Sequence[str],
        sent: Collection[str],
        held_modules: Collection[str],
    ) -> list[str]:
        """The plan notes: what of each product this plan holds, and what is held here.
        pending: the migrations that run; held: the ids of the migrations held; sent: the keys of the
        module changes and drops that are sent; held_modules: those that are not."""
        counts: dict[str, list[int]] = {}
        for file in [m.file for m in pending] + list(held):
            if file in self.migration_product:
                counts.setdefault(self.migration_product[file], [0, 0])[0] += 1
        for key in (*sent, *held_modules):
            product = product_of_key(self.config, key)
            if product is not None:
                counts.setdefault(product.name, [0, 0])[1] += 1
        out: list[str] = []
        if counts:
            parts = [
                f"{name} ({_count(n, 'migration')}, {_count(m, 'module')})"
                for name, (n, m) in sorted(counts.items())
            ]
            out.append(f"products: {', '.join(parts)}")
        by_name = {p.name: p for p in self.config.products}
        for name in sorted(self.held):
            items = [file for file in held if self.migration_product.get(file) == name]
            items += sorted(key for key in held_modules if getattr(self.holds(key), "name", None) == name)
            if items:
                promote_to = by_name[name].promote_to
                out.append(f'held in {self.env}: {name} (promote_to = "{promote_to}"): {", ".join(items)}')
        return out


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _chain_migrations(files: Mapping[str, bytes]) -> list[Migration]:
    """The migrations of the chain of the release that are not withdrawn and that read. A file that
    does not read is refused by the planner when it is pending, with its own reason code."""
    if chain.SUM_PATH not in files:
        return []
    found: list[Migration] = []
    for entry in chain.parse_sum(files[chain.SUM_PATH].decode("utf-8")).entries:
        data = files.get(_MIGRATIONS_DIR + entry.file)
        if entry.withdrawn or data is None:
            continue
        try:
            found.append(chain.parse_migration(data.decode("utf-8"), entry.file))
        except (ToolError, UnicodeDecodeError):
            continue
    return found


def hold(config: Config, env: str, files: Mapping[str, bytes]) -> Hold | None:
    """The holds of env for a release (files: path -> bytes). None without [products]."""
    if not config.products:
        return None
    held = frozenset(p.name for p in config.products if is_held(p, env))
    product_by: dict[str, str] = {}
    held_migrations: dict[str, Migration] = {}
    for migration in _chain_migrations(files):
        touched, _ = touched_products(config, migration)
        if not touched:
            continue
        product_by[migration.file] = next((name for name in touched if name in held), touched[0])
        if any(name in held for name in touched):
            held_migrations[migration.file] = migration
    return Hold(config, env, held, product_by, held_migrations)


def check_held_modules(
    hold: Hold,
    pending: Iterable[Migration],
    sent: Iterable[ModuleFile],
    absent: Collection[str],
) -> None:
    """Refuse (SET_ASIDE_DEPENDENCY) a pending migration or a module to send that names a held
    module that this database does not hold. absent: the keys of the held modules with no managed
    row here."""
    if not absent:
        return
    wanted = {_fold_key(key): key for key in absent}
    texts = [(m.file, batch.text) for m in pending for batch in m.batches]
    texts += [(module.key, module.text) for module in sorted(sent, key=lambda m: m.key)]
    for who, text in texts:
        shared = sorted(set(wanted) & modules.named_objects(text))
        if shared:
            key = wanted[shared[0]]
            product = hold.holds(key)
            assert product is not None
            raise refused(
                "SET_ASIDE_DEPENDENCY",
                f"{who} names {key}, a module of the product {product.name}, which is held in {hold.env} "
                f'(promote_to = "{product.promote_to}") and is not in this database. Either raise '
                f"promote_to of [products.{product.name}] by pull request, or take {who} out of the release",
                blocked=who,
                set_aside=key,
                reason=set_aside.HELD,
                object=key,
            )


def _fold_key(key: str) -> tuple[str, str]:
    _, schema, name = names.parse_object_key(key)
    return (schema or "").casefold(), name.casefold()


# ------------------------------------------------------------------ the command `products`
def object_files(config: Config, files: Mapping[str, bytes]) -> dict[str, list[str]]:
    """product name -> the paths of its object files, sorted. A file is read by its name
    (<schema>.<name>.sql), as the layout of a database repository names it."""
    found: dict[str, list[str]] = {p.name: [] for p in config.products}
    for path in sorted(files):
        try:
            kind, stem = names.key_for_path(path)
        except ValueError:
            continue
        if kind == "SCHEMA" or "." not in stem:
            continue
        schema, name = stem.split(".", 1)
        product = product_of(config, schema, name)
        if product is not None:
            found[product.name].append(path)
    return found


def codeowners(config: Config, files: Mapping[str, bytes]) -> list[str]:
    """CODEOWNERS lines for the object files of each product that has owners. One glob for a
    pattern of the product when the files of a folder that the glob matches are exactly files of
    the product (CODEOWNERS compares with letter case, so the glob must match the names as they
    are); one line for each other file."""
    by_product = object_files(config, files)
    every = sorted(files)
    lines: list[str] = []
    for product in config.products:
        if not product.owners:
            continue
        mine = by_product[product.name]
        left = set(mine)
        owners = " ".join(product.owners)
        folders = sorted({path.rsplit("/", 1)[0] for path in mine})
        for folder in folders:
            for pattern in product.objects:
                glob = f"{folder}/{pattern}.sql"
                matched = {
                    path
                    for path in every
                    if path.rsplit("/", 1)[0] == folder and fnmatch.fnmatchcase(path, glob)
                }
                in_folder = {path for path in mine if path.rsplit("/", 1)[0] == folder}
                if matched and matched <= in_folder and matched & left and _single(pattern):
                    lines.append(f"/{glob} {owners}")
                    left -= matched
        lines += [f"/{path} {owners}" for path in sorted(left)]
    return lines


def _single(pattern: str) -> bool:
    """A pattern whose * and ? CODEOWNERS reads as fnmatch does: no bracket and no slash."""
    return not any(c in pattern for c in "[]/\\")
