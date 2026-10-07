-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:allow DROP_SEQUENCE [staging].[LoadNo] reason: the staging load is retired
DROP SEQUENCE [staging].[LoadNo];
GO
-- azsqlcd:allow DROP_TYPE [staging].[Row_tt] reason: the staging load is retired
DROP TYPE [staging].[Row_tt];
GO
-- azsqlcd:allow DROP_SCHEMA [staging] reason: the staging load is retired
DROP SCHEMA [staging];
