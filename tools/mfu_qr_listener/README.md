# MFU timer QR scanner

The MFU SE02 `mfu-qr-listener.service` runs `/mnt/mfu/qr_listener.py` with a
Honeywell USB keyboard-wedge scanner. Deploy `qr_listener.py` and
`scan_decoder.py` together into `/mnt/mfu/`, then restart that service.

The decoder preserves Shift-dependent letter case and URL punctuation. The
scanner reports HTTP(S) QR codes as `URL` to `/api/timer/scan-report`; JAN,
minute, second and START codes retain their existing timer actions. Opening a
scanned URL remains a deliberate button click in the timer admin page.
