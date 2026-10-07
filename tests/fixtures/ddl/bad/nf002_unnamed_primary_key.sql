-- expect: NF002
-- says: PRIMARY KEY
-- line: 7
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL,
    PRIMARY KEY CLUSTERED ([Id])
);
