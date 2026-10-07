CREATE OR ALTER PROCEDURE [dbo].[usp_CastDefault]
(
    @Days int = CAST('7' AS int),
    @Mode AS varchar(10) = 'AS',
    @Rows int OUT
)
AS
BEGIN
    SELECT @Rows = COUNT(*) FROM [dbo].[AuditLog] AS a WHERE a.[Mode] = @Mode AND a.[AgeDays] <= @Days;
END;
