-- expect: SYNTAX
-- says: 'AUTO'
-- line: 7
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL,
    [Status] tinyint CONSTRAINT [DF_T_Status] DEFAULT 0 AUTO NOT NULL
);
