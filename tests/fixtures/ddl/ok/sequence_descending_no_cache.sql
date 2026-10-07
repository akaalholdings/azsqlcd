-- path: schema/sequences/sales.InvoiceNo.sql
CREATE SEQUENCE [sales].[InvoiceNo]
    AS decimal(18,0)
    START WITH -100
    INCREMENT BY -1
    MINVALUE -1000000
    MAXVALUE -1
    NO CYCLE
    NO CACHE;
