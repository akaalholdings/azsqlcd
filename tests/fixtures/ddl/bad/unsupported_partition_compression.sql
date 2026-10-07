-- expect: UNSUPPORTED
-- says: ON PARTITIONS
-- line: 5
CREATE NONCLUSTERED INDEX [IX_Telemetry_Device] ON [dbo].[Telemetry] ([DeviceId])
    WITH (DATA_COMPRESSION = PAGE ON PARTITIONS (1 TO 12));
