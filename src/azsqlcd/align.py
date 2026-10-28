"""azsqlcd align: the script that makes a database equal to the files of the repository. Read-only.

The tool runs none of it. A DBA reviews onboarding/<env>/align.sql and runs it in a maintenance
window; then `azsqlcd baseline` must pass. The two models are those that the other commands use:
the database as catalog_tables.read_model reads it under [scope] (or the one schema of --schema, as
export reads it), and the files as gen and verify read them (gen.load_model), cut to the same scope.

Sections of align.sql, in the order in which they run:
  1. Constraint names: sp_rename of each constraint that the engine named and that a file holds in
     the same shape (catalog_tables.rename_constraints_sql). The statements that follow use the
     names of the files, so the renames come first.
  2. Table-class objects: the operations of diff.diff (the engine of gen, in its order). A change
     that diff refuses gets no statement; the object is listed under "Hand-write" with the code
     and the reason, and is left as the database has it. So is a file for the name of an external
     table or of a history table. An object that only the database has is never dropped. A
     statement that loses data, or that unmasks a column, is written commented out, after a line
     "-- REVIEW: loses data: <why>" or "-- REVIEW: unmasks column ...".
  3. Modules: the file text of each module that differs from its file or is missing, in the order
     of a deploy. A module is never dropped.
  4. Constraint state: WITH CHECK CHECK CONSTRAINT for a CHECK or FOREIGN KEY constraint that is
     disabled or not trusted, ALTER INDEX ... REBUILD for a disabled index or key, each after a
     comment with the row count of its table. Every rebuild comes first (a foreign key cannot be
     enabled while the index of the key that it references is disabled), smallest table first and
     the clustered index first in its table; then the checks, smallest table first. A constraint or
     an index that section 2 creates again is not listed: it is created enabled and trusted.

The objects of a product that is held in the environment ([products], products.hold) are not
compared, present or not: the held migration makes their change when promote_to is raised, and it
fails if a DBA made it before. align.md lists them under "Held in <env>". A module that names a held
object that the database lacks, or holds in another shape than its file, is not sent: Hand-write.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace

from azsqlcd import (
    catalog,
    catalog_tables,
    diff,
    emit,
    gen,
    modules,
    names,
    onboard,
    plan,
    products,
    replay,
    tables,
)
from azsqlcd.catalog_tables import SystemNamed
from azsqlcd.config import Config, Product, for_env, in_scope
from azsqlcd.model import (
    ABSENT_IN_OTHER,
    ABSENT_IN_SELF,
    AddConstraint,
    AlterColumn,
    Check,
    CreateIndex,
    DropColumn,
    DropConstraint,
    DropIndex,
    ForeignKey,
    Model,
    Operation,
    PrimaryKey,
    Table,
    Unique,
    UnmaskColumn,
    fold,
)
from azsqlcd.session import Session

NOTHING = "nothing to align"
NO_STATEMENT = "no statement: see Hand-write in align.md"
SECTIONS = ("1. Constraint names", "2. Table-class objects", "3. Modules", "4. Constraint state")
# diff.classify codes of a statement that can lose rows or values
_LOSES = {
    "DROP_TABLE": "drops the table",
    "DROP_COLUMN": "drops column",
    "DROP_SEQUENCE": "drops the sequence",
    "DROP_TYPE": "drops the type",
    "ALTER_COLUMN_LOSSY": "gets a type",
}


@dataclass(frozen=True)
class Item:
    object_key: str
    note: str


@dataclass(frozen=True)
class Statement:
    object_key: str
    sql: str  # one statement, ending with ';'
    loses: str | None = None  # why it can lose data; then it is written commented out
    unmasks: str | None = None  # the column that it unmasks; then it is written commented out

    @property
    def review(self) -> str | None:
        """The text of its '-- REVIEW:' line; None when the statement runs as it is."""
        return f"loses data: {self.loses}" if self.loses is not None else self.unmasks


@dataclass(frozen=True)
class AlignResult:
    renames: tuple[SystemNamed, ...]  # fixed_name: the name of the file
    statements: tuple[Statement, ...]
    differs: tuple[Item, ...]  # table-class objects that differ, and the property paths
    modules: tuple[modules.ModuleFile, ...]  # in deploy order
    module_items: tuple[Item, ...]
    states: tuple[tuple[str, int, str], ...]  # (table key, row count, statement), in run order
    hand_write: tuple[Item, ...]
    only_here: tuple[Item, ...]
    held: tuple[Item, ...]  # the objects of a product that is held here: not aligned
    sql: str  # onboarding/<env>/align.sql
    md: str  # onboarding/<env>/align.md

    @property
    def counts(self) -> str:
        return (
            f"{len(self.renames)} rename(s), {len(self.statements)} statement(s), {len(self.modules)} "
            f"module(s), {len(self.states)} constraint state statement(s), {len(self.hand_write)} to "
            f"hand-write, {len(self.only_here)} only in the database, {len(self.held)} held"
        )


def _renamed(table: Table, old: str, new: str) -> Table:
    """The table with the constraint or DEFAULT of name old under the name new."""
    constraints = tuple(
        replace(c, name=new) if c.name is not None and fold(c.name) == fold(old) else c
        for c in table.constraints
    )
    columns = tuple(
        replace(c, default=replace(c.default, name=new))
        if c.default is not None and c.default.name is not None and fold(c.default.name) == fold(old)
        else c
        for c in table.columns
    )
    return replace(table, columns=columns, constraints=constraints)


def _table_of(op: Operation) -> str | None:
    table = getattr(op, "table", None)
    schema = getattr(op, "schema", None)
    if isinstance(table, str) and isinstance(schema, str):
        return names.object_key("TABLE", schema, table)
    return None


def _key_of(op: Operation) -> str:
    found = _table_of(op)
    if found is not None:
        return found
    keys = diff.touched_objects([op])
    return keys[0] if keys else "-"


def _loses(op: Operation, codes: list[str]) -> str | None:
    found = [code for code in codes if code in _LOSES]
    if not found:
        return None
    of = names.qualified(op.schema, op.table) if isinstance(op, DropColumn | AlterColumn) else ""
    if isinstance(op, DropColumn):
        return f"drops column {names.quote(op.column)} of {of}"
    if isinstance(op, AlterColumn):
        return (
            f"column {names.quote(op.column)} of {of} gets a type that may not hold every value "
            "(shorter, another type or another collation)"
        )
    return _LOSES[found[0]]


def _unmasks(op: Operation, codes: list[str]) -> str | None:
    if "UNMASK" not in codes or not isinstance(op, UnmaskColumn):
        return None
    return f"unmasks column {names.quote(op.column)} of {names.qualified(op.schema, op.table)}"


@dataclass
class _Tables:
    renames: list[SystemNamed]
    statements: list[Statement]
    differs: list[Item]
    hand: list[Item]
    only: list[Item]
    head: Model  # the files, with what stays as the database has it
    declared_of: dict[str, str]  # case-folded '<table key>.<name as read>' -> declared name
    recreated: set[tuple[str, str]]  # case-folded (table key, name) that a statement drops or adds
    held: list[Item] = field(default_factory=list)


def _tables(
    live_read: catalog_tables.CatalogRead, head: Model, held: Collection[str] = frozenset()
) -> _Tables:
    """held: the case-folded keys of the objects of a held product. They are not compared: neither
    the database nor the files give a statement for them."""
    live = Model(obj for obj in live_read.model.values() if fold(obj.key) not in held)
    hand: list[Item] = []
    unsupported = {fold(u.object_key): u for u in live_read.unsupported}
    external = {fold(key) for key in live_read.external_tables}
    history = {fold(key): current for key, current in live_read.history_tables}
    kept = []
    for obj in head.values():
        key = fold(obj.key)
        if key in unsupported:
            hand.append(Item(obj.key, f"{unsupported[key].code}: {unsupported[key].reason}"))
        elif key in external:
            hand.append(Item(obj.key, "an external table holds this name"))
        elif key in history:
            hand.append(Item(obj.key, f"the history table of {history[key]} holds this name"))
        else:
            kept.append(obj)
    head = Model(kept)
    only = [
        Item(u.object_key, f"{u.code}: {u.reason}")
        for u in live_read.unsupported
        if u.object_key not in head and fold(u.object_key) not in held
    ]

    # the live model holds a fixed name for each engine-named constraint; give it the declared
    # name (after the rename) or its live name (no file holds it: a statement names it so). A
    # table with no file gives no declared name: it stays as the database has it
    by_table: dict[str, list[SystemNamed]] = {}
    for item in live_read.system_named:
        by_table.setdefault(fold(item.table_key), []).append(item)
    renames: list[SystemNamed] = []
    declared_of: dict[str, str] = {}
    named: dict[str, Table] = {}
    for key, items in by_table.items():
        table = live.get(items[0].table_key)
        declared = head.get(items[0].table_key)
        if not isinstance(table, Table):
            continue
        matched = dict(tables.declared_names(table, declared, items)) if declared is not None else {}
        for item in items:
            name = matched.get(item)
            if name is not None:
                renames.append(replace(item, fixed_name=name))
                declared_of[fold(f"{item.table_key}.{item.fixed_name}")] = name
            table = _renamed(table, item.fixed_name, name if name is not None else item.live_name)
        named[key] = table
    # each model is built once: Model.add and Model.replace build the whole model again
    live = Model(named.get(fold(obj.key), obj) for obj in live.values())
    extra = [live[key] for key in live if key not in head]
    only += [Item(obj.key, "no file") for obj in extra]
    head = Model([*head.values(), *extra])  # never dropped: it stays as the database has it

    refused: set[str] = set()
    while True:
        try:
            ops = diff.diff(live, head)
            break
        except diff.GenRefused as error:
            progress = False
            for refusal in error.refusals:
                hand.append(Item(refusal.object_key, f"{refusal.code}: {refusal.message}. {refusal.hint}"))
                key = refusal.object_key
                if key in head and fold(key) not in refused:
                    refused.add(fold(key))
                    head = head.replace(live[key]) if key in live else head.remove(key)
                    progress = True
            if not progress:
                raise RuntimeError(f"align: a refusal names no object of the files: {error}") from None

    statements: list[Statement] = []
    state = live
    for op in ops:
        statements.append(
            Statement(
                _key_of(op),
                emit.emit_operation(op),
                _loses(op, codes := diff.classify(op, state)),
                _unmasks(op, codes),
            )
        )
        state = replay.apply(state, op)
    paths: dict[str, list[str]] = {}
    for key, path in live.diff_paths(head, expressions=False):
        # self is the database, other is the files
        shown = path.replace(ABSENT_IN_OTHER, "(not in the file)").replace(
            ABSENT_IN_SELF, "(not in the database)"
        )
        paths.setdefault(key, []).append(shown)
    differs = [Item(key, ", ".join(found)) for key, found in paths.items()]
    recreated: set[tuple[str, str]] = set()
    for op in ops:
        table = _table_of(op)
        name = (
            op.constraint.name
            if isinstance(op, AddConstraint)
            else op.index.name
            if isinstance(op, CreateIndex)
            else op.name
            if isinstance(op, DropConstraint | DropIndex)
            else None
        )
        if table is not None and name is not None:
            recreated.add((fold(table), fold(name)))
    return _Tables(renames, statements, differs, hand, only, head, declared_of, recreated)


def _states(
    session: Session,
    read: catalog_tables.CatalogRead,
    head: Model,
    declared_of: Mapping[str, str],
    recreated: set[tuple[str, str]],
    filed: Collection[str],
) -> tuple[list[tuple[str, int, str]], list[Item]]:
    """The constraint state statements in run order, and the constraints that keep their state.
    filed: the case-folded keys of the objects that have a file; no other table is looked at."""
    found: list[tuple[str, int, str]] = []  # (table key, rank, statement)
    notes: list[Item] = []
    for key, capture in read.captures.items():
        table = head.get(key)
        if not isinstance(table, Table) or fold(key) not in filed:
            continue
        wanted = {fold(c.name): c for c in table.constraints if c.name is not None}
        indexes = {fold(i.name) for i in table.indexes}
        clustered = table.clustered_item()
        for label, value in sorted(capture.items()):
            if not isinstance(value, dict) or not label.startswith(("constraint [", "index [")):
                continue
            name = label.split(" ", 1)[1][1:-1].replace("]]", "]")
            name = declared_of.get(fold(f"{key}.{name}"), name)
            if (fold(key), fold(name)) in recreated:
                continue
            target = names.qualified(table.schema, table.name)
            constraint = wanted.get(fold(name))
            if label.startswith("constraint [") and isinstance(constraint, Check | ForeignKey):
                if not (value.get("is_disabled") or value.get("is_not_trusted")):
                    continue
                if constraint.not_for_replication:
                    notes.append(
                        Item(
                            key,
                            f"{names.quote(name)} is NOT FOR REPLICATION: the engine keeps it not trusted",
                        )
                    )
                    continue
                found.append(
                    (key, 2, f"ALTER TABLE {target} WITH CHECK CHECK CONSTRAINT {names.quote(name)};")
                )
            elif value.get("is_disabled") and (
                (label.startswith("index [") and fold(name) in indexes)
                or isinstance(constraint, PrimaryKey | Unique)
            ):
                # no other index of a table can be rebuilt while its clustered index is disabled
                first = clustered is not None and fold(clustered.name or "") == fold(name)
                found.append(
                    (key, 0 if first else 1, f"ALTER INDEX {names.quote(name)} ON {target} REBUILD;")
                )
    facts = catalog.table_facts(session, sorted({key for key, _, _ in found})) if found else {}
    rows = {fold(key): fact.rows for key, fact in facts.items()}
    # every rebuild before every check: a foreign key cannot be enabled while the index of the key
    # that it references (in any table) is disabled
    ordered = sorted(found, key=lambda s: (s[1] == 2, rows.get(fold(s[0]), 0), fold(s[0]), s[1], s[2]))
    notes.sort(key=lambda item: (fold(item.object_key), item.note))
    return [(key, rows.get(fold(key), 0), sql) for key, _, sql in ordered], notes


def align(
    session: Session,
    config: Config,
    files: Mapping[str, bytes],
    env: str,
    target_id: str,
    schema: str | None = None,
) -> AlignResult:
    """What makes this database equal to the files (path -> bytes of the working tree). Read-only.

    Raises ToolError REFUSED before any batch: OUT_OF_SCOPE (as export --schema), MODEL_INVALID,
    MODULE_INVALID; ORD004 (a module cycle) from the deploy order.
    """
    config = for_env(config, env)  # [env.<name>] not_managed: outside the scope, never read or written
    scope = onboard.export_scope(config, schema)
    in_files = plan.module_files(files)
    table_model = config.project.table_model
    head = (
        Model(obj for obj in gen.load_model(files).values() if in_scope(scope, obj.key))
        if table_model
        else Model()
    )

    # a held product (products.hold): its objects are right here as they are, at the level before
    # the held changes. A statement that makes a held change, or creates a held object, would make
    # the held migration fail when promote_to is raised. So none of them is compared or aligned
    hold = products.hold(config, env, files) if config.products else None
    held: dict[str, tuple[str, Product]] = {}
    if hold is not None:
        for key in [*(key for key in in_files if in_scope(scope, key)), *head]:
            product = hold.holds(key)
            if product is not None:
                held[fold(key)] = (key, product)
    files_head = head
    head = Model(head[key] for key in head if fold(key) not in held)

    found = _Tables([], [], [], [], [], head, {}, set())
    states: list[tuple[str, int, str]] = []
    # the held objects that this database lacks, or holds in another shape than the file: a module
    # that names one is not sent (plan: SET_ASIDE_DEPENDENCY)
    changed: dict[tuple[str, str], str] = {}
    if table_model:
        read = catalog_tables.read_model(session, scope=scope)
        found = _tables(read, head, held)
        filed = {fold(key) for key in head}
        states, notes = _states(session, read, found.head, found.declared_of, found.recreated, filed)
        found.hand += notes
        for key in files_head:
            live = read.model.get(key)
            if fold(key) in held and (
                live is None or Model([live]).diff_paths(Model([files_head[key]]), expressions=False)
            ):
                changed[_named(key)] = key
    hand, only = found.hand, found.only

    compared = onboard.compare_modules(session, files, env, scope)
    for item in compared:
        if fold(item.object_key) in held and item.status in (onboard.DIFFERS, onboard.MISSING_HERE):
            changed[_named(item.object_key)] = item.object_key
    module_items: list[Item] = []
    send: list[modules.ModuleFile] = []
    for item in compared:
        if fold(item.object_key) in held:
            continue
        if item.status == onboard.ONLY_HERE:
            only.append(Item(item.object_key, item.note or "no file"))
        elif item.status == onboard.MISSING_HERE and item.note:
            hand.append(Item(item.object_key, item.note))
        elif item.status in (onboard.DIFFERS, onboard.MISSING_HERE):
            module = in_files[item.object_key]
            named = sorted(set(changed) & modules.named_objects(module.text))
            if named:
                key, product = held[fold(changed[named[0]])]
                hand.append(
                    Item(
                        item.object_key,
                        f"names {key} of the product {product.name}, held in {env} "
                        f'(promote_to = "{product.promote_to}")',
                    )
                )
                continue
            send.append(module)
            note = (
                "missing" if item.status == onboard.MISSING_HERE else "the definition differs from the file"
            )
            if item.note.startswith("the text is the text of the file"):
                note = item.note
            module_items.append(Item(item.object_key, note))
    found.held = [
        Item(key, f'product {product.name}, promote_to = "{product.promote_to}": not aligned')
        for key, product in sorted(held.values(), key=lambda pair: fold(pair[0]))
    ]
    order, _ = modules.deploy_order(send, modules.build_edges(send, ()))
    by_key = {module.key: module for module in send}
    ordered = tuple(by_key[key] for key in order)
    return _result(env, target_id, schema, found, ordered, module_items, states)


def _named(key: str) -> tuple[str, str]:
    """The case-folded (schema, name) of an object key, as modules.named_objects gives it."""
    _, schema, name = names.parse_object_key(key)
    return (schema or "").casefold(), name.casefold()


# ------------------------------------------------------------------ the files
def _commented(sql: str) -> str:
    return "\n".join(f"-- {line}" for line in sql.splitlines())


def _script(
    found: _Tables, ordered: tuple[modules.ModuleFile, ...], states: list[tuple[str, int, str]]
) -> str:
    parts: list[str] = []
    renames = catalog_tables.rename_constraints_sql(found.renames)
    if renames:
        parts.append(f"-- {SECTIONS[0]}\n{renames}")
    if found.statements:
        lines = [f"-- {SECTIONS[1]}\n"]
        for statement in found.statements:
            if statement.review is None:
                lines.append(f"{statement.sql}\nGO\n")
            else:
                lines.append(f"-- REVIEW: {statement.review}\n{_commented(statement.sql)}\nGO\n")
        parts.append("".join(lines))
    if ordered:
        parts.append(f"-- {SECTIONS[2]}\n" + "".join(f"{module.text.rstrip()}\nGO\n" for module in ordered))
    if states:
        parts.append(
            f"-- {SECTIONS[3]}\n"
            + "".join(f"-- {key.partition(':')[2]}: {count} rows\n{sql}\nGO\n" for key, count, sql in states)
        )
    if not parts:
        return f"-- {NOTHING}\n" if not found.hand else f"-- {NO_STATEMENT}\n"
    head = (
        "-- azsqlcd align. Review every statement. A DBA runs this script in a maintenance window; the tool\n"
        "-- runs none of it. Then run azsqlcd baseline. A line '-- REVIEW:' (loses data, or unmasks a\n"
        "-- column) is followed by a statement that is commented out: run it only when that is accepted.\n\n"
    )
    return head + "\n".join(parts)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _list(title: str, items: list[Item], none: str = "None.") -> list[str]:
    rows = "".join(f"| {_cell(item.object_key)} | {_cell(item.note)} |\n" for item in items)
    return [f"## {title}\n", "| Object | Note |\n|---|---|\n" + rows if items else f"{none}\n"]


def _report(
    env: str,
    target_id: str,
    schema: str | None,
    found: _Tables,
    module_items: list[Item],
    states: list[tuple[str, int, str]],
    empty: bool,
) -> str:
    of = "" if schema is None else f", the schema {names.quote(schema)}"
    review = [s for s in found.statements if s.review is not None]
    counts = (
        ("Constraint names", len(found.renames)),
        ("Table-class statements", len(found.statements)),
        ("To review: loses data", len([s for s in review if s.loses is not None])),
        ("To review: unmasks", len([s for s in review if s.unmasks is not None])),
        ("Modules", len(module_items)),
        ("Constraint state", len(states)),
        ("Hand-write", len(found.hand)),
        ("Only in the database", len(found.only)),
        (f"Held in {_cell(env)}", len(found.held)),
    )
    parts = [
        f"# azsqlcd align: {_cell(env)} / {_cell(target_id)}{of}\n",
        "The tool only read the database. align.sql holds the statements that make it equal to the files "
        "of the repository.\n",
        "Nothing to align.\n" if empty else "",
        "| Section | Count |\n|---|---|\n" + "".join(f"| {name} | {count} |\n" for name, count in counts),
        "## Steps\n",
        "1. Review align.sql. Each statement under a line `-- REVIEW:` (loses data, or unmasks a column) is "
        "commented out; take it only when that is accepted.\n"
        '2. Write what is under "Hand-write" by hand, or leave the object as it is and refresh the '
        "database from prod.\n"
        "3. A DBA runs align.sql in a maintenance window. The tool never runs it.\n"
        "4. Run `azsqlcd baseline` for this target with a release that holds these files"
        + ("" if schema is None else f", with `--schema {schema}`")
        + " (`--report-only` first). It must pass.\n",
    ]
    parts += _list("Table-class objects that differ", found.differs)
    parts += _list("To review", [Item(s.object_key, s.review or "") for s in review])
    parts += _list("Modules", module_items)
    parts += _list(
        "Constraint names",
        [
            Item(r.table_key, f"{r.kind} {names.quote(r.live_name)} -> {names.quote(r.fixed_name)}")
            for r in found.renames
        ],
    )
    parts += _list("Constraint state", [Item(key, f"{count} rows: {sql}") for key, count, sql in states])
    parts += _list(f"Held in {_cell(env)}", found.held)
    parts += _list("Hand-write", found.hand)
    parts += _list("Only in the database", found.only)
    if found.only:
        by_kind: dict[tuple[str, str], int] = {}
        for item in found.only:
            kind = (item.object_key.partition(":")[0], names.quote(names.schema_of(item.object_key)))
            by_kind[kind] = by_kind.get(kind, 0) + 1
        parts.append(
            "| Kind | Schema | Count |\n|---|---|---|\n"
            + "".join(
                f"| {kind} | {_cell(schema)} | {count} |\n"
                for (kind, schema), count in sorted(by_kind.items())
            )
        )
    parts.append(
        "An object only in the database is never dropped: add its file, exclude it in `[scope]` of "
        "azsqlcd.toml, or drop it by hand.\n"
    )
    return "\n".join(part for part in parts if part)


def _result(
    env: str,
    target_id: str,
    schema: str | None,
    found: _Tables,
    ordered: tuple[modules.ModuleFile, ...],
    module_items: list[Item],
    states: list[tuple[str, int, str]],
) -> AlignResult:
    sql = _script(found, ordered, states)
    empty = sql == f"-- {NOTHING}\n"  # a Hand-write item gives NO_STATEMENT
    return AlignResult(
        renames=tuple(found.renames),
        statements=tuple(found.statements),
        differs=tuple(found.differs),
        modules=ordered,
        module_items=tuple(module_items),
        states=tuple(states),
        hand_write=tuple(found.hand),
        only_here=tuple(found.only),
        held=tuple(found.held),
        sql=sql,
        md=_report(env, target_id, schema, found, module_items, states, empty),
    )
