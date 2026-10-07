CREATE TABLE [stock].[Warehouse] (
    [WarehouseId] smallint IDENTITY(1, 1) NOT NULL,
    [Code] char(4) NOT NULL,
    [Name] nvarchar(100) NOT NULL,
    [CountryCode] char(2) NOT NULL,
    [IsActive] bit NOT NULL CONSTRAINT [DF_Warehouse_IsActive] DEFAULT (1),
    CONSTRAINT [PK_Warehouse] PRIMARY KEY CLUSTERED ([WarehouseId]),
    CONSTRAINT [UQ_Warehouse_Code] UNIQUE NONCLUSTERED ([Code])
);
