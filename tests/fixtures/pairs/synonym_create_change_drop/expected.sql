DROP SYNONYM [dbo].[Cust];
GO
DROP SYNONYM [dbo].[Gone];
GO
CREATE SYNONYM [dbo].[Cust] FOR [sales].[Order];
GO
CREATE SYNONYM [dbo].[Ord] FOR [sales].[Order];
GO
