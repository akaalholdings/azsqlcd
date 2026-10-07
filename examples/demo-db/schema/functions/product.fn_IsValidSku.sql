CREATE OR ALTER FUNCTION [product].[fn_IsValidSku] (@Sku varchar(20))
RETURNS bit
WITH SCHEMABINDING
AS
BEGIN
    -- Scalar function. The CHECK constraint [CK_Product_Sku] of [product].[Product] uses it.
    RETURN CASE
        WHEN @Sku IS NULL THEN 0
        WHEN LEN(@Sku) < 3 THEN 0
        WHEN @Sku LIKE '%[^A-Z0-9-]%' THEN 0
        ELSE 1
    END;
END;
