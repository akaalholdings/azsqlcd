CREATE CLUSTERED COLUMNSTORE INDEX [CCI_FactOrderLine] ON [dw].[FactOrderLine] ORDER ([ProductKey]) WITH (MAXDOP = 2) ON [PS_OrderYear]([OrderDate]);
