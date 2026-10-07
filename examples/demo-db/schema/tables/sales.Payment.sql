CREATE TABLE [sales].[Payment] (
    [PaymentId] bigint IDENTITY(1, 1) NOT NULL,
    [OrderId] bigint NOT NULL,
    [Amount] decimal(19, 4) NOT NULL,
    [Method] varchar(20) NOT NULL,
    [Reference] nvarchar(100) NULL,
    [PaidUtc] datetime2(3) NOT NULL CONSTRAINT [DF_Payment_PaidUtc] DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT [PK_Payment] PRIMARY KEY CLUSTERED ([PaymentId]),
    CONSTRAINT [CK_Payment_Amount] CHECK ([Amount] <> 0),
    CONSTRAINT [CK_Payment_Method] CHECK ([Method] IN ('card', 'transfer', 'voucher')),
    CONSTRAINT [FK_Payment_Order] FOREIGN KEY ([OrderId]) REFERENCES [sales].[Order] ([OrderId])
);
GO
CREATE NONCLUSTERED INDEX [IX_Payment_OrderId] ON [sales].[Payment] ([OrderId])
    INCLUDE ([Amount]);
