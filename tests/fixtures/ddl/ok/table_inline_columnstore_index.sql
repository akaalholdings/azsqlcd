-- path: schema/tables/dw.Reading.sql
-- the parser reads an inline columnstore index; NF000 asks for CREATE INDEX in a batch of its own
create table dw.Reading
(
    ReadingId bigint not null,
    SensorId int not null,
    Value decimal(18, 4) null,
    index NCCI_Reading nonclustered columnstore (SensorId, Value) with (compression_delay = 5 minutes)
);
