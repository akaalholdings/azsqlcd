CREATE TABLE [product].[PriceHistory] (
    [PriceHistoryId] int IDENTITY(1, 1) NOT NULL,
    [ProductId] int NOT NULL,
    [ListPrice] decimal(19, 4) NOT NULL,
    [ValidFromUtc] datetime2(3) NOT NULL CONSTRAINT [DF_PriceHistory_ValidFromUtc] DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT [PK_PriceHistory] PRIMARY KEY CLUSTERED ([PriceHistoryId]),
    CONSTRAINT [CK_PriceHistory_ListPrice] CHECK ([ListPrice] >= 0),
    CONSTRAINT [FK_PriceHistory_Product] FOREIGN KEY ([ProductId]) REFERENCES [product].[Product] ([ProductId]) ON DELETE CASCADE
);
GO
CREATE NONCLUSTERED INDEX [IX_PriceHistory_Product] ON [product].[PriceHistory] ([ProductId], [ValidFromUtc] DESC)
    INCLUDE ([ListPrice]);
