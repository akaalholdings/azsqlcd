CREATE OR ALTER FUNCTION [sales].[fn_Tax] (@Total decimal(18, 2))
RETURNS decimal(18, 2)
AS
BEGIN
    RETURN @Total * 0.2;
END
