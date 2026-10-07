CREATE OR ALTER FUNCTION [sales].[fn_OrderStatusName] (@Status tinyint)
RETURNS nvarchar(20)
WITH SCHEMABINDING
AS
BEGIN
    -- Scalar function. The values are those of the CHECK constraint [CK_Order_Status].
    RETURN CASE @Status
        WHEN 0 THEN N'New'
        WHEN 1 THEN N'Paid'
        WHEN 2 THEN N'Shipped'
        WHEN 3 THEN N'Delivered'
        WHEN 4 THEN N'Cancelled'
        ELSE N'Unknown'
    END;
END;
