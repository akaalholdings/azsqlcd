-- expect: UNSUPPORTED
-- says: filegroup [FG_ARCHIVE]
-- line: 8
-- path: schema/tables/dbo.Archive.sql
CREATE TABLE [dbo].[Archive] (
    [ArchiveId] bigint NOT NULL
)
ON [FG_ARCHIVE];
