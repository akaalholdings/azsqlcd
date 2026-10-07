CREATE TABLE [sales].[Tag] (
    [TagId] int NOT NULL,
    [Code] varchar(20) NOT NULL,
    [Label] nvarchar(50) NOT NULL,
    CONSTRAINT [PK_Tag] PRIMARY KEY CLUSTERED ([TagId]),
    CONSTRAINT [UQ_Tag_Code] UNIQUE NONCLUSTERED ([Code])
);
