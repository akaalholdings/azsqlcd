-- path: schema/types/dbo.GuidList.sql
CREATE TYPE [dbo].[GuidList] AS TABLE (
    [Id] uniqueidentifier ROWGUIDCOL NOT NULL
);
