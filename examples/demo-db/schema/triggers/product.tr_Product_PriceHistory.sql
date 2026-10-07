CREATE OR ALTER TRIGGER [product].[tr_Product_PriceHistory]
ON [product].[Product]
AFTER INSERT, UPDATE
AS
BEGIN
    SET NOCOUNT ON;

    INSERT INTO [product].[PriceHistory] ([ProductId], [ListPrice])
    SELECT i.[ProductId], i.[ListPrice]
    FROM inserted AS i
    LEFT JOIN deleted AS d ON d.[ProductId] = i.[ProductId]
    WHERE d.[ProductId] IS NULL
       OR d.[ListPrice] <> i.[ListPrice];
END;
