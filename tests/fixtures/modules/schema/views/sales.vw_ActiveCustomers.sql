CREATE OR ALTER VIEW sales.vw_ActiveCustomers
AS
SELECT c.CustomerId, c.Name, c.IsActive
FROM sales.Customer AS c
WHERE c.IsActive = 1
WITH CHECK OPTION;
