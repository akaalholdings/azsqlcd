-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
CREATE TABLE [stock].[Bin] (
    [BinId] int NOT NULL
);
GO
ALTER TABLE [stock].[Bin] ADD [Region] varchar(10) NOT NULL;
GO
ALTER TABLE [stock].[Warehouse] ADD
    [Region] varchar(10) NOT NULL CONSTRAINT [DF_Warehouse_Region] DEFAULT ('EU'),
    [Note] nvarchar(200) NULL,
    [Version] rowversion NOT NULL;
