CREATE TABLE [sales].[Order] (
    [OrderId] int NOT NULL,
    [Status] tinyint NOT NULL
);
GO
CREATE NONCLUSTERED INDEX [IX_Order_Status] ON [sales].[Order] ([Status]);
