-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
ALTER TABLE [sales].[OrderLoad] SWITCH TO [sales].[Order];
