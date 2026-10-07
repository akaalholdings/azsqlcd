-- azsqlcd:migration 0003__add_flag_v2
-- azsqlcd:mode tx
ALTER TABLE [sales].[Order] ADD [Flag] bit NULL CONSTRAINT [DF_Order_Flag] DEFAULT (0);
