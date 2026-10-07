-- path: schema/tables/dbo.Document.sql
CREATE TABLE [dbo].[Document] (
    [DocumentId] uniqueidentifier NOT NULL CONSTRAINT [DF_Document_Id] DEFAULT (NEWID()),
    [Title] nvarchar(200) COLLATE Latin1_General_100_CI_AS_SC NOT NULL,
    [Slug] varchar(200) COLLATE [Latin1_General_100_BIN2_UTF8] NOT NULL,
    [Notes] nvarchar(max) NULL,
    CONSTRAINT [PK_Document] PRIMARY KEY NONCLUSTERED ([DocumentId])
);
