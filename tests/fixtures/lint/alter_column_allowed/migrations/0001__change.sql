-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:allow ALTER_COLUMN_LOSSY [sales].[Order].[Note] reason: widening from 200 to 400
ALTER TABLE [sales].[Order] ALTER COLUMN [Note] nvarchar(400) NULL;
