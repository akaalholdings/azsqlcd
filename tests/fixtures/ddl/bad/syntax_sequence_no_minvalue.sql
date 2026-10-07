-- expect: SYNTAX
-- says: NO MINVALUE
-- line: 6
-- path: schema/sequences/dbo.S.sql
CREATE SEQUENCE [dbo].[S] AS int START WITH 1 INCREMENT BY 1
    NO MINVALUE
    MAXVALUE 100 NO CYCLE NO CACHE;
