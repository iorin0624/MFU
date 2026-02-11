-- bulk refund mail notification tracking
ALTER TABLE event_refunds
  ADD COLUMN notified_at DATETIME NULL,
  ADD COLUMN notify_to_email VARCHAR(255) NULL,
  ADD COLUMN notify_error TEXT NULL;

CREATE INDEX ix_event_refunds_notified_at ON event_refunds(notified_at);
