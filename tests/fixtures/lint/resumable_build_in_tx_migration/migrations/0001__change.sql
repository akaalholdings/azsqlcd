-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:allow LONG_LOCK [sales].[Order] reason: the table is small in every environment
CREATE NONCLUSTERED INDEX [IX_Order_Status] ON [sales].[Order] ([Status])
    WITH (ONLINE = ON, RESUMABLE = ON);
