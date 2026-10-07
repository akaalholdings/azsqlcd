-- expect: SYNTAX
-- says: one action per statement
-- line: 5
ALTER TABLE [dbo].[Customer] ADD
    [IsActive] bit NOT NULL CONSTRAINT [DF_Customer_IsActive] DEFAULT (1),
    [Notes] nvarchar(max) NULL;
