CREATE TABLE [billing].[Invoice] (
    [InvoiceId] bigint NOT NULL CONSTRAINT [DF_Invoice_InvoiceId] DEFAULT (NEXT VALUE FOR [billing].[InvoiceNo]),
    [CustomerId] int NOT NULL,
    [Amount] [billing].[Money] NOT NULL,
    CONSTRAINT [PK_Invoice] PRIMARY KEY CLUSTERED ([InvoiceId]),
    CONSTRAINT [FK_Invoice_Customer] FOREIGN KEY ([CustomerId]) REFERENCES [sales].[Customer] ([CustomerId])
);
GO
CREATE NONCLUSTERED INDEX [IX_Invoice_Customer] ON [billing].[Invoice] ([CustomerId]);
