-- azsqlcd:migration 0002__ix_order_customer_open
-- azsqlcd:mode nontx expected-minutes: 40
CREATE NONCLUSTERED INDEX [IX_OrderHeader_Customer_Open]
ON [sales].[OrderHeader] ([CustomerId] ASC, [OrderDate] DESC)
INCLUDE ([TotalDue], [CurrencyCode])
WHERE [Status] = 'O' AND [ShippedUtc] IS NULL
WITH (ONLINE = ON (WAIT_AT_LOW_PRIORITY (MAX_DURATION = 5 MINUTES, ABORT_AFTER_WAIT = SELF)), RESUMABLE = ON, MAX_DURATION = 60 MINUTES, DATA_COMPRESSION = PAGE, FILLFACTOR = 90, MAXDOP = 4, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF)
ON [PRIMARY];
