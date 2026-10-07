-- path: schema/tables/sales.InvoiceLine.sql
CREATE TABLE [sales].[InvoiceLine] (
    [InvoiceLineId] bigint IDENTITY(1000, 10) NOT NULL,
    [InvoiceId] int NOT NULL,
    [TenantId] int NOT NULL,
    [Sku] varchar(20) COLLATE Latin1_General_100_BIN2 NOT NULL,
    [Quantity] int NOT NULL CONSTRAINT [DF_InvoiceLine_Quantity] DEFAULT ((1)),
    [UnitPrice] decimal(19, 4) NOT NULL,
    [Note] nvarchar(max) NULL,
    [CreatedUtc] datetime2(3) NOT NULL CONSTRAINT [DF_InvoiceLine_CreatedUtc] DEFAULT (sysutcdatetime()),
    [LineTotal] AS ([Quantity] * [UnitPrice]) PERSISTED NOT NULL,
    [IsLarge] AS (CASE WHEN [Quantity] > 100 THEN 1 ELSE 0 END),
    CONSTRAINT [PK_InvoiceLine] PRIMARY KEY NONCLUSTERED ([InvoiceLineId]) WITH (DATA_COMPRESSION = PAGE, FILLFACTOR = 90),
    CONSTRAINT [UQ_InvoiceLine_Sku] UNIQUE NONCLUSTERED ([InvoiceId], [Sku] DESC),
    CONSTRAINT [CK_InvoiceLine_Quantity] CHECK ([Quantity] > 0 AND [Quantity] <= 10000),
    CONSTRAINT [FK_InvoiceLine_Invoice] FOREIGN KEY ([InvoiceId]) REFERENCES [sales].[Invoice] ([InvoiceId]) ON DELETE CASCADE,
    CONSTRAINT [FK_InvoiceLine_Tenant] FOREIGN KEY ([TenantId]) REFERENCES [admin].[Tenant] ([TenantId]) ON UPDATE CASCADE
);
GO
CREATE CLUSTERED INDEX [CIX_InvoiceLine] ON [sales].[InvoiceLine] ([InvoiceId], [InvoiceLineId] DESC);
GO
CREATE UNIQUE NONCLUSTERED INDEX [UX_InvoiceLine_Open] ON [sales].[InvoiceLine] ([TenantId], [Sku])
    INCLUDE ([Quantity], [UnitPrice])
    WHERE [Note] IS NULL
    WITH (FILLFACTOR = 80, IGNORE_DUP_KEY = OFF);
