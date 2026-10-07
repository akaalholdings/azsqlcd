CREATE OR ALTER FUNCTION [dbo].[fn_Round] (@Total decimal(18, 2))
RETURNS decimal(18, 2)
AS
BEGIN
    RETURN @Total * 0.3;
END
