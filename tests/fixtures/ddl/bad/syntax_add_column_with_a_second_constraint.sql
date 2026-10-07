-- expect: SYNTAX
-- says: one action per statement
-- line: 4
ALTER TABLE [dbo].[Customer] ADD [Age] tinyint NULL CONSTRAINT [CK_Customer_Age] CHECK ([Age] < 150);
