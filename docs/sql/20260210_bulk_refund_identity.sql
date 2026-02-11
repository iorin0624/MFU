-- bulk refund safety migration
ALTER TABLE event_payments
  ADD COLUMN event_member_id BIGINT UNSIGNED NULL,
  ADD COLUMN external_login_user_id BIGINT UNSIGNED NULL;

CREATE INDEX ix_event_member_id ON event_payments(event_member_id);
CREATE INDEX ix_external_login_user_id ON event_payments(external_login_user_id);
CREATE INDEX ix_event_identity ON event_payments(event_id, event_member_id, external_login_user_id);

ALTER TABLE event_refunds
  ADD COLUMN bulk_refund_run_id CHAR(36) NULL,
  ADD COLUMN created_by_admin VARCHAR(64) NULL;

CREATE INDEX ix_bulk_refund_run ON event_refunds(bulk_refund_run_id);

-- deterministic backfill only
UPDATE event_payments p
JOIN mfu_event_member m ON m.payment_row_id = p.id
   SET p.event_member_id = m.id,
       p.external_login_user_id = COALESCE(p.external_login_user_id, m.user_id)
 WHERE p.event_member_id IS NULL;

UPDATE event_payments p
JOIN mfu_payment_request pr ON pr.token = p.payment_token
   SET p.external_login_user_id = pr.user_id
 WHERE p.external_login_user_id IS NULL
   AND p.payment_token IS NOT NULL;


ALTER TABLE mfu_event_member
  ADD COLUMN receipt_note TEXT NULL;
