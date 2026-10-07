-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
SET NOCOUNT ON
DELETE FROM [sales].[Customer]
