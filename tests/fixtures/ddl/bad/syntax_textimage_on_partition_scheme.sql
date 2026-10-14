-- expect: SYNTAX
-- says: TEXTIMAGE_ON takes a filegroup
-- line: 8
CREATE TABLE [sales].[OrderLine] (
    [OrderDate] date NOT NULL,
    [Note] nvarchar(max) NULL
)
TEXTIMAGE_ON [PS_OrderYear]([OrderDate]);
