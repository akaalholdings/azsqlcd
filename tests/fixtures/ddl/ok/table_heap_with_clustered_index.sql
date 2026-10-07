-- path: schema/tables/dw.Reading.sql
CREATE TABLE [dw].[Reading] (
    [DeviceId] int NOT NULL,
    [ReadingUtc] datetime2(3) NOT NULL,
    [Reading] float NOT NULL
);
GO
CREATE UNIQUE CLUSTERED INDEX [CIX_Reading] ON [dw].[Reading] ([ReadingUtc] DESC, [DeviceId]) ON [PRIMARY];
