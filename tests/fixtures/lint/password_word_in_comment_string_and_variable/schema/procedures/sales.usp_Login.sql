CREATE OR ALTER PROCEDURE [sales].[usp_Login]
    @Password nvarchar(100) = N''
AS
-- never write PASSWORD = 'x' in a file
SELECT [UserId], N'hint: PASSWORD = ''x'' is refused' AS [Hint]
FROM [sales].[User]
WHERE [PasswordHash] = HASHBYTES('SHA2_256', @Password) AND [Secret] = @Password;
