-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
DELETE FROM tempdb..Staging WHERE [Day] < 10;
