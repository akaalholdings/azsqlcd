-- expect: SYNTAX
-- says: without brackets
-- line: 4
EXEC sys.sp_rename N'[sales].[Order].[Stat]', N'[Status]', N'COLUMN';
