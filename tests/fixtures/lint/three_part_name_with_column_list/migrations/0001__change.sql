-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
INSERT INTO [other].[dbo].[Order] ([Status]) VALUES (1);
INSERT INTO [sales].[Order] ([Status]) VALUES (1);
