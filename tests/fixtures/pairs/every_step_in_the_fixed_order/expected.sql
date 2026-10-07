EXEC sys.sp_rename N'[sales].[Customer].[Nme]', N'Name', N'COLUMN';
GO
ALTER TABLE [old].[Part] DROP CONSTRAINT [FK_Part_Thing];
GO
ALTER TABLE [old].[Thing] DROP CONSTRAINT [FK_Thing_Part];
GO
ALTER TABLE [sales].[Order] DROP CONSTRAINT [FK_Order_Customer];
GO
ALTER TABLE [sales].[Customer] DROP CONSTRAINT [CK_Customer_Credit];
GO
ALTER TABLE [sales].[Customer] DROP CONSTRAINT [DF_Customer_Credit];
GO
ALTER TABLE [sales].[Customer] DROP CONSTRAINT [DF_Customer_Fax];
GO
DROP INDEX [IX_Customer_Fax] ON [sales].[Customer];
GO
ALTER TABLE [sales].[Customer] DROP CONSTRAINT [PK_Customer];
GO
ALTER TABLE [sales].[Customer] DROP CONSTRAINT [UQ_Customer_Email];
GO
ALTER TABLE [sales].[Customer] DROP COLUMN [Fax];
GO
DROP TABLE [old].[Part];
GO
DROP TABLE [old].[Thing];
GO
CREATE SCHEMA [billing];
GO
CREATE TYPE [billing].[Money] FROM decimal(19, 4) NOT NULL;
GO
CREATE TYPE [billing].[MoneyList] AS TABLE (
    [Amount] [billing].[Money] NOT NULL
);
GO
CREATE SEQUENCE [billing].[InvoiceNo] AS bigint START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 NO CYCLE CACHE;
GO
ALTER SEQUENCE [sales].[OrderNo] MAXVALUE 99999999;
GO
CREATE TABLE [billing].[Invoice] (
    [InvoiceId] bigint NOT NULL CONSTRAINT [DF_Invoice_InvoiceId] DEFAULT (NEXT VALUE FOR [billing].[InvoiceNo]),
    [CustomerId] int NOT NULL,
    [Amount] [billing].[Money] NOT NULL,
    CONSTRAINT [PK_Invoice] PRIMARY KEY CLUSTERED ([InvoiceId])
);
GO
ALTER TABLE [sales].[Customer] ADD [Tier] tinyint NOT NULL CONSTRAINT [DF_Customer_Tier] DEFAULT ((1));
GO
ALTER TABLE [sales].[Customer] ALTER COLUMN [Email] varchar(320) NULL;
GO
ALTER TABLE [sales].[Customer] ADD CONSTRAINT [PK_Customer] PRIMARY KEY NONCLUSTERED ([CustomerId]);
GO
ALTER TABLE [sales].[Customer] ADD CONSTRAINT [UQ_Customer_Mail] UNIQUE NONCLUSTERED ([Email]);
GO
CREATE NONCLUSTERED INDEX [IX_Invoice_Customer] ON [billing].[Invoice] ([CustomerId]);
GO
CREATE CLUSTERED INDEX [CIX_Customer_Name] ON [sales].[Customer] ([Name]);
GO
ALTER TABLE [sales].[Customer] ADD CONSTRAINT [CK_Customer_Tier] CHECK ([Tier] BETWEEN 1 AND 5);
GO
ALTER TABLE [sales].[Order] ADD CONSTRAINT [DF_Order_Note] DEFAULT (N'') FOR [Note];
GO
ALTER TABLE [billing].[Invoice] ADD CONSTRAINT [FK_Invoice_Customer] FOREIGN KEY ([CustomerId]) REFERENCES [sales].[Customer] ([CustomerId]);
GO
ALTER TABLE [sales].[Order] ADD CONSTRAINT [FK_Order_Customer] FOREIGN KEY ([CustomerId]) REFERENCES [sales].[Customer] ([CustomerId]);
GO
DROP SYNONYM [old].[Things];
GO
DROP SYNONYM [sales].[Clients];
GO
CREATE SYNONYM [sales].[Clients] FOR [billing].[Invoice];
GO
DROP SEQUENCE [old].[Seq];
GO
DROP TYPE [old].[FlagList];
GO
DROP TYPE [old].[Flag];
GO
DROP SCHEMA [old];
GO
