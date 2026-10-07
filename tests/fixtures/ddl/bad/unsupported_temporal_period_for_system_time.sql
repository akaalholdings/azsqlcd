-- expect: UNSUPPORTED
-- says: PERIOD FOR SYSTEM_TIME
-- line: 9
-- path: schema/tables/dbo.Product.sql
CREATE TABLE [dbo].[Product] (
    [ProductId] int NOT NULL,
    [ValidFrom] datetime2(7) NOT NULL,
    [ValidTo] datetime2(7) NOT NULL,
    PERIOD FOR SYSTEM_TIME ([ValidFrom], [ValidTo]),
    CONSTRAINT [PK_Product] PRIMARY KEY CLUSTERED ([ProductId])
);
