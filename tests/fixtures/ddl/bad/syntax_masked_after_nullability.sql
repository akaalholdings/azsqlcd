-- expect: SYNTAX
-- says: MASKED WITH (FUNCTION = '...') must directly follow the data type
-- line: 7
-- path: schema/tables/dbo.Patient.sql
CREATE TABLE [dbo].[Patient] (
    [PatientId] int NOT NULL,
    [Email] varchar(320) NOT NULL MASKED WITH (FUNCTION = 'email()')
);
