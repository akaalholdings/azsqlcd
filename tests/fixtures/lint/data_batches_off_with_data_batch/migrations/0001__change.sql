-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
ALTER TABLE [sales].[Order] ADD [Status] int NULL;
GO
-- azsqlcd:data
UPDATE [sales].[Order] SET [Status] = 1;
GO
-- azsqlcd:data
-- azsqlcd:allow TRUNCATE [sales].[Old] reason: nothing reads it
COMMIT;
DROP TABLE [sales].[Old];
