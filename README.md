# MFU

## 配送追跡管理の適用手順

1. SQLを適用します。

```bash
mysql -u <user> -p <database> < /mnt/mfu/app/docs/sql/20260416_shipment_tracking.sql
```

2. アプリを再起動します。

## 配送追跡CLI

```bash
cd /mnt/mfu && /usr/bin/python3 /mnt/mfu/app/shipment_tracking/cli.py
```

## cron 設定例

```cron
0 * * * * cd /mnt/mfu && /usr/bin/python3 /mnt/mfu/app/shipment_tracking/cli.py >> /mnt/mfu/logs/shipment_tracking_cron.log 2>&1
```
