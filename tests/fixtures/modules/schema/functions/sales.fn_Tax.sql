CREATE OR ALTER FUNCTION [sales].[fn_Tax] (@Amount decimal(19, 4), @Rate decimal(5, 4) = 0.2000)
RETURNS decimal(19, 4)
WITH SCHEMABINDING
AS
BEGIN
    RETURN ROUND(@Amount * @Rate, 2);
END;
