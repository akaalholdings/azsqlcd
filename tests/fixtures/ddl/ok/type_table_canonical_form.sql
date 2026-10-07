-- path: schema/types/sales.InvoiceLineList.sql
CREATE TYPE [sales].[InvoiceLineList] AS TABLE (
    [LineNumber] smallint NOT NULL,
    [Sku] varchar(20) NOT NULL,
    [Quantity] int NOT NULL DEFAULT ((1)),
    [UnitPrice] money NOT NULL,
    [Total] AS ([Quantity] * [UnitPrice]),
    PRIMARY KEY CLUSTERED ([LineNumber]),
    UNIQUE NONCLUSTERED ([Sku] DESC) WITH (IGNORE_DUP_KEY = ON),
    CHECK ([Quantity] > 0),
    INDEX [IX_Sku] NONCLUSTERED ([Sku], [LineNumber])
);
