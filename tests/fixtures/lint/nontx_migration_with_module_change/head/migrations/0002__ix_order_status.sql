-- azsqlcd:migration 0002__ix_order_status
-- azsqlcd:mode nontx expected-minutes: 40
CREATE NONCLUSTERED INDEX [IX_Order_Status] ON [sales].[Order] ([Status])
    WITH (ONLINE = ON (WAIT_AT_LOW_PRIORITY (MAX_DURATION = 5 MINUTES, ABORT_AFTER_WAIT = SELF)), RESUMABLE = ON);
