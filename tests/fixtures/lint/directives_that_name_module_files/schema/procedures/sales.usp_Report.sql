-- azsqlcd:after [sales].[vw_Open]
-- azsqlcd:ignore-dep SALES."FN_TAX"
-- a plain comment: azsqlcd:anything
/* -- azsqlcd:nothing, inside a block comment */
CREATE OR ALTER PROCEDURE [sales].[usp_Report]
AS
SELECT [One], N'-- azsqlcd:text' AS [Text] FROM [sales].[vw_Open];
