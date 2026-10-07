-- azsqlcd:migration 0001__add_note
-- azsqlcd:mode tx
ALTER TABLE [sales].[Order] ADD [Note] nvarchar(400) NULL;
