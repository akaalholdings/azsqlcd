CREATE TABLE [hr].[Department] (
    [DepartmentId] int NOT NULL,
    [HeadId] int NULL,
    CONSTRAINT [PK_Department] PRIMARY KEY CLUSTERED ([DepartmentId]),
    CONSTRAINT [FK_Department_Head] FOREIGN KEY ([HeadId]) REFERENCES [hr].[Employee] ([EmployeeId])
);
