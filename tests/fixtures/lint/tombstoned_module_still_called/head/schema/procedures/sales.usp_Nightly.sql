CREATE OR ALTER PROCEDURE [sales].[usp_Nightly]
AS
BEGIN
    EXEC [sales].[usp_Old];
END
