CREATE TABLE [old].[Thing] (
    [ThingId] int NOT NULL,
    [PartId] int NULL,
    [On] [old].[Flag] NOT NULL,
    CONSTRAINT [PK_Thing] PRIMARY KEY CLUSTERED ([ThingId]),
    CONSTRAINT [FK_Thing_Part] FOREIGN KEY ([PartId]) REFERENCES [old].[Part] ([PartId])
);
