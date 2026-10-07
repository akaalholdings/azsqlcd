-- expect: SYNTAX
-- says: [schema].[table].[name]
-- line: 4
EXEC sys.sp_rename N'[Order].[Stat]', N'Status', N'COLUMN';
