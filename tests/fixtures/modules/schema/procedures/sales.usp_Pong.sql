CREATE OR ALTER PROCEDURE [sales].[usp_Pong] @n int
AS
IF @n > 0
BEGIN
    SET @n -= 1;
    EXEC [sales].[usp_Ping] @n = @n;
END;
