-- path: schema/types/dbo.OrderLineList.sql
CREATE TYPE [dbo].[OrderLineList] AS TABLE (
    [LineNumber] smallint NOT NULL,
    [Sku] varchar(20) NOT NULL,
    [Quantity] int NOT NULL CHECK ([Quantity] > 0),
    [UnitPrice] money NOT NULL DEFAULT (0),
    [Total] AS ([Quantity] * [UnitPrice]),
    PRIMARY KEY CLUSTERED ([LineNumber]),
    UNIQUE NONCLUSTERED ([Sku] DESC),
    INDEX [IX_Sku] NONCLUSTERED ([Sku])
);
