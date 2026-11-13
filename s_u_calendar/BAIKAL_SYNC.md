# SUC calendar source

The source of truth is Baïkal's CalDAV collections for user `iori0624`:

- `default` (メインカレンダー): non-cancelled events mark dates busy, including all-day events with `TRANSP:TRANSPARENT`.
- `japan-holidays` (日本の祝日): names are displayed as holidays; they never mark dates busy.

MFU never reads the Baïkal database. A single server-side systemd timer checks
both collection sync tokens once per minute. If a token changes, or at least
once per day, it fetches and parses the calendar objects and publishes a
snapshot to MFU's own `su_calendar_days` / `su_calendar_holidays` tables.
CalDAV/authentication/parser errors leave the last successful snapshot intact.
Only rows with `synced_busy=1` may be cleared by automatic sync. Manually
marked busy rows (`synced_busy=0`) are preserved.

The rolling busy range is the current month through the eleventh following month.
Changing that range forces a full refresh even if CalDAV sync tokens are unchanged.
Holiday names are cached for the current and following year. View requests
read MFU's local snapshot; they do not query CalDAV themselves.

The Baïkal password is not in this repository. Production reads it from
`/mnt/mfu/secrets/suc-baikal-password` (root-owned, group `mfu`, mode `0640`).
Install dependencies from `requirements.txt` in the MFU Python environment.
Install the two units in `deploy/systemd/` under `/etc/systemd/system/`, then
enable `mfu-suc-baikal-sync.timer`. A dry run can be performed with:

```sh
cd /mnt/mfu
sudo -u mfu /mnt/mfu/venv/bin/python app/s_u_calendar/baikal_sync.py
```

Add `--apply` only after reviewing the reported added/cleared dates. Inspect
`journalctl -u mfu-suc-baikal-sync.service` for failures. The old Outlook route
paths remain as compatibility aliases, but they now use Baïkal, not Graph.
The legacy `sync_baikal_6m` and `sync_outlook_6m` API paths are also aliases
for the current 12-month sync; new callers should use `sync_baikal_12m`.
