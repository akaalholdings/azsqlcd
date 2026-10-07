CREATE OR ALTER PROCEDURE [sales].[usp_Close]
    @OrderId int
AS
BEGIN
    SET XACT_ABORT ON;
    IF @OrderId IS NULL
        RETURN 1;
    BEGIN TRANSACTION;
    UPDATE [sales].[Order] SET [Status] = 9 WHERE [OrderId] = @OrderId;
    COMMIT;
    RETURN 0;
END
