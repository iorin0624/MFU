# Uber 日次データの0件数レコード削除手順

以下のSQLを実行して、deliveries=0 かつ金額がすべて0のレコードのみを削除します。

## 削除件数の確認

```sql
SELECT COUNT(*) AS will_delete
FROM uber_daily
WHERE deliveries = 0
  AND net_yen = 0 AND promo_yen = 0 AND other_yen = 0 AND tip_yen = 0;
```

## 実行用SQL

```sql
DELETE FROM uber_daily
WHERE deliveries = 0
  AND net_yen = 0 AND promo_yen = 0 AND other_yen = 0 AND tip_yen = 0;
```
