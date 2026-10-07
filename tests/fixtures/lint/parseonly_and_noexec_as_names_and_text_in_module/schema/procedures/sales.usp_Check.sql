CREATE OR ALTER PROCEDURE [sales].[usp_Check]
AS
BEGIN
    -- SET PARSEONLY ON; SET NOEXEC ON;
    SET NOCOUNT ON;
    SELECT [PARSEONLY], "parseonly", noexec, N'SET PARSEONLY ON; SET NOEXEC ON' AS [Text]
    FROM [sales].[Settings] /* set noexec on */;
    UPDATE [sales].[Settings] SET noexec = 1 WHERE [PARSEONLY] = 0;
END
