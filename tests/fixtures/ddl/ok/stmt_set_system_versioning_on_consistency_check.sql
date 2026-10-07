alter table dbo.Team set (system_versioning = on
    (history_table = dbo.Team_History, data_consistency_check = off, history_retention_period = 1 week));
