CREATE TABLE [sales].[Voucher] (
    [VoucherId] int NOT NULL,
    [Code] nvarchar(40) NOT NULL,
    CONSTRAINT [PK_Voucher] PRIMARY KEY CLUSTERED ([VoucherId]),
    CONSTRAINT [UQ_Voucher_Code] UNIQUE NONCLUSTERED ([Code])
);
