-- path: schema/tables/sales.OrderLine.sql
CREATE TABLE [sales].[OrderLine] (
    [OrderId] int NOT NULL,
    [LineNumber] smallint NOT NULL CONSTRAINT [CK_OrderLine_LineNumber] CHECK ([LineNumber] > 0),
    [Quantity] int NOT NULL CONSTRAINT [CK_OrderLine_Quantity] CHECK ([Quantity] > 0 AND [Quantity] <= 10000),
    [UnitPrice] money NOT NULL,
    [Discount] decimal(5,4) NOT NULL,
    [Status] char(1) NOT NULL CONSTRAINT [CK_OrderLine_Status] CHECK ([Status] IN ('N', 'P', 'S', 'X')),
    [Sku] varchar(20) NOT NULL CONSTRAINT [CK_OrderLine_Sku] CHECK ([Sku] LIKE '[A-Z][A-Z]-[0-9][0-9][0-9][0-9]%' OR [Sku] LIKE 'LEGACY\_%' ESCAPE '\'),
    [ShipDate] date NULL,
    [DueDate] date NULL,
    CONSTRAINT [CK_OrderLine_Dates] CHECK ([ShipDate] IS NULL OR [DueDate] IS NULL OR [ShipDate] <= [DueDate]),
    CONSTRAINT [CK_OrderLine_Price] CHECK (([UnitPrice] >= (0)) AND ([UnitPrice] * [Quantity]) < 1000000)
);
