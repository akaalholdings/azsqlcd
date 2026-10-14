-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
ALTER TABLE [sales].[OrderLineStage] REBUILD PARTITION = 3 WITH (DATA_COMPRESSION = PAGE);
