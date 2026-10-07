CREATE TABLE [sales].[Tag] (
    [TagId] int NOT NULL,
    [Code] varchar(20) NOT NULL,
    [Label] nvarchar(50) NOT NULL,
    CONSTRAINT [UQ_Tag_Label] UNIQUE NONCLUSTERED ([Label])
);
