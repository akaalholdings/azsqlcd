-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
CREATE TABLE #work ([OrderId] int NOT NULL);
INSERT INTO #work ([OrderId]) SELECT [OrderId] FROM [sales].[Order] WHERE [Status] IS NULL;
ALTER TABLE #work ADD [Done] bit NULL;
DROP TABLE #work;
