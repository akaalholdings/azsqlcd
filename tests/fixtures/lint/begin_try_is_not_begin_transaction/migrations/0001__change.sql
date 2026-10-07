-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
BEGIN TRY
    UPDATE [sales].[Order] SET [Status] = 1 WHERE [Status] IS NULL;
END TRY
BEGIN CATCH
    THROW;
END CATCH
