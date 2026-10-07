-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
UPDATE [sales].[Audit] SET [DROP] = 1, "create" = N'ALTER' WHERE [commit] = 2;
