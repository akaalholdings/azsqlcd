"""The table model against a database: hooks for plan and deploy, export, baseline compare.

This module sends no batch of its own. Every catalog read goes through catalog_tables.py and
catalog.py; the recorded state comes from state.read_state.

Read-back (A14) compares the catalog with the model of the release, object by object, with
Model.diff_paths(expressions=False). Before that the live object is brought to what a file can say:
  * a live index, constraint or DEFAULT that the model does not hold is left out: it is an
    unmanaged sub-object (Part 2 (c) 4). The capture that read_back returns leaves it out too,
    so it is not recorded and takes no part in drift;
  * a constraint that the engine named is matched to a constraint of the model of the same shape
    (same kind, columns, target and actions; a DEFAULT by its column; a CHECK by its kind);
  * options and the owner of a schema: catalog_tables.project_options. An option that the file
    does not state is compared with the engine default;
  * a column whose file states the collation of the database: the reader gives no collation for
    such a column, so the live column gets the declared one;
  * a computed column: where the file does not say NOT NULL, nullability is not compared; where
    it says PERSISTED NOT NULL, the catalog must say persisted and not nullable.
A difference is reported as object, property path and two hashes, never as text (A25).

System-versioned temporal tables: the period columns (generated, hidden) and Table.temporal (period,
name of the history table, retention) are fields of the model and properties of the capture, so
read-back, drift and the baseline compare see them as any other field. A history table is owned
by the engine: export writes no file for it and lists it in the report; it is never unmanaged,
never `only_here`, and nothing depends on it being managed.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from azsqlcd import catalog, catalog_tables, chain, diff, emit, names, state
from azsqlcd.catalog import Difference
from azsqlcd.catalog_tables import SystemNamed, Unsupported, project_options
from azsqlcd.config import Config
from azsqlcd.errors import ToolError, failed, refused
from azsqlcd.lex import BOM
from azsqlcd.model import (
    AddColumn,
    AddConstraint,
    AlterColumn,
    AlterColumnProperty,
    Check,
    Column,
    Constraint,
    CreateIndex,
    CreateSchema,
    CreateSequence,
    CreateSynonym,
    CreateTable,
    CreateType,
    DropColumn,
    DropTable,
    ForeignKey,
    Model,
    ModelObject,
    Operation,
    PrimaryKey,
    Rename,
    Schema,
    Sequence,
    Table,
    TableType,
    Unique,
    fold,
)
from azsqlcd.parse import ParseError, parse_object_file, parse_statement
from azsqlcd.plan import TableFindings, Work
from azsqlcd.release import Bundle
from azsqlcd.session import Session
from azsqlcd.state import ObjectRow, State

SNAPSHOT_FORMAT = 1
EQUAL, DIFFERS, ONLY_HERE, MISSING_HERE = "equal", "differs", "only_here", "missing_here"
ROUNDTRIP = "ROUNDTRIP"  # export: the file of the object does not read back as the catalog object
DEPENDS_ON_UNMANAGED = "DEPENDS_ON_UNMANAGED"  # export: the object needs one that is not managed
EXISTS, SUPPORTED = "exists", "unsupported"  # properties of an object as a whole
REFRESHABLE = ("V", "IF", "TF")  # sys.objects.type of a view and of a table-valued function (A12)
_QUOTED = re.compile(r"\[((?:[^\]]|\]\])*)\]")  # one bracket-quoted name of a blocker label

type Captures = dict[str, dict[str, Any] | None]  # as runner.Captures: None = the object is gone


# ------------------------------------------------------------------ the release
def head_model(files: Mapping[str, bytes]) -> Model:
    """The model of the table-class object files of a release. ToolError REFUSED MODEL_INVALID."""
    objects: list[ModelObject] = []
    for path in sorted(files):
        try:
            kind = names.key_for_path(path)[0]
        except ValueError:
            continue
        if kind not in names.TABLE_CLASS_KINDS:
            continue
        try:
            objects += parse_object_file(files[path].decode("utf-8").removeprefix(BOM), path)
        except UnicodeDecodeError:
            raise refused("MODEL_INVALID", f"{path} is not valid UTF-8", path=path) from None
        except ParseError as e:
            message = f"{path} line {e.line}: {e.code}: {e.message}"
            raise refused("MODEL_INVALID", message, path=path, line=e.line) from None
    try:
        return Model(objects)
    except ValueError as e:
        raise refused(
            "MODEL_INVALID", f"the table-class files of the release do not fit together: {e}"
        ) from None


def _operations(migration: chain.Migration) -> list[Operation]:
    """The operations of the model batches of a migration. Data and raw batches are not modelled."""
    found: list[Operation] = []
    for batch in migration.batches:
        if batch.kind != "model":
            continue
        try:
            found.append(parse_statement(batch.text))
        except ParseError as e:
            line = batch.first_line + e.line - 1
            message = f"{migration.file} line {line}: {e.code}: {e.message}"
            raise refused("MIGRATION_INVALID", message, file=migration.file, line=line) from None
    return found


def _table_key(schema: str, name: str) -> str:
    return names.object_key("TABLE", schema, name)


def _managed(recorded: State) -> dict[str, ObjectRow]:
    """Key as stored -> row of every managed object (E5)."""
    return {key: row for key, row in recorded.objects.items() if row.status == "managed"}


@dataclass(frozen=True)
class _Target:
    """A table (column None) or a column that a pending statement drops, renames or retypes."""

    table_key: str
    column: str | None

    @property
    def table(self) -> str:
        _, schema, name = names.parse_object_key(self.table_key)
        return names.qualified(schema or "", name)

    @property
    def text(self) -> str:
        return self.table if self.column is None else f"{self.table}.{names.quote(self.column)}"


# ------------------------------------------------------------------ read-back comparison
def _sha256(obj: ModelObject | None) -> str | None:
    if obj is None:
        return None
    return hashlib.sha256(Model([obj]).to_canonical_json().encode("utf-8")).hexdigest()


def _same_shape(live: Constraint, declared: Constraint) -> bool:
    """Could the engine-named live constraint be this constraint of the model? Text is not compared."""
    if type(live) is not type(declared):
        return False
    if isinstance(live, ForeignKey):
        return replace(live, name=declared.name) == declared
    if isinstance(live, PrimaryKey | Unique) and isinstance(declared, PrimaryKey | Unique):
        return (live.clustered, live.columns) == (declared.clustered, declared.columns)
    return isinstance(live, Check)


def _columns(
    live: Table | TableType, declared: Table | TableType, database_collation: Callable[[], str | None]
) -> tuple[Column, ...]:
    """A computed column says NOT NULL only when the file does, and a column whose file states the
    collation of the database has that collation (see the module docstring)."""
    found: list[Column] = []
    for column in live.columns:
        other = declared.column(column.name)
        if column.computed and other is not None and other.computed and other.nullable is None:
            column = replace(column, nullable=None)
        if other is not None and other.collation is not None and column.collation is None:
            default = database_collation()  # asked only for a file that states a collation
            if default is not None and default.casefold() == other.collation.casefold():
                column = replace(column, collation=other.collation)
        found.append(column)
    return tuple(found)


def _aligned(
    live: ModelObject,
    declared: ModelObject,
    engine_named: Collection[str],
    database_collation: Callable[[], str | None],
) -> tuple[ModelObject, set[str] | None, dict[str, str]]:
    """The live object as the comparison with the model needs it, the capture labels of the
    sub-objects that the model holds (None: every label), and fixed name -> declared name of each
    engine-named constraint that the model holds.

    engine_named: the fixed names that read_model gave to constraints of this table.
    database_collation: gives the collation of the database, or None when it is not known.
    """
    if isinstance(live, TableType) and isinstance(declared, TableType):
        columns_ = _columns(live, declared, database_collation)
        return project_options(replace(live, columns=columns_), declared), None, {}
    if not isinstance(live, Table) or not isinstance(declared, Table):
        return project_options(live, declared), None, {}
    fixed = {fold(name) for name in engine_named}
    keep: set[str] = set()
    renames: dict[str, str] = {}
    wanted = {fold(c.name): c for c in declared.constraints if c.name}
    have = {fold(c.name) for c in live.constraints if c.name}
    free = [c for name, c in wanted.items() if name not in have]
    constraints: list[Constraint] = []
    for constraint in live.constraints:
        name = constraint.name or ""
        match = constraint if fold(name) in wanted else None
        if match is None and fold(name) in fixed:
            twin = next((c for c in free if _same_shape(constraint, c)), None)
            if twin is not None:
                free.remove(twin)
                match = replace(constraint, name=twin.name)
        if match is not None:
            constraints.append(match)
            keep.add(f"constraint {names.quote(name)}")
            if fold(name) in fixed and match.name:
                renames[name] = wanted[fold(match.name)].name or match.name
    indexes = tuple(index for index in live.indexes if declared.index(index.name) is not None)
    keep |= {f"index {names.quote(index.name)}" for index in indexes}
    columns: list[Column] = []
    for column in _columns(live, declared, database_collation):
        other = declared.column(column.name)
        default = column.default
        if default is not None and default.name:
            wanted_default = other.default if other is not None else None
            if wanted_default is None:
                default = None
            else:
                keep.add(f"constraint {names.quote(default.name)}")
                if fold(default.name) in fixed:
                    if wanted_default.name:
                        renames[default.name] = wanted_default.name
                    default = replace(default, name=wanted_default.name)
        columns.append(replace(column, default=default))
    cut = replace(live, columns=tuple(columns), constraints=tuple(constraints), indexes=indexes)
    return project_options(cut, declared), keep, renames


def rename_script(engine_named: Iterable[SystemNamed]) -> str:
    """rename-constraints.sql for these constraints: EXEC sys.sp_rename only. '' when there is none."""
    return catalog_tables.rename_constraints_sql(engine_named)


def _problem(
    key: str, properties: list[str], declared: ModelObject | None, live: ModelObject | None
) -> dict[str, Any]:
    return {
        "object": key,
        "properties": properties,
        "expected_sha256": _sha256(declared),
        "live_sha256": _sha256(live),
    }


# ------------------------------------------------------------------ hooks
class Hooks:
    """plan.TableHooks and runner.TableHooks for one release with table_model = true.

    model is the head model: the table-class files of the bundle.
    """

    def __init__(self, bundle: Bundle, config: Config) -> None:
        if not config.project.table_model:
            raise ValueError("table hooks are for a project with table_model = true")
        self._bundle = bundle
        self.model = head_model(bundle.files)

    # -------------------------------------------------------------- what the release changes
    def _touched(self, ops: Iterable[Operation]) -> list[str]:
        try:
            keys = diff.touched_objects(ops, self.model)
        except ValueError as e:
            raise refused("RENAME_NOT_RESOLVED", f"a pending rename cannot be followed: {e}") from None
        # the spelling of the files, so that a key equals the key that the state holds
        return [self.model[key].key if key in self.model else key for key in keys]

    def touched_table_objects(self, work: Work) -> Collection[str]:
        """Keys of the table-class objects that the pending migrations create, alter or drop."""
        return self._touched(op for migration in work.pending for op in _operations(migration))

    def _targets(self, work: Work) -> list[_Target]:
        found: dict[tuple[str, str], _Target] = {}
        for migration in work.pending:
            for op in _operations(migration):
                match op:
                    case DropTable():
                        target = _Target(_table_key(op.schema, op.name), None)
                    case DropColumn() | AlterColumn():
                        target = _Target(_table_key(op.schema, op.table), op.column)
                    case AlterColumnProperty(property="SPARSE"):
                        target = _Target(_table_key(op.schema, op.table), op.column)
                    case Rename(kind="column"):
                        target = _Target(_table_key(op.old[0], op.old[1]), op.old[2])
                    case Rename(kind="table"):
                        target = _Target(_table_key(op.old[0], op.old[1]), None)
                    case Rename(kind="object") if any(
                        _table_key(op.old[0], name) in self.model for name in (op.old[1], op.new_name)
                    ):  # sp_rename N'OBJECT' does not say table or constraint; the model does
                        target = _Target(_table_key(op.old[0], op.old[1]), None)
                    case _:
                        continue
                found.setdefault((fold(target.table_key), fold(target.column or "")), target)
        return list(found.values())

    def unrecorded_objects(self, work: Work, state: State) -> list[str]:
        """Keys of the model of the release that have no managed row and that no pending migration
        creates (Part 2 (c) 8). Such an object was never compared with this database: run baseline.
        """
        managed = {fold(key) for key in _managed(state)}
        created: set[str] = set()
        for migration in work.pending:
            for op in _operations(migration):
                match op:
                    case CreateSchema():
                        created.add(fold(op.schema.key))
                    case CreateType():
                        created.add(fold(op.type.key))
                    case CreateSequence():
                        created.add(fold(op.sequence.key))
                    case CreateSynonym():
                        created.add(fold(op.synonym.key))
                    case CreateTable():
                        created.add(fold(op.table.key))
                    case Rename(kind="table" | "object"):  # the new name of a table has no row yet
                        created.add(fold(_table_key(op.old[0], op.new_name)))
                    case _:
                        pass
        return [key for key in self.model if fold(key) not in managed and fold(key) not in created]

    # -------------------------------------------------------------- drift
    def table_drift(
        self, session: Session, state: State, keys: Collection[str]
    ) -> dict[str, list[Difference]]:
        """Recorded capture against the live capture, for each key that differs.

        A recorded object that is gone has the difference 'exists'; one that the reader can no
        longer hold has the difference 'unsupported'. A property of catalog_tables.NOT_IN_DRIFT
        (start_value of a sequence) is not compared.
        """
        if not keys:
            return {}
        found = catalog_tables.read_model(session, keys)
        live = {fold(key): capture for key, capture in found.captures.items()}
        outside = {fold(item.object_key) for item in found.unsupported}
        rows = {fold(key): row for key, row in state.objects.items()}
        drift: dict[str, list[Difference]] = {}
        for key in keys:
            row = rows[fold(key)]
            if fold(key) in live:
                skipped = catalog_tables.NOT_IN_DRIFT.get(str(row.capture.get("kind")), frozenset())
                differences = [
                    difference
                    for difference in catalog.capture_differences(row.capture, live[fold(key)])
                    if difference.property not in skipped
                ]
            else:
                gone = SUPPORTED if fold(key) in outside else EXISTS
                differences = [Difference(gone, row.catalog_sha256, None)]
            if differences:
                drift[key] = differences
        return drift

    # -------------------------------------------------------------- plan step 8
    def _blockers(self, session: Session, work: Work, config: Config, recorded: State) -> list[str]:
        rows = _managed(recorded)
        managed = {fold(key) for key in rows}
        known: set[str] = set()
        for key, row in rows.items():
            if key.startswith("TABLE:"):
                known |= {fold(label) for label in catalog_tables.recorded_sub_objects(key, row.capture)}
        unbound = {fold(key) for key in work.unbinds}
        acknowledged = {
            tuple(fold(part.strip()) for part in entry.split("->")) for entry in config.unmanaged_dependants
        }
        found: list[str] = []
        engine_named: dict[str, tuple[SystemNamed, ...]] = {}  # case-folded table key -> its renames
        for target in self._targets(work):
            for label in catalog_tables.blockers(session, target.table_key, target.column):
                module = label.removeprefix("SCHEMABOUND ")
                if module == label:
                    if fold(label) not in known:
                        found.append(
                            self._needs_rename(session, label, known, engine_named)
                            or f"{label} uses {target.text} and is not recorded: the release cannot know "
                            "it. Drop it by hand, or take it into the table file by pull request"
                        )
                elif fold(module) not in managed:
                    found.append(f"{module} is schema-bound to {target.text} and is not managed")
                elif fold(module) not in unbound:
                    found.append(
                        f"{module} is schema-bound to {target.text}: the migration needs the line "
                        f"'-- azsqlcd:unbind {module.split(':', 1)[1]}'"
                    )
            for dependant in catalog.dependants_of(session, [target.table_key]):
                if fold(dependant.key) in managed:
                    continue
                name = dependant.key.split(":", 1)[1]
                if {(fold(name), fold(target.text)), (fold(name), fold(target.table))} & acknowledged:
                    continue
                found.append(
                    f"{dependant.key} uses {target.table} and is not managed. If the change of "
                    f'{target.text} is safe for it, add "{name} -> {target.text}" to [ack] '
                    "unmanaged_dependants in azsqlcd.toml"
                )
        return found

    def _needs_rename(
        self, session: Session, label: str, known: Collection[str], cache: dict[str, tuple[SystemNamed, ...]]
    ) -> str | None:
        """The blocker text for an index or a foreign key that is recorded under the name of the
        files and that the engine named in this database; None when the label is not such a one.

        The operator must rename the constraint, never drop it: it is a managed key of the table.
        """
        parts = [part.replace("]]", "]") for part in _QUOTED.findall(label)]
        if len(parts) != 3 or not label.startswith(("INDEX ", "FOREIGN KEY ")):
            return None
        schema, table, live_name = parts
        table_key = _table_key(schema, table)
        if fold(table_key) not in cache:
            cache[fold(table_key)] = catalog_tables.read_model(session, [table_key]).system_named
        for item in cache[fold(table_key)]:
            kind = "FOREIGN KEY" if item.kind == "FOREIGN KEY" else "INDEX"
            recorded_label = f"{kind} {names.qualified(schema, table)}.{names.quote(item.fixed_name)}"
            if (
                fold(item.live_name) == fold(live_name)
                and label.startswith(f"{kind} ")
                and fold(recorded_label) in known
            ):
                rename = catalog_tables.rename_constraints_sql([item]).replace("\nGO\n", "")
                return (
                    f"{label} is the {item.kind} constraint that the release names "
                    f"{names.quote(item.fixed_name)}: the engine named it in this database. Do not drop "
                    f"it. Rename it, then plan again: {rename}"
                )
        return None

    def _dependants(self, session: Session, work: Work, recorded: State) -> tuple[list[str], list[str]]:
        """(pre-broken, refresh set): the managed, unchanged dependants of the touched tables."""
        managed = {fold(key): row for key, row in _managed(recorded).items()}
        changed = {fold(key) for key in (*(c.key for c in work.module_changes), *work.drops, *work.unbinds)}
        tables = [key for key in self.touched_table_objects(work) if key.startswith("TABLE:")]
        broken: list[str] = []
        refresh: list[str] = []
        for dependant in catalog.dependants_of(session, tables):
            row = managed.get(fold(dependant.key))
            if row is None or fold(dependant.key) in changed:
                continue
            if catalog.broken_references(session, dependant.key):
                broken.append(dependant.key)
            elif not dependant.schema_bound and row.capture.get("type") in REFRESHABLE:
                refresh.append(dependant.key)
        return broken, refresh

    def blockers_and_dependants(self, session: Session, work: Work, config: Config) -> TableFindings:
        """Plan step 8 for every table and column that a pending statement drops, renames or retypes.

        Blockers: an index, a statistics object or a foreign key on it that no capture holds; a
        schema-bound module on it that is not managed, or that the migration does not unbind; an
        unmanaged module that uses the table, unless [ack] unmanaged_dependants has the line
        "[s].[module] -> [s].[table]" or "[s].[module] -> [s].[table].[column]".
        pre_broken: managed, unchanged modules on a touched table that do not bind now (A12).
        """
        recorded = state.read_state(session)
        blockers = self._blockers(session, work, config, recorded)
        return TableFindings(tuple(blockers), tuple(self._dependants(session, work, recorded)[0]))

    def refresh_set(self, session: Session, work: Work) -> list[str]:
        """Managed, unchanged, not schema-bound views and table-valued functions on a touched
        table that bind now, in key order (A12)."""
        return self._dependants(session, work, state.read_state(session))[1]

    def dependants_to_check(self, session: Session, work: Work) -> list[str]:
        """Keys of the managed modules that the runner must check after the change (A12), in key order.

        Every managed module of any kind, schema-bound or not, that references a table which a
        pending statement creates, alters, drops or renames, as sys.sql_expression_dependencies
        holds it now. The set is read before the change: after DROP TABLE or sp_rename the old
        name resolves to no object, and the catalog no longer tells who used it. A module that
        the release drops is left out; one that the release changes stays in.
        """
        tables = {fold(key): key for key in self.touched_table_objects(work) if key.startswith("TABLE:")}
        for target in self._targets(work):  # the name before a rename, whatever the model holds
            tables.setdefault(fold(target.table_key), target.table_key)
        if not tables:
            return []
        managed = {fold(key): key for key in _managed(state.read_state(session))}
        dropped = {fold(key) for key in work.drops}
        return sorted(
            managed[fold(dependant.key)]
            for dependant in catalog.dependants_of(session, list(tables.values()))
            if fold(dependant.key) in managed and fold(dependant.key) not in dropped
        )

    def dependant_findings(self, session: Session, keys: Collection[str]) -> dict[str, list[str]]:
        """key -> what catalog.broken_references reports for the module now, sorted. Read-only.

        The plan calls this for the dependants to check, before the change. A sound module can
        have findings (a procedure with a #temp table; error 2020 for some procedures: measured on
        Azure SQL Database, RO-2), so the runner fails a dependant only for a finding of the read
        after the change that this result does not hold. Every key has an entry; an empty list
        says that the module was read and has no finding.
        """
        return {key: sorted(catalog.broken_references(session, key)) for key in sorted(set(keys))}

    # -------------------------------------------------------------- A20
    def sub_object_collisions(self, session: Session, work: Work, state: State) -> list[str]:
        """Names that a pending statement creates, that exist in the database and are not recorded.

        Index and constraint names (A20), and the names of new tables, sequences, synonyms, types
        and schemas (plan step 5). A constraint is an object of the schema of its table.
        """
        objects: set[tuple[str, str]] = set()
        indexes: set[tuple[str, str, str]] = set()
        types: set[tuple[str, str]] = set()
        schemas: set[str] = set()
        for migration in work.pending:
            for op in _operations(migration):
                match op:
                    case CreateIndex():
                        indexes.add((op.schema, op.table, op.index.name))
                    case AddConstraint():
                        if op.constraint.name:
                            objects.add((op.schema, op.constraint.name))
                            if isinstance(op.constraint, PrimaryKey | Unique):
                                indexes.add((op.schema, op.table, op.constraint.name))
                    case AddColumn():
                        if op.column.default is not None and op.column.default.name:
                            objects.add((op.schema, op.column.default.name))
                    case CreateTable():
                        table = op.table
                        used = [c.name for c in table.constraints]
                        used += [c.default.name for c in table.columns if c.default is not None]
                        objects |= {(table.schema, name) for name in (table.name, *used) if name}
                    case CreateSequence():
                        objects.add((op.sequence.schema, op.sequence.name))
                    case CreateSynonym():
                        objects.add((op.synonym.schema, op.synonym.name))
                    case CreateType():
                        types.add((op.type.schema, op.type.name))
                    case CreateSchema():
                        schemas.add(op.schema.name)
                    case _:
                        pass
        present = catalog_tables.names_present(
            session, objects=objects, indexes=indexes, types=types, schemas=schemas
        )
        recorded: set[str] = set()
        for key, row in _managed(state).items():
            kind, schema, name = names.parse_object_key(key)
            if schema is None:
                recorded.add(fold(f"SCHEMA {names.quote(name)}"))
                continue
            recorded.add(fold(f"{'TYPE' if kind == 'TYPE' else 'OBJECT'} {names.qualified(schema, name)}"))
            if kind == "TABLE":
                recorded |= {fold(label) for label in catalog_tables.recorded_sub_objects(key, row.capture)}
                recorded |= {
                    fold(f"OBJECT {names.quote(schema)}.{label.removeprefix('constraint ')}")
                    for label in row.capture
                    if label.startswith("constraint ")
                }
        return sorted(label for label in present if fold(label) not in recorded)

    # -------------------------------------------------------------- read-back
    def _refuse_later_changes(self, keys: Collection[str], through: str) -> None:
        """The model after `through` is the head model only for objects that nothing changes later."""

        def not_computable(message: str, **detail: Any) -> ToolError:
            return refused("READBACK_NOT_COMPUTABLE", message, migration=through, **detail)

        files = self._bundle.files
        if chain.SUM_PATH not in files:
            raise not_computable("the release has no migrations.sum")
        order = chain.effective_order(chain.parse_sum(files[chain.SUM_PATH].decode("utf-8")))
        entries = [entry.file for entry in order if entry.file == through or not entry.withdrawn]
        if through not in entries:
            raise not_computable(f"{through} is not a migration of this release")
        asked = {fold(key) for key in keys}
        for file in entries[entries.index(through) + 1 :]:
            data = files.get(f"migrations/{file}")
            if data is None:
                raise not_computable(f"the release does not hold the file of {file}", later=file)
            try:
                touched = self._touched(_operations(chain.parse_migration(data.decode("utf-8"), file)))
            except ToolError as e:
                raise not_computable(f"{file} cannot be read: {e.message}", later=file) from None
            again = sorted(key for key in touched if fold(key) in asked)
            if again:
                raise not_computable(
                    f"{file}, a later migration of this release, changes {again[0]} again: the model "
                    f"after {through} cannot be computed for it. Check the database by hand, then give "
                    "--force-no-readback",
                    later=file,
                    objects=again,
                )

    def read_back(
        self, session: Session, bundle: Bundle, keys: Collection[str], through: str | None
    ) -> Captures:
        """Compare the catalog with the model for these keys; return the captures to record.

        through None: the model of the release. through = a migration id: the model after that
        migration, which is the model of the release when no later migration changes the same
        objects; else ToolError REFUSED READBACK_NOT_COMPUTABLE. A key that the model does not
        hold must be gone from the database: its capture is None. Raises ToolError FAILED
        READBACK_MISMATCH with object, property paths and two hashes for each object that differs.
        """
        if bundle.manifest != self._bundle.manifest:
            raise ValueError("read_back was given another release than the one of these hooks")
        if through is not None:
            self._refuse_later_changes(keys, through)
        found = catalog_tables.read_model(session, keys)
        live_captures = {fold(key): capture for key, capture in found.captures.items()}
        outside = {fold(item.object_key): item for item in found.unsupported}
        engine_named: dict[str, list[str]] = defaultdict(list)
        for item in found.system_named:
            engine_named[fold(item.table_key)].append(item.fixed_name)
        asked: list[str | None] = []

        def database_collation() -> str | None:
            if not asked:
                asked.append(catalog_tables.database_collation(session))
            return asked[0]

        captures: Captures = {}
        problems: list[dict[str, Any]] = []
        for key in keys:
            declared, live = self.model.get(key), found.model.get(key)
            if fold(key) in outside:
                problems.append(_problem(key, [SUPPORTED], declared, None))
            elif declared is None and live is None:
                captures[key] = None
            elif declared is None or live is None:
                problems.append(_problem(key, [EXISTS], declared, live))
            else:
                aligned, keep, _ = _aligned(live, declared, engine_named[fold(key)], database_collation)
                paths = Model([declared]).diff_paths(Model([aligned]), expressions=False)
                if paths:
                    problems.append(_problem(key, [path for _, path in paths], declared, aligned))
                    continue
                capture = live_captures[fold(key)]
                captures[key] = {
                    label: value
                    for label, value in capture.items()
                    if keep is None or not label.startswith(("index ", "constraint ")) or label in keep
                }
        if problems:
            first = problems[0]
            raise failed(
                "READBACK_MISMATCH",
                f"{first['object']} is not in the database as the model of the release says "
                f"({', '.join(first['properties'][:5])}; {len(problems)} object(s) in all). In a property, "
                "'absent in other' means: not in the database",
                objects=problems,
            )
        return captures

    def engine_named(self, session: Session, keys: Collection[str]) -> tuple[SystemNamed, ...]:
        """The constraints of these objects that the engine named and that the model of the release
        holds, each with the name that its file gives it (fixed_name). Read-only.

        Such a constraint reads back as the constraint of the model (same shape), but its name in
        the database is not the name of the file: a capture under the name of the file would be a
        name that the database does not have. An engine-named constraint that the model does not
        hold is an unmanaged sub-object and is not in the result.
        """
        found = catalog_tables.read_model(session, keys)
        by_table: dict[str, list[SystemNamed]] = defaultdict(list)
        for item in found.system_named:
            by_table[fold(item.table_key)].append(item)
        renames: list[SystemNamed] = []
        for key in keys:
            declared, live, items = self.model.get(key), found.model.get(key), by_table.get(fold(key))
            if declared is None or live is None or not items:
                continue
            wanted = _aligned(live, declared, [item.fixed_name for item in items], lambda: None)[2]
            renames += [
                replace(item, fixed_name=wanted[item.fixed_name])
                for item in items
                if item.fixed_name in wanted
            ]
        return tuple(renames)


if TYPE_CHECKING:
    from azsqlcd.runner import TableHooks as _RunnerTableHooks

    _check: type[_RunnerTableHooks] = Hooks  # the class has the shape that plan and runner ask for


# ------------------------------------------------------------------ export
@dataclass(frozen=True)
class TableExport:
    files: dict[str, bytes]  # 'schema/<kind dir>/<name>.sql' -> text of the object file
    report_md: str  # the table-class part of onboarding/<env>/export.md; the toml list is onboard's
    unmanaged: tuple[Unsupported, ...]  # code UNSUPPORTED | ROUNDTRIP | DEPENDS_ON_UNMANAGED
    rename_constraints_sql: str  # onboarding/<env>/rename-constraints.sql; '' when nothing to rename
    snapshot: dict[str, Any]  # JSON for onboarding/<env>/snapshot.json: {format, captures, column_order}
    # (key of a history table, key of its temporal table): owned by the engine, no file, not unmanaged
    history_tables: tuple[tuple[str, str], ...] = ()


def _needs(obj: ModelObject) -> list[str]:
    """Keys of the objects without which the file of obj does not fit into a model."""
    if isinstance(obj, Schema):
        return []
    found = [] if fold(obj.schema) == "dbo" else [names.object_key("SCHEMA", None, obj.schema)]
    types = [obj.type] if isinstance(obj, Sequence) else []
    if isinstance(obj, Table | TableType):
        types = [c.type for c in obj.columns if c.type is not None]
        found += [_table_key(c.ref_schema, c.ref_table) for c in obj.constraints if isinstance(c, ForeignKey)]
    if isinstance(obj, Table) and obj.temporal is not None and fold(obj.temporal.history_schema) != "dbo":
        # the history table itself is no object of the model; CREATE TABLE needs its schema
        found.append(names.object_key("SCHEMA", None, obj.temporal.history_schema))
    return found + [names.object_key("TYPE", t.schema, t.name) for t in types if t.schema is not None]


def _file(obj: ModelObject) -> tuple[str, str] | Unsupported:
    """(path, text) of the object file, or why the object stays unmanaged (Part 2 (c) 2)."""
    try:
        path = names.path_for(obj.kind, None if isinstance(obj, Schema) else obj.schema, obj.name)
    except ValueError:
        return Unsupported(obj.key, catalog_tables.UNSUPPORTED, "its name cannot be a file name")
    try:
        text = emit.emit_object_file(obj)
        (parsed,) = parse_object_file(text, path)
        differences = emit.token_roundtrip_differences(text, path)
    except ParseError as e:
        return Unsupported(obj.key, ROUNDTRIP, f"its file does not parse: {e.code}: {e.message}")
    except ValueError as e:
        return Unsupported(obj.key, ROUNDTRIP, f"it cannot be written as a file: {e}")
    paths = [path_ for _, path_ in Model([parsed]).diff_paths(Model([obj]))]
    if paths or differences:
        what = ", ".join(paths[:5]) or f"{len(differences)} token difference(s)"
        return Unsupported(obj.key, ROUNDTRIP, f"its file does not read back as the catalog object: {what}")
    return path, text


def _report(
    files: Mapping[str, bytes],
    unmanaged: Iterable[Unsupported],
    by_config: Iterable[str],
    renames: Collection[SystemNamed],
    captures: Mapping[str, Mapping[str, Any]],
    history_tables: Collection[tuple[str, str]] = (),
) -> str:
    unmanaged, by_config = list(unmanaged), sorted(by_config, key=fold)
    lines = [
        "## Table-class objects",
        "",
        f"Files written: {len(files)}. Left unmanaged: {len(unmanaged)}.",
        "",
    ]
    if history_tables:
        lines += [
            "### History tables of temporal tables",
            "",
            f"{len(history_tables)} table(s). The file of a temporal table names its history table; the "
            "engine keeps the structure. They are not unmanaged objects and do not go into azsqlcd.toml.",
            "",
        ]
        lines += [
            f"- `{history}`: history table of `{current}`, owned by the engine, not exported"
            for history, current in history_tables
        ]
        lines.append("")
    if unmanaged:
        lines += ["### Unmanaged", "", "| Object | Code | Reason |", "|---|---|---|"]
        lines += [f"| `{u.object_key}` | {u.code} | {u.reason.replace('|', '/')} |" for u in unmanaged]
        lines.append("")
    if renames:
        lines += [
            "### Constraints with a name that the engine made",
            "",
            f"{len(renames)} constraint(s). The files use fixed names. Review rename-constraints.sql "
            "and run it on this database; it holds EXEC sys.sp_rename only.",
            "",
        ]
        lines += [
            f"- `{r.table_key}` {r.kind}: {names.quote(r.live_name)} -> {names.quote(r.fixed_name)}"
            for r in renames
        ]
        lines.append("")
    flags = ("is_disabled", "is_not_trusted")  # NOT FOR REPLICATION is in the file
    states = [
        f"- `{key}` {label}: {', '.join(flag for flag in flags if value.get(flag))}"
        for key, capture in sorted(captures.items())
        for label, value in sorted(capture.items())
        if isinstance(value, dict) and any(value.get(flag) for flag in flags)
    ]
    if states:
        lines += ["### State that a file cannot say", "", "Recorded in the capture, not in the file:", ""]
        lines += [*states, ""]
    # the list for [unmanaged] objects is not written here: onboard.export writes one list for the
    # whole report, with the modules and the table-class objects (two lists were one too many)
    if by_config:
        lines += ["### Not exported: listed in azsqlcd.toml", ""]
        lines += [f"- `{key}`" for key in by_config]
        lines.append("")
    return "\n".join(lines)


def export_tables(session: Session, config: Config) -> TableExport:
    """The table-class objects of the catalog as object files (Part 2 (c) 1 to 4). Read-only; writes
    nothing to disk.

    One file for each object that the model can hold and whose file reads back as the catalog
    object, and passes the token round trip. A schema file for each schema that holds a managed
    object, never for dbo. An object of [unmanaged] objects in azsqlcd.toml is skipped. An object
    that needs an object which is not exported (the schema, an alias type, the table of a foreign
    key, the schema of the history table) stays unmanaged too: its file would not fit into the
    model. So does the second of two objects with one file path, and a table whose fixed constraint
    name any object of its schema has (catalog.list_user_objects).

    A system-versioned temporal table is a table file like any other. Its history table gets no
    file and is not unmanaged: it is in history_tables and in the report, whether the temporal
    table itself was exported or not.
    """
    # the names of a schema are one namespace: a fixed constraint name must be free in all of it
    taken = {(found.schema, found.name) for found in catalog.list_user_objects(session)}
    found = catalog_tables.read_model(session, taken=taken)
    by_config = {fold(key) for key in config.unmanaged_objects}
    unmanaged = list(found.unsupported)
    texts: dict[str, tuple[str, str]] = {}  # case-folded key -> (path, text)
    kept: dict[str, ModelObject] = {}
    owner: dict[str, str] = {}  # path without case -> key: a file system holds one of two such paths
    for key, obj in found.model.items():
        if fold(key) in by_config:
            continue
        result = _file(obj)
        if not isinstance(result, Unsupported) and result[0].casefold() in owner:
            other = owner[result[0].casefold()]
            reason = f"its file name is the file name of {other}"
            result = Unsupported(obj.key, catalog_tables.UNSUPPORTED, reason)
        if isinstance(result, Unsupported):
            unmanaged.append(result)
        else:
            texts[fold(key)], kept[fold(key)] = result, obj
            owner[result[0].casefold()] = obj.key
    while True:
        lost = [(key, need) for key, obj in kept.items() for need in _needs(obj) if fold(need) not in kept]
        if not lost:
            break
        for key, need in dict(lost).items():
            unmanaged.append(
                Unsupported(kept[key].key, DEPENDS_ON_UNMANAGED, f"it needs {need}, which is not managed")
            )
            del kept[key], texts[key]
    files = {path: text.encode("utf-8") for path, text in sorted(texts.values())}
    renames = [item for item in found.system_named if fold(item.table_key) in kept]
    captures = {obj.key: found.captures[obj.key] for obj in kept.values()}
    order = {
        obj.key: [c.name for c in obj.columns] for obj in kept.values() if isinstance(obj, Table | TableType)
    }
    unmanaged.sort(key=lambda item: fold(item.object_key))
    listed = [
        key for key in config.unmanaged_objects if names.parse_object_key(key)[0] in names.TABLE_CLASS_KINDS
    ]
    return TableExport(
        files,
        _report(files, unmanaged, listed, renames, captures, found.history_tables),
        tuple(unmanaged),
        catalog_tables.rename_constraints_sql(renames),
        {"format": SNAPSHOT_FORMAT, "captures": captures, "column_order": order},
        found.history_tables,
    )


# ------------------------------------------------------------------ baseline compare
@dataclass(frozen=True)
class BaselineItem:
    object_key: str
    state: str  # equal | differs | only_here | missing_here
    properties: tuple[str, ...] = ()  # what differs: capture properties, 'unsupported', 'snapshot'
    warnings: tuple[str, ...] = ()  # reported, never a difference


@dataclass(frozen=True)
class TableBaseline:
    items: tuple[BaselineItem, ...]  # by object key
    rename_constraints_sql: str  # onboarding/<env>/rename-constraints.sql for this database
    captures: dict[str, dict[str, Any]]  # live capture of every managed object that the catalog holds


def _against_reference(
    reference: Mapping[str, Any], live: Mapping[str, Any], engine_named: Iterable[SystemNamed]
) -> tuple[list[str], list[SystemNamed], list[str]]:
    """(properties that differ, renames, sub-objects that only this database has).

    A constraint that the engine named here is the constraint of the reference with the same
    capture value (kind, columns, target, expression text), whatever name the reference has.
    """
    differing = [d.property for d in catalog.capture_differences(dict(reference), dict(live))]
    fixed = {f"constraint {names.quote(item.fixed_name)}": item for item in engine_named}
    free = [label for label in fixed if label not in reference]
    renames = [item for label, item in fixed.items() if label in reference and label not in differing]
    for label in [p for p in differing if p.startswith("constraint ") and p not in live]:
        twin = next((other for other in free if live[other] == reference[label]), None)
        if twin is not None:
            free.remove(twin)
            differing.remove(label)
            wanted = label.removeprefix("constraint ")[1:-1].replace("]]", "]")
            renames.append(replace(fixed[twin], fixed_name=wanted))
    matched = set(fixed) - set(free)
    extra = sorted(
        label
        for label in live
        if label.startswith(("index ", "constraint ")) and label not in reference and label not in matched
    )
    return differing, renames, extra


def compare_with_snapshot(
    session: Session, snapshot: Mapping[str, Any], managed_keys: Collection[str]
) -> TableBaseline:
    """This database against the snapshot of the reference database (Part 2 (c) 6). Read-only.

    Catalog against catalog: expression text is compared, because the engine wrote both sides.
    An item for each managed key and for each table-class object that only this database has.
    differs: the capture properties that differ; 'unsupported' when the object is outside the
    model here; 'snapshot' when the snapshot does not hold the managed key. Another column order
    and sub-objects that only this database has are warnings. ToolError REFUSED SNAPSHOT_INVALID
    for a dict that export_tables did not make.

    The temporal facts of a table are capture properties ('temporal', and the period columns), so
    a switched-off versioning, another history table or another retention is `differs`. A history
    table has no item: it is the engine's part of its temporal table, never `only_here`.
    """
    reference, order = snapshot.get("captures"), snapshot.get("column_order")
    if (
        snapshot.get("format") != SNAPSHOT_FORMAT
        or not isinstance(reference, dict)
        or not isinstance(order, dict)
    ):
        raise refused("SNAPSHOT_INVALID", f"the snapshot is not a table snapshot of format {SNAPSHOT_FORMAT}")
    reference = {fold(key): capture for key, capture in reference.items()}
    order = {fold(key): columns for key, columns in order.items()}
    found = catalog_tables.read_model(session)
    outside = {fold(item.object_key) for item in found.unsupported}
    engine_named: dict[str, list[SystemNamed]] = defaultdict(list)
    for item in found.system_named:
        engine_named[fold(item.table_key)].append(item)
    items: list[BaselineItem] = []
    renames: list[SystemNamed] = []
    captures: dict[str, dict[str, Any]] = {}
    managed = {fold(key): key for key in managed_keys}
    for folded, key in managed.items():
        live = found.model.get(key)
        if live is None:
            gone = folded in outside
            items.append(BaselineItem(key, DIFFERS if gone else MISSING_HERE, (SUPPORTED,) if gone else ()))
            continue
        captures[live.key] = capture = found.captures[live.key]
        if folded not in reference:
            items.append(BaselineItem(live.key, DIFFERS, ("snapshot",)))
            continue
        differing, matched, extra = _against_reference(reference[folded], capture, engine_named[folded])
        renames += matched
        warnings = [f"{label} exists only here: an unmanaged sub-object" for label in extra]
        here = [fold(c.name) for c in live.columns] if isinstance(live, Table | TableType) else []
        there = [fold(name) for name in order.get(folded, [])]
        if here != there and sorted(here) == sorted(there):
            warnings.append("the columns are in another order here")
        items.append(
            BaselineItem(live.key, DIFFERS if differing else EQUAL, tuple(differing), tuple(warnings))
        )
    items += [BaselineItem(key, ONLY_HERE) for key in found.model if fold(key) not in managed]
    items += [
        BaselineItem(u.object_key, ONLY_HERE, (SUPPORTED,))
        for u in found.unsupported
        if fold(u.object_key) not in managed
    ]
    items.sort(key=lambda item: fold(item.object_key))
    return TableBaseline(tuple(items), catalog_tables.rename_constraints_sql(renames), captures)
