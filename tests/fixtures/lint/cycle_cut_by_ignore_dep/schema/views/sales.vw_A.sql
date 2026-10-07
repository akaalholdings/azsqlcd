-- azsqlcd:ignore-dep [sales].[vw_B]
CREATE OR ALTER VIEW [sales].[vw_A]
AS
SELECT [Id] FROM [sales].[Order] /* was [sales].[vw_B] */ WHERE [Note] <> 'sales.vw_B';
