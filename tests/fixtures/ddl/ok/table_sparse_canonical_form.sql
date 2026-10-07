-- path: schema/tables/sales.Attribute.sql
CREATE TABLE [sales].[Attribute] (
    [AttributeId] int IDENTITY(1, 1) NOT NULL,
    [Colour] varchar(20) COLLATE Latin1_General_100_CI_AS SPARSE NULL,
    [Card] char(16) SPARSE MASKED WITH (FUNCTION = 'default()') NULL,
    [Weight] decimal(9, 3) SPARSE NULL,
    CONSTRAINT [PK_Attribute] PRIMARY KEY CLUSTERED ([AttributeId])
);
