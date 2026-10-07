CREATE SCHEMA [hr];
GO
CREATE TABLE [hr].[Department] (
    [DepartmentId] int NOT NULL,
    [HeadId] int NULL,
    CONSTRAINT [PK_Department] PRIMARY KEY CLUSTERED ([DepartmentId])
);
GO
CREATE TABLE [hr].[Employee] (
    [EmployeeId] int NOT NULL,
    [DepartmentId] int NOT NULL,
    [ManagerId] int NULL,
    CONSTRAINT [PK_Employee] PRIMARY KEY CLUSTERED ([EmployeeId])
);
GO
ALTER TABLE [hr].[Department] ADD CONSTRAINT [FK_Department_Head] FOREIGN KEY ([HeadId]) REFERENCES [hr].[Employee] ([EmployeeId]);
GO
ALTER TABLE [hr].[Employee] ADD CONSTRAINT [FK_Employee_Department] FOREIGN KEY ([DepartmentId]) REFERENCES [hr].[Department] ([DepartmentId]);
GO
ALTER TABLE [hr].[Employee] ADD CONSTRAINT [FK_Employee_Manager] FOREIGN KEY ([ManagerId]) REFERENCES [hr].[Employee] ([EmployeeId]);
GO
