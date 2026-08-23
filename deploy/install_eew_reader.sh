#!/bin/sh
set -eu

OUT=/root/eew-history-reader.conf
PASSWORD="$(openssl rand -hex 32)"

mysql --protocol=socket <<SQL
CREATE USER IF NOT EXISTS 'eew_reader'@'192.168.103.16' IDENTIFIED BY '${PASSWORD}';
ALTER USER 'eew_reader'@'192.168.103.16' IDENTIFIED BY '${PASSWORD}';
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'eew_reader'@'192.168.103.16';
GRANT SELECT ON eew_history.eew_reports TO 'eew_reader'@'192.168.103.16';
FLUSH PRIVILEGES;
SQL

umask 077
cat >"$OUT" <<EOF
[mysql]
host=192.168.103.17
port=3306
database=eew_history
user=eew_reader
password=${PASSWORD}
connect_timeout=5
EOF
chmod 600 "$OUT"
echo "EEW reader account and transfer file were created."
mysql --protocol=socket -NBe "SHOW GRANTS FOR 'eew_reader'@'192.168.103.16'"
