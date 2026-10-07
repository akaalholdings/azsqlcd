-- path: schema/tables/dbo.Patient.sql
-- the parser reads these; NF000 asks for the canonical place and spelling of each
create table dbo.Patient
(
    PatientId int not null identity not for replication constraint PK_Patient primary key nonclustered,
    RowId uniqueidentifier not null rowguidcol constraint DF_Patient_RowId default newid(),
    Email varchar(320) null,
    Phone varchar(20) sparse null,
    Notes nvarchar(max) null sparse,
    Parent int null constraint FK_Patient_Parent references dbo.Patient (PatientId) not for replication,
    constraint CK_Patient_Email check not for replication (Email like '%@%')
)
with (data_compression = none);
