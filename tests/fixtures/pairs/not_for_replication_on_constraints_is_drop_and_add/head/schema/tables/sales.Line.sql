CREATE TABLE [sales].[Line] (
    [LineId] int NOT NULL,
    [ParentId] int NULL,
    [Qty] int NOT NULL,
    CONSTRAINT [PK_Line] PRIMARY KEY CLUSTERED ([LineId]),
    CONSTRAINT [CK_Line_Qty] CHECK NOT FOR REPLICATION ([Qty] > 0),
    CONSTRAINT [FK_Line_Parent] FOREIGN KEY ([ParentId]) REFERENCES [sales].[Line] ([LineId])
);
