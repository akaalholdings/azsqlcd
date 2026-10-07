-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:allow DROP_TABLE [sales].[Todo] reason: todos moved to the planner service in r40
DROP TABLE [sales].[Todo];
