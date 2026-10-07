"""Module definitions as sys.sql_modules of a real Azure SQL Database holds them.

Measured on WideWorldImporters (live read-only run, engine facts): in 45 of 47 modules the text
starts with white space before CREATE ('\\r\\n' in 41, ' \\r\\n' in 4); the line ends are CRLF with
some bare LF; the verb is as written; a module that was made with CREATE OR ALTER is stored as
'CREATE   VIEW' (the words OR ALTER are gone, their three spaces stay).

The file that export writes has 'CREATE OR ALTER VIEW' with one space: the white space between the
verb and the kind word is what the engine left of the verb, not content (live spike L7, T2). A
deploy of that file stores 'CREATE   VIEW' again, so the export of a deployed module is its file.

The texts are Python strings with escapes, not files: a checkout or an editor that changes line
ends must not change a fixture whose point is its line ends and its leading white space.
"""

from typing import NamedTuple


class Live(NamedTuple):
    kind: str  # VIEW | PROCEDURE | FUNCTION
    type_code: str  # sys.objects.type as the driver gives it: char(2), padded
    schema: str
    name: str
    definition: str  # sys.sql_modules.definition
    file_text: str  # the object file that export writes for it
    schema_bound: bool = False


# '\r\n' before CREATE, CRLF with two bare LF, the header on several lines
SEARCH_FOR_PEOPLE = Live(
    "PROCEDURE",
    "P ",
    "Website",
    "SearchForPeople",
    "\r\nCREATE PROCEDURE [Website].[SearchForPeople]\r\n@SearchText nvarchar(1000),\n"
    "@MaximumRowsToReturn int\r\nWITH EXECUTE AS OWNER\r\nAS\r\nBEGIN\r\n"
    "    SELECT TOP(@MaximumRowsToReturn) p.[PersonID]\n    FROM [Application].[People] AS p\r\n"
    "    WHERE p.[SearchName] LIKE N'%' + @SearchText + N'%';\r\nEND;\r\n",
    "\nCREATE OR ALTER PROCEDURE [Website].[SearchForPeople]\n@SearchText nvarchar(1000),\n"
    "@MaximumRowsToReturn int\nWITH EXECUTE AS OWNER\nAS\nBEGIN\n"
    "    SELECT TOP(@MaximumRowsToReturn) p.[PersonID]\n    FROM [Application].[People] AS p\n"
    "    WHERE p.[SearchName] LIKE N'%' + @SearchText + N'%';\nEND;\n",
)

# ' \r\n' before CREATE, a name without brackets
SUPPLIERS = Live(
    "VIEW",
    "V ",
    "Website",
    "Suppliers",
    " \r\nCREATE VIEW Website.Suppliers\r\nAS\r\nSELECT s.[SupplierID], s.[SupplierName]\r\n"
    "FROM [Purchasing].[Suppliers] AS s\r\n",
    " \nCREATE OR ALTER VIEW Website.Suppliers\nAS\nSELECT s.[SupplierID], s.[SupplierName]\n"
    "FROM [Purchasing].[Suppliers] AS s\n",
)

# no white space before CREATE; made with CREATE OR ALTER: the engine stores 'CREATE   VIEW'
CUSTOMERS = Live(
    "VIEW",
    "V ",
    "Website",
    "Customers",
    "CREATE   VIEW [Website].[Customers]\r\nAS\r\n"
    "SELECT c.[CustomerID]\r\nFROM [Sales].[Customers] AS c;\r\n",
    "CREATE OR ALTER VIEW [Website].[Customers]\nAS\nSELECT c.[CustomerID]\nFROM [Sales].[Customers] AS c;\n",
)

# white space before CREATE, three spaces after it, and a schema binding to take away
ORDER_TOTALS = Live(
    "VIEW",
    "V ",
    "Website",
    "OrderTotals",
    "\r\n\r\nCREATE   VIEW [Website].[OrderTotals]\r\nWITH SCHEMABINDING\r\nAS\r\n"
    "SELECT o.[OrderID], COUNT_BIG(*) AS [Lines]\nFROM [Sales].[OrderLines] AS o\r\n"
    "GROUP BY o.[OrderID];\r\n",
    "\n\nCREATE OR ALTER VIEW [Website].[OrderTotals]\nWITH SCHEMABINDING\nAS\n"
    "SELECT o.[OrderID], COUNT_BIG(*) AS [Lines]\nFROM [Sales].[OrderLines] AS o\nGROUP BY o.[OrderID];\n",
    schema_bound=True,
)

# a tab and an indent before CREATE, three spaces after it, SCHEMABINDING first of two options
CALCULATE_PRICE = Live(
    "FUNCTION",
    "FN",
    "Website",
    "CalculateCustomerPrice",
    "\t\r\n  CREATE   FUNCTION [Website].[CalculateCustomerPrice] (@StockItemID int)\r\n"
    "RETURNS decimal(18, 2)\r\nWITH SCHEMABINDING, EXECUTE AS OWNER\r\nAS\r\n"
    "BEGIN\r\n    RETURN 1;\nEND;\r\n",
    "\t\n  CREATE OR ALTER FUNCTION [Website].[CalculateCustomerPrice] (@StockItemID int)\n"
    "RETURNS decimal(18, 2)\nWITH SCHEMABINDING, EXECUTE AS OWNER\nAS\nBEGIN\n    RETURN 1;\nEND;\n",
    schema_bound=True,
)

LIVE = (SEARCH_FOR_PEOPLE, SUPPLIERS, CUSTOMERS, ORDER_TOTALS, CALCULATE_PRICE)

# definition of a schema-bound fixture -> the ALTER that removes the binding (one space after the verb)
UNBOUND = {
    ORDER_TOTALS.name: (
        "\r\n\r\nALTER VIEW [Website].[OrderTotals]\r\n\r\nAS\r\n"
        "SELECT o.[OrderID], COUNT_BIG(*) AS [Lines]\nFROM [Sales].[OrderLines] AS o\r\n"
        "GROUP BY o.[OrderID];\r\n"
    ),
    CALCULATE_PRICE.name: (
        "\t\r\n  ALTER FUNCTION [Website].[CalculateCustomerPrice] (@StockItemID int)\r\n"
        "RETURNS decimal(18, 2)\r\nWITH EXECUTE AS OWNER\r\nAS\r\nBEGIN\r\n    RETURN 1;\nEND;\r\n"
    ),
}
