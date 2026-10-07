-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
CREATE USER [loader] WITHOUT LOGIN;
ALTER ROLE [db_datawriter] ADD MEMBER [loader];
ALTER AUTHORIZATION ON SCHEMA::[staging] TO [loader];
DENY DELETE ON [sales].[Order] TO [loader];
REVOKE SELECT ON [sales].[Order] FROM [loader];
