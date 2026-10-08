CREATE OR ALTER VIEW [audit].[vw_Log]
AS
-- one ')' in a comment
SELECT [Id], [Note )], N'((' AS [Open]
FROM [audit].[Log];
