-- expect: NF004
-- says: the engine stores this masking function as 'default()'
-- line: 7
-- path: schema/tables/dbo.Patient.sql
CREATE TABLE [dbo].[Patient] (
    [PatientId] int NOT NULL,
    [Email] varchar(320) MASKED WITH (FUNCTION = 'DEFAULT( )') NOT NULL
);
