-- path: schema/tables/dbo.Money.sql
CREATE TABLE [dbo].[Money] (
    [AsDecimal] decimal(18, 4) NOT NULL,
    [AsNumeric] numeric(18, 4) NOT NULL,
    [Wide] numeric(38, 0) NULL
);
