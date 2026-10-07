-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
SET NOCOUNT ON;
SET XACT_ABORT OFF;
SET NOEXEC ON;
SET PARSEONLY ON;
SAVE TRANSACTION before_update;
UPDATE [sales].[Order] SET [Status] = 1 WHERE [Status] IS NULL;
