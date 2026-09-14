ALTER TABLE recurring_expense_masters
    ADD COLUMN IF NOT EXISTS email_receipt_enabled TINYINT(1) NOT NULL DEFAULT 0 AFTER allow_skip;

ALTER TABLE recurring_expense_attachments
    ADD COLUMN IF NOT EXISTS source_kind VARCHAR(16) NOT NULL DEFAULT 'upload' AFTER sha256,
    ADD COLUMN IF NOT EXISTS source_key CHAR(64) NULL AFTER source_kind;

CREATE UNIQUE INDEX IF NOT EXISTS uq_recurring_expense_attachment_source
    ON recurring_expense_attachments (month_id, source_key);
