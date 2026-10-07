-- path: schema/tables/dbo.lower_case.sql
create table dbo.lower_case (
    id int identity not null constraint pk_lower_case primary key clustered,
    name nvarchar(40) not null constraint df_lower_case_name default (n'x'),
    amount numeric(10, 2) null
)
