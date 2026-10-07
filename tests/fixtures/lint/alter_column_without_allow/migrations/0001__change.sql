-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
ALTER TABLE [sales].[Order] ALTER COLUMN [Note] nvarchar(400) NULL;
