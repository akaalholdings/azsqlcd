-- expect: SYNTAX
-- says: starts with CREATE TABLE
-- line: 5
-- path: schema/tables/dbo.S.sql
CREATE SEQUENCE [dbo].[S] AS int START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 100 NO CYCLE NO CACHE;
