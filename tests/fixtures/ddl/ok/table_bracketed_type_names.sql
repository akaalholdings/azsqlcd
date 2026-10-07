-- path: schema/tables/dbo.Scripted.sql
CREATE TABLE [dbo].[Scripted](
	[Id] [int] NOT NULL,
	[Name] [nvarchar](50) NOT NULL,
	[Amount] [decimal](18, 2) NULL,
	[Stamp] [datetime2](7) NOT NULL,
	[Blob] [varbinary](max) NULL
)
