-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
CREATE SYNONYM [dbo].[LegacyOrder] FOR [archive].[dbo].[Order];
