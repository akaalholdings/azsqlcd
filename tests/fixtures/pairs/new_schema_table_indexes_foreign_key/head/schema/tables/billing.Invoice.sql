CREATE TABLE [billing].[Invoice] (
    [InvoiceId] bigint IDENTITY(1, 1) NOT NULL,
    [CustomerId] int NOT NULL,
    [Total] decimal(19, 4) NOT NULL CONSTRAINT [DF_Invoice_Total] DEFAULT ((0)),
    [IssuedUtc] datetime2(3) NOT NULL CONSTRAINT [DF_Invoice_IssuedUtc] DEFAULT (sysutcdatetime()),
    CONSTRAINT [PK_Invoice] PRIMARY KEY NONCLUSTERED ([InvoiceId]),
    CONSTRAINT [CK_Invoice_Total] CHECK ([Total] >= 0),
    CONSTRAINT [FK_Invoice_Customer] FOREIGN KEY ([CustomerId]) REFERENCES [sales].[Customer] ([CustomerId])
);
GO
CREATE CLUSTERED INDEX [CIX_Invoice] ON [billing].[Invoice] ([IssuedUtc], [InvoiceId]);
GO
CREATE NONCLUSTERED INDEX [IX_Invoice_Customer] ON [billing].[Invoice] ([CustomerId])
    INCLUDE ([Total]);
