-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:allow LONG_LOCK [sales].[OrderLine] reason: 2000 rows, built in under a second
CREATE NONCLUSTERED INDEX [IX_OrderLine_Quantity] ON [sales].[OrderLine] ([Quantity])
    ON [PS_OrderYear]([OrderDate])
[sales].[usp_Purge]
