-- azsqlcd:allow RENAME [sales].[Order] reason: one name for the header table
EXECUTE [sys].[sp_rename] N'sales.Order', N'Order Header', N'object'
