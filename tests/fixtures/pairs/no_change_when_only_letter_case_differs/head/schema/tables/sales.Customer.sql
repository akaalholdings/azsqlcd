CREATE TABLE [sales].[Customer] (
    [customerid] int NOT NULL,
    [Name] nvarchar(100) NOT NULL,
    [Email] varchar(200) NULL,
    CONSTRAINT [PK_Customer] PRIMARY KEY CLUSTERED ([customerid])
);
