CREATE TABLE [sales].[Archive] (
    [ArchiveId] bigint NOT NULL,
    [Body] nvarchar(400) NULL
);
GO
CREATE CLUSTERED COLUMNSTORE INDEX [CCI_Archive] ON [sales].[Archive];
