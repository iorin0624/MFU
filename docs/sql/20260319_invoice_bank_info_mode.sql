ALTER TABLE invoice_headers
    ADD COLUMN bank_info_mode VARCHAR(32) NOT NULL DEFAULT 'inline' AFTER issuer_template_id,
    ADD COLUMN payout_access_token_id BIGINT NULL AFTER bank_info_mode;
