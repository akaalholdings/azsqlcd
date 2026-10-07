CREATE TABLE [sales].[Line] (
    [LineId] int NOT NULL,
    [Qty] int NOT NULL,
    [Price] decimal(10, 2) NOT NULL,
    [Total] AS ([Qty] * [Price] * 2) PERSISTED NOT NULL,
    CONSTRAINT [PK_Line] PRIMARY KEY CLUSTERED ([LineId])
);
GO
CREATE NONCLUSTERED INDEX [IX_Line_Total] ON [sales].[Line] ([Total]);
