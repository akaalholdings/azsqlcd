-- path: schema/tables/dbo.Ticket.sql
CREATE TABLE [dbo].[Ticket] (
    [TicketId] bigint IDENTITY(100000, 10) NOT NULL,
    [Seq] int NOT NULL,
    [Title] nvarchar(200) NOT NULL
);
