ALTER TABLE [hr].[Department] DROP CONSTRAINT [FK_Department_Head];
GO
ALTER TABLE [hr].[Employee] DROP CONSTRAINT [FK_Employee_Department];
GO
DROP TABLE [hr].[Department];
GO
DROP TABLE [hr].[Employee];
GO
DROP SCHEMA [hr];
GO
