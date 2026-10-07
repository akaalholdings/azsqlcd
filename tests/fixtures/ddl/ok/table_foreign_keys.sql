-- path: schema/tables/sales.OrderHeader.sql
CREATE TABLE [sales].[OrderHeader] (
    [OrderId] int NOT NULL CONSTRAINT [PK_OrderHeader] PRIMARY KEY CLUSTERED,
    [CustomerId] int NOT NULL CONSTRAINT [FK_OrderHeader_Customer] FOREIGN KEY REFERENCES [dbo].[Customer] ([CustomerId]) ON DELETE NO ACTION ON UPDATE CASCADE,
    [BillToId] int NULL CONSTRAINT [FK_OrderHeader_BillTo] REFERENCES [dbo].[Address] ([AddressId]) ON DELETE SET NULL,
    [CurrencyCode] char(3) NOT NULL CONSTRAINT [DF_OrderHeader_Currency] DEFAULT ('GBP') CONSTRAINT [FK_OrderHeader_Currency] REFERENCES [ref].[Currency] ([CurrencyCode]) ON UPDATE CASCADE,
    [TenantId] int NOT NULL,
    [WarehouseId] int NOT NULL,
    [BinId] int NOT NULL,
    CONSTRAINT [FK_OrderHeader_Bin] FOREIGN KEY ([TenantId], [WarehouseId], [BinId]) REFERENCES [inventory].[Bin] ([TenantId], [WarehouseId], [BinId]) ON DELETE CASCADE ON UPDATE NO ACTION,
    CONSTRAINT [FK_OrderHeader_Tenant] FOREIGN KEY ([TenantId]) REFERENCES [admin].[Tenant] ([TenantId]) ON DELETE SET DEFAULT
);
