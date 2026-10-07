CREATE TABLE [sales].[Account] (
    [AccountId] int NOT NULL,
    [Balance] decimal(19, 4) NOT NULL CONSTRAINT [DF__Account__Balance] DEFAULT ((0)),
    [Kind] char(1) NOT NULL,
    [OpenedUtc] datetime2(3) NOT NULL,
    [ClosedUtc] datetime2(3) NULL CONSTRAINT [DF_Account_ClosedUtc] DEFAULT (NULL),
    CONSTRAINT [PK__Account__3214EC07] PRIMARY KEY CLUSTERED ([AccountId]),
    CONSTRAINT [CK__Account__1A2B] CHECK ([Balance] > -1000),
    CONSTRAINT [CK_Account_Kind] CHECK ([Kind] IN ('A', 'B'))
);
GO
CREATE NONCLUSTERED INDEX [idx1] ON [sales].[Account] ([OpenedUtc]);
