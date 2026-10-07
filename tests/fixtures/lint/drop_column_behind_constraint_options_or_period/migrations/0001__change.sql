-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
ALTER TABLE [dbo].[Customer] DROP CONSTRAINT [PK_Customer] WITH (ONLINE = ON), COLUMN [Email];
GO
ALTER TABLE [dbo].[Customer] DROP PERIOD FOR SYSTEM_TIME, COLUMN [ValidFrom], COLUMN [ValidTo];
