-- expect: SYNTAX
-- says: parentheses
-- line: 7
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL,
    [Status] tinyint NOT NULL CONSTRAINT [DF_T_Status] DEFAULT CASE WHEN 1 = 1 THEN 0 ELSE 1 END
);
