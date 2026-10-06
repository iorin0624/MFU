# MFU common notification sender

Raspberry Pi, Asterisk and other Linux hosts can send notifications through MFU instead of posting directly to Discord.

1. Create an external source in `/admin/notification-sources`.
2. Put the one-time token in `MFU_NOTIFICATION_TOKEN`.
3. Send a notification:

```sh
python3 mfu_notify.py --feature train_status --severity warning --title '列車遅延' --body '遅延が発生しています'
```

Failed sends remain in a local SQLite queue. Retry from cron or a systemd timer:

```sh
python3 mfu_notify.py --drain
```

During gradual migration, existing programs may instead use the one-time Discord-compatible URL shown on the source-management page. The server converts Discord `content` and `embeds` into the common notification format and can continue forwarding to Discord.
