"""The table-class catalog reader. FakeSession gives catalog rows; the tests pin what the tool reads.

No test here runs T-SQL. rows_from_model() writes the result sets of the reader for a model, so
that the reader is proven against the model of the parser: what a file says, read back from the
catalog rows of that file, is the same model.
"""

import dataclasses
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from azsqlcd import catalog, catalog_tables, lex, names
from azsqlcd.catalog_tables import (
    OPTION_DEFAULTS,
    UNSUPPORTED,
    CatalogRead,
    SystemNamed,
    project_options,
    read_model,
    rename_constraints_sql,
)
from azsqlcd.errors import Exit, ToolError
from azsqlcd.model import (
    INDEX_OPTION_VALUES,
    AliasType,
    Check,
    Column,
    Computed,
    DefaultConstraint,
    Expression,
    ForeignKey,
    Identity,
    Index,
    KeyColumn,
    Model,
    ModelObject,
    PrimaryKey,
    Schema,
    Sequence,
    Synonym,
    Table,
    TableType,
    Temporal,
    TypeRef,
    Unique,
    builtin_shape,
    fold,
)
from azsqlcd.parse import parse_object_file
from fixtures.pairs import loader
from support.fake_session import FakeSession

DDL_OK = Path(__file__).resolve().parents[1] / "fixtures" / "ddl" / "ok"
WRITES = frozenset("INSERT UPDATE DELETE MERGE EXEC EXECUTE CREATE ALTER DROP INTO SET".split())
HOSTILE = "x'; DROP TABLE [azsqlcd].[run]; --]"

# The contract of the reader: query name -> the columns of its result set, in order. The tag of a
# batch is /* azsqlcd:read_<query name> */.
COLUMNS: dict[str, tuple[str, ...]] = {
    "schemas": ("name", "owner_name", "module_count"),
    "tabulars": (
        "object_id",
        "kind",
        "schema_name",
        "name",
        "temporal_type",
        "ledger_type",
        "is_memory_optimized",
        "is_node",
        "is_edge",
        "is_external",
        "has_fulltext_index",
        "history_schema",
        "history_table",
        "history_retention_period",
        "history_retention_period_unit",
        "period_start_column",
        "period_end_column",
        "current_schema",
        "current_table",
    ),
    "columns": (
        "object_id",
        "column_id",
        "name",
        "type_schema",
        "type_name",
        "type_is_user_defined",
        "type_is_assembly",
        "max_length",
        "precision",
        "scale",
        "is_nullable",
        "collation_name",
        "is_identity",
        "seed_value",
        "increment_value",
        "identity_not_for_replication",
        "is_computed",
        "definition",
        "is_persisted",
        "is_sparse",
        "is_column_set",
        "is_masked",
        "encryption_type",
        "is_rowguidcol",
        "is_filestream",
        "generated_always_type",
        "is_hidden",
        "xml_collection_id",
        "masking_function",
    ),
    "defaults": ("parent_object_id", "parent_column_id", "name", "definition", "is_system_named"),
    "checks": (
        "parent_object_id",
        "name",
        "definition",
        "is_system_named",
        "is_disabled",
        "is_not_trusted",
        "is_not_for_replication",
    ),
    "indexes": (
        "object_id",
        "index_id",
        "name",
        "type",
        "type_desc",
        "is_unique",
        "is_primary_key",
        "is_unique_constraint",
        "constraint_is_system_named",
        "is_hypothetical",
        "auto_created",
        "has_filter",
        "filter_definition",
        "fill_factor",
        "is_padded",
        "ignore_dup_key",
        "allow_row_locks",
        "allow_page_locks",
        "optimize_for_sequential_key",
        "no_recompute",
        "is_disabled",
        "data_space_type",
        "partition_count",
        "data_compression",
        "xml_compression",
        "compression_delay",
    ),
    "index_columns": (
        "object_id",
        "index_id",
        "key_ordinal",
        "index_column_id",
        "is_descending_key",
        "is_included_column",
        "column_name",
        "column_store_order_ordinal",
    ),
    "foreign_keys": (
        "parent_object_id",
        "name",
        "is_system_named",
        "referenced_schema",
        "referenced_table",
        "delete_referential_action_desc",
        "update_referential_action_desc",
        "is_disabled",
        "is_not_trusted",
        "is_not_for_replication",
        "constraint_column_id",
        "column_name",
        "referenced_column_name",
    ),
    "alias_types": (
        "schema_name",
        "name",
        "base_type_name",
        "is_assembly_type",
        "max_length",
        "precision",
        "scale",
        "is_nullable",
    ),
    "sequences": (
        "schema_name",
        "name",
        "type_schema",
        "type_name",
        "type_is_user_defined",
        "precision",
        "scale",
        "start_value",
        "increment",
        "minimum_value",
        "maximum_value",
        "is_cycling",
        "is_cached",
        "cache_size",
    ),
    "synonyms": ("schema_name", "name", "base_object_name"),
}
COMPRESSION = {"NONE": 0, "ROW": 1, "PAGE": 2, "COLUMNSTORE": 3, "COLUMNSTORE_ARCHIVE": 4}
# what a normal table has in the temporal columns of the tabulars row (all NULL in the catalog)
NOT_TEMPORAL: dict[str, Any] = dict.fromkeys(COLUMNS["tabulars"][11:])
GENERATED_ALWAYS_TYPE = {None: 0, "ROW_START": 1, "ROW_END": 2}  # sys.columns.generated_always_type
RETENTION_UNIT = {"DAYS": 3, "WEEKS": 4, "MONTHS": 5, "YEARS": 6}  # history_retention_period_unit
HISTORY_ID = 100_000  # object_id of the history table of table n is n + HISTORY_ID

type CatalogRows = dict[str, list[dict[str, Any]]]


# ------------------------------------------------------------------ a model as catalog rows
def sql(expression: Expression) -> str:
    return " ".join(expression.tokens)


def type_facts(type_: TypeRef) -> dict[str, Any]:
    """A declared data type as sys.columns and sys.types hold it: lengths in bytes."""
    facts = {"type_schema": "sys", "type_name": type_.name, "type_is_user_defined": 0, "type_is_assembly": 0}
    facts |= {"max_length": 0, "precision": type_.precision or 0, "scale": type_.scale or 0}
    if type_.schema is not None:
        return facts | {"type_schema": type_.schema, "type_is_user_defined": 1}
    if type_.length == "max":
        facts["max_length"] = -1
    elif type_.length is not None:
        low = type_.name.lower()
        facts["max_length"] = (
            8 + 4 * type_.length
            if low == "vector"
            else 2 * type_.length
            if low in ("nchar", "nvarchar")
            else type_.length
        )
    return facts


def column_rows(object_id: int, column_id: int, column: Column) -> dict[str, Any]:
    row: dict[str, Any] = dict.fromkeys(COLUMNS["columns"], 0)
    row |= {"object_id": object_id, "column_id": column_id, "name": column.name}
    row |= {"collation_name": column.collation, "encryption_type": None, "definition": None}
    row |= {"seed_value": None, "increment_value": None, "identity_not_for_replication": None}
    row |= {"is_persisted": None}
    # sys.masked_columns has a row only for a masked column: NULL through the LEFT JOIN otherwise
    row |= {"is_masked": int(column.masked is not None), "masking_function": column.masked}
    if column.computed is not None:
        # only PERSISTED NOT NULL makes the column not nullable in this catalog
        row |= type_facts(TypeRef("int")) | {"is_nullable": 0 if column.nullable is False else 1}
        row |= {"is_computed": 1, "definition": sql(column.computed.expression)}
        return row | {"is_persisted": int(column.computed.persisted)}
    assert column.type is not None
    row |= type_facts(column.type) | {"is_nullable": int(bool(column.nullable))}
    row |= {"generated_always_type": GENERATED_ALWAYS_TYPE[column.generated], "is_hidden": int(column.hidden)}
    if column.identity is not None:
        row |= {"is_identity": 1, "identity_not_for_replication": int(column.identity.not_for_replication)}
        row |= {
            "seed_value": Decimal(column.identity.seed),
            "increment_value": Decimal(column.identity.increment),
        }
    return row | {"is_rowguidcol": int(column.rowguidcol), "is_sparse": int(column.sparse)}


def index_row(
    object_id: int, index_id: int, item: PrimaryKey | Unique | Index, is_type: bool, engine_name: str
) -> dict[str, Any]:
    options = dict(item.options)
    row: dict[str, Any] = dict.fromkeys(COLUMNS["indexes"], 0)
    row |= {
        "object_id": object_id,
        "index_id": index_id,
        "name": item.name or engine_name,
        "type": 1 if item.clustered else 2,
        "type_desc": "CLUSTERED" if item.clustered else "NONCLUSTERED",
        "is_unique": 1,
        "constraint_is_system_named": None,
        "filter_definition": None,
        "fill_factor": int(options.get("FILLFACTOR", "0")),
        "is_padded": int(options.get("PAD_INDEX") == "ON"),
        "ignore_dup_key": int(options.get("IGNORE_DUP_KEY") == "ON"),
        "allow_row_locks": int(options.get("ALLOW_ROW_LOCKS") != "OFF"),
        "allow_page_locks": int(options.get("ALLOW_PAGE_LOCKS") != "OFF"),
        "optimize_for_sequential_key": int(options.get("OPTIMIZE_FOR_SEQUENTIAL_KEY") == "ON"),
        "no_recompute": int(options.get("STATISTICS_NORECOMPUTE") == "ON"),
        "data_space_type": "FG",
        # the table of a table type has no partition row
        "partition_count": 0 if is_type else 1,
        "data_compression": None if is_type else COMPRESSION[options.get("DATA_COMPRESSION", "NONE")],
        "xml_compression": None if is_type else int(options.get("XML_COMPRESSION") == "ON"),
        "compression_delay": None,
    }
    if isinstance(item, Index) and item.columnstore:
        row |= {"type": 5 if item.clustered else 6, "is_unique": 0, "fill_factor": 0}
        row |= {"type_desc": ("" if item.clustered else "NON") + "CLUSTERED COLUMNSTORE"}
        row |= {"data_compression": COMPRESSION[options.get("DATA_COMPRESSION", "COLUMNSTORE")]}
        row |= {"compression_delay": int(options.get("COMPRESSION_DELAY", "0"))}
    if isinstance(item, Index):
        row |= {"is_unique": int(item.unique), "has_filter": int(item.filter is not None)}
        row |= {"filter_definition": None if item.filter is None else sql(item.filter)}
    else:
        row |= {"is_primary_key": int(isinstance(item, PrimaryKey))}
        row |= {"is_unique_constraint": int(isinstance(item, Unique))}
        row |= {"constraint_is_system_named": int(item.name is None)}
    return row


def heap_row(object_id: int, is_type: bool) -> dict[str, Any]:
    row: dict[str, Any] = dict.fromkeys(COLUMNS["indexes"], 0)
    row |= {"object_id": object_id, "name": None, "type_desc": "HEAP", "constraint_is_system_named": None}
    row |= {"filter_definition": None, "allow_row_locks": 1, "allow_page_locks": 1, "no_recompute": None}
    row |= {"data_space_type": "FG", "partition_count": 0 if is_type else 1}
    row |= {"data_compression": None if is_type else 0, "xml_compression": None if is_type else 0}
    return row | {"compression_delay": None}


def history_rows(
    object_id: int, table: Table, temporal: Temporal, head: dict[str, Any], rows: CatalogRows
) -> None:
    """A system-versioned table: the temporal facts of its row, and its history table.

    The history table is a table of the catalog like any other (temporal_type 1): the columns of
    the current table as plain columns, no key, no constraint.
    """
    period, unit = temporal.retention or (None, None)
    head |= {
        "temporal_type": 2,
        "history_schema": temporal.history_schema,
        "history_table": temporal.history_table,
        "history_retention_period": -1 if period is None else period,
        "history_retention_period_unit": -1 if unit is None else RETENTION_UNIT[unit],
        "period_start_column": temporal.period_start,
        "period_end_column": temporal.period_end,
    }
    history: dict[str, Any] = dict.fromkeys(COLUMNS["tabulars"], 0) | NOT_TEMPORAL
    history |= {"object_id": object_id + HISTORY_ID, "kind": "TABLE", "temporal_type": 1}
    history |= {"schema_name": temporal.history_schema, "name": temporal.history_table}
    history |= {"current_schema": table.schema, "current_table": table.name}
    rows["tabulars"].append(history)
    rows["indexes"].append(heap_row(object_id + HISTORY_ID, False))
    for column_id, column in enumerate(table.columns, 1):
        if column.computed is None:
            plain = Column(column.name, column.type, column.nullable, collation=column.collation)
            rows["columns"].append(column_rows(object_id + HISTORY_ID, column_id, plain))


def tabular_rows(object_id: int, obj: Table | TableType, rows: CatalogRows) -> None:
    is_type = isinstance(obj, TableType)
    head: dict[str, Any] = dict.fromkeys(COLUMNS["tabulars"], 0) | NOT_TEMPORAL
    head |= {"object_id": object_id, "kind": obj.kind, "schema_name": obj.schema, "name": obj.name}
    rows["tabulars"].append(head)
    if isinstance(obj, Table) and obj.temporal is not None:
        history_rows(object_id, obj, obj.temporal, head, rows)
    for column_id, column in enumerate(obj.columns, 1):
        rows["columns"].append(column_rows(object_id, column_id, column))
        if column.default is not None:
            rows["defaults"].append(
                {
                    "parent_object_id": object_id,
                    "parent_column_id": column_id,
                    "name": column.default.name or f"DF__engine__{object_id}_{column_id}",
                    "definition": sql(column.default.expression),
                    "is_system_named": int(column.default.name is None),
                }
            )
    keyed = [c for c in obj.constraints if isinstance(c, PrimaryKey | Unique)] + list(obj.indexes)
    keyed.sort(key=lambda item: not item.clustered)
    if not any(item.clustered for item in keyed):
        heap = heap_row(object_id, is_type)
        if isinstance(obj, Table) and obj.compression is not None:
            heap["data_compression"] = COMPRESSION[obj.compression]
        rows["indexes"].append(heap)
    for number, item in enumerate(keyed):
        index_id = 1 if item.clustered else number + 2
        rows["indexes"].append(
            index_row(object_id, index_id, item, is_type, f"K__engine__{object_id}_{number}")
        )
        members = [(c.name, int(c.descending), 0, 0) for c in item.columns]
        members += [(name, 0, 1, 0) for name in (item.included if isinstance(item, Index) else ())]
        if isinstance(item, Index) and item.columnstore:
            # the engine lists every column of a clustered columnstore index, and the columns of a
            # nonclustered one, as included columns; an ORDER column has its ordinal
            order = [fold(c.name) for c in item.columns]
            listed = [c.name for c in obj.columns] if item.clustered else list(item.included)
            members = [
                (name, 0, 1, order.index(fold(name)) + 1 if fold(name) in order else 0) for name in listed
            ]
        for position, (name, descending, included, ordinal) in enumerate(members, 1):
            rows["index_columns"].append(
                {
                    "object_id": object_id,
                    "index_id": index_id,
                    "key_ordinal": 0 if included else position,
                    "index_column_id": position,
                    "is_descending_key": descending,
                    "is_included_column": included,
                    "column_name": name,
                    "column_store_order_ordinal": ordinal,
                }
            )
    for number, constraint in enumerate(obj.constraints):
        if isinstance(constraint, Check):
            rows["checks"].append(
                {
                    "parent_object_id": object_id,
                    "name": constraint.name or f"CK__engine__{object_id}_{number}",
                    "definition": sql(constraint.expression),
                    "is_system_named": int(constraint.name is None),
                    "is_disabled": 0,
                    "is_not_trusted": 0,
                    "is_not_for_replication": int(constraint.not_for_replication),
                }
            )
        elif isinstance(constraint, ForeignKey):
            pairs = zip(constraint.columns, constraint.ref_columns, strict=True)
            for position, (column, ref_column) in enumerate(pairs, 1):
                rows["foreign_keys"].append(
                    {
                        "parent_object_id": object_id,
                        "name": constraint.name,
                        "is_system_named": 0,
                        "referenced_schema": constraint.ref_schema,
                        "referenced_table": constraint.ref_table,
                        "delete_referential_action_desc": constraint.on_delete.replace(" ", "_"),
                        "update_referential_action_desc": constraint.on_update.replace(" ", "_"),
                        "is_disabled": 0,
                        "is_not_trusted": 0,
                        "is_not_for_replication": int(constraint.not_for_replication),
                        "constraint_column_id": position,
                        "column_name": column,
                        "referenced_column_name": ref_column,
                    }
                )


def rows_from_model(model: Model) -> CatalogRows:
    """The result sets of the reader for a database that holds exactly this model.

    One dict for each row, keyed by the names of COLUMNS, so that a test can change one fact.
    A system-versioned table brings its history table: the catalog holds it, the model does not.
    Built-in schemas are added, as every database has them. A schema of the model that holds no
    table-class object is given one module: an empty schema is not managed.
    """
    rows: CatalogRows = {name: [] for name in COLUMNS}
    used = {fold(obj.schema) for obj in model.values() if not isinstance(obj, Schema)}
    for name in ("dbo", "sys", "guest", "INFORMATION_SCHEMA", "db_owner"):
        rows["schemas"].append({"name": name, "owner_name": name, "module_count": 0})
    for object_id, obj in enumerate(model.values(), 1000):
        match obj:
            case Schema():
                modules = 0 if fold(obj.name) in used else 1
                rows["schemas"].append(
                    {"name": obj.name, "owner_name": obj.owner or "dbo", "module_count": modules}
                )
            case Table() | TableType():
                tabular_rows(object_id, obj, rows)
            case AliasType():
                facts = type_facts(obj.base)
                rows["alias_types"].append(
                    {
                        "schema_name": obj.schema,
                        "name": obj.name,
                        "base_type_name": obj.base.name,
                        "is_assembly_type": 0,
                        "max_length": facts["max_length"],
                        "precision": facts["precision"],
                        "scale": facts["scale"],
                        "is_nullable": int(obj.nullable),
                    }
                )
            case Sequence():
                facts = type_facts(obj.type)
                rows["sequences"].append(
                    {
                        "schema_name": obj.schema,
                        "name": obj.name,
                        "type_schema": facts["type_schema"],
                        "type_name": facts["type_name"],
                        "type_is_user_defined": facts["type_is_user_defined"],
                        "precision": facts["precision"],
                        "scale": facts["scale"],
                        # sql_variant columns, CAST to decimal(38, 0): the driver gives Decimal
                        "start_value": Decimal(obj.start),
                        "increment": Decimal(obj.increment),
                        "minimum_value": Decimal(obj.minvalue),
                        "maximum_value": Decimal(obj.maxvalue),
                        "is_cycling": int(obj.cycle),
                        "is_cached": int(obj.cached),
                        "cache_size": obj.cache_size,
                    }
                )
            case Synonym():
                base = names.qualified(obj.target_schema, obj.target_name)
                rows["synonyms"].append(
                    {"schema_name": obj.schema, "name": obj.name, "base_object_name": base}
                )
    return rows


def session_for(rows: CatalogRows) -> FakeSession:
    """A session that answers each query of the reader with the rows, in the column order of COLUMNS."""
    db = FakeSession()
    for name, columns in COLUMNS.items():
        for row in rows[name]:
            assert tuple(row) == columns, f"a {name} row does not have the columns of the contract"
        db.respond(f"/* azsqlcd:read_{name} */", [[tuple(row.values()) for row in rows[name]]])
    return db


def read(rows: CatalogRows, keys: list[str] | None = None) -> CatalogRead:
    return read_model(session_for(rows), keys)


def only(rows: CatalogRows, query: str, **where: Any) -> dict[str, Any]:
    """The one row of a query that has these values."""
    found = [row for row in rows[query] if all(row[name] == value for name, value in where.items())]
    assert len(found) == 1, f"{len(found)} {query} rows match {where}"
    return found[0]


# ------------------------------------------------------------------ models
ORDER = Table(
    "sales",
    "Order",
    (
        Column("OrderId", TypeRef("int"), False, identity=None),
        Column("CustomerId", TypeRef("int"), False),
        Column(
            "Status",
            TypeRef("tinyint"),
            False,
            default=DefaultConstraint("DF_Order_Status", Expression.from_sql("((0))")),
        ),
        Column("Note", TypeRef("nvarchar", length=50), True),
        Column("Total", None, None, computed=Computed(Expression.from_sql("([Status]*(2))"))),
    ),
    (
        PrimaryKey("PK_Order", True, (KeyColumn("OrderId"),)),
        Unique("UQ_Order_Note", False, (KeyColumn("Note"), KeyColumn("Status", True))),
        Check("CK_Order_Status", Expression.from_sql("([Status]<(9))")),
        ForeignKey("FK_Order_Customer", ("CustomerId",), "sales", "Customer", ("CustomerId",), "CASCADE"),
    ),
    (Index("IX_Order_Status", False, False, (KeyColumn("Status"),), ("Note",)),),
)
CUSTOMER = Table(
    "sales",
    "Customer",
    (Column("CustomerId", TypeRef("int"), False),),
    (PrimaryKey("PK_Customer", True, (KeyColumn("CustomerId"),)),),
)
SALES = Schema("sales")
ORDER_KEY, CUSTOMER_KEY = ORDER.key, CUSTOMER.key


def sales_rows() -> CatalogRows:
    return rows_from_model(Model([SALES, ORDER, CUSTOMER]))


def order_of(found: CatalogRead) -> Table:
    table = found.model[ORDER_KEY]
    assert isinstance(table, Table)
    return table


# ------------------------------------------------------------------ the property
def fixture_models() -> list[Any]:
    cases = [
        pytest.param(loader.model(case, "head"), id=f"pair:{case}")
        for case in loader.CASES
        if not case.startswith("refuse_")
    ]
    for path in sorted(DDL_OK.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        first = text.splitlines()[0]
        if first.startswith("-- path: "):
            objects = parse_object_file(text, first.removeprefix("-- path: ").strip())
            cases.append(pytest.param(Model(objects), id=f"ddl:{path.stem}"))
    return cases


def states_a_default(obj: ModelObject) -> bool:
    """True when the file writes something that is what the engine does anyway."""
    if isinstance(obj, Schema):
        return obj.owner is not None and fold(obj.owner) == "dbo"
    if not isinstance(obj, Table | TableType):
        return False
    keyed = [c for c in obj.constraints if isinstance(c, PrimaryKey | Unique)] + list(obj.indexes)

    def default(item: PrimaryKey | Unique | Index, name: str) -> str:
        columnstore = isinstance(item, Index) and item.columnstore
        return (catalog_tables.COLUMNSTORE_OPTION_DEFAULTS if columnstore else OPTION_DEFAULTS)[name]

    return any(
        value == default(item, name) or (name, value) == ("FILLFACTOR", "100")
        for item in keyed
        for name, value in item.options
    )


def test_the_fixture_models_cover_every_kind_of_object():
    kinds = {type(obj) for case in fixture_models() for obj in case.values[0].values()}
    assert kinds == {Schema, Table, TableType, AliasType, Sequence, Synonym}
    assert len(fixture_models()) > 50


@pytest.mark.parametrize("model", fixture_models())
def test_the_catalog_rows_of_a_model_are_read_back_as_that_model(model: Model):
    found = read(rows_from_model(model))

    assert found.unsupported == ()
    assert found.system_named == ()
    assert set(found.captures) == set(found.model)
    # a file may state a value that is the default of the engine; the catalog cannot say that it did
    projected = Model(project_options(found.model[key], obj) for key, obj in model.items())
    assert projected.diff_paths(model) == []
    plain = Model(obj for obj in model.values() if not states_a_default(obj))
    assert Model(found.model[key] for key in plain).diff_paths(plain) == []
    for key, obj in model.items():
        if states_a_default(obj):
            assert found.model[key] != obj  # the reader does not invent what was declared


# ------------------------------------------------------------------ declared units
def test_nvarchar_50_is_read_as_length_50_not_100():
    rows = sales_rows()
    assert only(rows, "columns", name="Note")["max_length"] == 100  # bytes, as sys.columns holds it

    note = order_of(read(rows)).column("Note")

    assert note is not None and note.type == TypeRef("nvarchar", length=50)


@pytest.mark.parametrize(
    ("type_name", "max_length", "precision", "scale", "expected"),
    [
        ("nchar", 10, 0, 0, TypeRef("nchar", length=5)),
        ("nvarchar", -1, 0, 0, TypeRef("nvarchar", length="max")),
        ("varchar", 50, 0, 0, TypeRef("varchar", length=50)),
        ("varbinary", -1, 0, 0, TypeRef("varbinary", length="max")),
        ("binary", 16, 0, 0, TypeRef("binary", length=16)),
        ("decimal", 9, 19, 4, TypeRef("decimal", precision=19, scale=4)),
        ("numeric", 5, 5, 0, TypeRef("numeric", precision=5, scale=0)),
        ("datetime2", 8, 27, 7, TypeRef("datetime2", scale=7)),
        ("time", 3, 8, 0, TypeRef("time", scale=0)),
        ("datetimeoffset", 10, 34, 7, TypeRef("datetimeoffset", scale=7)),
        ("float", 8, 53, 0, TypeRef("float")),
        ("real", 4, 24, 0, TypeRef("real")),
        ("timestamp", 8, 0, 0, TypeRef("timestamp")),
        ("sysname", 256, 0, 0, TypeRef("sysname")),
        ("xml", -1, 0, 0, TypeRef("xml")),
        ("vector", 8 + 4 * 1536, 0, 0, TypeRef("vector", length=1536)),
    ],
)
def test_a_data_type_is_read_in_the_units_of_its_declaration(
    type_name: str, max_length: int, precision: int, scale: int, expected: TypeRef
):
    rows = sales_rows()
    only(rows, "columns", name="Note").update(
        type_name=type_name, max_length=max_length, precision=precision, scale=scale
    )

    note = order_of(read(rows)).column("Note")

    assert note is not None and note.type == expected
    assert (note.type.length, note.type.precision, note.type.scale) == (
        expected.length,
        expected.precision,
        expected.scale,
    )


def test_a_column_of_an_alias_type_names_the_type_with_its_schema():
    rows = sales_rows()
    only(rows, "columns", name="Note").update(type_schema="sales", type_name="Code", type_is_user_defined=1)

    note = order_of(read(rows)).column("Note")

    assert note is not None and note.type == TypeRef("Code", "sales")


def test_a_number_of_a_sequence_is_read_as_int_whatever_the_driver_gives():
    model = Model([Sequence("dbo", "OrderNo", TypeRef("bigint"), 1, 1, -(2**63), 2**63 - 1, False, True, 50)])
    rows = rows_from_model(model)
    assert isinstance(rows["sequences"][0]["start_value"], Decimal)
    rows["sequences"][0]["maximum_value"] = "9223372036854775807"  # a driver that gives text

    found = read(rows)

    sequence = found.model["SEQUENCE:[dbo].[OrderNo]"]
    assert isinstance(sequence, Sequence)
    numbers = (sequence.start, sequence.increment, sequence.minvalue, sequence.maxvalue)
    assert numbers == (1, 1, -(2**63), 2**63 - 1)
    assert all(type(number) is int for number in numbers)
    json.dumps(found.captures["SEQUENCE:[dbo].[OrderNo]"])  # a Decimal would not be JSON


def test_a_number_that_is_not_whole_is_a_defect_of_the_query_not_a_fact():
    rows = rows_from_model(Model([Sequence("dbo", "N", TypeRef("int"), 1, 1, 1, 9, False, False)]))
    rows["sequences"][0]["increment"] = Decimal("1.5")

    with pytest.raises(TypeError, match="not a whole number"):
        read(rows)


def test_a_sequence_without_cache_has_no_cache_size():
    rows = rows_from_model(Model([Sequence("dbo", "N", TypeRef("int"), 1, 1, 1, 9, False, False)]))
    rows["sequences"][0]["cache_size"] = 50  # the catalog can keep the old size after NO CACHE

    sequence = read(rows).model["SEQUENCE:[dbo].[N]"]

    assert isinstance(sequence, Sequence) and (sequence.cached, sequence.cache_size) == (False, None)


# ------------------------------------------------------------------ as declared
def test_an_option_at_the_engine_default_is_not_written_into_the_model():
    rows = sales_rows()
    for row in rows["indexes"]:
        row.update(fill_factor=100, is_padded=0, ignore_dup_key=0, allow_row_locks=1, allow_page_locks=1)
        row.update(optimize_for_sequential_key=0, no_recompute=0, data_compression=0)
    only(rows, "indexes", name="PK_Order")["fill_factor"] = 0

    table = order_of(read(rows))

    assert [c.options for c in table.constraints if isinstance(c, PrimaryKey | Unique)] == [(), ()]
    assert [i.options for i in table.indexes] == [()]


def test_every_option_that_is_not_the_engine_default_is_in_the_model():
    rows = sales_rows()
    only(rows, "indexes", name="IX_Order_Status").update(
        fill_factor=80,
        is_padded=1,
        ignore_dup_key=1,
        allow_row_locks=0,
        allow_page_locks=0,
        optimize_for_sequential_key=1,
        no_recompute=1,
        data_compression=2,
    )

    index = order_of(read(rows)).index("IX_Order_Status")

    assert index is not None and index.options == (
        ("ALLOW_PAGE_LOCKS", "OFF"),
        ("ALLOW_ROW_LOCKS", "OFF"),
        ("DATA_COMPRESSION", "PAGE"),
        ("FILLFACTOR", "80"),
        ("IGNORE_DUP_KEY", "ON"),
        ("OPTIMIZE_FOR_SEQUENTIAL_KEY", "ON"),
        ("PAD_INDEX", "ON"),
        ("STATISTICS_NORECOMPUTE", "ON"),
    )


def test_the_table_of_engine_defaults_covers_every_option_of_the_model():
    assert set(OPTION_DEFAULTS) == set(INDEX_OPTION_VALUES)


def index_with(*options: tuple[str, str]) -> Table:
    return Table(
        "dbo",
        "T",
        (Column("a", TypeRef("int"), False),),
        (PrimaryKey("PK_T", True, (KeyColumn("a"),), options),),
        (Index("IX_T", False, False, (KeyColumn("a"),), options=options),),
    )


def test_a_live_option_that_is_not_the_engine_default_is_compared_whether_the_file_states_it_or_not():
    # A14 (OM-03): what a file does not state is the engine default, so a live value that is not
    # the default is a difference. IGNORE_DUP_KEY = ON on a key would pass a read-back otherwise
    live = index_with(("DATA_COMPRESSION", "PAGE"), ("FILLFACTOR", "80"))

    assert project_options(live, index_with()) == live != index_with()
    assert project_options(live, index_with(("FILLFACTOR", "80"))) == live != index_with(("FILLFACTOR", "80"))
    assert project_options(live, live) == live
    # a stated option that differs stays a difference
    assert project_options(live, index_with(("FILLFACTOR", "90"))) != index_with(("FILLFACTOR", "90"))
    for option in OPTION_DEFAULTS:
        value = next(v for v in sorted(INDEX_OPTION_VALUES[option] or ["80"]) if v != OPTION_DEFAULTS[option])
        if (option, value) == ("FILLFACTOR", "100"):
            continue
        assert project_options(index_with((option, value)), index_with()) != index_with(), option


@pytest.mark.parametrize(
    "stated",
    [
        ("FILLFACTOR", "100"),
        ("FILLFACTOR", "0"),
        ("PAD_INDEX", "OFF"),
        ("ALLOW_ROW_LOCKS", "ON"),
        ("DATA_COMPRESSION", "NONE"),
    ],
)
def test_a_stated_engine_default_equals_a_catalog_that_holds_the_default(stated: tuple[str, str]):
    assert project_options(index_with(), index_with(stated)) == index_with(stated)


def test_a_stated_default_does_not_hide_a_live_value_that_is_not_the_default():
    live = index_with(("FILLFACTOR", "80"), ("PAD_INDEX", "ON"))
    declared = index_with(("FILLFACTOR", "100"), ("PAD_INDEX", "OFF"))

    assert project_options(live, declared) == live != declared


@pytest.mark.parametrize("stated", [("FILLFACTOR", "90"), ("PAD_INDEX", "ON"), ("DATA_COMPRESSION", "PAGE")])
def test_a_stated_option_is_not_met_by_a_catalog_that_holds_the_engine_default(stated: tuple[str, str]):
    projected = project_options(index_with(), index_with(stated))

    assert projected != index_with(stated)
    assert projected == index_with((stated[0], OPTION_DEFAULTS[stated[0]]))


def test_the_owner_of_a_schema_is_compared_only_when_the_file_states_one():
    rows = rows_from_model(Model([Schema("sales"), Schema("audit", "auditor")]))
    only(rows, "schemas", name="sales")["owner_name"] = "azsqlcd-sales-dev-deploy"
    live = read(rows).model

    assert live["SCHEMA:[audit]"] == Schema("audit", "auditor")
    assert project_options(live["SCHEMA:[sales]"], Schema("sales")) == Schema("sales")
    assert project_options(live["SCHEMA:[sales]"], Schema("sales", "dbo")) != Schema("sales", "dbo")
    assert project_options(Schema("x"), Schema("x", "dbo")) == Schema("x", "dbo")  # read_model leaves dbo out


def test_a_collation_is_in_the_model_only_when_the_query_says_it_is_not_the_database_default():
    rows = sales_rows()
    only(rows, "columns", name="Note")["collation_name"] = "Latin1_General_100_BIN2"

    db = session_for(rows)
    table = order_of(read_model(db))

    note, status = table.column("Note"), table.column("Status")
    assert note is not None and note.collation == "Latin1_General_100_BIN2"
    assert status is not None and status.collation is None
    (batch,) = db.sent("azsqlcd:read_columns")
    assert "c.[collation_name] <> CAST(DATABASEPROPERTYEX(DB_NAME(), N'Collation')" in batch


@pytest.mark.parametrize(
    ("is_persisted", "is_nullable", "expected"),
    [(1, 0, False), (1, 1, None), (0, 0, None), (0, 1, None)],
)
def test_a_computed_column_is_not_null_only_when_it_is_persisted_and_not_nullable(
    is_persisted: int, is_nullable: int, expected: bool | None
):
    rows = sales_rows()
    only(rows, "columns", name="Total").update(is_persisted=is_persisted, is_nullable=is_nullable)

    total = order_of(read(rows)).column("Total")

    assert total is not None and total.type is None and total.nullable is expected
    assert total.computed == Computed(Expression.from_sql("([Status]*(2))"), bool(is_persisted))


# ------------------------------------------------------------------ names that the engine made
def test_a_system_named_default_gets_the_fixed_name_and_is_listed_for_the_rename_script():
    rows = sales_rows()
    only(rows, "defaults", name="DF_Order_Status").update(
        name="DF__Order__Status__3A81B327", is_system_named=1
    )

    found = read(rows)

    status = order_of(found).column("Status")
    assert status is not None and status.default is not None and status.default.name == "DF_Order_Status"
    assert found.system_named == (
        SystemNamed(ORDER_KEY, "DEFAULT", "DF__Order__Status__3A81B327", "DF_Order_Status"),
    )
    assert rename_constraints_sql(found.system_named) == (
        "EXEC sys.sp_rename N'[sales].[DF__Order__Status__3A81B327]', N'DF_Order_Status', N'OBJECT';\nGO\n"
    )
    assert "3A81B327" not in json.dumps(found.captures)  # a capture never holds a name the engine made


def test_every_kind_of_system_named_constraint_gets_the_fixed_name_of_the_design():
    rows = sales_rows()
    only(rows, "defaults", name="DF_Order_Status").update(name="DF__1", is_system_named=1)
    only(rows, "checks", name="CK_Order_Status").update(name="CK__1", is_system_named=1)
    only(rows, "indexes", name="PK_Order").update(name="PK__1", constraint_is_system_named=1)
    only(rows, "indexes", name="UQ_Order_Note").update(name="UQ__1", constraint_is_system_named=1)
    only(rows, "foreign_keys", name="FK_Order_Customer").update(name="FK__1", is_system_named=1)

    found = read(rows)

    assert order_of(found) == ORDER.__class__(
        "sales",
        "Order",
        ORDER.columns,
        (
            PrimaryKey("PK_Order", True, (KeyColumn("OrderId"),)),
            Unique("UQ_Order_Note_Status", False, (KeyColumn("Note"), KeyColumn("Status", True))),
            Check("CK_Order_1", Expression.from_sql("([Status]<(9))")),
            ForeignKey(
                "FK_Order_Customer_1", ("CustomerId",), "sales", "Customer", ("CustomerId",), "CASCADE"
            ),
        ),
        ORDER.indexes,
    )
    assert [(item.kind, item.live_name, item.fixed_name) for item in found.system_named] == [
        ("CHECK", "CK__1", "CK_Order_1"),
        ("DEFAULT", "DF__1", "DF_Order_Status"),
        ("FOREIGN KEY", "FK__1", "FK_Order_Customer_1"),
        ("PRIMARY KEY", "PK__1", "PK_Order"),
        ("UNIQUE", "UQ__1", "UQ_Order_Note_Status"),
    ]
    assert {item.table_key for item in found.system_named} == {ORDER_KEY}


def test_the_number_of_a_fixed_name_follows_the_definition_and_skips_a_name_that_is_taken():
    rows = sales_rows()
    check = only(rows, "checks", name="CK_Order_Status")
    rows["checks"] = [
        check | {"name": "CK_Order_1"},  # a human gave this name
        check | {"name": "CK__zz", "definition": "([Status]>(0))", "is_system_named": 1},
        check | {"name": "CK__aa", "definition": "([Status]>(1))", "is_system_named": 1},
    ]

    found = read(rows)

    assert {(item.live_name, item.fixed_name) for item in found.system_named} == {
        ("CK__zz", "CK_Order_2"),
        ("CK__aa", "CK_Order_3"),
    }
    # the same two checks under other engine names get the same fixed names
    for row, name in zip(rows["checks"][1:], ("CK__b", "CK__a"), strict=True):
        row["name"] = name
    assert {(item.live_name, item.fixed_name) for item in read(rows).system_named} == {
        ("CK__b", "CK_Order_2"),
        ("CK__a", "CK_Order_3"),
    }


def test_a_fixed_name_that_another_table_of_the_schema_has_quarantines_the_table():
    other = Table(
        "sales",
        "Other",
        (Column("a", TypeRef("int"), False),),
        (Check("DF_Order_Status", Expression.from_sql("([a]>(0))")),),
    )
    rows = rows_from_model(Model([SALES, ORDER, CUSTOMER, other]))
    only(rows, "defaults", name="DF_Order_Status").update(name="DF__1", is_system_named=1)

    found = read(rows)

    assert ORDER_KEY not in found.model and ORDER_KEY not in found.captures
    assert found.system_named == ()
    (item,) = found.unsupported
    assert (item.object_key, item.code) == (ORDER_KEY, UNSUPPORTED)
    assert "[DF_Order_Status]" in item.reason
    assert other.key in found.model


def test_a_fixed_name_that_an_object_outside_the_model_has_quarantines_the_table():
    # OM-10: the ledger table is not in the model; its constraint still has the name in the schema
    table = Table(
        "dbo", "t", (Column("id", TypeRef("int"), False),), (PrimaryKey("PKX", True, (KeyColumn("id"),)),)
    )
    other = Table(
        "dbo", "other", (Column("id", TypeRef("int"), False),), (Unique("PK_t", False, (KeyColumn("id"),)),)
    )
    rows = rows_from_model(Model([table, other]))
    only(rows, "indexes", name="PKX").update(name="PK__t__3213E83F00000000", constraint_is_system_named=1)
    only(rows, "tabulars", name="other")["ledger_type"] = 2

    found = read(rows)

    assert list(found.model) == [] and found.system_named == ()
    assert [(item.object_key, item.code) for item in found.unsupported] == [
        (other.key, UNSUPPORTED),
        (table.key, UNSUPPORTED),
    ]
    assert "[PK_t]" in found.unsupported[1].reason


def test_a_fixed_name_that_the_caller_knows_as_taken_quarantines_the_table():
    rows = sales_rows()
    only(rows, "defaults", name="DF_Order_Status").update(name="DF__1", is_system_named=1)

    assert ORDER_KEY in read_model(session_for(rows), taken=[("sales", "DF_Other")]).model
    found = read_model(session_for(rows), taken=[("SALES", "df_order_status")])  # a view, a procedure, ...

    assert ORDER_KEY not in found.model and found.system_named == ()
    assert [(item.object_key, item.code) for item in found.unsupported] == [(ORDER_KEY, UNSUPPORTED)]


def test_the_collation_of_the_database_is_read_as_the_engine_gives_it():
    db = FakeSession()
    db.respond("azsqlcd:database_collation", [[("SQL_Latin1_General_CP1_CI_AS",)]])

    assert catalog_tables.database_collation(db) == "SQL_Latin1_General_CP1_CI_AS"
    (batch,) = db.batches
    assert batch.startswith("/* azsqlcd:database_collation */ SELECT CAST(DATABASEPROPERTYEX(DB_NAME(), ")
    assert catalog_tables.database_collation(FakeSession()) is None  # no answer is not a collation


def test_a_fixed_name_that_is_longer_than_an_identifier_quarantines_the_table():
    long = Table(
        "dbo",
        "T" * 100,
        (
            Column(
                "C" * 30,
                TypeRef("int"),
                False,
                default=DefaultConstraint("DF_x", Expression.from_sql("((0))")),
            ),
        ),
    )
    rows = rows_from_model(Model([long]))
    assert read(rows).unsupported == ()  # a name that a human gave is kept, whatever its length
    only(rows, "defaults", name="DF_x").update(name="DF__1", is_system_named=1)

    found = read(rows)

    assert long.key not in found.model and found.system_named == ()
    assert "longer than 128" in found.unsupported[0].reason


def test_the_constraints_of_a_table_type_have_no_name_and_no_rename():
    kind = TableType(
        "dbo",
        "Lines",
        (
            Column("n", TypeRef("int"), False, default=DefaultConstraint(None, Expression.from_sql("((1))"))),
            Column("sku", TypeRef("varchar", length=20), False),
        ),
        (
            PrimaryKey(None, True, (KeyColumn("n"),)),
            Unique(None, False, (KeyColumn("sku"),)),
            Check(None, Expression.from_sql("([n]>(0))")),
        ),
        (Index("IX_sku", False, False, (KeyColumn("sku"),)),),
    )

    found = read(rows_from_model(Model([kind])))

    assert found.model[kind.key] == kind
    assert found.system_named == ()
    assert sorted(found.captures[kind.key]) == [
        "class",
        "column [n]",
        "column [sku]",
        "columns",
        "constraint [CK_Lines_1]",
        "constraint [DF_Lines_n]",
        "constraint [PK_Lines]",
        "constraint [UQ_Lines_sku]",
        "index [IX_sku]",
        "kind",
    ]


def test_the_rename_script_holds_only_sp_rename_and_every_name_is_a_literal():
    script = rename_constraints_sql(
        [
            SystemNamed("TABLE:[sales].[O'Brien]", "CHECK", HOSTILE, "CK_O'Brien_1"),
            SystemNamed("TABLE:[dbo].[A]", "PRIMARY KEY", "PK__A__1", "PK_A"),
        ]
    )

    batches = [batch.text for batch in lex.split_batches(script)]
    assert len(batches) == 2
    for batch in batches:
        found = lex.significant(lex.tokenize(batch))
        assert [t.text for t in found[:4]] == ["EXEC", "sys", ".", "sp_rename"]
        assert [t.kind for t in found[4:]] == ["nstring", "op", "nstring", "op", "nstring", "op"]
    assert [t.value for t in lex.tokenize(batches[1]) if t.kind == "nstring"] == [
        f"[sales].[{HOSTILE.replace(']', ']]')}]",
        "CK_O'Brien_1",
        "OBJECT",
    ]
    assert rename_constraints_sql([]) == ""


# ------------------------------------------------------------------ outside the model
def test_an_object_outside_the_model_is_quarantined_and_the_rest_is_read():
    rows = sales_rows()
    only(rows, "tabulars", name="Order")["ledger_type"] = 2

    found = read(rows)

    assert ORDER_KEY not in found.model and ORDER_KEY not in found.captures
    (item,) = found.unsupported
    assert (item.object_key, item.code) == (ORDER_KEY, UNSUPPORTED)
    assert "ledger" in item.reason
    assert CUSTOMER_KEY in found.model and "SCHEMA:[sales]" in found.model  # the rest is read


# ------------------------------------------------------------------ system-versioned temporal tables
def period_column(name: str, generated: str, *, hidden: bool = False) -> Column:
    return Column(name, TypeRef("datetime2", scale=7), False, generated=generated, hidden=hidden)


def price(
    *,
    retention: tuple[int, str] | None = None,
    hidden: bool = False,
    history: tuple[str, str] = ("sales", "Price_History"),
) -> Table:
    """A system-versioned table as the canonical file of the design declares it."""
    return Table(
        "sales",
        "Price",
        (
            Column("PriceId", TypeRef("int"), False),
            Column("Amount", TypeRef("decimal", precision=18, scale=2), False),
            period_column("ValidFrom", "ROW_START", hidden=hidden),
            period_column("ValidTo", "ROW_END", hidden=hidden),
        ),
        (PrimaryKey("PK_Price", True, (KeyColumn("PriceId"),)),),
        (),
        Temporal("ValidFrom", "ValidTo", *history, retention),
    )


PRICE = price()
PRICE_KEY = PRICE.key
PRICE_HISTORY_KEY = names.object_key("TABLE", "sales", "Price_History")
QUOTE = Table(
    "sales",
    "Quote",
    (Column("QuoteId", TypeRef("int"), False), Column("PriceId", TypeRef("int"), False)),
    (
        PrimaryKey("PK_Quote", True, (KeyColumn("QuoteId"),)),
        ForeignKey("FK_Quote_Price", ("PriceId",), "sales", "Price", ("PriceId",)),
    ),
)


def price_rows(table: Table = PRICE) -> CatalogRows:
    return rows_from_model(Model([SALES, table, QUOTE]))


def versioning_off(rows: CatalogRows, *, drop_period: bool = False) -> CatalogRows:
    """The catalog after ALTER TABLE [sales].[Price] SET (SYSTEM_VERSIONING = OFF): the period and
    its columns stay, the history table is a table like any other. drop_period: after DROP PERIOD
    FOR SYSTEM_TIME too, when the two columns are plain datetime2 columns."""
    head = only(rows, "tabulars", name="Price")
    head.update(temporal_type=0, history_schema=None, history_table=None)
    head.update(history_retention_period=None, history_retention_period_unit=None)
    only(rows, "tabulars", temporal_type=1).update(temporal_type=0, current_schema=None, current_table=None)
    if drop_period:
        head.update(period_start_column=None, period_end_column=None)
        for row in rows["columns"]:
            if row["object_id"] == head["object_id"]:
                row.update(generated_always_type=0, is_hidden=0)
    return rows


def price_column(rows: CatalogRows, name: str) -> dict[str, Any]:
    return only(rows, "columns", object_id=only(rows, "tabulars", name="Price")["object_id"], name=name)


def price_of(found: CatalogRead) -> Table:
    table = found.model[PRICE_KEY]
    assert isinstance(table, Table)
    return table


def test_a_system_versioned_table_is_read_with_its_period_and_history_table():
    found = read(price_rows())

    table = price_of(found)
    assert table.temporal == Temporal("ValidFrom", "ValidTo", "sales", "Price_History")
    generated = {column.name: column.generated for column in table.columns}
    assert generated == {"PriceId": None, "Amount": None, "ValidFrom": "ROW_START", "ValidTo": "ROW_END"}
    assert table == PRICE
    assert found.unsupported == ()  # nothing is quarantined only because temporal_type is 2


def test_a_history_table_is_owned_not_exported_and_not_quarantined():
    found = read(price_rows())

    assert found.history_tables == ((PRICE_HISTORY_KEY, PRICE_KEY),)
    assert PRICE_HISTORY_KEY not in found.model and PRICE_HISTORY_KEY not in found.captures
    assert all(item.object_key != PRICE_HISTORY_KEY for item in found.unsupported)
    assert sorted(found.model) == sorted(["SCHEMA:[sales]", PRICE_KEY, QUOTE.key])


def test_a_foreign_key_to_a_temporal_table_no_longer_quarantines_the_referencing_table():
    found = read(price_rows())

    assert found.unsupported == ()
    quote = found.model[QUOTE.key]
    assert isinstance(quote, Table) and quote == QUOTE
    (key,) = [c for c in quote.constraints if isinstance(c, ForeignKey)]
    assert names.object_key("TABLE", key.ref_schema, key.ref_table) in found.model


def test_hidden_period_columns_are_read_as_hidden():
    found = read(price_rows(price(hidden=True)))

    hidden = {column.name: column.hidden for column in price_of(found).columns}
    assert hidden == {"PriceId": False, "Amount": False, "ValidFrom": True, "ValidTo": True}
    assert all(not column.hidden for column in price_of(read(price_rows())).columns)


def test_one_hidden_period_column_is_read_as_one_hidden_column():
    rows = price_rows()
    price_column(rows, "ValidTo")["is_hidden"] = 1

    hidden = {column.name: column.hidden for column in price_of(read(rows)).columns}

    assert hidden == {"PriceId": False, "Amount": False, "ValidFrom": False, "ValidTo": True}


def test_retention_of_6_months_is_read_and_infinite_is_none():
    assert price_of(read(price_rows(price(retention=(6, "MONTHS"))))).temporal == Temporal(
        "ValidFrom", "ValidTo", "sales", "Price_History", (6, "MONTHS")
    )
    infinite = price_rows()
    head = only(infinite, "tabulars", name="Price")
    assert (head["history_retention_period"], head["history_retention_period_unit"]) == (-1, -1)
    temporal = price_of(read(infinite)).temporal
    assert temporal is not None and temporal.retention is None


@pytest.mark.parametrize(
    ("period", "unit", "retention"),
    [(7, 3, (7, "DAYS")), (2, 4, (2, "WEEKS")), (6, 5, (6, "MONTHS")), (1, 6, (1, "YEARS"))],
)
def test_each_retention_unit_of_the_catalog_is_the_unit_of_the_model(
    period: int, unit: int, retention: tuple[int, str]
):
    rows = price_rows()
    only(rows, "tabulars", name="Price").update(
        history_retention_period=period, history_retention_period_unit=unit
    )

    temporal = price_of(read(rows)).temporal

    assert temporal is not None and temporal.retention == retention


def test_a_retention_unit_that_the_tool_does_not_know_quarantines_the_table():
    rows = price_rows()
    only(rows, "tabulars", name="Price").update(history_retention_period=1, history_retention_period_unit=9)

    found = read(rows)

    assert [(item.object_key, item.code) for item in found.unsupported] == [(PRICE_KEY, UNSUPPORTED)]
    assert "retention" in found.unsupported[0].reason


def test_a_table_with_a_period_and_no_versioning_is_quarantined():
    # a table file cannot say PERIOD FOR SYSTEM_TIME without SYSTEM_VERSIONING = ON
    found = read(versioning_off(price_rows()))

    assert PRICE_KEY not in found.model and PRICE_KEY not in found.captures
    assert [(item.object_key, item.code) for item in found.unsupported] == [(PRICE_KEY, UNSUPPORTED)]
    assert "not system-versioned" in found.unsupported[0].reason
    assert found.history_tables == ()
    assert PRICE_HISTORY_KEY in found.model  # no longer owned by the engine: a table like any other


def test_a_table_whose_period_was_dropped_too_is_a_plain_table():
    found = read(versioning_off(price_rows(price(hidden=True)), drop_period=True))

    table = price_of(found)
    assert table.temporal is None and found.unsupported == ()
    assert all(column.generated is None and not column.hidden for column in table.columns)


def test_the_capture_of_a_temporal_table_holds_its_temporal_facts():
    captures = read(price_rows(price(retention=(6, "MONTHS"), hidden=True))).captures

    capture = captures[PRICE_KEY]
    assert capture["temporal"] == {
        "period_start": "ValidFrom",
        "period_end": "ValidTo",
        "history_schema": "sales",
        "history_table": "Price_History",
        "retention": [6, "MONTHS"],
    }
    assert capture["column [ValidFrom]"]["generated_always"] == "ROW_START"
    assert capture["column [ValidTo]"]["generated_always"] == "ROW_END"
    assert capture["column [ValidFrom]"]["is_hidden"] is True
    assert "generated_always" not in capture["column [Amount]"]
    assert "is_hidden" not in capture["column [Amount]"]
    json.dumps(capture)  # flat JSON, as every capture


def test_the_capture_of_a_table_that_is_not_temporal_is_what_it_was_before():
    # a stored capture of an older tool has none of these properties; a new one would be drift
    capture = read(price_rows()).captures[QUOTE.key]

    assert "temporal" not in capture
    assert set(capture["column [PriceId]"]) == {
        "type",
        "type_schema",
        "max_length",
        "precision",
        "scale",
        "is_nullable",
        "identity",
        "is_computed",
        "is_persisted",
        "definition",
        "collation",
    }


def differing(stored: CatalogRows, live: CatalogRows) -> list[str]:
    """The capture properties of the Price table that drift reports between two catalogs."""
    before, after = read(stored).captures[PRICE_KEY], read(live).captures[PRICE_KEY]
    return [d.property for d in catalog.capture_differences(before, after)]


def test_drift_sees_versioning_switched_off():
    # with the period dropped the table is a plain table again, and its capture says what changed;
    # with the period left it is outside the model (see test_tables: the difference 'unsupported')
    off = versioning_off(price_rows(), drop_period=True)

    assert differing(price_rows(), off) == ["column [ValidFrom]", "column [ValidTo]", "temporal"]
    assert differing(price_rows(), price_rows()) == []
    assert PRICE_KEY not in read(versioning_off(price_rows())).captures


@pytest.mark.parametrize(
    ("change", "property_"),
    [
        ({"history_table": "Price_Old"}, "temporal"),
        ({"history_schema": "audit"}, "temporal"),
        ({"history_retention_period": 6, "history_retention_period_unit": 5}, "temporal"),
        ({"period_start_column": "ValidTo", "period_end_column": "ValidFrom"}, "temporal"),
    ],
)
def test_drift_sees_a_changed_history_table_retention_or_period(change: dict[str, Any], property_: str):
    live = price_rows()
    only(live, "tabulars", name="Price").update(change)
    if "period_start_column" in change:  # the engine holds the kind on the column
        price_column(live, "ValidFrom")["generated_always_type"] = 2
        price_column(live, "ValidTo")["generated_always_type"] = 1

    assert property_ in differing(price_rows(), live)


def test_drift_sees_a_period_column_that_is_hidden_now():
    live = price_rows()
    price_column(live, "ValidFrom")["is_hidden"] = 1

    assert differing(price_rows(), live) == ["column [ValidFrom]"]
    assert differing(live, price_rows()) == ["column [ValidFrom]"]


def test_the_history_tables_of_a_database_are_listed_with_their_temporal_tables():
    db = FakeSession()
    db.respond("/* azsqlcd:history_tables */", [[("audit", "Price_History", "sales", "Price")]])

    found = catalog_tables.history_tables(db)

    assert found == {"TABLE:[audit].[Price_History]": PRICE_KEY}
    (batch,) = db.batches
    tokens = lex.significant(lex.tokenize(batch))
    assert tokens[0].text == "SELECT" and not {t.text.upper() for t in tokens if t.kind == "word"} & WRITES


def test_the_schema_of_a_history_table_is_a_schema_of_the_model():
    # CREATE TABLE ... HISTORY_TABLE = [audit].[Price_History] needs the schema audit
    table = price(history=("audit", "Price_History"))
    rows = rows_from_model(Model([SALES, table]))
    rows["schemas"].append({"name": "audit", "owner_name": "dbo", "module_count": 0})
    rows["schemas"].append({"name": "empty", "owner_name": "dbo", "module_count": 0})

    found = read(rows)

    assert sorted(found.model) == sorted(["SCHEMA:[audit]", "SCHEMA:[sales]", PRICE_KEY])
    assert found.history_tables == ((names.object_key("TABLE", "audit", "Price_History"), PRICE_KEY),)


def test_the_name_of_a_history_table_is_not_free_for_a_fixed_constraint_name():
    table = Table(
        "sales", "t", (Column("id", TypeRef("int"), False),), (PrimaryKey("PKX", True, (KeyColumn("id"),)),)
    )
    rows = rows_from_model(Model([SALES, price(history=("sales", "PK_t")), table]))
    only(rows, "indexes", name="PKX").update(name="PK__t__3213E83F00000000", constraint_is_system_named=1)

    found = read(rows)

    assert [(item.object_key, item.code) for item in found.unsupported] == [(table.key, UNSUPPORTED)]
    assert "[PK_t]" in found.unsupported[0].reason
    assert PRICE_KEY in found.model


def test_a_versioned_table_whose_period_cannot_be_read_is_quarantined_not_read_as_a_plain_table():
    rows = price_rows()
    only(rows, "tabulars", name="Price").update(period_start_column=None, period_end_column=None)

    found = read(rows)

    assert [(item.object_key, item.code) for item in found.unsupported] == [(PRICE_KEY, UNSUPPORTED)]
    assert "cannot be read" in found.unsupported[0].reason


def test_a_temporal_table_with_another_property_outside_the_model_is_still_quarantined():
    rows = price_rows()
    only(rows, "tabulars", name="Price")["is_memory_optimized"] = 1

    found = read(rows)

    assert [(item.object_key, item.reason) for item in found.unsupported] == [(PRICE_KEY, "memory-optimized")]
    assert found.history_tables == ((PRICE_HISTORY_KEY, PRICE_KEY),)  # still not an object of its own


@pytest.mark.parametrize(
    ("query", "name", "change", "says"),
    [
        ("tabulars", "Order", {"temporal_type": 1}, "history table"),  # no current table: a defect
        ("tabulars", "Order", {"temporal_type": 3}, "temporal"),
        ("tabulars", "Order", {"temporal_type": 2}, "cannot be read"),  # no period, no history table
        ("tabulars", "Order", {"period_start_column": "Note", "period_end_column": "Status"},
         "not system-versioned"),
        ("tabulars", "Order", {"ledger_type": 2}, "ledger"),
        ("tabulars", "Order", {"is_memory_optimized": 1}, "memory-optimized"),
        ("tabulars", "Order", {"is_node": 1}, "graph"),
        ("tabulars", "Order", {"is_edge": 1}, "graph"),
        ("tabulars", "Order", {"is_external": 1}, "external"),
        ("tabulars", "Order", {"has_fulltext_index": 1}, "full-text"),
        ("indexes", "PK_Order", {"data_space_type": "PS"}, "partitioned"),
        ("indexes", "PK_Order", {"partition_count": 4}, "partitioned"),
        ("indexes", "IX_Order_Status", {"type": 6, "type_desc": "NONCLUSTERED COLUMNSTORE"}, "COLUMNSTORE"),
        ("indexes", "IX_Order_Status", {"type": 3, "type_desc": "XML"}, "XML"),
        ("indexes", "IX_Order_Status", {"type": 4, "type_desc": "SPATIAL"}, "SPATIAL"),
        ("indexes", "IX_Order_Status", {"type": 9, "type_desc": "JSON"}, "JSON"),
        ("indexes", "IX_Order_Status", {"type": 8, "type_desc": "VECTOR"}, "VECTOR"),
        ("indexes", "IX_Order_Status", {"type": 7, "type_desc": "NONCLUSTERED HASH"}, "HASH"),
        ("indexes", "IX_Order_Status", {"data_compression": 3}, "compression"),
        ("indexes", "IX_Order_Status", {"type": 0, "xml_compression": 1}, "XML compression"),
        ("indexes", "IX_Order_Status", {"type": 0, "data_compression": 3}, "heap with a compression"),
        ("indexes", "IX_Order_Status", {"type": 6, "data_compression": 2}, "columnstore index with"),
        ("indexes", "IX_Order_Status", {"type": 6, "xml_compression": 1}, "columnstore index with"),
        ("columns", "Note", {"is_masked": 1}, "masking"),
        ("columns", "OrderId", {"is_sparse": 1}, "SPARSE"),
        ("columns", "Note", {"is_column_set": 1}, "column set"),
        ("columns", "Note", {"encryption_type": 1}, "Always Encrypted"),
        ("columns", "Note", {"is_rowguidcol": 1}, "ROWGUIDCOL"),
        ("columns", "Note", {"is_filestream": 1}, "FILESTREAM"),
        ("columns", "Note", {"generated_always_type": 1}, "GENERATED ALWAYS AS ROW START"),
        ("columns", "Note", {"generated_always_type": 2}, "GENERATED ALWAYS AS ROW END"),
        ("columns", "Note", {"generated_always_type": 7}, "ledger"),  # transaction id, sequence number
        ("columns", "Note", {"is_hidden": 1}, "HIDDEN"),
        ("columns", "Note", {"type_name": "xml", "max_length": -1, "xml_collection_id": 65537}, "typed xml"),
        ("columns", "Note", {"type_name": "Point", "type_is_user_defined": 1, "type_is_assembly": 1}, "CLR"),
        ("columns", "Note", {"type_name": "newtype"}, "newtype"),
        ("columns", "Note", {"max_length": 7}, "odd byte length"),
        ("columns", "Note", {"type_name": "vector", "max_length": 10}, "vector"),
    ],
)  # fmt: skip
def test_a_table_with_a_property_outside_the_model_is_quarantined(
    query: str, name: str, change: dict[str, Any], says: str
):
    rows = sales_rows()
    only(rows, query, name=name).update(change)

    found = read(rows)

    assert ORDER_KEY not in found.model and ORDER_KEY not in found.captures
    assert [(item.object_key, item.code) for item in found.unsupported] == [(ORDER_KEY, UNSUPPORTED)]
    assert says in found.unsupported[0].reason
    assert all(item.table_key != ORDER_KEY for item in found.system_named)


@pytest.mark.parametrize(
    ("query", "name", "column"),
    [
        ("defaults", "DF_Order_Status", "definition"),
        ("checks", "CK_Order_Status", "definition"),
        ("columns", "Total", "definition"),
    ],
)
def test_a_definition_that_the_catalog_hides_stops_the_read(query: str, name: str, column: str):
    rows = sales_rows()
    only(rows, query, name=name)[column] = None  # no VIEW DEFINITION on the table

    with pytest.raises(ToolError) as caught:
        read(rows)

    assert (caught.value.exit_code, caught.value.reason_code) == (Exit.REFUSED, "NO_VIEW_DEFINITION")
    assert ORDER_KEY in caught.value.message


def test_a_filter_that_the_catalog_hides_stops_the_read():
    rows = sales_rows()
    only(rows, "indexes", name="IX_Order_Status").update(has_filter=1, filter_definition=None)

    with pytest.raises(ToolError) as caught:
        read(rows)

    assert caught.value.reason_code == "NO_VIEW_DEFINITION"


def test_a_filtered_index_keeps_its_filter():
    rows = sales_rows()
    only(rows, "indexes", name="IX_Order_Status").update(has_filter=1, filter_definition="([Status]=(1))")

    found = read(rows)

    index = order_of(found).index("IX_Order_Status")
    assert index is not None and index.filter == Expression.from_sql("([Status]=(1))")
    assert found.captures[ORDER_KEY]["index [IX_Order_Status]"]["filter"] == "([Status]=(1))"


def test_a_compressed_heap_is_read_with_its_compression():
    heap = Table("dbo", "Heap", (Column("a", TypeRef("int"), True),))
    rows = rows_from_model(Model([heap]))
    plain = read(rows)
    assert plain.model[heap.key] == heap and plain.captures[heap.key]["heap_compression"] is None
    only(rows, "indexes", type=0)["data_compression"] = 2

    found = read(rows)

    assert found.unsupported == ()
    assert found.model[heap.key] == dataclasses.replace(heap, compression="PAGE")
    assert found.captures[heap.key]["heap_compression"] == "PAGE"
    # the read-back names the property when the file and the database differ
    assert found.model.diff_paths(Model([heap])) == [(heap.key, "compression")]


def test_the_new_column_facts_are_read_and_a_column_without_them_keeps_its_capture():
    before = read(sales_rows()).captures[ORDER_KEY]
    rows = sales_rows()
    only(rows, "columns", name="Note").update({"is_sparse": 1})
    identity = {"is_identity": 1, "seed_value": 1, "increment_value": 1, "identity_not_for_replication": 1}
    only(rows, "columns", name="OrderId").update(identity)
    only(rows, "checks", name="CK_Order_Status")["is_not_for_replication"] = 1
    only(rows, "indexes", name="IX_Order_Status")["xml_compression"] = 1

    found = read(rows)

    assert found.unsupported == ()
    table = order_of(found)
    note, order_id = table.column("Note"), table.column("OrderId")
    assert note is not None and note.sparse and not note.rowguidcol
    assert order_id is not None and order_id.identity == Identity(1, 1, True)
    check = table.constraint("CK_Order_Status")
    assert isinstance(check, Check) and check.not_for_replication
    index = table.index("IX_Order_Status")
    assert index is not None and ("XML_COMPRESSION", "ON") in index.options
    capture = found.captures[ORDER_KEY]
    assert capture["column [Note]"] == before["column [Note]"] | {"is_sparse": True}
    assert capture["column [OrderId]"]["identity_not_for_replication"] is True
    # a column that uses none of the new facts has the capture that it had before they were read
    assert capture["column [Status]"] == before["column [Status]"]
    new_keys = {"is_sparse", "is_rowguidcol", "identity_not_for_replication"}
    assert not any(new_keys & set(v) for k, v in before.items() if k.startswith("column ["))


def test_a_columnstore_index_is_read_with_its_list_order_and_options():
    fact = Table(
        "dw",
        "Fact",
        (
            Column("d", TypeRef("int"), False),
            Column("s", TypeRef("int"), False),
            Column("g", TypeRef("uniqueidentifier"), False, rowguidcol=True),
        ),
        indexes=(
            Index(
                "CCI",
                False,
                True,
                (KeyColumn("s"), KeyColumn("d")),
                options=(("DATA_COMPRESSION", "COLUMNSTORE_ARCHIVE"),),
                columnstore=True,
            ),
        ),
    )
    wide = Table(
        "dw",
        "Wide",
        (Column("a", TypeRef("int"), False), Column("b", TypeRef("int"), True)),
        indexes=(
            Index(
                "NCCI",
                False,
                False,
                (),
                ("b", "a"),
                Expression.from_sql("([a]>(0))"),
                (("COMPRESSION_DELAY", "10"),),
                True,
            ),
        ),
        compression="ROW",
    )
    model = Model([Schema("dw"), fact, wide])

    found = read(rows_from_model(model))

    assert found.unsupported == () and found.model.diff_paths(model) == []
    capture = found.captures[fact.key]["index [CCI]"]
    assert (capture["columnstore"], capture["clustered"], capture["included"]) == (True, True, [])
    assert capture["columns"] == [["s", False], ["d", False]]
    assert capture["options"] == {"DATA_COMPRESSION": "COLUMNSTORE_ARCHIVE"}
    assert found.captures[fact.key]["column [g]"]["is_rowguidcol"] is True
    assert found.captures[wide.key]["heap_compression"] == "ROW"
    assert found.captures[wide.key]["index [NCCI]"]["included"] == ["a", "b"]
    # a rowstore index of the catalog has no such key: its capture is what it was
    assert "columnstore" not in read(sales_rows()).captures[ORDER_KEY]["index [IX_Order_Status]"]


def test_two_constraints_that_come_to_one_fixed_name_put_the_table_outside_the_model():
    # TQ-10: two UNIQUE constraints that the engine named, on the same columns, have one fixed name.
    # The second must not take the place of the first in the capture.
    twice = Table(
        "dbo",
        "Twice",
        (Column("a", TypeRef("int"), False),),
        (Unique("UQ_first", False, (KeyColumn("a"),)), Unique("UQ_second", True, (KeyColumn("a"),))),
    )
    rows = rows_from_model(Model([twice]))
    first, second = only(rows, "indexes", name="UQ_first"), only(rows, "indexes", name="UQ_second")
    first.update(name="UQ__Twice__3BD0198E1A2B3C4D", constraint_is_system_named=1)
    second.update(name="UQ__Twice__3BD0198F5E6F7A8B", constraint_is_system_named=1)

    found = read(rows)

    assert twice.key not in found.model and twice.key not in found.captures
    (item,) = found.unsupported
    assert (item.object_key, item.code) == (twice.key, UNSUPPORTED)
    assert item.reason == "two of its sub-objects come to the name constraint [UQ_Twice_a]"
    assert found.system_named == ()  # no rename script for a table that is not managed
    # one of the two with a name of its own: both are held
    second.update(name="UQ_Twice_second", constraint_is_system_named=0)
    assert sorted(label for label in read(rows).captures[twice.key] if label.startswith("constraint ")) == [
        "constraint [UQ_Twice_a]",
        "constraint [UQ_Twice_second]",
    ]


@pytest.mark.parametrize("data_space", ["PS", "PS "])
def test_a_partitioned_table_is_quarantined_whatever_padding_the_type_code_has(data_space: str):
    # sys.data_spaces.type is char(2), as sys.objects.type: a driver can give it with padding
    rows = sales_rows()
    only(rows, "indexes", name="PK_Order")["data_space_type"] = data_space

    found = read(rows)

    assert ORDER_KEY not in found.model
    assert [(u.object_key, u.reason) for u in found.unsupported if u.object_key == ORDER_KEY] == [
        (ORDER_KEY, "it is partitioned")
    ]
    padded = sales_rows()
    for row in padded["indexes"]:
        row["data_space_type"] = "FG "  # a padded file group is no partition scheme
    assert ORDER_KEY in read(padded).model


def test_an_auto_created_or_hypothetical_index_is_not_part_of_the_table():
    rows = sales_rows()
    real = only(rows, "indexes", name="IX_Order_Status")
    members = [row for row in rows["index_columns"] if row["index_id"] == real["index_id"]]
    for index_id, name, flag in (
        (90, "nci_wi_Order_1", "auto_created"),
        (91, "_dta_index_Order", "is_hypothetical"),
    ):
        # either would quarantine the table if it counted: the type is not a rowstore
        rows["indexes"].append(real | {"index_id": index_id, "name": name, flag: 1, "type": 6})
        rows["index_columns"] += [row | {"index_id": index_id} for row in members]

    found = read(rows)

    assert found.unsupported == ()
    assert [index.name for index in order_of(found).indexes] == ["IX_Order_Status"]
    assert not any("nci_wi" in label or "_dta_" in label for label in found.captures[ORDER_KEY])


def test_a_synonym_whose_base_name_has_not_two_parts_is_quarantined():
    rows = rows_from_model(Model([Synonym("dbo", "Two", "sales", "Order")]))
    rows["synonyms"] += [
        {"schema_name": "dbo", "name": "Three", "base_object_name": "[other_db].[sales].[Order]"},
        {"schema_name": "dbo", "name": "One", "base_object_name": "[Order]"},
        {"schema_name": "dbo", "name": "Odd", "base_object_name": "[a]..[b]"},
    ]

    found = read(rows)

    assert list(found.model) == ["SYNONYM:[dbo].[Two]"]
    assert [item.object_key for item in found.unsupported] == [
        "SYNONYM:[dbo].[Odd]",
        "SYNONYM:[dbo].[One]",
        "SYNONYM:[dbo].[Three]",
    ]
    assert {item.code for item in found.unsupported} == {UNSUPPORTED}


def test_a_clr_type_and_an_alias_type_on_an_unknown_base_are_quarantined():
    rows = rows_from_model(Model([AliasType("dbo", "Code", TypeRef("varchar", length=10), False)]))
    alias = rows["alias_types"][0]
    rows["alias_types"] += [
        alias | {"name": "Clr", "base_type_name": None, "is_assembly_type": 1},
        alias | {"name": "Odd", "base_type_name": "newtype"},
    ]

    found = read(rows)

    assert list(found.model) == ["TYPE:[dbo].[Code]"]
    assert [item.object_key for item in found.unsupported] == ["TYPE:[dbo].[Clr]", "TYPE:[dbo].[Odd]"]


def test_an_object_in_a_built_in_schema_is_quarantined_and_dbo_is_not_such_a_schema():
    guest = Table("guest", "T", (Column("a", TypeRef("int"), True),))
    plain = Table("dbo", "T", (Column("a", TypeRef("int"), True),))

    found = read(rows_from_model(Model([guest, plain])))

    assert list(found.model) == [plain.key]
    assert [(item.object_key, item.code) for item in found.unsupported] == [(guest.key, UNSUPPORTED)]
    assert "[guest]" in found.unsupported[0].reason


def test_the_tables_of_the_tool_itself_are_not_read_and_not_reported():
    state_table = Table("azsqlcd", "run", (Column("run_id", TypeRef("bigint"), False),))

    found = read(rows_from_model(Model([state_table, CUSTOMER, SALES])))

    assert list(found.model) == ["SCHEMA:[sales]", CUSTOMER_KEY]
    assert found.unsupported == ()


# ------------------------------------------------------------------ schemas
def test_a_schema_is_in_the_model_only_when_it_holds_a_managed_object():
    rows = sales_rows()
    rows["schemas"] += [
        {"name": "empty", "owner_name": "dbo", "module_count": 0},
        {"name": "reports", "owner_name": "dbo", "module_count": 3},  # views only
        {"name": "azsqlcd", "owner_name": "dbo", "module_count": 0},
    ]

    schemas = [key for key in read(rows).model if key.startswith("SCHEMA:")]

    assert schemas == ["SCHEMA:[reports]", "SCHEMA:[sales]"]  # never dbo, sys, guest or a role schema


def test_an_asked_schema_is_read_when_it_exists_even_if_it_is_empty():
    rows = rows_from_model(Model())
    rows["schemas"] = [{"name": "new", "owner_name": "dbo", "module_count": 0}]

    assert list(read(rows, ["SCHEMA:[new]"]).model) == ["SCHEMA:[new]"]
    assert list(read(rows).model) == []


def test_a_schema_whose_owner_cannot_be_seen_is_not_read_as_owned_by_dbo():
    rows = sales_rows()
    only(rows, "schemas", name="sales")["owner_name"] = None

    found = read(rows)

    assert "SCHEMA:[sales]" not in found.model
    assert [item.object_key for item in found.unsupported] == ["SCHEMA:[sales]"]


# ------------------------------------------------------------------ captures
def test_a_capture_is_flat_json_with_one_property_for_each_sub_object():
    capture = read(sales_rows()).captures[ORDER_KEY]

    assert json.loads(json.dumps(capture)) == capture
    assert sorted(capture) == [
        "column [CustomerId]",
        "column [Note]",
        "column [OrderId]",
        "column [Status]",
        "column [Total]",
        "columns",
        "constraint [CK_Order_Status]",
        "constraint [DF_Order_Status]",
        "constraint [FK_Order_Customer]",
        "constraint [PK_Order]",
        "constraint [UQ_Order_Note]",
        "heap_compression",
        "index [IX_Order_Status]",
        "kind",
    ]
    assert capture["column [Note]"] == {
        "type": "nvarchar",
        "type_schema": None,
        "max_length": 100,
        "precision": 0,
        "scale": 0,
        "is_nullable": True,
        "identity": None,
        "is_computed": False,
        "is_persisted": False,
        "definition": None,
        "collation": None,
    }
    assert capture["constraint [DF_Order_Status]"] == {
        "kind": "DEFAULT",
        "column": "Status",
        "expression": "( ( 0 ) )",
    }
    assert capture["constraint [UQ_Order_Note]"] == {
        "kind": "UNIQUE",
        "clustered": False,
        "columns": [["Note", False], ["Status", True]],
        "options": {},
        "is_disabled": False,
    }
    assert capture["index [IX_Order_Status]"] == {
        "unique": False,
        "clustered": False,
        "columns": [["Status", False]],
        "included": ["Note"],
        "filter": None,
        "options": {},
        "is_disabled": False,
    }


def test_a_capture_holds_the_state_flags_of_checks_foreign_keys_and_indexes():
    rows = sales_rows()
    only(rows, "checks", name="CK_Order_Status").update(is_disabled=1, is_not_trusted=1)
    for row in rows["foreign_keys"]:
        row.update(is_not_trusted=1, is_not_for_replication=1)
    only(rows, "indexes", name="IX_Order_Status")["is_disabled"] = 1

    found = read(rows)
    capture = found.captures[ORDER_KEY]

    assert found.unsupported == ()  # the model does not hold these flags; the capture does (A13)
    check, key = capture["constraint [CK_Order_Status]"], capture["constraint [FK_Order_Customer]"]
    assert (check["is_disabled"], check["is_not_trusted"], check["is_not_for_replication"]) == (
        True,
        True,
        False,
    )
    assert (key["is_disabled"], key["is_not_trusted"], key["is_not_for_replication"]) == (False, True, True)
    assert capture["index [IX_Order_Status]"]["is_disabled"] is True
    assert key | {"is_not_trusted": False, "is_not_for_replication": False} == {
        "kind": "FOREIGN KEY",
        "columns": ["CustomerId"],
        "ref_schema": "sales",
        "ref_table": "Customer",
        "ref_columns": ["CustomerId"],
        "on_delete": "CASCADE",
        "on_update": "NO ACTION",
        "is_disabled": False,
        "is_not_trusted": False,
        "is_not_for_replication": False,
    }


def test_a_capture_holds_no_id_no_order_and_no_identity_position():
    rows = sales_rows()
    order_id = only(rows, "columns", name="OrderId")
    order_id.update(is_identity=1, seed_value=Decimal(1000), increment_value=Decimal(5))
    order_id["identity_not_for_replication"] = 0
    before = read(rows).captures

    # another database: other ids, the columns in another order
    for query in ("tabulars", "columns", "indexes", "index_columns"):
        for row in rows[query]:
            row["object_id"] += 5000
    for query in ("defaults", "checks", "foreign_keys"):
        for row in rows[query]:
            row["parent_object_id"] += 5000
    ids = {row["column_id"]: 10 - row["column_id"] for row in rows["columns"] if row["name"] != "CustomerId"}
    for row in rows["columns"]:
        row["column_id"] = ids.get(row["column_id"], row["column_id"])
    for row in rows["defaults"]:
        row["parent_column_id"] = ids.get(row["parent_column_id"], row["parent_column_id"])
    after = read(rows)

    assert [c.name for c in order_of(after).columns] != [c.name for c in ORDER.columns]
    assert after.captures == before
    assert before[ORDER_KEY]["column [OrderId]"]["identity"] == [1000, 5]
    assert before[ORDER_KEY]["columns"] == ["CustomerId", "Note", "OrderId", "Status", "Total"]


def test_each_kind_of_object_has_a_capture_that_names_its_kind():
    model = Model(
        [
            Schema("sales", "auditor"),
            AliasType("sales", "Code", TypeRef("nvarchar", length=10), False),
            Sequence("sales", "No", TypeRef("decimal", precision=18, scale=0), 5, 2, 1, 99, True, True, 20),
            Synonym("sales", "Old", "dbo", "Legacy"),
        ]
    )

    captures = read(rows_from_model(model)).captures

    assert captures == {
        "SCHEMA:[sales]": {"kind": "SCHEMA", "owner": "auditor"},
        "TYPE:[sales].[Code]": {
            "kind": "TYPE",
            "class": "alias",
            "base_type": "nvarchar",
            "max_length": 20,
            "precision": 0,
            "scale": 0,
            "is_nullable": False,
        },
        "SEQUENCE:[sales].[No]": {
            "kind": "SEQUENCE",
            "type": "decimal",
            "type_schema": None,
            "precision": 18,
            "scale": 0,
            "start_value": 5,
            "increment": 2,
            "minimum_value": 1,
            "maximum_value": 99,
            "is_cycling": True,
            "is_cached": True,
            "cache_size": 20,
        },
        "SYNONYM:[sales].[Old]": {"kind": "SYNONYM", "base_object_name": "[dbo].[Legacy]"},
    }


# ------------------------------------------------------------------ the batches
def test_the_reader_sends_only_select_batches_with_a_tag():
    db = session_for(sales_rows())

    read_model(db)
    read_model(
        db, [ORDER_KEY, "TYPE:[sales].[Code]", "SEQUENCE:[sales].[No]", "SYNONYM:[dbo].[S]", "SCHEMA:[sales]"]
    )

    assert len(db.batches) == 2 * len(COLUMNS)
    for batch in db.batches:
        found = lex.significant(lex.tokenize(batch))
        assert batch.startswith("/* azsqlcd:read_") and found[0].text == "SELECT"
        assert not {t.text.upper() for t in found if t.kind == "word"} & WRITES
        assert [t.text for t in found].count(";") == 1 and found[-1].text == ";"


def test_asked_keys_are_resolved_by_the_engine_and_only_their_kinds_are_read():
    db = session_for(sales_rows())

    read_model(db, [f"TABLE:[sales].[{HOSTILE.replace(']', ']]')}]", "TYPE:[sales].[Code]"])

    assert [batch.split("*/")[0].removeprefix("/* azsqlcd:read_").strip() for batch in db.batches] == [
        "tabulars",
        "columns",
        "defaults",
        "checks",
        "indexes",
        "index_columns",
        "foreign_keys",
        "alias_types",
    ]
    quoted = f"[sales].[{HOSTILE.replace(']', ']]')}]"
    for batch in db.batches[:7]:
        literals = [t.value for t in lex.tokenize(batch) if t.kind == "nstring"]
        assert quoted in literals and "[sales].[Code]" in literals  # names travel as literals only
        assert "t.[object_id] IN (OBJECT_ID(N'" in batch and "tt.[user_type_id] IN (TYPE_ID(N'" in batch
        assert "is_ms_shipped" not in batch


def test_a_read_of_everything_leaves_out_what_microsoft_ships():
    db = session_for(sales_rows())

    read_model(db)

    for name in ("tabulars", "sequences", "synonyms"):
        (batch,) = db.sent(f"azsqlcd:read_{name}")
        assert "[is_ms_shipped] = 0" in batch and "OBJECT_ID(" not in batch


def test_a_result_set_of_another_shape_is_a_defect_not_an_unsupported_object():
    short = FakeSession()
    short.respond("azsqlcd:read_tabulars", [[(1000, "TABLE", "sales", "Order")]])

    with pytest.raises(RuntimeError, match="query tabulars gave a row that has not 19 columns"):
        read_model(short)


def test_the_width_of_every_result_set_is_the_width_of_the_contract():
    assert {name: width for name, (_, width) in catalog_tables._QUERIES.items()} == {
        name: len(columns) for name, columns in COLUMNS.items()
    }


def test_a_module_key_is_refused_before_any_batch():
    db = FakeSession()

    with pytest.raises(ValueError, match="not the key of a table-class object"):
        read_model(db, ["VIEW:[sales].[v]"])

    assert db.batches == []


def test_no_keys_asked_sends_nothing_and_reads_nothing():
    db = FakeSession()

    found = read_model(db, [])

    assert db.batches == [] and len(found.model) == 0 and found.captures == {}


def test_an_object_that_was_asked_and_does_not_exist_is_absent():
    found = read(rows_from_model(Model([CUSTOMER])), [CUSTOMER_KEY, ORDER_KEY])

    assert list(found.model) == [CUSTOMER_KEY] and list(found.captures) == [CUSTOMER_KEY]


# ------------------------------------------------------------------ blockers and names
BLOCKER_ROWS = [
    ("INDEX", "sales", "Order", "IX_dba"),
    ("STATISTICS", "sales", "Order", "st_Legacy"),
    ("FOREIGN KEY", "sales", "Line", "FK_Line_Order"),
    ("MODULE", "rpt", "vOrders", "V "),
    ("MODULE", "rpt", "vOrders", "V "),
    ("MODULE", "rpt", "fn_x", "IF"),
]


def test_what_hangs_on_a_column_comes_back_as_labels():
    db = FakeSession()
    db.respond("azsqlcd:table_blockers", [BLOCKER_ROWS])

    found = catalog_tables.blockers(db, ORDER_KEY, "Legacy")

    assert found == [
        "FOREIGN KEY [sales].[Line].[FK_Line_Order]",
        "INDEX [sales].[Order].[IX_dba]",
        "SCHEMABOUND FUNCTION:[rpt].[fn_x]",
        "SCHEMABOUND VIEW:[rpt].[vOrders]",
        "STATISTICS [sales].[Order].[st_Legacy]",
    ]
    (batch,) = db.batches
    assert "COLUMNPROPERTY(OBJECT_ID(N'[sales].[Order]'), N'Legacy', N'ColumnId')" in batch
    assert "st.[user_created] = 1" in batch and "d.[is_schema_bound_reference] = 1" in batch
    # sys.objects.type and a name column have different collations: a UNION of both needs one stated
    assert "RTRIM(o.[type]) COLLATE DATABASE_DEFAULT" in batch
    tokens = lex.significant(lex.tokenize(batch))
    assert tokens[0].text == "SELECT" and not {t.text.upper() for t in tokens if t.kind == "word"} & WRITES


def test_a_whole_table_is_blocked_by_foreign_keys_of_other_tables_and_bound_modules_only():
    db = FakeSession()

    assert catalog_tables.blockers(db, ORDER_KEY, None) == []

    (batch,) = db.batches
    assert "sys.indexes" not in batch and "sys.stats" not in batch
    assert "fk.[referenced_object_id] = OBJECT_ID(N'[sales].[Order]')" in batch
    assert "fk.[parent_object_id] <> OBJECT_ID(N'[sales].[Order]')" in batch
    assert "referenced_minor_id" not in batch


def test_a_hostile_column_name_reaches_the_blocker_query_as_a_literal():
    db = FakeSession()

    catalog_tables.blockers(db, ORDER_KEY, HOSTILE)

    literals = [t.value for t in lex.tokenize(db.batches[0]) if t.kind == "nstring"]
    assert literals.count(HOSTILE) == 5  # index, statistics, foreign key twice, module
    assert "DROP" not in {t.text.upper() for t in lex.tokenize(db.batches[0]) if t.kind == "word"}


def test_blockers_are_asked_for_tables_only():
    with pytest.raises(ValueError, match="not the key of a table"):
        catalog_tables.blockers(FakeSession(), "SEQUENCE:[sales].[No]", None)


def test_the_sub_objects_of_a_capture_have_the_labels_of_the_blocker_scan():
    capture = read(sales_rows()).captures[ORDER_KEY]

    assert catalog_tables.recorded_sub_objects(ORDER_KEY, capture) == {
        "INDEX [sales].[Order].[IX_Order_Status]",
        "INDEX [sales].[Order].[PK_Order]",
        "INDEX [sales].[Order].[UQ_Order_Note]",
        "FOREIGN KEY [sales].[Order].[FK_Order_Customer]",
    }


def test_names_that_exist_are_resolved_by_the_engine_and_come_back_as_labels():
    db = FakeSession()
    db.respond("azsqlcd:names_present", [[(0,), (3,), (4,)]])

    found = catalog_tables.names_present(
        db,
        objects=[("sales", "PK_Order"), ("sales", "CK_new")],
        indexes=[("sales", "Order", "IX_new")],
        types=[("sales", "Code")],
        schemas=["audit"],
    )

    # the probes stand in the order: objects, indexes, types, schemas, each sorted
    assert found == {"OBJECT [sales].[CK_new]", "TYPE [sales].[Code]", "SCHEMA [audit]"}
    (batch,) = db.batches
    assert "(0, OBJECT_ID(N'[sales].[CK_new]'))" in batch and "(1, OBJECT_ID(N'[sales].[PK_Order]'))" in batch
    assert "(2, INDEXPROPERTY(OBJECT_ID(N'[sales].[Order]'), N'IX_new', N'IndexID'))" in batch
    assert "(3, TYPE_ID(N'[sales].[Code]'))" in batch and "(4, SCHEMA_ID(N'audit'))" in batch
    assert lex.significant(lex.tokenize(batch))[0].text == "SELECT"
    # TQ-06: a name exists when the engine gives an id for it; IS NULL would report the free names
    assert batch.endswith(") AS v ([n], [id]) WHERE v.[id] IS NOT NULL;")


def test_no_name_to_look_for_sends_no_batch():
    db = FakeSession()

    assert catalog_tables.names_present(db) == set()
    assert db.batches == []


def test_every_built_in_type_of_the_model_can_be_read():
    # a type the parser accepts and the reader cannot read would quarantine every table that has it
    for name in ("bigint", "bit", "money", "date", "text", "uniqueidentifier", "hierarchyid", "json"):
        assert builtin_shape(name) == "none"
        rows = sales_rows()
        only(rows, "columns", name="Note").update(type_name=name, max_length=8)
        note = order_of(read(rows)).column("Note")
        assert note is not None and note.type == TypeRef(name)


# ------------------------------------------------------------------ dynamic data masking
def masked_order(function: str | None = 'partial(1, "XXXX", 0)') -> Table:
    columns = tuple(dataclasses.replace(c, masked=function) if c.name == "Note" else c for c in ORDER.columns)
    return dataclasses.replace(ORDER, columns=columns)


def test_a_masked_column_is_in_the_model_with_the_function_text_of_sys_masked_columns():
    db = session_for(rows_from_model(Model([SALES, masked_order(), CUSTOMER])))

    found = read_model(db)

    note = order_of(found).column("Note")
    assert note is not None and note.masked == 'partial(1, "XXXX", 0)'
    assert list(found.unsupported) == []
    assert [c.masked for c in order_of(found).columns if c.name != "Note"] == [None] * 4
    (batch,) = db.sent("azsqlcd:read_columns")
    assert "mc.[masking_function]" in batch and "LEFT JOIN sys.masked_columns AS mc" in batch


def test_the_capture_holds_the_mask_only_for_a_masked_column_so_other_captures_stay_as_they_were():
    plain = read(rows_from_model(Model([SALES, ORDER, CUSTOMER]))).captures[ORDER_KEY]
    masked = read(rows_from_model(Model([SALES, masked_order(), CUSTOMER]))).captures[ORDER_KEY]

    assert all("masking_function" not in value for name, value in plain.items() if name.startswith("column "))
    assert masked["column [Note]"] == plain["column [Note]"] | {"masking_function": 'partial(1, "XXXX", 0)'}
    assert {name: value for name, value in masked.items() if name != "column [Note]"} == {
        name: value for name, value in plain.items() if name != "column [Note]"
    }


def test_a_mask_that_is_removed_or_changed_in_the_database_is_another_capture_of_the_column():
    stored = read(rows_from_model(Model([SALES, masked_order(), CUSTOMER]))).captures[ORDER_KEY]
    removed = read(rows_from_model(Model([SALES, ORDER, CUSTOMER]))).captures[ORDER_KEY]
    changed = read(rows_from_model(Model([SALES, masked_order("default()"), CUSTOMER]))).captures[ORDER_KEY]

    for live in (removed, changed):
        assert [name for name in stored if stored[name] != live[name]] == ["column [Note]"]


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"is_masked": 1, "masking_function": None}, "masking function cannot be read"),
        ({"is_masked": 1, "masking_function": " "}, "masking function cannot be read"),
        ({"is_masked": 0, "masking_function": "default()"}, "masking function cannot be read"),
    ],
)
def test_a_mask_that_the_catalog_rows_do_not_state_whole_is_quarantined(change: dict[str, Any], says: str):
    rows = sales_rows()
    only(rows, "columns", name="Note").update(change)

    found = read(rows)

    assert ORDER_KEY not in found.model
    assert [(item.object_key, item.code) for item in found.unsupported] == [(ORDER_KEY, UNSUPPORTED)]
    assert "dynamic data masking" in found.unsupported[0].reason and says in found.unsupported[0].reason


def test_a_masked_column_of_a_table_type_stays_outside_the_model():
    kind = TableType("dbo", "IdList", (Column("Id", TypeRef("int"), False),))
    rows = rows_from_model(Model([kind]))
    only(rows, "columns", name="Id").update(is_masked=1, masking_function="default()")

    found = read(rows)

    assert kind.key not in found.model
    assert "dynamic data masking in a table type" in found.unsupported[0].reason
