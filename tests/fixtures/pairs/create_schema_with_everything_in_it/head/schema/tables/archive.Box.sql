CREATE TABLE [archive].[Box] (
    [BoxId] int NOT NULL CONSTRAINT [DF_Box_BoxId] DEFAULT (NEXT VALUE FOR [archive].[BoxNo]),
    [Code] [archive].[Code] NOT NULL,
    CONSTRAINT [PK_Box] PRIMARY KEY CLUSTERED ([BoxId])
);
