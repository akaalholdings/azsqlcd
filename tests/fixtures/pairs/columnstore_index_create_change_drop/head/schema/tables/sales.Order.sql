CREATE TABLE [sales].[Order] (
    [OrderId] int NOT NULL,
    [Total] money NOT NULL,
    CONSTRAINT [PK_Order] PRIMARY KEY CLUSTERED ([OrderId])
);
GO
CREATE NONCLUSTERED COLUMNSTORE INDEX [NCCI_Order] ON [sales].[Order] ([OrderId], [Total])
    WHERE [Total] > 0
    WITH (COMPRESSION_DELAY = 10);
