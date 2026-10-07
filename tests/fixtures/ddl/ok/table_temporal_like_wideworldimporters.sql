-- path: schema/tables/Application.People.sql
CREATE TABLE [Application].[People] (
    [PersonID] int NOT NULL CONSTRAINT [DF_Application_People_PersonID] DEFAULT (NEXT VALUE FOR [Sequences].[PersonID]),
    [FullName] nvarchar(50) NOT NULL,
    [PreferredName] nvarchar(50) NOT NULL,
    [SearchName] AS (concat([PreferredName],N' ',[FullName])) PERSISTED NOT NULL,
    [IsEmployee] bit NOT NULL,
    [CustomFields] nvarchar(max) NULL,
    [OtherLanguages] AS (json_query([CustomFields],N'$.OtherLanguages')),
    [LastEditedBy] int NOT NULL,
    [ValidFrom] datetime2(7) GENERATED ALWAYS AS ROW START NOT NULL,
    [ValidTo] datetime2(7) GENERATED ALWAYS AS ROW END NOT NULL,
    CONSTRAINT [PK_Application_People] PRIMARY KEY CLUSTERED ([PersonID]),
    CONSTRAINT [FK_Application_People_Application_People] FOREIGN KEY ([LastEditedBy]) REFERENCES [Application].[People] ([PersonID]),
    PERIOD FOR SYSTEM_TIME ([ValidFrom], [ValidTo])
)
WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [Application].[People_Archive]));
GO
CREATE NONCLUSTERED INDEX [IX_Application_People_FullName] ON [Application].[People] ([FullName]);
GO
CREATE NONCLUSTERED INDEX [IX_Application_People_IsEmployee] ON [Application].[People] ([IsEmployee]);
