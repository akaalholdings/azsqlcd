-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:allow TEMPORAL_OFF [sales].[Price] reason: the table goes in the next batch; finance keeps sales.Price_History
ALTER TABLE [sales].[Price] SET (SYSTEM_VERSIONING = OFF);
GO
-- azsqlcd:allow DROP_TABLE [sales].[Price] reason: replaced by sales.PriceList in r31
DROP TABLE [sales].[Price];
