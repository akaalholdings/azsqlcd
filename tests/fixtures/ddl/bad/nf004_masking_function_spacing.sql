-- expect: NF004
-- says: the engine stores this masking function as 'partial(1, "xX", 0)'
-- line: 4
ALTER TABLE [dbo].[Customer] ALTER COLUMN [Email] ADD MASKED WITH (FUNCTION = 'Partial(1,"xX",0)');
