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
     and the reason, and is left as the database has it. An object that only the database has is
     never dropped. A statement that loses data is written commented out, after a line
     "-- REVIEW: loses data: <why>".
  3. Modules: the file text of each module that differs from its file or is missing, in the order
     of a deploy. A module is never dropped.
  4. Constraint state: WITH CHECK CHECK CONSTRAINT for a CHECK or FOREIGN KEY constraint that is
     disabled or not trusted, ALTER INDEX ... REBUILD for a disabled index or key, each after a
     comment with the row count of its table, smallest table first. A constraint or an index that
     section 2 creates again is not listed: it is created enabled and trusted.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace

from azsqlcd import catalog, catalog_tables, diff, emit, gen, modules, names, onboard, plan, replay, tables
from azsqlcd.catalog_tables import SystemNamed
from azsqlcd.config import Config, in_scope
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
    fold,
)
from azsqlcd.session import Session

NOTHING = "nothing to align"
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
    sql: str  # onboarding/<env>/align.sql
    md: str  # onboarding/<env>/align.md

    @property
    def counts(self) -> str:
        return (
            f"{len(self.renames)} rename(s), {len(self.statements)} statement(s), {len(self.modules)} "
            f"module(s), {len(self.states)} constraint state statement(s), {len(self.hand_write)} to "
            f"hand-write, {len(self.only_here)} only in the database"
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


def _tables(live_read: catalog_tables.CatalogRead, head: Model) -> _Tables:
    live = live_read.model
    hand: list[Item] = []
    only: list[Item] = []
    unsupported = {fold(u.object_key): u for u in live_read.unsupported}
    for key in list(head):
        found = unsupported.get(fold(key))
        if found is not None:
            hand.append(Item(key, f"{found.code}: {found.reason}"))
            head = head.remove(key)
    only += [
        Item(u.object_key, f"{u.code}: {u.reason}") for u in live_read.unsupported if u.object_key not in head
    ]
    for key in live:
        if key not in head:
            only.append(Item(key, "no file"))
            head = head.add(live[key])  # never dropped: it stays as the database has it

    # the live model holds a fixed name for each engine-named constraint; give it the declared
    # name (after the rename) or its live name (no file holds it: a statement names it so)
    by_table: dict[str, list[SystemNamed]] = {}
    for item in live_read.system_named:
        by_table.setdefault(fold(item.table_key), []).append(item)
    renames: list[SystemNamed] = []
    declared_of: dict[str, str] = {}
    for items in by_table.values():
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
        live = live.replace(table)

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
            Statement(_key_of(op), emit.emit_operation(op), _loses(op, diff.classify(op, state)))
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
    found: list[tuple[str, str]] = []
    notes: list[Item] = []
    for key, capture in read.captures.items():
        table = head.get(key)
        if not isinstance(table, Table) or fold(key) not in filed:
            continue
        wanted = {fold(c.name): c for c in table.constraints if c.name is not None}
        indexes = {fold(i.name) for i in table.indexes}
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
                found.append((key, f"ALTER TABLE {target} WITH CHECK CHECK CONSTRAINT {names.quote(name)};"))
            elif value.get("is_disabled") and (
                (label.startswith("index [") and fold(name) in indexes)
                or isinstance(constraint, PrimaryKey | Unique)
            ):
                found.append((key, f"ALTER INDEX {names.quote(name)} ON {target} REBUILD;"))
    facts = catalog.table_facts(session, sorted({key for key, _ in found})) if found else {}
    rows = {fold(key): fact.rows for key, fact in facts.items()}
    # in a table the rebuilds first: a foreign key is checked against the index of its key
    ordered = sorted(
        ((key, rows.get(fold(key), 0), sql) for key, sql in found),
        key=lambda s: (s[1], fold(s[0]), not s[2].startswith("ALTER INDEX"), s[2]),
    )
    return ordered, notes


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
    scope = onboard.export_scope(config, schema)
    in_files = plan.module_files(files)
    table_model = config.project.table_model
    head = (
        Model(obj for obj in gen.load_model(files).values() if in_scope(scope, obj.key))
        if table_model
        else Model()
    )

    found = _Tables([], [], [], [], [], head, {}, set())
    states: list[tuple[str, int, str]] = []
    if table_model:
        read = catalog_tables.read_model(session, scope=scope)
        found = _tables(read, head)
        filed = {fold(key) for key in head}
        states, notes = _states(session, read, found.head, found.declared_of, found.recreated, filed)
        found.hand += notes
    hand, only = found.hand, found.only

    module_items: list[Item] = []
    send: list[modules.ModuleFile] = []
    for item in onboard.compare_modules(session, files, env, scope):
        if item.status == onboard.ONLY_HERE:
            only.append(Item(item.object_key, item.note or "no file"))
        elif item.status == onboard.MISSING_HERE and item.note:
            hand.append(Item(item.object_key, item.note))
        elif item.status in (onboard.DIFFERS, onboard.MISSING_HERE):
            send.append(in_files[item.object_key])
            note = (
                "missing" if item.status == onboard.MISSING_HERE else "the definition differs from the file"
            )
            if item.note.startswith("the text is the text of the file"):
                note = item.note
            module_items.append(Item(item.object_key, note))
    order, _ = modules.deploy_order(send, modules.build_edges(send, ()))
    by_key = {module.key: module for module in send}
    ordered = tuple(by_key[key] for key in order)
    return _result(env, target_id, schema, found, ordered, module_items, states)


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
            if statement.loses is None:
                lines.append(f"{statement.sql}\nGO\n")
            else:
                lines.append(f"-- REVIEW: loses data: {statement.loses}\n{_commented(statement.sql)}\nGO\n")
        parts.append("".join(lines))
    if ordered:
        parts.append(f"-- {SECTIONS[2]}\n" + "".join(f"{module.text.rstrip()}\nGO\n" for module in ordered))
    if states:
        parts.append(
            f"-- {SECTIONS[3]}\n"
            + "".join(f"-- {key.partition(':')[2]}: {count} rows\n{sql}\nGO\n" for key, count, sql in states)
        )
    if not parts:
        return f"-- {NOTHING}\n"
    head = (
        "-- azsqlcd align. Review every statement. A DBA runs this script in a maintenance window; the tool\n"
        "-- runs none of it. Then run azsqlcd baseline. A line '-- REVIEW: loses data' is followed by a\n"
        "-- statement that is commented out: run it only when the loss is accepted.\n\n"
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
    review = [s for s in found.statements if s.loses is not None]
    counts = (
        ("Constraint names", len(found.renames)),
        ("Table-class statements", len(found.statements)),
        ("To review: loses data", len(review)),
        ("Modules", len(module_items)),
        ("Constraint state", len(states)),
        ("Hand-write", len(found.hand)),
        ("Only in the database", len(found.only)),
    )
    parts = [
        f"# azsqlcd align: {_cell(env)} / {_cell(target_id)}{of}\n",
        "The tool only read the database. align.sql holds the statements that make it equal to the files "
        "of the repository.\n",
        "Nothing to align.\n" if empty else "",
        "| Section | Count |\n|---|---|\n" + "".join(f"| {name} | {count} |\n" for name, count in counts),
        "## Steps\n",
        "1. Review align.sql. Each statement under a line `-- REVIEW: loses data` is commented out; take it "
        "only when the loss is accepted.\n"
        '2. Write what is under "Hand-write" by hand, or leave the object as it is and refresh the '
        "database from prod.\n"
        "3. A DBA runs align.sql in a maintenance window. The tool never runs it.\n"
        "4. Run `azsqlcd baseline` for this target with a release that holds these files"
        + ("" if schema is None else f", with `--schema {schema}`")
        + " (`--report-only` first). It must pass.\n",
    ]
    parts += _list("Table-class objects that differ", found.differs)
    parts += _list("To review: loses data", [Item(s.object_key, s.loses or "") for s in review])
    parts += _list("Modules", module_items)
    parts += _list(
        "Constraint names",
        [
            Item(r.table_key, f"{r.kind} {names.quote(r.live_name)} -> {names.quote(r.fixed_name)}")
            for r in found.renames
        ],
    )
    parts += _list("Constraint state", [Item(key, f"{count} rows: {sql}") for key, count, sql in states])
    parts += _list("Hand-write", found.hand)
    parts += _list(
        "Only in the database",
        found.only,
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
    empty = sql == f"-- {NOTHING}\n"
    return AlignResult(
        renames=tuple(found.renames),
        statements=tuple(found.statements),
        differs=tuple(found.differs),
        modules=ordered,
        module_items=tuple(module_items),
        states=tuple(states),
        hand_write=tuple(found.hand),
        only_here=tuple(found.only),
        sql=sql,
        md=_report(env, target_id, schema, found, module_items, states, empty),
    )
