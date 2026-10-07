-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
ALTER TABLE sales.Settings ADD Value nvarchar(100) NULL, Start datetime2(0) NULL, Disable bit NOT NULL CONSTRAINT DF_Settings_Disable DEFAULT 0;
GO
CREATE UNIQUE INDEX IX_Settings_Value ON sales.Settings (Value) WHERE Value IS NOT NULL AND Disable = 0;
