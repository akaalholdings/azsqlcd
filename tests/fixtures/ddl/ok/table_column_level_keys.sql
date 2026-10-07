-- path: schema/tables/ref.Currency.sql
CREATE TABLE [ref].[Currency] (
    [CurrencyId] int NOT NULL CONSTRAINT [PK_Currency] PRIMARY KEY NONCLUSTERED,
    [CurrencyCode] char(3) NOT NULL CONSTRAINT [UQ_Currency_Code] UNIQUE CLUSTERED,
    [Name] nvarchar(60) NOT NULL CONSTRAINT [UQ_Currency_Name] UNIQUE NONCLUSTERED
);
