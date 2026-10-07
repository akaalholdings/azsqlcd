-- expect: UNSUPPORTED
-- says: FILESTREAM
-- line: 7
-- path: schema/tables/dbo.Attachment.sql
CREATE TABLE [dbo].[Attachment] (
    [AttachmentId] uniqueidentifier NOT NULL,
    [Content] varbinary(max) FILESTREAM NULL
);
