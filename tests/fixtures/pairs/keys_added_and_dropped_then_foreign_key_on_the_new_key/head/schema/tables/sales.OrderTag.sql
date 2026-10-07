CREATE TABLE [sales].[OrderTag] (
    [OrderId] int NOT NULL,
    [Code] varchar(20) NOT NULL,
    CONSTRAINT [FK_OrderTag_Tag] FOREIGN KEY ([Code]) REFERENCES [sales].[Tag] ([Code])
);
