-- expect: SYNTAX
-- says: period column [ValidFrom] must be GENERATED ALWAYS AS ROW_START
-- line: 5
-- path: schema/tables/dbo.Product.sql
CREATE TABLE [dbo].[Product] (
    [ProductId] int NOT NULL,
    [ValidFrom] datetime2(7) NOT NULL,
    [ValidTo] datetime2(7) NOT NULL,
    CONSTRAINT [PK_Product] PRIMARY KEY CLUSTERED ([ProductId]),
    PERIOD FOR SYSTEM_TIME ([ValidFrom], [ValidTo])
)
WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [dbo].[ProductHistory]));
