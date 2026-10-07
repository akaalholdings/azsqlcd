-- expect: SYNTAX
-- says: a computed column cannot be masked
-- line: 8
-- path: schema/tables/dbo.Patient.sql
CREATE TABLE [dbo].[Patient] (
    [PatientId] int NOT NULL,
    [Name] nvarchar(50) NOT NULL,
    [Shown] AS (upper([Name])) MASKED WITH (FUNCTION = 'default()')
);
