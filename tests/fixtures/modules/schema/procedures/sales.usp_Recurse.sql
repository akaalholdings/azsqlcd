CREATE OR ALTER PROCEDURE [sales].[usp_Recurse] @Depth int = 0
AS
IF @Depth < 10
BEGIN
    SET @Depth += 1;
    EXEC [sales].[usp_Recurse] @Depth = @Depth;
END;
