CREATE OR ALTER PROCEDURE [sales].[usp_CancelOrder]
    @OrderId bigint,
    @Note nvarchar(1000) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    UPDATE [sales].[Order]
    SET [Status] = 4,
        [Note] = COALESCE(@Note, [Note])
    WHERE [OrderId] = @OrderId
      AND [Status] IN (0, 1);

    IF @@ROWCOUNT = 0
        THROW 50040, N'The order does not exist, or it is shipped already.', 1;
END;
