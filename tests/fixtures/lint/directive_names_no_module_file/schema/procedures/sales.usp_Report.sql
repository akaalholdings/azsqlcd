-- azsqlcd:after [sales].[Order]
-- azsqlcd:ignore-dep [sales].[vw_Closed]
-- azsqlcd:after [dbo].[vw_Open]
CREATE OR ALTER PROCEDURE [sales].[usp_Report]
AS
SELECT [One] FROM [sales].[vw_Open];
