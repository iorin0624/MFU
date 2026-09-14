ALTER TABLE recurring_expense_masters
    ADD COLUMN IF NOT EXISTS link_url VARCHAR(2048) NULL AFTER name;
