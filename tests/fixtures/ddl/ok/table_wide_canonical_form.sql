-- path: schema/tables/sales.Document.sql
CREATE TABLE [sales].[Document] (
    [DocumentId] bigint IDENTITY(1, 1) NOT FOR REPLICATION NOT NULL,
    [RowGuid] uniqueidentifier ROWGUIDCOL NOT NULL CONSTRAINT [DF_Document_RowGuid] DEFAULT (newsequentialid()),
    [OwnerId] int NOT NULL,
    [Email] varchar(320) COLLATE Latin1_General_100_CI_AS MASKED WITH (FUNCTION = 'email()') NULL,
    [CardNo] char(16) MASKED WITH (FUNCTION = 'partial(0, "XXXXXXXXXXXX", 4)') NOT NULL,
    [Salary] decimal(19, 4) MASKED WITH (FUNCTION = 'random(1.0000, 100.0000)') NULL,
    [BornOn] date MASKED WITH (FUNCTION = 'datetime("Y")') NULL,
    [Notes] nvarchar(400) NULL,
    [Body] xml NULL,
    CONSTRAINT [PK_Document] PRIMARY KEY CLUSTERED ([DocumentId]) WITH (XML_COMPRESSION = ON),
    CONSTRAINT [CK_Document_Owner] CHECK NOT FOR REPLICATION ([OwnerId] > 0),
    CONSTRAINT [FK_Document_Owner] FOREIGN KEY ([OwnerId]) REFERENCES [sales].[Owner] ([OwnerId]) ON DELETE CASCADE NOT FOR REPLICATION
);
GO
CREATE NONCLUSTERED COLUMNSTORE INDEX [NCCI_Document] ON [sales].[Document] ([OwnerId], [Salary])
    WHERE [OwnerId] > 100
    WITH (COMPRESSION_DELAY = 10, DATA_COMPRESSION = COLUMNSTORE_ARCHIVE);
