CREATE TABLE [sales].[Account] (
    [AccountId] int NOT NULL,
    [Balance] decimal(19, 4) NOT NULL CONSTRAINT [DF_Account_Balance] DEFAULT ((100)),
    [Kind] char(1) NOT NULL CONSTRAINT [DF_Account_Kind] DEFAULT ('A'),
    [OpenedUtc] datetime2(3) NOT NULL,
    [ClosedUtc] datetime2(3) NULL,
    CONSTRAINT [PK_Account] PRIMARY KEY CLUSTERED ([AccountId]),
    CONSTRAINT [CK_Account_Dates] CHECK ([ClosedUtc] IS NULL OR [ClosedUtc] >= [OpenedUtc]),
    CONSTRAINT [CK_Account_Kind] CHECK ([Kind] IN ('A', 'B', 'C'))
);
