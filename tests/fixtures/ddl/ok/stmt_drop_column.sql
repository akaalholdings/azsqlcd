-- azsqlcd:allow DROP_COLUMN [dbo].[Customer].[LegacyCode] reason: unused since r12
ALTER TABLE [dbo].[Customer] DROP COLUMN [LegacyCode];
