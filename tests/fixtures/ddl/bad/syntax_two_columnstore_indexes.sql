-- expect: SYNTAX
-- says: more than one columnstore index
-- line: 5
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [a] int NOT NULL,
    INDEX [NCCI_1] NONCLUSTERED COLUMNSTORE ([a]),
    INDEX [NCCI_2] NONCLUSTERED COLUMNSTORE ([a])
);
