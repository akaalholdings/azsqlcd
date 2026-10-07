CREATE OR ALTER PROCEDURE [sales].[usp_Ping] @n int
AS
IF @n > 0
BEGIN
    SET @n -= 1;
    EXEC [sales].[usp_Pong] @n = @n;
END;
