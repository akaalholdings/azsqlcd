-- expect: UNSUPPORTED
-- says: external tables
-- line: 4
CREATE EXTERNAL TABLE [dbo].[Remote] ([Id] int NOT NULL) WITH (DATA_SOURCE = [RemoteSource]);
