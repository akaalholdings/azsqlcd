-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
SET NOCOUNT, XACT_ABORT OFF;
SET NOCOUNT, ANSI_WARNINGS ON;
SET IMPLICIT_TRANSACTIONS ON;
UPDATE [sales].[Order] SET [Status] = 1 WHERE [Status] IS NULL;
