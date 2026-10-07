-- azsqlcd:migration 0002__add_flag
-- azsqlcd:mode tx
ALTER TABLE [sales].[Order] ADD [Flag] bit NULL;
