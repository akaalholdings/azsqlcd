-- expect: SYNTAX
-- says: N'...' literal
-- line: 4
EXEC sys.sp_rename @objname = N'[sales].[Order].[Stat]', @newname = N'Status', @objtype = N'COLUMN';
