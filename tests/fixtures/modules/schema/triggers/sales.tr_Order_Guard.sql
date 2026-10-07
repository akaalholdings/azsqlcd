-- azsqlcd:after [sales].[tr_Order_Audit]
CREATE OR ALTER TRIGGER [sales].[tr_Order_Guard] ON [sales].[Order]
WITH EXECUTE AS OWNER
INSTEAD OF DELETE
AS
BEGIN
    SET NOCOUNT ON;
    THROW 50001, N'Orders are never deleted; CREATE a cancellation AS a new row', 1;
END;
