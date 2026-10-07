CREATE OR ALTER PROCEDURE [sales].[usp_PlaceOrder]
    @CustomerId int,
    @CurrencyCode char(3),
    @Lines [sales].[OrderLineInput] READONLY,
    @ShipToAddressId int = NULL,
    @OrderId bigint OUTPUT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    IF NOT EXISTS (SELECT 1 FROM @Lines)
        THROW 50010, N'An order needs at least one line.', 1;

    IF EXISTS (
        SELECT 1
        FROM @Lines AS l
        LEFT JOIN [product].[vw_ActiveProducts] AS p ON p.[ProductId] = l.[ProductId]
        WHERE p.[ProductId] IS NULL
    )
        THROW 50011, N'A line names a product that does not exist or is not active.', 1;

    BEGIN TRANSACTION;

    INSERT INTO [sales].[Order] ([CustomerId], [ShipToAddressId], [CurrencyCode])
    VALUES (@CustomerId, @ShipToAddressId, @CurrencyCode);

    SET @OrderId = SCOPE_IDENTITY();

    INSERT INTO [sales].[OrderLine] ([OrderId], [LineNumber], [ProductId], [Quantity], [UnitPrice], [DiscountPercent])
    SELECT
        @OrderId,
        ROW_NUMBER() OVER (ORDER BY l.[ProductId]),
        l.[ProductId],
        l.[Quantity],
        p.[ListPrice],
        l.[DiscountPercent]
    FROM @Lines AS l
    JOIN [product].[Product] AS p ON p.[ProductId] = l.[ProductId];

    COMMIT TRANSACTION;
END;
