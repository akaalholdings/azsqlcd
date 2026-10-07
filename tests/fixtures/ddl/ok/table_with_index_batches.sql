-- path: schema/tables/sales.Order.sql
CREATE TABLE [sales].[Order] (
    [OrderId] int IDENTITY(1,1) NOT NULL,
    [CustomerId] int NOT NULL,
    [OrderDate] date NOT NULL,
    [Status] tinyint NOT NULL CONSTRAINT [DF_Order_Status] DEFAULT (0),
    [ShippedUtc] datetime2(3) NULL,
    [TotalDue] money NOT NULL,
    [CurrencyCode] char(3) NOT NULL,
    CONSTRAINT [PK_Order] PRIMARY KEY CLUSTERED ([OrderId])
);
GO
CREATE NONCLUSTERED INDEX [IX_Order_Customer_Open] ON [sales].[Order] ([CustomerId] ASC, [OrderDate] DESC)
    INCLUDE ([TotalDue], [CurrencyCode])
    WHERE [Status] = 0 AND [ShippedUtc] IS NULL
    WITH (FILLFACTOR = 90, DATA_COMPRESSION = PAGE);
GO
CREATE UNIQUE NONCLUSTERED INDEX [UX_Order_Shipped] ON [sales].[Order] ([ShippedUtc], [OrderId]) WHERE ([ShippedUtc] IS NOT NULL);
GO
CREATE NONCLUSTERED INDEX [IX_Order_Status] ON [sales].[Order] ([Status]);
GO
