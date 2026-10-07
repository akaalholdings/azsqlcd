CREATE TABLE [sales].[Order] (
    [OrderId] int NOT NULL,
    [Total] money NOT NULL,
    CONSTRAINT [PK_Order] PRIMARY KEY CLUSTERED ([OrderId])
);
