-- path: schema/tables/purchasing.Supplier.sql
CREATE TABLE [purchasing].[Supplier] (
    [SupplierId] int NOT NULL,
    [BankAccountName] nvarchar(50) MASKED WITH (FUNCTION = 'default()') NULL,
    [BankAccountNumber] nvarchar(20) MASKED WITH (FUNCTION = 'default()') NULL,
    [ValidFrom] datetime2(7) GENERATED ALWAYS AS ROW START NOT NULL,
    [ValidTo] datetime2(7) GENERATED ALWAYS AS ROW END NOT NULL,
    CONSTRAINT [PK_Supplier] PRIMARY KEY CLUSTERED ([SupplierId]),
    PERIOD FOR SYSTEM_TIME ([ValidFrom], [ValidTo])
)
WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [purchasing].[Supplier_Archive]));
