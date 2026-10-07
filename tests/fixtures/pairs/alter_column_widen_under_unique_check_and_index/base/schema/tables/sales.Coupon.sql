CREATE TABLE [sales].[Coupon] (
    [CouponId] int NOT NULL,
    [Code] varchar(20) NOT NULL,
    [Tag] nvarchar(5) NULL,
    [Hash] varbinary(16) NOT NULL,
    CONSTRAINT [PK_Coupon] PRIMARY KEY CLUSTERED ([CouponId]),
    CONSTRAINT [UQ_Coupon_Code] UNIQUE NONCLUSTERED ([Code]),
    CONSTRAINT [CK_Coupon_Tag] CHECK (LEN([Tag]) > 0)
);
GO
CREATE NONCLUSTERED INDEX [IX_Coupon_Tag] ON [sales].[Coupon] ([Tag])
    INCLUDE ([Code]);
GO
CREATE UNIQUE NONCLUSTERED INDEX [UX_Coupon_Hash] ON [sales].[Coupon] ([Hash]);
