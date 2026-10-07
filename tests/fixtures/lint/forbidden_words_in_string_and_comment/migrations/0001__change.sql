-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
-- COMMIT and ROLLBACK are done by the tool; no GRANT here
UPDATE [sales].[Order]
SET [Note] = N'COMMIT; ROLLBACK; RETURN; DROP TABLE x; USE other' /* GOTO done; RAISERROR */
WHERE [Note] = 'BEGIN TRANSACTION';
