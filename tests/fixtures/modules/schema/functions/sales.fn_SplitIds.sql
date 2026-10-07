CREATE OR ALTER FUNCTION [sales].[fn_SplitIds] (@List nvarchar(max), @Sep nchar(1) = N',')
RETURNS @t TABLE ([Id] int NOT NULL PRIMARY KEY WITH (IGNORE_DUP_KEY = ON), [Pos] int NOT NULL)
WITH SCHEMABINDING
AS
BEGIN
    INSERT INTO @t ([Id], [Pos])
    SELECT CAST(s.[value] AS int), s.[ordinal]
    FROM STRING_SPLIT(@List, @Sep, 1) AS s;
    RETURN;
END;
