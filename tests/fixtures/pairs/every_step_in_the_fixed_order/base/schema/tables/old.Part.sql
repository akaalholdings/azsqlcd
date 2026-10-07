CREATE TABLE [old].[Part] (
    [PartId] int NOT NULL,
    [ThingId] int NULL,
    CONSTRAINT [PK_Part] PRIMARY KEY CLUSTERED ([PartId]),
    CONSTRAINT [FK_Part_Thing] FOREIGN KEY ([ThingId]) REFERENCES [old].[Thing] ([ThingId])
);
