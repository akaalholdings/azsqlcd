-- expect: UNSUPPORTED
-- says: DROP_EXISTING
-- line: 5
CREATE UNIQUE CLUSTERED INDEX [CIX_Telemetry] ON [dbo].[Telemetry] ([ReadingUtc], [DeviceId])
    WITH (DROP_EXISTING = ON, ONLINE = ON);
