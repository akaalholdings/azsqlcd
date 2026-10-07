-- azsqlcd:ignore-dep [sales].[usp_Pong]
CREATE OR ALTER PROCEDURE [sales].[usp_Ping]
AS
EXEC [sales].[usp_Pong];
