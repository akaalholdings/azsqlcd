-- expect: SYNTAX
-- says: MASKED WITH (FUNCTION = '...') must directly follow the data type
-- line: 6
-- path: schema/tables/dbo.Patient.sql
CREATE TABLE [dbo].[Patient] (
    [PatientId] int IDENTITY(1, 1) MASKED WITH (FUNCTION = 'default()') NOT NULL
);
