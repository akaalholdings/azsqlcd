-- azsqlcd:migration 0001__change
-- azsqlcd:mode nontx expected-minutes: 40
CREATE NONCLUSTERED INDEX [IX_Order_Status] ON [sales].[Order] ([Status])
    WITH (ONLINE = ON, RESUMABLE = ON);
