-- expect: SYNTAX
-- says: TABLE or SEQUENCE after ALTER
-- line: 4
ALTER PARTITION FUNCTION [PF_OrderYear]() SPLIT RANGE ('2027-01-01');
