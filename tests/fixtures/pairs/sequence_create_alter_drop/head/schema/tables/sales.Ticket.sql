CREATE TABLE [sales].[Ticket] (
    [TicketId] int NOT NULL,
    [Number] bigint NOT NULL CONSTRAINT [DF_Ticket_Number] DEFAULT (NEXT VALUE FOR [sales].[TicketNo])
);
