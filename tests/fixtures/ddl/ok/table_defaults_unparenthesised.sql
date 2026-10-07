-- path: schema/tables/dbo.Loose.sql
CREATE TABLE [dbo].[Loose] (
    [a] int CONSTRAINT [DF_Loose_a] DEFAULT 0 NOT NULL,
    [b] datetime CONSTRAINT [DF_Loose_b] DEFAULT GETDATE() NOT NULL,
    [c] int CONSTRAINT [DF_Loose_c] DEFAULT NULL NULL,
    [d] int CONSTRAINT [DF_Loose_d] DEFAULT -1 NOT NULL,
    [e] bigint CONSTRAINT [DF_Loose_e] DEFAULT NEXT VALUE FOR [dbo].[LooseSeq] NOT NULL,
    [f] int CONSTRAINT [DF_Loose_f] DEFAULT (1) + (2) NOT NULL,
    [g] nvarchar(5) CONSTRAINT [DF_Loose_g] DEFAULT N'a' + N'b' NULL,
    [h] varchar(3) CONSTRAINT [DF_Loose_h] DEFAULT 'x' COLLATE Latin1_General_BIN2 NOT NULL,
    [i] datetime2(3) CONSTRAINT [DF_Loose_i] DEFAULT CURRENT_TIMESTAMP NOT NULL,
    [j] date CONSTRAINT [DF_Loose_j] DEFAULT CONVERT(date, '2026-01-01') NULL
);
