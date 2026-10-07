-- expect: UNSUPPORTED
-- says: partition schemes
-- line: 9
-- path: schema/tables/dbo.Telemetry.sql
CREATE TABLE [dbo].[Telemetry] (
    [DeviceId] int NOT NULL,
    [ReadingUtc] datetime2(3) NOT NULL
)
ON [ps_Monthly] ([ReadingUtc]);
