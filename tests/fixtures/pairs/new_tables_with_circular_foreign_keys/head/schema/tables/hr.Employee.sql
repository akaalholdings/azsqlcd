CREATE TABLE [hr].[Employee] (
    [EmployeeId] int NOT NULL,
    [DepartmentId] int NOT NULL,
    [ManagerId] int NULL,
    CONSTRAINT [PK_Employee] PRIMARY KEY CLUSTERED ([EmployeeId]),
    CONSTRAINT [FK_Employee_Department] FOREIGN KEY ([DepartmentId]) REFERENCES [hr].[Department] ([DepartmentId]),
    CONSTRAINT [FK_Employee_Manager] FOREIGN KEY ([ManagerId]) REFERENCES [hr].[Employee] ([EmployeeId])
);
