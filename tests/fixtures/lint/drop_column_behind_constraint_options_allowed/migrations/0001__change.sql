-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:allow DROP_COLUMN [dbo].[Customer].[Email] reason: moved to dbo.Contact in r52
ALTER TABLE [dbo].[Customer] DROP CONSTRAINT [PK_Customer] WITH (ONLINE = ON), COLUMN [Email];
