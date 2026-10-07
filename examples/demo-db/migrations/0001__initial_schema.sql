-- azsqlcd:migration 0001__initial_schema
-- azsqlcd:mode tx
CREATE SCHEMA [product];
GO
CREATE SCHEMA [sales];
GO
CREATE SCHEMA [stock];
GO
CREATE TYPE [product].[Sku] FROM varchar(20) NOT NULL;
GO
CREATE TYPE [sales].[OrderLineInput] AS TABLE (
    [ProductId] int NOT NULL,
    [Quantity] int NOT NULL,
    [DiscountPercent] decimal(5, 2) NOT NULL DEFAULT (0),
    PRIMARY KEY CLUSTERED ([ProductId]),
    CHECK ([Quantity] > 0)
);
GO
CREATE SEQUENCE [sales].[OrderNumberSeq] AS int START WITH 100000 INCREMENT BY 1 MINVALUE 100000 MAXVALUE 2147483647 NO CYCLE CACHE 50;
GO
CREATE SEQUENCE [stock].[MovementIdSeq] AS bigint START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE CACHE 100;
GO
CREATE TABLE [product].[Category] (
    [CategoryId] int IDENTITY(1, 1) NOT NULL,
    [ParentCategoryId] int NULL,
    [Name] nvarchar(100) NOT NULL,
    [IsActive] bit NOT NULL CONSTRAINT [DF_Category_IsActive] DEFAULT (1),
    CONSTRAINT [PK_Category] PRIMARY KEY CLUSTERED ([CategoryId]),
    CONSTRAINT [UQ_Category_Name] UNIQUE NONCLUSTERED ([Name]),
    CONSTRAINT [CK_Category_NotOwnParent] CHECK ([ParentCategoryId] <> [CategoryId])
);
GO
CREATE TABLE [product].[PriceHistory] (
    [PriceHistoryId] int IDENTITY(1, 1) NOT NULL,
    [ProductId] int NOT NULL,
    [ListPrice] decimal(19, 4) NOT NULL,
    [ValidFromUtc] datetime2(3) NOT NULL CONSTRAINT [DF_PriceHistory_ValidFromUtc] DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT [PK_PriceHistory] PRIMARY KEY CLUSTERED ([PriceHistoryId]),
    CONSTRAINT [CK_PriceHistory_ListPrice] CHECK ([ListPrice] >= 0)
);
GO
-- azsqlcd:deploy-module [product].[fn_IsValidSku]
CREATE TABLE [product].[Product] (
    [ProductId] int IDENTITY(1, 1) NOT NULL,
    [Sku] [product].[Sku] NOT NULL,
    [Name] nvarchar(200) NOT NULL,
    [CategoryId] int NOT NULL,
    [ListPrice] decimal(19, 4) NOT NULL,
    [WeightGrams] int NULL,
    [IsActive] bit NOT NULL CONSTRAINT [DF_Product_IsActive] DEFAULT (1),
    [CreatedUtc] datetime2(3) NOT NULL CONSTRAINT [DF_Product_CreatedUtc] DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT [PK_Product] PRIMARY KEY CLUSTERED ([ProductId]),
    CONSTRAINT [UQ_Product_Sku] UNIQUE NONCLUSTERED ([Sku]),
    CONSTRAINT [CK_Product_ListPrice] CHECK ([ListPrice] >= 0),
    CONSTRAINT [CK_Product_Sku] CHECK ([product].[fn_IsValidSku]([Sku]) = 1),
    CONSTRAINT [CK_Product_WeightGrams] CHECK ([WeightGrams] IS NULL OR [WeightGrams] > 0)
);
GO
CREATE TABLE [sales].[Address] (
    [AddressId] int IDENTITY(1, 1) NOT NULL,
    [CustomerId] int NOT NULL,
    [Kind] char(1) NOT NULL CONSTRAINT [DF_Address_Kind] DEFAULT ('S'),
    [Line1] nvarchar(200) NOT NULL,
    [Line2] nvarchar(200) NULL,
    [City] nvarchar(100) NOT NULL,
    [PostalCode] nvarchar(20) NOT NULL,
    [CountryCode] char(2) NOT NULL,
    CONSTRAINT [PK_Address] PRIMARY KEY CLUSTERED ([AddressId]),
    CONSTRAINT [CK_Address_Kind] CHECK ([Kind] IN ('B', 'S'))
);
GO
CREATE TABLE [sales].[Customer] (
    [CustomerId] int IDENTITY(1, 1) NOT NULL,
    [Email] nvarchar(320) NOT NULL,
    [DisplayName] nvarchar(200) NOT NULL,
    [CountryCode] char(2) NOT NULL,
    [IsActive] bit NOT NULL CONSTRAINT [DF_Customer_IsActive] DEFAULT (1),
    [CreatedUtc] datetime2(3) NOT NULL CONSTRAINT [DF_Customer_CreatedUtc] DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT [PK_Customer] PRIMARY KEY CLUSTERED ([CustomerId]),
    CONSTRAINT [CK_Customer_CountryCode] CHECK ([CountryCode] LIKE '[A-Z][A-Z]'),
    CONSTRAINT [CK_Customer_Email] CHECK ([Email] LIKE '_%@_%')
);
GO
CREATE TABLE [sales].[Order] (
    [OrderId] bigint IDENTITY(1, 1) NOT NULL,
    [OrderNumber] int NOT NULL CONSTRAINT [DF_Order_OrderNumber] DEFAULT (NEXT VALUE FOR [sales].[OrderNumberSeq]),
    [CustomerId] int NOT NULL,
    [ShipToAddressId] int NULL,
    [Status] tinyint NOT NULL CONSTRAINT [DF_Order_Status] DEFAULT (0),
    [CurrencyCode] char(3) NOT NULL,
    [OrderedUtc] datetime2(3) NOT NULL CONSTRAINT [DF_Order_OrderedUtc] DEFAULT (SYSUTCDATETIME()),
    [ShippedUtc] datetime2(3) NULL,
    [Note] nvarchar(1000) NULL,
    CONSTRAINT [PK_Order] PRIMARY KEY CLUSTERED ([OrderId]),
    CONSTRAINT [UQ_Order_OrderNumber] UNIQUE NONCLUSTERED ([OrderNumber]),
    CONSTRAINT [CK_Order_Shipped] CHECK ([ShippedUtc] IS NULL OR [ShippedUtc] >= [OrderedUtc]),
    CONSTRAINT [CK_Order_Status] CHECK ([Status] IN (0, 1, 2, 3, 4))
);
GO
CREATE TABLE [sales].[OrderLine] (
    [OrderId] bigint NOT NULL,
    [LineNumber] int NOT NULL,
    [ProductId] int NOT NULL,
    [Quantity] int NOT NULL,
    [UnitPrice] decimal(19, 4) NOT NULL,
    [DiscountPercent] decimal(5, 2) NOT NULL CONSTRAINT [DF_OrderLine_DiscountPercent] DEFAULT (0),
    [LineTotal] AS (CONVERT(decimal(19, 4), [Quantity] * [UnitPrice] * (100 - [DiscountPercent]) / 100)) PERSISTED NOT NULL,
    CONSTRAINT [PK_OrderLine] PRIMARY KEY CLUSTERED ([OrderId], [LineNumber]),
    CONSTRAINT [CK_OrderLine_DiscountPercent] CHECK ([DiscountPercent] BETWEEN 0 AND 100),
    CONSTRAINT [CK_OrderLine_Quantity] CHECK ([Quantity] > 0),
    CONSTRAINT [CK_OrderLine_UnitPrice] CHECK ([UnitPrice] >= 0)
);
GO
CREATE TABLE [sales].[OrderStatusHistory] (
    [OrderStatusHistoryId] bigint IDENTITY(1, 1) NOT NULL,
    [OrderId] bigint NOT NULL,
    [OldStatus] tinyint NULL,
    [NewStatus] tinyint NOT NULL,
    [ChangedUtc] datetime2(3) NOT NULL CONSTRAINT [DF_OrderStatusHistory_ChangedUtc] DEFAULT (SYSUTCDATETIME()),
    [ChangedBy] nvarchar(128) NOT NULL CONSTRAINT [DF_OrderStatusHistory_ChangedBy] DEFAULT (ORIGINAL_LOGIN()),
    CONSTRAINT [PK_OrderStatusHistory] PRIMARY KEY NONCLUSTERED ([OrderStatusHistoryId])
);
GO
CREATE TABLE [sales].[Payment] (
    [PaymentId] bigint IDENTITY(1, 1) NOT NULL,
    [OrderId] bigint NOT NULL,
    [Amount] decimal(19, 4) NOT NULL,
    [Method] varchar(20) NOT NULL,
    [Reference] nvarchar(100) NULL,
    [PaidUtc] datetime2(3) NOT NULL CONSTRAINT [DF_Payment_PaidUtc] DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT [PK_Payment] PRIMARY KEY CLUSTERED ([PaymentId]),
    CONSTRAINT [CK_Payment_Amount] CHECK ([Amount] <> 0),
    CONSTRAINT [CK_Payment_Method] CHECK ([Method] IN ('card', 'transfer', 'voucher'))
);
GO
CREATE TABLE [stock].[StockLevel] (
    [WarehouseId] smallint NOT NULL,
    [ProductId] int NOT NULL,
    [QuantityOnHand] int NOT NULL CONSTRAINT [DF_StockLevel_QuantityOnHand] DEFAULT (0),
    [QuantityReserved] int NOT NULL CONSTRAINT [DF_StockLevel_QuantityReserved] DEFAULT (0),
    [ReorderPoint] int NOT NULL CONSTRAINT [DF_StockLevel_ReorderPoint] DEFAULT (0),
    [QuantityAvailable] AS ([QuantityOnHand] - [QuantityReserved]),
    [UpdatedUtc] datetime2(3) NOT NULL CONSTRAINT [DF_StockLevel_UpdatedUtc] DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT [PK_StockLevel] PRIMARY KEY CLUSTERED ([WarehouseId], [ProductId]),
    CONSTRAINT [CK_StockLevel_Quantities] CHECK ([QuantityOnHand] >= 0 AND [QuantityReserved] >= 0 AND [QuantityReserved] <= [QuantityOnHand])
);
GO
CREATE TABLE [stock].[StockMovement] (
    [MovementId] bigint NOT NULL CONSTRAINT [DF_StockMovement_MovementId] DEFAULT (NEXT VALUE FOR [stock].[MovementIdSeq]),
    [WarehouseId] smallint NOT NULL,
    [ProductId] int NOT NULL,
    [Quantity] int NOT NULL,
    [Reason] varchar(20) NOT NULL,
    [OrderId] bigint NULL,
    [MovedUtc] datetime2(3) NOT NULL CONSTRAINT [DF_StockMovement_MovedUtc] DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT [PK_StockMovement] PRIMARY KEY CLUSTERED ([MovementId]),
    CONSTRAINT [CK_StockMovement_Quantity] CHECK ([Quantity] <> 0),
    CONSTRAINT [CK_StockMovement_Reason] CHECK ([Reason] IN ('receipt', 'shipment', 'adjustment', 'return'))
);
GO
CREATE TABLE [stock].[Warehouse] (
    [WarehouseId] smallint IDENTITY(1, 1) NOT NULL,
    [Code] char(4) NOT NULL,
    [Name] nvarchar(100) NOT NULL,
    [CountryCode] char(2) NOT NULL,
    [IsActive] bit NOT NULL CONSTRAINT [DF_Warehouse_IsActive] DEFAULT (1),
    CONSTRAINT [PK_Warehouse] PRIMARY KEY CLUSTERED ([WarehouseId]),
    CONSTRAINT [UQ_Warehouse_Code] UNIQUE NONCLUSTERED ([Code])
);
GO
CREATE NONCLUSTERED INDEX [IX_Category_ParentCategoryId] ON [product].[Category] ([ParentCategoryId]);
GO
CREATE NONCLUSTERED INDEX [IX_PriceHistory_Product] ON [product].[PriceHistory] ([ProductId], [ValidFromUtc] DESC) INCLUDE ([ListPrice]);
GO
CREATE NONCLUSTERED INDEX [IX_Product_CategoryId] ON [product].[Product] ([CategoryId]) INCLUDE ([Name], [ListPrice]) WHERE [IsActive] = 1;
GO
CREATE NONCLUSTERED INDEX [IX_Address_CustomerId] ON [sales].[Address] ([CustomerId]);
GO
CREATE UNIQUE NONCLUSTERED INDEX [UX_Customer_Email] ON [sales].[Customer] ([Email]);
GO
CREATE NONCLUSTERED INDEX [IX_Order_CustomerId] ON [sales].[Order] ([CustomerId], [OrderedUtc] DESC);
GO
CREATE NONCLUSTERED INDEX [IX_Order_Open] ON [sales].[Order] ([OrderedUtc]) INCLUDE ([CustomerId]) WHERE [Status] IN (0, 1);
GO
CREATE NONCLUSTERED INDEX [IX_OrderLine_ProductId] ON [sales].[OrderLine] ([ProductId]) INCLUDE ([Quantity], [LineTotal]);
GO
CREATE CLUSTERED INDEX [CIX_OrderStatusHistory] ON [sales].[OrderStatusHistory] ([OrderId], [ChangedUtc]);
GO
CREATE NONCLUSTERED INDEX [IX_Payment_OrderId] ON [sales].[Payment] ([OrderId]) INCLUDE ([Amount]);
GO
CREATE NONCLUSTERED INDEX [IX_StockLevel_ProductId] ON [stock].[StockLevel] ([ProductId]) INCLUDE ([QuantityOnHand], [QuantityReserved]);
GO
CREATE NONCLUSTERED INDEX [IX_StockMovement_Product] ON [stock].[StockMovement] ([ProductId], [MovedUtc] DESC) WITH (DATA_COMPRESSION = PAGE);
GO
ALTER TABLE [product].[Category] ADD CONSTRAINT [FK_Category_Parent] FOREIGN KEY ([ParentCategoryId]) REFERENCES [product].[Category] ([CategoryId]);
GO
ALTER TABLE [product].[PriceHistory] ADD CONSTRAINT [FK_PriceHistory_Product] FOREIGN KEY ([ProductId]) REFERENCES [product].[Product] ([ProductId]) ON DELETE CASCADE;
GO
ALTER TABLE [product].[Product] ADD CONSTRAINT [FK_Product_Category] FOREIGN KEY ([CategoryId]) REFERENCES [product].[Category] ([CategoryId]);
GO
ALTER TABLE [sales].[Address] ADD CONSTRAINT [FK_Address_Customer] FOREIGN KEY ([CustomerId]) REFERENCES [sales].[Customer] ([CustomerId]) ON DELETE CASCADE;
GO
ALTER TABLE [sales].[Order] ADD CONSTRAINT [FK_Order_Address] FOREIGN KEY ([ShipToAddressId]) REFERENCES [sales].[Address] ([AddressId]);
GO
ALTER TABLE [sales].[Order] ADD CONSTRAINT [FK_Order_Customer] FOREIGN KEY ([CustomerId]) REFERENCES [sales].[Customer] ([CustomerId]);
GO
ALTER TABLE [sales].[OrderLine] ADD CONSTRAINT [FK_OrderLine_Order] FOREIGN KEY ([OrderId]) REFERENCES [sales].[Order] ([OrderId]) ON DELETE CASCADE;
GO
ALTER TABLE [sales].[OrderLine] ADD CONSTRAINT [FK_OrderLine_Product] FOREIGN KEY ([ProductId]) REFERENCES [product].[Product] ([ProductId]);
GO
ALTER TABLE [sales].[OrderStatusHistory] ADD CONSTRAINT [FK_OrderStatusHistory_Order] FOREIGN KEY ([OrderId]) REFERENCES [sales].[Order] ([OrderId]) ON DELETE CASCADE;
GO
ALTER TABLE [sales].[Payment] ADD CONSTRAINT [FK_Payment_Order] FOREIGN KEY ([OrderId]) REFERENCES [sales].[Order] ([OrderId]);
GO
ALTER TABLE [stock].[StockLevel] ADD CONSTRAINT [FK_StockLevel_Product] FOREIGN KEY ([ProductId]) REFERENCES [product].[Product] ([ProductId]);
GO
ALTER TABLE [stock].[StockLevel] ADD CONSTRAINT [FK_StockLevel_Warehouse] FOREIGN KEY ([WarehouseId]) REFERENCES [stock].[Warehouse] ([WarehouseId]);
GO
ALTER TABLE [stock].[StockMovement] ADD CONSTRAINT [FK_StockMovement_Order] FOREIGN KEY ([OrderId]) REFERENCES [sales].[Order] ([OrderId]);
GO
ALTER TABLE [stock].[StockMovement] ADD CONSTRAINT [FK_StockMovement_StockLevel] FOREIGN KEY ([WarehouseId], [ProductId]) REFERENCES [stock].[StockLevel] ([WarehouseId], [ProductId]);
GO
CREATE SYNONYM [dbo].[LegacyOrder] FOR [sales].[Order];
GO
