-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
DECLARE @id int, @log TABLE ([OrderId] int NOT NULL);
SELECT [OrderId] INTO #open FROM [sales].[Order] WHERE [Status] IS NULL;
INSERT INTO [sales].[OrderArchive] ([OrderId]) SELECT [OrderId] FROM #open;
UPDATE [sales].[Order] SET [Status] = 1 OUTPUT inserted.[OrderId] INTO @log WHERE [Status] IS NULL;
DECLARE c CURSOR LOCAL FAST_FORWARD FOR SELECT [OrderId] FROM #open;
OPEN c;
FETCH NEXT FROM c INTO @id;
CLOSE c;
DEALLOCATE c;
