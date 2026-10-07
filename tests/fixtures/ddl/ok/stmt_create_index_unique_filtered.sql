CREATE UNIQUE NONCLUSTERED INDEX [UX_Customer_Email] ON [dbo].[Customer] ([Email]) WHERE [Email] IS NOT NULL;
