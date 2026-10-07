-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
DBCC CHECKIDENT (N'sales.Order', RESEED, 1000);
