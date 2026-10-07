CREATE OR ALTER TRIGGER [sales].[tr_Order_StatusHistory]
ON [sales].[Order]
AFTER INSERT, UPDATE
AS
BEGIN
    SET NOCOUNT ON;

    INSERT INTO [sales].[OrderStatusHistory] ([OrderId], [OldStatus], [NewStatus])
    SELECT i.[OrderId], d.[Status], i.[Status]
    FROM inserted AS i
    LEFT JOIN deleted AS d ON d.[OrderId] = i.[OrderId]
    WHERE d.[OrderId] IS NULL
       OR d.[Status] <> i.[Status];
END;
