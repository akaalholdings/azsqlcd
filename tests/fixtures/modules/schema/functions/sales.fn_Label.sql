CREATE OR ALTER FUNCTION [sales].[fn_Label] (@OrderId int, @Prefix varchar(40) = 'WITH SCHEMABINDING AS')
RETURNS varchar(60)
WITH EXEC AS CALLER
AS
BEGIN
    RETURN @Prefix + '-' + CAST(@OrderId AS varchar(12));
END;
