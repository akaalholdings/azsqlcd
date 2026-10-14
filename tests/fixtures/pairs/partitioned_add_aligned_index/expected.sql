CREATE NONCLUSTERED INDEX [IX_OrderLine_Product] ON [sales].[OrderLine] ([ProductId]) ON [PS_OrderYear]([OrderDate]);
GO
CREATE NONCLUSTERED INDEX [IX_Shipment_ShippedOn] ON [sales].[Shipment] ([ShippedOn]) ON [PS_ShipYear]([ShippedOn]);
GO
