CREATE TABLE [sales].[Old] (
    [Id] int NOT NULL,
    [V] int NULL
);
GO
CREATE NONCLUSTERED COLUMNSTORE INDEX [NCCI_Old] ON [sales].[Old] ([Id], [V]);
