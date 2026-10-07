CREATE OR ALTER PROC [sales].[usp_PlaceOrder]
    @CustomerId int,
    @Lines [sales].[OrderLine_tt] READONLY,
    @Note nvarchar(200) = N'AS GO CREATE',
    @Priority tinyint = 3,
    @OrderId int = NULL OUTPUT
AS
BEGIN
    SET NOCOUNT ON;
    SET @OrderId = NEXT VALUE FOR [sales].[OrderNo];
    INSERT INTO [sales].[Order] ([OrderId], [CustomerId], [Note]) VALUES (@OrderId, @CustomerId, @Note);
    INSERT INTO [sales].[OrderLine] ([OrderId], [LineNo], [Qty], [UnitPrice])
    SELECT @OrderId, l.[LineNo], l.[Qty], l.[UnitPrice] FROM @Lines AS l;
    SELECT n.[n] FROM fn_Numbers(@Priority) AS n;
END;
