-- azsqlcd:ignore-dep [sales].[vw_OpenOrders]
CREATE OR ALTER PROCEDURE [rpt].[usp_Words]
AS
BEGIN
    -- GO
    /* CREATE OR ALTER VIEW [sales].[vw_ActiveCustomers] WITH SCHEMABINDING AS SELECT 1
GO */
    DECLARE @sql nvarchar(max) = N'CREATE VIEW [rpt].[tmp] WITH SCHEMABINDING AS SELECT [OrderId] FROM [sales].[vw_OrderTotals];
GO
';
    IF OBJECT_ID(N'[sales].[vw_OpenOrders]') IS NOT NULL
        SELECT COUNT(*) AS [n] FROM [sales].[vw_OpenOrders];
    SELECT m.[Margin] FROM [rpt].[vw_Margin] AS m;
END;
