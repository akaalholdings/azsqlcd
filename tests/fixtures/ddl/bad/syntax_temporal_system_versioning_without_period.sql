-- expect: SYNTAX
-- says: SYSTEM_VERSIONING = ON needs PERIOD FOR SYSTEM_TIME
-- line: 10
-- path: schema/tables/dbo.Product.sql
CREATE TABLE [dbo].[Product] (
    [ProductId] int NOT NULL,
    [ValidFrom] datetime2(7) NOT NULL,
    [ValidTo] datetime2(7) NOT NULL
)
WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [dbo].[ProductHistory]));
