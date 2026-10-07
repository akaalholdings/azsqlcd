CREATE OR ALTER PROC dbo.usp_ParamAs @a AS int, @b AS varchar(20) = 'x' OUTPUT WITH RECOMPILE, EXECUTE AS 'report_reader' AS
SELECT @a AS a, @b AS b;
EXEC dbo.usp_CastDefault @Rows = @a OUTPUT;
