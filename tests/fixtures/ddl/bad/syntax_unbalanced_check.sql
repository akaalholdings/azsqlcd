-- expect: SYNTAX
-- says: found ';'
-- line: 8
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL,
    CONSTRAINT [CK_T_Id] CHECK (([Id] > 0)
);
