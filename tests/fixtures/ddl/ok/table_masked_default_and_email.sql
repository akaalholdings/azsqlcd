-- path: schema/tables/dbo.Patient.sql
-- not canonical on purpose: bare names, lower-case keywords, an N literal
create table dbo.Patient (
    PatientId int not null,
    Email varchar(320) masked with (function = 'email()') not null,
    Notes nvarchar(max) MASKED WITH (FUNCTION = N'default()') NULL,
    BirthDate date MASKED WITH (FUNCTION = 'default()') NULL,
    constraint PK_Patient primary key clustered (PatientId)
);
