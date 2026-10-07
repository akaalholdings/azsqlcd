-- azsqlcd:allow ALTER_COLUMN_LOSSY [sales].[Order].[Status] reason: values fit, checked in r41
ALTER TABLE [sales].[Order] ALTER COLUMN [Status] smallint NOT NULL WITH (ONLINE = ON);
