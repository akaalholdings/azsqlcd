CREATE TABLE [dbo].[Staff] (
    [StaffId] int NOT NULL,
    [Name] nvarchar(100) NOT NULL,
    [ValidFrom] datetime2(7) GENERATED ALWAYS AS ROW START NOT NULL,
    [ValidTo] datetime2(7) GENERATED ALWAYS AS ROW END NOT NULL,
    CONSTRAINT [PK_Staff] PRIMARY KEY CLUSTERED ([StaffId]),
    PERIOD FOR SYSTEM_TIME ([ValidFrom], [ValidTo])
)
WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [dbo].[Staff_History]));
