CREATE TABLE [sales].[Line] (
    [LineId] int NOT NULL,
    [Qty] int NOT NULL,
    [Price] decimal(10, 2) NOT NULL,
    [Gross] AS ([Qty] * [Price] * 1.2),
    CONSTRAINT [PK_Line] PRIMARY KEY CLUSTERED ([LineId])
);
