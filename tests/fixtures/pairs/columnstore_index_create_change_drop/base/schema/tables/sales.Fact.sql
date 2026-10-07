CREATE TABLE [sales].[Fact] (
    [DateKey] int NOT NULL,
    [Amount] money NOT NULL
);
GO
CREATE CLUSTERED COLUMNSTORE INDEX [CCI_Fact] ON [sales].[Fact];
