CREATE TABLE [sales].[Order] (
    [OrderId] int NOT NULL,
    [Status] varchar(10) NOT NULL CONSTRAINT [DF_Order_Status] DEFAULT ('new')
);
