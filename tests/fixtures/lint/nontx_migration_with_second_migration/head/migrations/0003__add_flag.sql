-- azsqlcd:migration 0003__add_flag
-- azsqlcd:mode tx
ALTER TABLE [sales].[Order] ADD [Flag] bit NULL;
