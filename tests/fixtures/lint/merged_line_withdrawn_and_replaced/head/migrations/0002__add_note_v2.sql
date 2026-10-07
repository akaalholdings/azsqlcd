-- azsqlcd:migration 0002__add_note_v2
-- azsqlcd:mode tx
ALTER TABLE [sales].[Order] ADD [Note] nvarchar(400) NULL;
