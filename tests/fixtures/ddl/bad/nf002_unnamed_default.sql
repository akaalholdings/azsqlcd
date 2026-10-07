-- expect: NF002
-- says: DEFAULT
-- line: 7
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL,
    [Status] tinyint NOT NULL DEFAULT (0)
);
