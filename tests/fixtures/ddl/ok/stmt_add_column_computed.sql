ALTER TABLE [dbo].[Customer] ADD [FullName] AS ([FirstName] + N' ' + [LastName]) PERSISTED;
