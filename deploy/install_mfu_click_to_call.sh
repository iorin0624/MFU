#!/usr/bin/env bash
set -euo pipefail

stamp=$(date +%Y%m%d%H%M%S)
cp -a /etc/asterisk/extensions_custom.conf "/etc/asterisk/extensions_custom.conf.bak.${stamp}"
cp -a /usr/local/sbin/mfu-whitelist-gateway "/usr/local/sbin/mfu-whitelist-gateway.bak.${stamp}"
cp -a /etc/sudoers.d/mfu-whitelist "/etc/sudoers.d/mfu-whitelist.bak.${stamp}"

install -o root -g root -m 0755 /tmp/mfu-click-to-call /usr/local/sbin/mfu-click-to-call
install -o root -g root -m 0755 /tmp/mfu-whitelist-gateway /usr/local/sbin/mfu-whitelist-gateway
install -o root -g root -m 0440 /tmp/mfu-whitelist.sudoers /etc/sudoers.d/mfu-whitelist
install -o root -g asterisk -m 0640 /tmp/mfu_click_to_call.conf /etc/asterisk/mfu_click_to_call.conf

if ! grep -Fq 'U(mfu-click-to-call-answered^${MFU_C2C_JOB})' /etc/asterisk/extensions_custom.conf; then
  sed -i \
    '/mfu-rtp-diagnostics-attach,s,1/a\ same => n,ExecIf($["${MFU_C2C_JOB}"!=""]?Set(DIAL_TRUNK_OPTIONS=${DIAL_TRUNK_OPTIONS}U(mfu-click-to-call-answered^${MFU_C2C_JOB})))' \
    /etc/asterisk/extensions_custom.conf
fi

if ! grep -Fxq '#include mfu_click_to_call.conf' /etc/asterisk/extensions_custom.conf; then
  printf '\n#include mfu_click_to_call.conf\n' >> /etc/asterisk/extensions_custom.conf
fi

/usr/bin/python3 -m py_compile /usr/local/sbin/mfu-click-to-call
/bin/bash -n /usr/local/sbin/mfu-whitelist-gateway
/usr/sbin/visudo -cf /etc/sudoers.d/mfu-whitelist
/usr/sbin/asterisk -rx 'dialplan reload'
