-- expect: UNSUPPORTED
-- says: DATA_CONSISTENCY_CHECK in CREATE TABLE
-- line: 12
-- path: schema/tables/dbo.Product.sql
CREATE TABLE [dbo].[Product] (
    [ProductId] int NOT NULL,
    [ValidFrom] datetime2(7) GENERATED ALWAYS AS ROW START NOT NULL,
    [ValidTo] datetime2(7) GENERATED ALWAYS AS ROW END NOT NULL,
    CONSTRAINT [PK_Product] PRIMARY KEY CLUSTERED ([ProductId]),
    PERIOD FOR SYSTEM_TIME ([ValidFrom], [ValidTo])
)
WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [dbo].[ProductHistory], DATA_CONSISTENCY_CHECK = ON));
