-- expect: SYNTAX
-- says: WITH after MASKED
-- line: 7
-- path: schema/tables/dbo.Patient.sql
CREATE TABLE [dbo].[Patient] (
    [PatientId] int NOT NULL,
    [Email] varchar(320) MASKED (FUNCTION = 'email()') NOT NULL
);
