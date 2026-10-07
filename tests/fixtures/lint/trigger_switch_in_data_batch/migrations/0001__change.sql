-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
DISABLE TRIGGER [sales].[tr_Order_Audit] ON [sales].[Order];
UPDATE [sales].[Order] SET [Status] = 1 WHERE [Status] IS NULL;
ENABLE TRIGGER [sales].[tr_Order_Audit] ON [sales].[Order];
