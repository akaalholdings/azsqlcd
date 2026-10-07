CREATE OR ALTER PROCEDURE [sales].[usp_RecordPayment]
    @OrderId bigint,
    @Amount decimal(19, 4),
    @Method varchar(20),
    @Reference nvarchar(100) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    BEGIN TRANSACTION;

    INSERT INTO [sales].[Payment] ([OrderId], [Amount], [Method], [Reference])
    VALUES (@OrderId, @Amount, @Method, @Reference);

    -- New (0) becomes Paid (1) when the payments cover the order total.
    UPDATE o
    SET o.[Status] = 1
    FROM [sales].[Order] AS o
    WHERE o.[OrderId] = @OrderId
      AND o.[Status] = 0
      AND (SELECT SUM(p.[Amount]) FROM [sales].[Payment] AS p WHERE p.[OrderId] = o.[OrderId])
          >= (SELECT t.[OrderTotal] FROM [sales].[vw_OrderTotals] AS t WHERE t.[OrderId] = o.[OrderId]);

    COMMIT TRANSACTION;
END;
