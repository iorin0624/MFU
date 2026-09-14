ALTER TABLE recurring_expense_months
    ADD COLUMN IF NOT EXISTS freee_memo VARCHAR(255) NULL AFTER actual_amount,
    ADD COLUMN IF NOT EXISTS freee_synced_memo VARCHAR(255) NULL AFTER freee_synced_issue_date;
