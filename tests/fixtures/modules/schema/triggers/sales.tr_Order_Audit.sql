CREATE OR ALTER TRIGGER [sales].[tr_Order_Audit]
ON [sales].[Order]
AFTER INSERT, UPDATE
AS
BEGIN
    SET NOCOUNT ON;
    INSERT INTO [dbo].[AuditLog] ([Mode], [OrderId])
    SELECT 'AS', i.[OrderId] FROM inserted AS i;
    EXEC [sales].[usp_Ping] @n = 1;
END;
