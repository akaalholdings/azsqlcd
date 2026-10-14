-- path: schema/tables/dbo.Telemetry.sql
CREATE TABLE [dbo].[Telemetry] (
    [DeviceId] int NOT NULL,
    [ReadingUtc] datetime2(3) NOT NULL
)
ON [ps_Monthly] ([ReadingUtc]);
GO
CREATE CLUSTERED INDEX [CIX_Telemetry] ON [dbo].[Telemetry] ([ReadingUtc])
    ON [ps_Monthly] ([ReadingUtc]);
