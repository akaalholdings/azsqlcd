-- path: schema/tables/my.schema.Order Details]v2.sql
CREATE TABLE [my.schema].[Order Details]]v2] (
    [Order ID] int NOT NULL,
    "Line Number" smallint NOT NULL,
    [Weird]]Name] nvarchar(10) NULL,
    [dotted.name] int NULL,
    "double""quoted" int NULL,
    [select] int NULL,
    [Ünïcödé] nvarchar(5) NULL,
    CONSTRAINT [PK Order Details] PRIMARY KEY CLUSTERED ([Order ID], "Line Number")
);
GO
CREATE NONCLUSTERED INDEX [IX Order Details]]v2 select] ON [my.schema].[Order Details]]v2] ([select] DESC) INCLUDE ([Weird]]Name], "double""quoted");
