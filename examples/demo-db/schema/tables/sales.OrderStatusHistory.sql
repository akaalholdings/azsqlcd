CREATE TABLE [sales].[OrderStatusHistory] (
    [OrderStatusHistoryId] bigint IDENTITY(1, 1) NOT NULL,
    [OrderId] bigint NOT NULL,
    [OldStatus] tinyint NULL,
    [NewStatus] tinyint NOT NULL,
    [ChangedUtc] datetime2(3) NOT NULL CONSTRAINT [DF_OrderStatusHistory_ChangedUtc] DEFAULT (SYSUTCDATETIME()),
    [ChangedBy] nvarchar(128) NOT NULL CONSTRAINT [DF_OrderStatusHistory_ChangedBy] DEFAULT (ORIGINAL_LOGIN()),
    CONSTRAINT [PK_OrderStatusHistory] PRIMARY KEY NONCLUSTERED ([OrderStatusHistoryId]),
    CONSTRAINT [FK_OrderStatusHistory_Order] FOREIGN KEY ([OrderId]) REFERENCES [sales].[Order] ([OrderId]) ON DELETE CASCADE
);
GO
CREATE CLUSTERED INDEX [CIX_OrderStatusHistory] ON [sales].[OrderStatusHistory] ([OrderId], [ChangedUtc]);
