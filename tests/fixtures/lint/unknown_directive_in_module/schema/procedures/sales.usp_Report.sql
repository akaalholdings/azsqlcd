-- azsqlcd:ignore_dep [sales].[vw_Open]
-- azsqlcd:deploy-module [sales].[vw_Open]
CREATE OR ALTER PROCEDURE [sales].[usp_Report]
AS
    -- azsqlcd:after [sales].[vw_Open]
SELECT [One] FROM [sales].[vw_Open];
