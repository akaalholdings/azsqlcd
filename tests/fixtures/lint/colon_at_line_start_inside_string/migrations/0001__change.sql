-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
UPDATE [sales].[Template] SET [Body] = N'Dear customer
:r is not read here
' WHERE [TemplateId] = 3;
