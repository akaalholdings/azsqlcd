-- expect: UNSUPPORTED
-- says: partition functions and partition schemes
-- line: 4
CREATE PARTITION SCHEME [PS_OrderYear] AS PARTITION [PF_OrderYear] ALL TO ([PRIMARY]);
