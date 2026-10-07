-- expect: NF004
-- says: the engine stores this masking function as 'random(1.00, 12.50)'
-- line: 7
-- path: schema/tables/dbo.Patient.sql
CREATE TABLE [dbo].[Patient] (
    [PatientId] int NOT NULL,
    [Weight] decimal(9, 2) MASKED WITH (FUNCTION = 'random(1, 12.5)') NOT NULL
);
