-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:raw TABLE:[ext].[Feed] reason: external table, outside the model
-- azsqlcd:allow RAW TABLE:[ext].[Feed] reason: reviewed by the DBA team
CREATE DATABASE SCOPED CREDENTIAL [feed] WITH IDENTITY = 'SHARED ACCESS SIGNATURE',
    SECRET = 'sv=2026-01-01&sig=abc';
