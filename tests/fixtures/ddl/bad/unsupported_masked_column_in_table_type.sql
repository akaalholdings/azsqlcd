-- expect: UNSUPPORTED
-- says: dynamic data masking (MASKED) in a table type
-- line: 7
-- path: schema/types/dbo.PatientList.sql
CREATE TYPE [dbo].[PatientList] AS TABLE (
    [PatientId] int NOT NULL,
    [Email] varchar(320) MASKED WITH (FUNCTION = 'email()') NOT NULL
);
