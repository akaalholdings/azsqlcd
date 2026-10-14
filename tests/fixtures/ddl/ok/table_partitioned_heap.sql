-- path: schema/tables/sales.OrderLineStage.sql
-- not in the canonical form on purpose: lower case, bare names, a space before the column
create table sales.OrderLineStage (
    OrderLineId bigint not null,
    OrderDate date not null,
    Quantity int not null
) on PS_OrderYear (OrderDate) with (data_compression = page);
