-- path: schema/tables/dbo.Commented.sql
-- header comment; with semicolon
/* block comment
   /* nested block comment with
GO
   */
   still in the outer comment */
CREATE TABLE [dbo].[Commented] ( -- trailing comment
    [Id] int NOT NULL, /* inline */
    [Name] /* between tokens */ varchar(10) NULL -- last column, no comma
    /* CONSTRAINT fake CHECK (1=0), */
);
