CREATE TABLE [sales].[Customer] (
    [CustomerId] int NOT NULL,
    [Name] nvarchar(100) NOT NULL,
    CONSTRAINT [PK_Customer] PRIMARY KEY CLUSTERED ([CustomerId]) WITH (DATA_COMPRESSION = PAGE)
);
