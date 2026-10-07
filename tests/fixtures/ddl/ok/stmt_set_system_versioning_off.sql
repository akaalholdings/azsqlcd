-- azsqlcd:allow TEMPORAL_OFF [dbo].[Team] reason: the table goes; dbo.Team_History stays for the auditors
ALTER TABLE [dbo].[Team] SET (SYSTEM_VERSIONING = OFF);
