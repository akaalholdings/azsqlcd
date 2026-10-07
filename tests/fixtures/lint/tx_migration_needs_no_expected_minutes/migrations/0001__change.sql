-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
ALTER TABLE [sales].[Order] ADD [Note] nvarchar(200) NULL;
