-- path: schema/tables/dbo.Price.sql
-- Not the canonical form: lower case, PERIOD before the key, retention with INFINITE and a
-- named DEFAULT on each period column, as a catalog can hold them.
create table dbo.Price (
    PriceId int not null,
    Amount money not null,
    ValidFrom datetime2(0) generated always as row start not null
        constraint DF_Price_ValidFrom default (sysutcdatetime()),
    ValidTo datetime2(0) generated always as row end hidden not null
        constraint DF_Price_ValidTo default (CONVERT(datetime2(0), '9999-12-31 23:59:59')),
    period for system_time (ValidFrom, ValidTo),
    constraint PK_Price primary key clustered (PriceId)
) on [PRIMARY]
with (system_versioning = on (history_table = dbo.PriceHistory, history_retention_period = infinite));
