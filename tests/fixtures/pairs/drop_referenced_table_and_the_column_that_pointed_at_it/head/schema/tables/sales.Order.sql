CREATE TABLE [sales].[Order] (
    [OrderId] int NOT NULL,
    [Note] nvarchar(200) NULL,
    [Stat] tinyint NOT NULL,
    CONSTRAINT [PK_Order] PRIMARY KEY CLUSTERED ([OrderId])
);
