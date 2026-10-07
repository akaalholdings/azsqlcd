CREATE TABLE [sales].[Customer] (
    [CustomerId] int NOT NULL,
    [Nme] nvarchar(100) NOT NULL,
    [Email] varchar(200) NULL,
    [Fax] varchar(30) NULL CONSTRAINT [DF_Customer_Fax] DEFAULT (''),
    [Credit] int NOT NULL CONSTRAINT [DF_Customer_Credit] DEFAULT ((0)),
    CONSTRAINT [PK_Customer] PRIMARY KEY CLUSTERED ([CustomerId]),
    CONSTRAINT [UQ_Customer_Email] UNIQUE NONCLUSTERED ([Email]),
    CONSTRAINT [CK_Customer_Credit] CHECK ([Credit] >= 0)
);
GO
CREATE NONCLUSTERED INDEX [IX_Customer_Fax] ON [sales].[Customer] ([Fax]);
