-- path: schema/tables/sales.OrderEvent.sql
-- The key states the place of the table. The inline index states none: the engine puts it on the
-- scheme of the table, and so does the model (the canonical form writes the clause).
CREATE TABLE [sales].[OrderEvent] (
    [OrderEventId] bigint NOT NULL,
    [OrderDate] date NOT NULL,
    [Kind] tinyint NOT NULL,
    CONSTRAINT [PK_OrderEvent] PRIMARY KEY CLUSTERED ([OrderEventId], [OrderDate] DESC) ON [PS_OrderYear] ([OrderDate]),
    INDEX [IX_OrderEvent_Kind] NONCLUSTERED ([Kind])
) ON [PS_OrderYear] ([OrderDate]);
