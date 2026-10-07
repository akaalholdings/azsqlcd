CREATE TABLE [sales].[Order] (
    [OrderId] bigint NOT NULL,
    [CustomerId] int NOT NULL,
    [Note] nvarchar(200) NULL,
    [Stat] smallint NOT NULL,
    CONSTRAINT [PK_Order] PRIMARY KEY CLUSTERED ([OrderId]),
    CONSTRAINT [FK_Order_Customer] FOREIGN KEY ([CustomerId]) REFERENCES [sales].[Customer] ([CustomerId])
);
GO
CREATE NONCLUSTERED INDEX [IX_Order_Customer] ON [sales].[Order] ([CustomerId]);
GO
CREATE NONCLUSTERED INDEX [IX_Order_Stat] ON [sales].[Order] ([Stat])
    INCLUDE ([CustomerId]);
