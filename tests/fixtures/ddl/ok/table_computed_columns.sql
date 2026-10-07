-- path: schema/tables/sales.Invoice.sql
CREATE TABLE [sales].[Invoice] (
    [InvoiceId] int IDENTITY(1000, 1) NOT NULL,
    [Net] decimal(19,4) NOT NULL,
    [TaxRate] decimal(5,4) NOT NULL,
    [Tax] AS ([Net] * [TaxRate]),
    [Gross] AS ([Net] + ([Net] * [TaxRate])) PERSISTED,
    [GrossRounded] AS (CONVERT(decimal(19,2), ROUND([Net] + ([Net] * [TaxRate]), 2))) PERSISTED NOT NULL,
    [CreatedUtc] datetime2(0) NOT NULL,
    [InvoiceYear] AS (DATEPART(year, [CreatedUtc])),
    [Ref] AS (CONCAT('INV-', RIGHT('000000' + CAST([InvoiceId] AS varchar(10)), 6))) PERSISTED,
    [Kind] AS (CASE WHEN [Net] = 0 THEN '),(' ELSE N'x' END) PERSISTED NOT NULL,
    [Doubled] AS [Net] * 2,
    CONSTRAINT [PK_Invoice] PRIMARY KEY CLUSTERED ([InvoiceId])
);
