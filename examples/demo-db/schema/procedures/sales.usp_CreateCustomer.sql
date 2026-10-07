CREATE OR ALTER PROCEDURE [sales].[usp_CreateCustomer]
    @Email nvarchar(320),
    @DisplayName nvarchar(200),
    @CountryCode char(2),
    @CustomerId int OUTPUT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    IF EXISTS (SELECT 1 FROM [sales].[Customer] WHERE [Email] = @Email)
        THROW 50001, N'A customer with this e-mail address exists.', 1;

    INSERT INTO [sales].[Customer] ([Email], [DisplayName], [CountryCode])
    VALUES (@Email, @DisplayName, UPPER(@CountryCode));

    SET @CustomerId = SCOPE_IDENTITY();
END;
