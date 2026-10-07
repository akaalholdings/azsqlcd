-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:allow LONG_LOCK [sales].[Heap] reason: 2000 rows, measured 1 s in test
ALTER TABLE [sales].[Heap] REBUILD WITH (DATA_COMPRESSION = PAGE);
GO
ALTER TABLE [sales].[Person] ALTER COLUMN [OldGuid] DROP ROWGUIDCOL;
GO
ALTER TABLE [sales].[Person] ALTER COLUMN [PersonId] ADD NOT FOR REPLICATION;
GO
-- azsqlcd:allow LONG_LOCK [sales].[Person] reason: 2000 rows, measured 1 s in test
ALTER TABLE [sales].[Person] ALTER COLUMN [Notes] DROP SPARSE;
GO
CREATE TABLE [sales].[Fact] (
    [DateKey] int NOT NULL,
    [Flags] int NOT NULL,
    [IsOpen] AS [Flags] & 4,
    [Amount] money NOT NULL CONSTRAINT [DF_Fact_Amount] DEFAULT $0
) WITH (DATA_COMPRESSION = ROW);
GO
CREATE CLUSTERED COLUMNSTORE INDEX [CCI_Fact] ON [sales].[Fact] ORDER ([DateKey])
    WITH (DATA_COMPRESSION = COLUMNSTORE_ARCHIVE);
GO
ALTER TABLE [sales].[Fact] ADD CONSTRAINT [CK_Fact_Amount] CHECK NOT FOR REPLICATION ([Amount] >= $0);
