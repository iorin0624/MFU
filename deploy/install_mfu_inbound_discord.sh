#!/usr/bin/env bash
set -euo pipefail

EXTENSIONS=/etc/asterisk/extensions_custom.conf
BACKUP="${EXTENSIONS}.mfu-inbound-discord.$(date +%Y%m%d%H%M%S).bak"

install -o root -g asterisk -m 0755 /tmp/mfu_blocked_call_notify.py /var/lib/asterisk/bin/mfu_blocked_call_notify.py
install -o root -g asterisk -m 0755 /tmp/mfu_blacklist_cdr.py /var/lib/asterisk/bin/mfu_blacklist_cdr.py
install -o root -g root -m 0755 /tmp/mfu-whitelist-apply /usr/local/sbin/mfu-whitelist-apply
install -o root -g asterisk -m 0640 /tmp/mfu_inbound_discord.conf /etc/asterisk/mfu_inbound_discord.conf
if [[ ! -e /etc/asterisk/caller_blacklist_actions_generated.conf ]]; then
  printf '%s\n' '; Generated placeholder' '[callerid-blacklist-actions]' \
    > /etc/asterisk/caller_blacklist_actions_generated.conf
  chown root:asterisk /etc/asterisk/caller_blacklist_actions_generated.conf
  chmod 0640 /etc/asterisk/caller_blacklist_actions_generated.conf
fi
cp -a "$EXTENSIONS" "$BACKUP"

python3 - "$EXTENSIONS" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
start = text.index("; BEGIN CALLER ID WHITELIST")
end = text.index("; END CALLER ID WHITELIST", start) + len("; END CALLER ID WHITELIST")
replacement = '''; BEGIN CALLER ID WHITELIST
#include caller_whitelist_generated.conf

[custom-callerid-whitelist]
exten => s,1,NoOp(Caller ID whitelist check: raw=${CALLERID(num)})
 same => n,Set(RAW_CID=${CALLERID(num)})
 same => n,Set(NORMALIZED_CID=${FILTER(0-9,${RAW_CID})})
 same => n,GotoIf($["${NORMALIZED_CID:0:2}" = "81"]?maybe-e164:check)
 same => n(maybe-e164),GotoIf($[${LEN(${NORMALIZED_CID})} >= 11]?normalize-e164:check)
 same => n(normalize-e164),Set(NORMALIZED_CID=0${NORMALIZED_CID:2})
 same => n(check),Set(MFU_CALLER_REGISTERED=0)
 same => n,Set(MFU_CALLER_NAME_B64=)
 same => n,GotoIf($["${NORMALIZED_CID}" = ""]?anonymous-name)
 same => n,GotoIf($[${EPOCH} < ${MFU_BLACKLIST_DISABLED_UNTIL}]?blacklist-disabled)
 same => n,GotoIf($["${DIALPLAN_EXISTS(callerid-blacklist-entries,${NORMALIZED_CID},1)}" = "1"]?blacklisted)
 same => n(blacklist-disabled),NoOp(Blacklist temporarily disabled for ${NORMALIZED_CID})
 same => n,Set(MFU_CT_RESERVATION_ID=)
 same => n,GosubIf($["${DIALPLAN_EXISTS(mfu-call-through-reservation,${NORMALIZED_CID},1)}" = "1"]?mfu-call-through-reservation,${NORMALIZED_CID},1)
 same => n,Set(MFU_CT_CURRENT_DID=${FILTER(0-9,${FROM_DID})})
 same => n,ExecIf($["${MFU_CT_CURRENT_DID}" = ""]?Set(MFU_CT_CURRENT_DID=${FILTER(0-9,${CDR(did)})}))
 same => n,GotoIf($["${MFU_CT_RESERVATION_ID}" != "" & ${EPOCH} < ${MFU_CT_EXPIRES_AT} & "${MFU_CT_CURRENT_DID}" = "${MFU_CT_INBOUND_DID}"]?call-through)
 same => n,GotoIf($["${DIALPLAN_EXISTS(callerid-whitelist-entries,${NORMALIZED_CID},1)}" = "1"]?load-name:unregistered-name)
 same => n(load-name),Set(MFU_CALLER_REGISTERED=1)
 same => n,Gosub(callerid-whitelist-entries,${NORMALIZED_CID},1)
 same => n,ExecIf($["${MFU_CALLER_NAME_B64}" != ""]?Set(CALLERID(name)=${BASE64_DECODE(${MFU_CALLER_NAME_B64})}))
 same => n,ExecIf($["${MFU_CALLER_NAME_B64}" = ""]?Set(CALLERID(name)=${NORMALIZED_CID}))
 same => n,Goto(mode)
 same => n(unregistered-name),ExecIf($["${CALLERID(name)}" = ""]?Set(CALLERID(name)=${NORMALIZED_CID}))
 same => n,Goto(mode)
 same => n(anonymous-name),Set(CALLERID(name)=${BASE64_DECODE(6Z2e6YCa55+l)})
 same => n,GotoIf($["${MFU_ANONYMOUS_HANGUP_ENABLED}" = "1"]?anonymous-rejected:anonymous)
 same => n(mode),GotoIf($[${EPOCH} < ${MFU_WHITELIST_DISABLED_UNTIL}]?allowed)
 same => n,GotoIf($["${NORMALIZED_CID}" = ""]?anonymous)
 same => n,GotoIf($["${MFU_CALLER_REGISTERED}" = "1"]?allowed:blocked)
 same => n(anonymous),GotoIf($[${EPOCH} < ${MFU_ANONYMOUS_ALLOWED_UNTIL}]?allowed:blocked-anonymous)
 same => n(anonymous-rejected),NoOp(Anonymous caller REJECT -> Hangup cause 21)
 same => n,Gosub(mfu-inbound-discord-notify,s,1(anonymous,hangup_21))
 same => n,Set(MFU_CDR_DID=${FILTER(0-9,${FROM_DID})})
 same => n,ExecIf($["${MFU_CDR_DID}" = ""]?Set(MFU_CDR_DID=${FILTER(0-9,${CDR(did)})}))
 same => n,Set(MFU_CDR_ID=${FILTER(0-9.,${UNIQUEID})})
 same => n,Set(CDR_PROP(disable)=1)
 same => n,TrySystem(/usr/bin/nohup /usr/bin/python3 /var/lib/asterisk/bin/mfu_anonymous_cdr.py '${MFU_CDR_DID}' '${MFU_CDR_ID}' </dev/null >/dev/null 2>&1 &)
 same => n,Hangup(21)
 same => n(call-through),Gosub(mfu-inbound-discord-notify,s,1(call_through,call_through_pin))
 same => n,Goto(custom-mfu-call-through,s,1)
 same => n(allowed),NoOp(Caller ID whitelist ALLOW: ${CALLERID(name)} <${NORMALIZED_CID}> -> Ring Group 106)
 same => n,GotoIf($["${NORMALIZED_CID}" = ""]?notify-allowed-anonymous)
 same => n,GotoIf($["${MFU_CALLER_REGISTERED}" = "1"]?notify-allowed-whitelist:notify-allowed-unregistered)
 same => n(notify-allowed-anonymous),Gosub(mfu-inbound-discord-notify,s,1(anonymous,ring_group_106))
 same => n,Goto(allowed-route)
 same => n(notify-allowed-whitelist),Gosub(mfu-inbound-discord-notify,s,1(whitelist,ring_group_106))
 same => n,Goto(allowed-route)
 same => n(notify-allowed-unregistered),Gosub(mfu-inbound-discord-notify,s,1(unregistered,ring_group_106))
 same => n(allowed-route),Goto(ext-group,106,1)
 same => n(blocked-anonymous),NoOp(Anonymous caller -> VoiceMail_Announce)
 same => n,Gosub(mfu-inbound-discord-notify,s,1(anonymous,voicemail_announce))
 same => n,Goto(app-announcement-1,s,1)
 same => n(blocked),NoOp(Caller ID whitelist BLOCK: ${CALLERID(name)} <${NORMALIZED_CID}> -> VoiceMail_Announce)
 same => n,Gosub(mfu-inbound-discord-notify,s,1(unregistered,voicemail_announce))
 same => n,Goto(app-announcement-1,s,1)
 same => n,Hangup()
 same => n(blacklisted),NoOp(Caller ID blacklist REJECT: <${NORMALIZED_CID}> -> Hangup cause 21)
 same => n,Set(MFU_BLACKLIST_NAME_B64=)
 same => n,Gosub(callerid-blacklist-entries,${NORMALIZED_CID},1)
 same => n,Set(MFU_BLACKLIST_NAME=${NORMALIZED_CID})
 same => n,ExecIf($["${MFU_BLACKLIST_NAME_B64}" != ""]?Set(MFU_BLACKLIST_NAME=${BASE64_DECODE(${MFU_BLACKLIST_NAME_B64})}))
 same => n,Set(CALLERID(name)=${BASE64_DECODE(QkzjgIA=)}${MFU_BLACKLIST_NAME})
 same => n,Set(MFU_CDR_DID=${FILTER(0-9,${FROM_DID})})
 same => n,ExecIf($["${MFU_CDR_DID}" = ""]?Set(MFU_CDR_DID=${FILTER(0-9,${CDR(did)})}))
 same => n,Set(MFU_CDR_ID=${FILTER(0-9.,${UNIQUEID})})
 same => n,Set(MFU_BLACKLIST_ACTION=hangup)
 same => n,GosubIf($["${DIALPLAN_EXISTS(callerid-blacklist-actions,${NORMALIZED_CID},1)}" = "1"]?callerid-blacklist-actions,${NORMALIZED_CID},1)
 same => n,Set(CDR_PROP(disable)=1)
 same => n,Set(MFU_BLACKLIST_START_EPOCH=${EPOCH})
 same => n,Set(CHANNEL(hangup_handler_push)=mfu-blacklist-cdr-finalize,s,1(${NORMALIZED_CID},${MFU_BLACKLIST_NAME_B64},${MFU_CDR_DID},${MFU_CDR_ID},${MFU_BLACKLIST_ACTION},${MFU_BLACKLIST_START_EPOCH}))
 same => n,GotoIf($["${MFU_BLACKLIST_ACTION}" = "ring_until_caller_hangup"]?blacklist-ring-until)
 same => n,GotoIf($["${MFU_BLACKLIST_ACTION}" = "ring_15"]?blacklist-ring-15)
 same => n,GotoIf($["${MFU_BLACKLIST_ACTION}" = "busy"]?blacklist-busy:blacklist-hangup)
 same => n(blacklist-hangup),Gosub(mfu-inbound-discord-notify,s,1(blacklist,hangup_21))
 same => n,Hangup(21)
 same => n(blacklist-ring-until),Gosub(mfu-inbound-discord-notify,s,1(blacklist,blacklist_ring_until))
 same => n,Ringing()
 same => n(blacklist-ring-loop),Wait(3600)
 same => n,Goto(blacklist-ring-loop)
 same => n(blacklist-ring-15),Set(MFU_RING15_LAST=${DB(mfu-blacklist-ring15/${NORMALIZED_CID})})
 same => n,GotoIf($["${MFU_RING15_LAST}" = ""]?blacklist-ring-15-first)
 same => n,GotoIf($[${EPOCH}-${MFU_RING15_LAST} < 45]?blacklist-ring-15-retry:blacklist-ring-15-first)
 same => n(blacklist-ring-15-first),Set(DB(mfu-blacklist-ring15/${NORMALIZED_CID})=${EPOCH})
 same => n,Gosub(mfu-inbound-discord-notify,s,1(blacklist,blacklist_ring_15))
 same => n,Ringing()
 same => n,Wait(15)
 ; Cause 19 (no answer) caused the upstream trunk to originate an immediate
 ; second inbound call. Cause 21 ends the attempt without carrier retry.
 same => n,Hangup(21)
 same => n(blacklist-ring-15-retry),NoOp(Suppress upstream retry for ${NORMALIZED_CID})
 same => n,Hangup(21)
 same => n(blacklist-busy),Gosub(mfu-inbound-discord-notify,s,1(blacklist,blacklist_busy))
 same => n,Hangup(17)
; END CALLER ID WHITELIST'''

text = text[:start] + replacement + text[end:]
include = "#include mfu_inbound_discord.conf"
if include not in text:
    text = text.rstrip() + "\n\n" + include + "\n"
path.write_text(text, encoding="utf-8")
PY

/usr/sbin/asterisk -rx 'dialplan reload'
echo "Installed MFU all-inbound Discord notifications; backup: $BACKUP"
