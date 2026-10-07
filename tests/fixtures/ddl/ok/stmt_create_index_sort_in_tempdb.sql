CREATE NONCLUSTERED INDEX [IX_Customer_LastName] ON [dbo].[Customer] ([LastName], [FirstName]) WITH (SORT_IN_TEMPDB = ON, ONLINE = OFF);
