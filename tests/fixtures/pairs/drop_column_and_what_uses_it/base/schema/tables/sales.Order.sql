CREATE TABLE [sales].[Order] (
    [OrderId] int NOT NULL,
    [CustomerId] int NOT NULL,
    [Note] nvarchar(200) NULL,
    [Stat] tinyint NOT NULL,
    [Legacy] int NOT NULL CONSTRAINT [DF_Order_Legacy] DEFAULT ((0)),
    CONSTRAINT [PK_Order] PRIMARY KEY CLUSTERED ([OrderId]),
    CONSTRAINT [CK_Order_Legacy] CHECK ([Legacy] >= 0),
    CONSTRAINT [FK_Order_Customer] FOREIGN KEY ([CustomerId]) REFERENCES [sales].[Customer] ([CustomerId])
);
GO
CREATE NONCLUSTERED INDEX [IX_Order_Customer] ON [sales].[Order] ([CustomerId]);
GO
CREATE NONCLUSTERED INDEX [IX_Order_Legacy] ON [sales].[Order] ([Legacy]);
GO
CREATE NONCLUSTERED INDEX [IX_Order_Stat] ON [sales].[Order] ([Stat])
    INCLUDE ([CustomerId]);
