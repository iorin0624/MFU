ALTER TABLE recurring_expense_masters
    ADD COLUMN IF NOT EXISTS freee_memo VARCHAR(255) NULL AFTER link_url,
    ADD COLUMN IF NOT EXISTS allow_skip TINYINT(1) NOT NULL DEFAULT 0 AFTER freee_memo;
