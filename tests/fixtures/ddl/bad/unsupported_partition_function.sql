-- expect: UNSUPPORTED
-- says: partition functions
-- line: 4
CREATE PARTITION FUNCTION [pf_Monthly] (datetime2(3)) AS RANGE RIGHT FOR VALUES ('2026-01-01', '2026-02-01');
