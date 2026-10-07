-- path: schema/tables/dbo.Customer.sql
CREATE TABLE [dbo].[Customer] (
    [CustomerId] int NOT NULL,
    [FirstName] nvarchar(50) NOT NULL,
    [LastName] nvarchar(50) NOT NULL,
    [Email] varchar(320) NULL,
    CONSTRAINT [PK_Customer] PRIMARY KEY CLUSTERED ([CustomerId])
);
