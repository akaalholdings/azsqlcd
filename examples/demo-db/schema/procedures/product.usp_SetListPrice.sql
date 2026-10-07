CREATE OR ALTER PROCEDURE [product].[usp_SetListPrice]
    @ProductId int,
    @ListPrice decimal(19, 4)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    IF NOT EXISTS (SELECT 1 FROM [product].[Product] WHERE [ProductId] = @ProductId)
        THROW 50050, N'The product does not exist.', 1;

    -- The trigger [product].[tr_Product_PriceHistory] records the new price.
    UPDATE [product].[Product]
    SET [ListPrice] = @ListPrice
    WHERE [ProductId] = @ProductId
      AND [ListPrice] <> @ListPrice;
END;
