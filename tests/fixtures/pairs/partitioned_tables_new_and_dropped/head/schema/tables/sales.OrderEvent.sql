CREATE TABLE [sales].[OrderEvent] (
    [OrderEventId] bigint NOT NULL,
    [OrderDate] date NOT NULL,
    [Kind] tinyint NOT NULL,
    CONSTRAINT [PK_OrderEvent] PRIMARY KEY CLUSTERED ([OrderDate], [OrderEventId]) WITH (STATISTICS_INCREMENTAL = ON) ON [PS_OrderYear]([OrderDate]),
    CONSTRAINT [UQ_OrderEvent] UNIQUE NONCLUSTERED ([OrderEventId]) ON [PRIMARY]
)
ON [PS_OrderYear]([OrderDate]);
GO
CREATE NONCLUSTERED INDEX [IX_OrderEvent_Kind] ON [sales].[OrderEvent] ([Kind])
    ON [PS_OrderYear]([OrderDate]);
