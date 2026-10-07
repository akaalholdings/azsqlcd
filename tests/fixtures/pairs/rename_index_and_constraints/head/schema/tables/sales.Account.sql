CREATE TABLE [sales].[Account] (
    [AccountId] int NOT NULL,
    [Balance] decimal(19, 4) NOT NULL CONSTRAINT [DF_Account_Balance] DEFAULT ((0)),
    [Kind] char(1) NOT NULL,
    [OpenedUtc] datetime2(3) NOT NULL,
    [ClosedUtc] datetime2(3) NULL CONSTRAINT [DF_Account_ClosedUtc] DEFAULT (NULL),
    CONSTRAINT [PK_Account] PRIMARY KEY CLUSTERED ([AccountId]),
    CONSTRAINT [CK_Account_Kind] CHECK ([Kind] IN ('A', 'B')),
    CONSTRAINT [CK_Account_Old] CHECK ([Balance] > -1000)
);
GO
CREATE NONCLUSTERED INDEX [IX_Account_Opened] ON [sales].[Account] ([OpenedUtc]);
