CREATE TYPE [sales].[OrderLineInput] AS TABLE (
    [ProductId] int NOT NULL,
    [Quantity] int NOT NULL,
    [DiscountPercent] decimal(5, 2) NOT NULL DEFAULT (0),
    PRIMARY KEY CLUSTERED ([ProductId]),
    CHECK ([Quantity] > 0)
);
