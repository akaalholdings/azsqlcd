-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
GRANT SELECT ON [sales].[Order] TO [report_reader];
