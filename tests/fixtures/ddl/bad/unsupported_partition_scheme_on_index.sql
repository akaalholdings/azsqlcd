-- expect: UNSUPPORTED
-- says: partition schemes
-- line: 11
-- path: schema/tables/dbo.Telemetry.sql
CREATE TABLE [dbo].[Telemetry] (
    [DeviceId] int NOT NULL,
    [ReadingUtc] datetime2(3) NOT NULL
);
GO
CREATE CLUSTERED INDEX [CIX_Telemetry] ON [dbo].[Telemetry] ([ReadingUtc])
    ON [ps_Monthly] ([ReadingUtc]);
