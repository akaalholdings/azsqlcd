-- path: schema/tables/dbo.Template.sql
CREATE TABLE [dbo].[Template] (
    [TemplateId] int NOT NULL,
    [Body] nvarchar(400) NOT NULL CONSTRAINT [DF_Template_Body] DEFAULT (N'line one
GO
line three; -- not a comment /* nor this */ it''s quoted'),
    [Sep] char(1) NOT NULL CONSTRAINT [DF_Template_Sep] DEFAULT (';'),
    [Marker] varchar(10) NOT NULL CONSTRAINT [CK_Template_Marker] CHECK ([Marker] <> '--' AND [Marker] <> '/*' AND [Marker] NOT LIKE '%GO%'),
    [Quote] nvarchar(10) NOT NULL CONSTRAINT [DF_Template_Quote] DEFAULT (N'''')
);
GO
CREATE NONCLUSTERED INDEX [IX_Template_Marker] ON [dbo].[Template] ([Marker]) WHERE [Marker] <> ');--' AND [Marker] <> N'GO';
