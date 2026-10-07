-- expect: SYNTAX
-- says: MAXVALUE
-- line: 5
-- path: schema/sequences/dbo.S.sql
CREATE SEQUENCE [dbo].[S] AS int START WITH 1 INCREMENT BY 1 MINVALUE 1 NO CYCLE NO CACHE;
