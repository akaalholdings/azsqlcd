-- expect: NF005
-- says: UNIQUE
-- line: 7
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL,
    CONSTRAINT [UQ_T] UNIQUE ([Id])
);
