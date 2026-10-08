#!/bin/bash
# mail_summary_watcher.sh - single-file watcher → OpenAI要約 → Discord通知
# 依存: inotifywait (inotify-tools), jq, python3, curl
# 例: sudo apt-get install -y inotify-tools jq

set -Eeuo pipefail

### ===== 設定 =====
WATCH_DIR="${WATCH_DIR:-/mnt/mfu/maildata/notify/new}"
OPENAI_API_KEY="${OPENAI_API_KEY:-}"
DISCORD_WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-}"
OPENAI_MODEL="${OPENAI_MODEL:-gpt-4o-mini}"
REDACT_CODES="${REDACT_CODES:-0}"             # 1で6〜10桁の数字を伏せる
LOG_FILE="${LOG_FILE:-/var/log/mail-summary-watcher/watcher.log}"
[[ -f /etc/default/mail_summary_watcher ]] && . /etc/default/mail_summary_watcher || true

### ==============

# ==== 確実なデフォルト適用（unset/空文字の両方を補正）====
: "${WATCH_DIR:=/mnt/mfu/maildata/notify/new}"
: "${OPENAI_API_KEY:=}"
: "${DISCORD_WEBHOOK_URL:=}"
: "${OPENAI_MODEL:=gpt-4o-mini}"
: "${REDACT_CODES:=0}"
: "${LOG_FILE:=/var/log/mail_summary_simple.log}"
: "${STATE_FILE:=/var/lib/mail_summary_watcher/seen.txt}"
: "${STATE_LOCK:=/var/run/mail_summary_watcher.lock}"
: "${CHECKPOINT_FILE:=/var/lib/mail_summary_watcher/checkpoint}"
: "${AU_PAY_OTP_HANDLER:=/usr/local/lib/mfu-notification-bridge/au_pay_otp_mail.py}"
# ===============================================================

# ログ・状態ファイルの準備（ここより前でSTATE_FILE/LOG_FILEを参照しない）
install -d -o root -g adm -m 0750 "$(dirname -- "${LOG_FILE}")"
touch "${LOG_FILE}"
chown root:adm "${LOG_FILE}"
chmod 0640 "${LOG_FILE}"
install -d -o root -g root -m 0750 "$(dirname -- "${STATE_FILE}")"
touch "${STATE_FILE}"
chmod 0600 "${STATE_FILE}"
exec 201>"${STATE_LOCK}"

# 以降の標準出力/標準エラーはログへ
exec >>"${LOG_FILE}" 2>&1

ts(){ date '+%Y-%m-%d %H:%M:%S'; }
on_err(){ echo "$(ts) [FATAL] line=$1 cmd=${BASH_COMMAND}" >&2; }
trap 'on_err $LINENO' ERR

need(){ command -v "$1" >/dev/null 2>&1 || { echo "$(ts) [ERROR] '$1' が必要です（sudo apt-get install -y $2）" >&2; exit 1; }; }
need inotifywait inotify-tools; need jq jq; need python3 python3; need curl curl
need sha256sum coreutils; need mkfifo coreutils; need find findutils; need sort coreutils

[[ -d "$WATCH_DIR" ]] || { mkdir -p "$WATCH_DIR"; echo "$(ts) [INFO] 作成: $WATCH_DIR" >&2; }
[[ -n "$OPENAI_API_KEY" ]] || { echo "$(ts) [ERROR] OPENAI_API_KEY 未設定" >&2; exit 1; }
[[ -n "$DISCORD_WEBHOOK_URL" ]] || { echo "$(ts) [ERROR] DISCORD_WEBHOOK_URL 未設定" >&2; exit 1; }

if [[ ! -e "$CHECKPOINT_FILE" ]]; then
  touch "$CHECKPOINT_FILE"
  chmod 0600 "$CHECKPOINT_FILE"
  echo "$(ts) [INFO] checkpoint initialized at current time" >&2
fi

wait_stable(){ # サイズが落ち着くまで待つ（最大30秒）
  local p="$1" last=-1 cur=0
  for _ in $(seq 1 150); do
    [[ -f "$p" ]] || { sleep 0.2; continue; }
    cur=$(stat -c%s "$p" 2>/dev/null || echo 0)
    if [[ $cur -gt 0 && $cur -eq $last ]]; then
      echo "$(ts) [DEBUG] wait_stable OK size=$cur path=$p" >&2
      return 0
    fi
    last=$cur; sleep 0.2
  done
  echo "$(ts) [WARN] wait_stable timeout path=$p last_size=$last" >&2
  return 0
}

parse_eml_to_json(){  # → {"subject":...,"from":...,"from_email":...,"to":...,"to_email":...,"date":...,"body":...}
python3 - "$1" <<'PY'
import sys, json, re, html
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr

def html2text(s: str) -> str:
    s = re.sub(r"(?is)<style.*?>.*?</style>", "", s)
    s = re.sub(r"(?is)<script.*?>.*?</script>", "", s)
    s = re.sub(r"(?i)</(p|div|tr|h[1-6]|li|br|table|thead|tbody|tfoot|th|td)>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)

p=sys.argv[1]
try:
    with open(p,"rb") as f:
        msg=BytesParser(policy=policy.default).parse(f)

    subj=(msg.get('Subject') or '').strip()
    frm=(msg.get('From') or '').strip()
    date=(msg.get('Date') or '').strip()
    name_from, addr_from = parseaddr(frm)

    # To/Delivered-To/X-Original-To から優先的に取得
    tos=[]
    for h in ('To','Delivered-To','X-Original-To'):
        vals = msg.get_all(h, []) or []
        if vals:
            tos += [a for _,a in getaddresses(vals)]
    to_display = ", ".join(tos)
    to_email = tos[0] if tos else ""

    body_text = ""
    # 1) get_body（plain優先→html）
    try:
        part = msg.get_body(preferencelist=('plain','html'))
        if part:
            if (part.get_content_type() or "").lower() == "text/html":
                body_text = html2text(part.get_content())
            else:
                body_text = part.get_content()
    except Exception:
        body_text = ""
    # 2) 空なら text/* 総なめ（htmlはテキスト化）
    if not body_text:
        parts=[]
        for part in msg.walk():
            ct = (part.get_content_type() or "")
            if ct.startswith("text/"):
                try:
                    content = part.get_content()
                except Exception:
                    try:
                        content = part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8','replace')
                    except Exception:
                        content = ""
                if ct == "text/html":
                    content = html2text(content)
                parts.append(content)
        body_text = "\n\n".join([p for p in parts if p]).strip()
    # 3) まだ空ならmsg全体から（html→text）
    if not body_text:
        try:
            raw = msg.get_content()
            body_text = html2text(raw)
        except Exception:
            body_text = ""
    # 4) 最後の手段：生データdecode→ヘッダ区切りで本文のみ→テキスト化
    if not body_text:
        try:
            with open(p,"rb") as f:
                raw_bytes = f.read()
            decoded=None
            for enc in ("utf-8","iso-2022-jp","cp932","latin1"):
                try:
                    decoded = raw_bytes.decode(enc); break
                except Exception: pass
            decoded = decoded or ""
            body_text = decoded.split("\n\n",1)[1] if "\n\n" in decoded else decoded
            body_text = html2text(body_text)
        except Exception:
            pass

    body_text = (body_text or "").replace("\r\n","\n").replace("\r","\n")
    body_text = re.sub(r"\n{3,}","\n\n",body_text).strip()

    out = {
        "subject": subj,
        "from": frm,
        "from_email": addr_from or "",
        "to": to_display,
        "to_email": to_email,
        "date": date,
        "body": body_text
    }
    print(json.dumps(out, ensure_ascii=False))
except Exception as e:
    print(json.dumps({"subject":"","from":"","from_email":"","to":"","to_email":"","date":"","body":f"ERROR parsing EML: {type(e).__name__}: {e}"}, ensure_ascii=False))
    sys.exit(0)
PY
}

maybe_redact(){ [[ "$REDACT_CODES" == "1" ]] && sed -E 's/([0-9]{6,10})/*******/g' || cat; }

summarize_openai(){ # 差出人は要約に含めない
  local j="$1" subject from_email to_email date body body_cap payload resp sum http tok_key
  subject=$(jq -r '.subject'    <<<"$j"); from_email=$(jq -r '.from_email' <<<"$j")
  to_email=$(jq -r '.to_email'  <<<"$j"); date=$(jq -r '.date' <<<"$j")
  body=$(jq -r '.body'          <<<"$j"); body_cap=${body:0:3000}

  echo "$(ts) [DEBUG] summarize_openai start subj_len=${#subject} body_cap=${#body_cap}" >&2

  tok_key="max_tokens"; [[ "$OPENAI_MODEL" == gpt-5* ]] && tok_key="max_completion_tokens"
  payload=$(jq -n --arg m "$OPENAI_MODEL" \
    --arg sys "あなたはメール要約アシスタント。3秒で内容把握できる要約を日本語で出力。1行目は20〜80文字で要点、続けて箇条書き最大5（日時/金額/重要アクション/期限/番号など）。※差出人情報は要約に含めない。" \
    --arg user "件名: ${subject}\n宛先: ${to_email}\n日付: ${date}\n\n本文:\n${body_cap}" \
    --arg key "$tok_key" \
    '{model:$m, messages:[{"role":"system","content":$sys},{"role":"user","content":$user}], temperature:0.1}
     | .[$key]=300')

  for i in 1 2 3; do
    echo "$(ts) [DEBUG] OpenAI req try=$i model=$OPENAI_MODEL key=$tok_key" >&2
    resp=$(curl -sS --connect-timeout 5 --max-time 20 -w '\nHTTP_STATUS:%{http_code}\n' \
      https://api.openai.com/v1/chat/completions \
      -H "Authorization: Bearer ${OPENAI_API_KEY}" \
      -H "Content-Type: application/json" \
      -d "$payload" || true)
    http=$(printf '%s' "$resp" | awk -F: '/^HTTP_STATUS:/{print $2}' | tr -d '\r\n ')
    sum=$(printf '%s' "$resp" | sed '/^HTTP_STATUS:/d' | jq -r '.choices[0].message.content // empty' 2>/dev/null || true)
    echo "$(ts) [DEBUG] OpenAI resp http=${http:-nil} summary_len=${#sum}" >&2

    if [[ "$http" == "200" || "$http" == "201" ]] && [[ -n "$sum" ]]; then
      printf '%s' "$sum"; return 0
    fi
    sleep $((i*2))
  done

  echo "$(ts) [WARN] OpenAI要約に失敗。本文先頭20行でフォールバック" >&2
  awk 'NF { print; count++; if (count >= 20) exit }' <<<"$body"
  return 0
}

post_discord(){ # 引数: text → 204/200なら成功
  local text="$1" http attempt
  for attempt in 1 2 3; do
    http=$(jq -n --arg msg "$text" '{content:$msg}' \
      | curl -sS --connect-timeout 5 --max-time 20 -w '%{http_code}' \
        -H "Content-Type: application/json" -X POST -d @- \
        "$DISCORD_WEBHOOK_URL" -o /dev/null || true)
    echo "$(ts) [DEBUG] Discord POST try=$attempt http=${http:-nil} len=${#text}" >&2
    if [[ "$http" == "204" || "$http" == "200" ]]; then
      return 0
    fi
    sleep $((attempt * 2))
  done
  return 1
}

# --- 重複判定ユーティリティ ---
fp_for_file(){ sha256sum "$1" | awk '{print $1}'; }   # ファイル内容の指紋
mark_seen(){  # 末尾追記＋最大2000件に維持（ロックは呼び出し側で取得）
  echo "$1" >> "$STATE_FILE"
  tail -n 2000 "$STATE_FILE" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "$STATE_FILE"
}

is_seen(){
  local sig="$1" found=1
  flock -x 201
  if grep -Fxq "$sig" "$STATE_FILE" 2>/dev/null; then
    found=0
  fi
  flock -u 201
  return "$found"
}

record_seen(){
  local sig="$1"
  flock -x 201
  if ! grep -Fxq "$sig" "$STATE_FILE" 2>/dev/null; then
    mark_seen "$sig"
  fi
  flock -u 201
}

advance_checkpoint(){
  local path="$1"
  if [[ ! -e "$CHECKPOINT_FILE" || "$path" -nt "$CHECKPOINT_FILE" ]]; then
    touch -r "$path" "$CHECKPOINT_FILE"
    chmod 0600 "$CHECKPOINT_FILE"
  fi
}

delete_processed_file(){
  local path="$1" attempt
  for attempt in 1 2 3; do
    if rm -f -- "$path"; then
      echo "$(ts) [DELETE] processed mail removed path=$path" >&2
      return 0
    fi
    echo "$(ts) [WARN] processed mail delete retry=$attempt path=$path" >&2
    sleep "$attempt"
  done
  echo "$(ts) [ERROR] processed mail could not be deleted path=$path" >&2
  return 1
}

process_file(){
  local path="$1" sig j subject from_email to_email date summary msg special_status
  echo "$(ts) [DETECT] $path"
  [[ -r "$path" ]] || { echo "$(ts) [WARN] not readable: $path" >&2; return 0; }

  wait_stable "$path"

  sig="$(fp_for_file "$path")"
  if is_seen "$sig"; then
    echo "$(ts) [INFO] skip duplicate sig=${sig:0:12} path=$path"
    advance_checkpoint "$path"
    delete_processed_file "$path"
    return 0
  fi

  # au PAY認証コードはAI要約を待たず、共通通知へ即時配信する。
  # 2=対象外のみ従来のメール要約へ進める。解析・配信失敗時は
  # 認証コードを通常要約やDiscord本文へ流さない。
  if [[ -x "$AU_PAY_OTP_HANDLER" ]]; then
    set +e
    "$AU_PAY_OTP_HANDLER" --eml "$path" --endpoint "$DISCORD_WEBHOOK_URL"
    special_status=$?
    set -e
    if [[ $special_status -eq 0 ]]; then
      record_seen "$sig"
      advance_checkpoint "$path"
      delete_processed_file "$path"
      echo "$(ts) [DONE] au PAY OTP posted and deleted path=$path" >&2
      return 0
    fi
    if [[ $special_status -ne 2 ]]; then
      echo "$(ts) [ERROR] au PAY OTP handler failed status=$special_status path=$path" >&2
      return 75
    fi
  fi

  j="$(parse_eml_to_json "$path")"; echo "$(ts) [DEBUG] parse exit=$? jlen=${#j}" >&2
  subject=$(jq -r '.subject' <<<"$j"); from_email=$(jq -r '.from_email' <<<"$j"); to_email=$(jq -r '.to_email' <<<"$j")
  date=$(jq -r '.date' <<<"$j")
  echo "$(ts) [DEBUG] meta subj_len=${#subject} sender_present=$([[ -n "$from_email" ]] && echo yes || echo no) recipient_present=$([[ -n "$to_email" ]] && echo yes || echo no)" >&2

  summary="$(summarize_openai "$j" | maybe_redact)"; echo "$(ts) [DEBUG] summary_len=${#summary}" >&2

  msg="差出人: ${from_email}
宛先:   ${to_email}
件名:   ${subject}
------
${summary}"
  msg=${msg:0:1500}

  if post_discord "$msg"; then
    record_seen "$sig"
    advance_checkpoint "$path"
    delete_processed_file "$path"
    echo "$(ts) [DONE] posted and deleted path=$path" >&2
  else
    echo "$(ts) [ERROR] Discord post failed after retries path=$path" >&2
    return 75
  fi
}

catch_up(){
  local -a pending=()
  local path
  mapfile -d '' -t pending < <(find "$WATCH_DIR" -maxdepth 1 -type f -newer "$CHECKPOINT_FILE" -print0 | sort -z)
  echo "$(ts) [INFO] catch-up pending=${#pending[@]}" >&2
  for path in "${pending[@]}"; do
    process_file "$path"
  done
}

EVENT_FIFO="/run/mail_summary_watcher.events.$$"
inotify_pid=""
cleanup(){
  if [[ -n "$inotify_pid" ]] && kill -0 "$inotify_pid" 2>/dev/null; then
    kill "$inotify_pid" 2>/dev/null || true
    wait "$inotify_pid" 2>/dev/null || true
  fi
  rm -f "$EVENT_FIFO"
}
trap cleanup EXIT

rm -f "$EVENT_FIFO"
mkfifo -m 0600 "$EVENT_FIFO"
exec 202<>"$EVENT_FIFO"
inotifywait -m -e moved_to -e close_write -e create --format '%w%f' "$WATCH_DIR" >&202 2>>"$LOG_FILE" &
inotify_pid=$!
sleep 0.2
kill -0 "$inotify_pid"

echo "$(ts) [START] Watching $WATCH_DIR inotify_pid=$inotify_pid"
catch_up

while true; do
  if IFS= read -r -t 10 -u 202 path; then
    process_file "$path"
    continue
  fi
  if ! kill -0 "$inotify_pid" 2>/dev/null; then
    wait "$inotify_pid" || true
    echo "$(ts) [ERROR] inotifywait stopped unexpectedly" >&2
    exit 76
  fi
done
