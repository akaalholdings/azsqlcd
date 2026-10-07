-- expect: UNSUPPORTED
-- says: temporal tables (GENERATED ALWAYS)
-- line: 7
-- path: schema/tables/dbo.Product.sql
CREATE TABLE [dbo].[Product] (
    [ProductId] int NOT NULL,
    [ValidFrom] datetime2(7) GENERATED ALWAYS AS ROW START NOT NULL,
    [ValidTo] datetime2(7) NOT NULL
);
