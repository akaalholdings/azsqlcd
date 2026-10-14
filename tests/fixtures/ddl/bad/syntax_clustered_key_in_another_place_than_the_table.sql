-- expect: SYNTAX
-- says: PRIMARY KEY [PK_OrderLine] is clustered: it is the table
-- line: 4
CREATE TABLE [sales].[OrderLine] (
    [OrderLineId] bigint NOT NULL,
    [OrderDate] date NOT NULL,
    CONSTRAINT [PK_OrderLine] PRIMARY KEY CLUSTERED ([OrderLineId], [OrderDate]) ON [PS_OrderYear]([OrderDate])
);
