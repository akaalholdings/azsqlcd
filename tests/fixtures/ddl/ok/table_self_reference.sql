-- path: schema/tables/hr.Employee.sql
CREATE TABLE [hr].[Employee] (
    [EmployeeId] int NOT NULL,
    [ManagerId] int NULL,
    CONSTRAINT [PK_Employee] PRIMARY KEY CLUSTERED ([EmployeeId]),
    CONSTRAINT [FK_Employee_Manager] FOREIGN KEY ([ManagerId]) REFERENCES [hr].[Employee] ([EmployeeId])
);
