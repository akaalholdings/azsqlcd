-- path: schema/tables/dbo.Ledger_Entry.sql
CREATE TABLE [dbo].[Ledger_Entry] (
    [TenantId] int NOT NULL,
    [EntryId] bigint NOT NULL,
    [PostedOn] date NOT NULL,
    [AccountCode] varchar(20) NOT NULL,
    [ExternalRef] varchar(50) NULL,
    [Memo] nvarchar(max) NULL,
    CONSTRAINT [PK_Ledger_Entry] PRIMARY KEY CLUSTERED ([TenantId] ASC, [PostedOn] DESC, [EntryId] ASC)
        WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = ON, FILLFACTOR = 95, DATA_COMPRESSION = PAGE) ON [PRIMARY],
    CONSTRAINT [UQ_Ledger_Entry_ExternalRef] UNIQUE NONCLUSTERED ([TenantId], [ExternalRef] DESC) WITH (FILLFACTOR = 80) ON [PRIMARY]
) ON [PRIMARY] TEXTIMAGE_ON [PRIMARY];
