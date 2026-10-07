-- expect: UNSUPPORTED
-- says: three-part names
-- line: 9
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL,
    [CustomerId] int NOT NULL,
    CONSTRAINT [FK_T_Customer] FOREIGN KEY ([CustomerId])
        REFERENCES [crm].[dbo].[Customer] ([CustomerId])
);
