-- path: schema/tables/dbo.Contact.sql
CREATE TABLE [dbo].[Contact] (
    [ContactId] int NOT NULL,
    [Phone] [dbo].[PhoneNumber] NULL,
    [Country] [ref].[Country Code] NOT NULL,
    [Mobile] dbo.PhoneNumber NULL
);
