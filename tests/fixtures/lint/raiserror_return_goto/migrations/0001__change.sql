-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
IF NOT EXISTS (SELECT 1 FROM [sales].[Order])
BEGIN
    RAISERROR(N'empty', 16, 1);
    RETURN;
END
GOTO done;
done:
UPDATE [sales].[Order] SET [Status] = 1 WHERE [Status] IS NULL;
