CREATE TABLE [product].[Category] (
    [CategoryId] int IDENTITY(1, 1) NOT NULL,
    [ParentCategoryId] int NULL,
    [Name] nvarchar(100) NOT NULL,
    [IsActive] bit NOT NULL CONSTRAINT [DF_Category_IsActive] DEFAULT (1),
    CONSTRAINT [PK_Category] PRIMARY KEY CLUSTERED ([CategoryId]),
    CONSTRAINT [UQ_Category_Name] UNIQUE NONCLUSTERED ([Name]),
    CONSTRAINT [CK_Category_NotOwnParent] CHECK ([ParentCategoryId] <> [CategoryId]),
    CONSTRAINT [FK_Category_Parent] FOREIGN KEY ([ParentCategoryId]) REFERENCES [product].[Category] ([CategoryId])
);
GO
CREATE NONCLUSTERED INDEX [IX_Category_ParentCategoryId] ON [product].[Category] ([ParentCategoryId]);
