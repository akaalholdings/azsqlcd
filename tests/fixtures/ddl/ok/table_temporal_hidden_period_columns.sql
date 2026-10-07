-- path: schema/tables/dbo.Rate.sql
CREATE TABLE [dbo].[Rate] (
    [RateId] int NOT NULL,
    [Percent] decimal(5, 2) NOT NULL,
    [SysStart] datetime2(3) GENERATED ALWAYS AS ROW START HIDDEN NOT NULL,
    [SysEnd] datetime2(3) GENERATED ALWAYS AS ROW END HIDDEN NOT NULL,
    CONSTRAINT [PK_Rate] PRIMARY KEY CLUSTERED ([RateId]),
    PERIOD FOR SYSTEM_TIME ([SysStart], [SysEnd])
)
WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [dbo].[Rate_History]));
