-- expect: SYNTAX
-- says: the masking function cannot be empty
-- line: 7
-- path: schema/tables/dbo.Patient.sql
CREATE TABLE [dbo].[Patient] (
    [PatientId] int NOT NULL,
    [Email] varchar(320) MASKED WITH (FUNCTION = '') NOT NULL
);
