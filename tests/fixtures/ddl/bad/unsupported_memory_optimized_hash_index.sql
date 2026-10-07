-- expect: UNSUPPORTED
-- says: HASH
-- line: 7
-- path: schema/tables/dbo.Hot.sql
CREATE TABLE [dbo].[Hot] (
    [Id] int NOT NULL,
    CONSTRAINT [PK_Hot] PRIMARY KEY NONCLUSTERED HASH ([Id]) WITH (BUCKET_COUNT = 1024)
);
