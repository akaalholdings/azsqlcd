-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
ALTER TABLE [sales].[Order] ADD [Note] nvarchar(200) NULL
DISABLE TRIGGER [sales].[trg_Order_Audit] ON [sales].[Order]
