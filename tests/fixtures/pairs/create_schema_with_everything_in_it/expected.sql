CREATE SCHEMA [archive];
GO
CREATE TYPE [archive].[Code] FROM char(4) NOT NULL;
GO
CREATE TYPE [archive].[CodeList] AS TABLE (
    [Code] [archive].[Code] NOT NULL
);
GO
CREATE SEQUENCE [archive].[BoxNo] AS int START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 2147483647 NO CYCLE CACHE 10;
GO
CREATE TABLE [archive].[Box] (
    [BoxId] int NOT NULL CONSTRAINT [DF_Box_BoxId] DEFAULT (NEXT VALUE FOR [archive].[BoxNo]),
    [Code] [archive].[Code] NOT NULL,
    CONSTRAINT [PK_Box] PRIMARY KEY CLUSTERED ([BoxId])
);
GO
CREATE SYNONYM [archive].[Boxes] FOR [archive].[Box];
GO
