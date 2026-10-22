# MFU SC download protocol v2 (server-side groundwork)

This protocol is opt-in. Existing pages create protocol 1 jobs and existing
Shortcuts continue to use the old manifest and completion behavior. The server
must not enable protocol 2 in those pages until a compatible Shortcut ships.
The administrator's current/minimum Shortcut versions are unchanged by this
server change.

## Browser flow

1. `POST /mobile-download/api/jobs` with the existing source/selection fields
   plus `"protocol_version": 2` and the normal browser CSRF token.
2. Keep the returned `job_id` and `resume_token` private. The resume token is
   returned only once and its hash is stored server-side. The browser must use
   `Cache-Control: no-store` semantics for this response.
3. `GET /mobile-download/api/jobs/{job_id}/progress` with
   `X-MFU-Resume-Token: <resume_token>` to display server-confirmed progress.
   Source access is checked again, including current login/view grants.
4. `POST /mobile-download/api/jobs/{job_id}/resume` with the resume-token and
   normal browser CSRF headers. Body: `{"retry_uncertain": ["<file_id>", ...]}`.
   Omit uncertain IDs unless the user explicitly accepts possible duplicates.
   The response contains a fresh, one-use launch link and the same logical
   job ID. Any previous bearer/launch token is invalidated. Original output
   names, including sequential numbers, are not renumbered.

The resume secret is not accepted in a URL or query string. A missing/expired
source, revoked grant, missing file, or expired 7-day progress record prevents
resume. Progress retention does not extend the source media's lifetime.

## Shortcut flow (client implementation to follow)

1. Exchange the one-use launch token as before, sending
   `X-MFU-Shortcut-Version: 2` or a newer supported version. A protocol 2
   job refuses older or unversioned Shortcuts. The returned manifest contains
   pending/saving/failed files and uncertain files explicitly approved for
   retry; confirmed `saved` files are never included.
2. For each file, call
   `POST /mobile-download/api/jobs/{job_id}/files/{file_id}/result` with the
   bearer token and `{"state":"saving"}` before fetching the media.
3. Fetch the manifest's `download_url` with the bearer token. A v2 file GET
   requires the `saving` state.
4. After the Photos save action succeeds, report `{"state":"saved"}`.
   For a definite failure, report `{"state":"failed","error_code":"..."}`.
   Error codes are short lowercase ASCII identifiers, not exception text.
5. Call the existing `POST /mobile-download/api/jobs/{job_id}/complete` only
   after all files are saved. The server also marks the job completed as soon
   as it commits the final `saved` result, so a crash immediately afterwards
   cannot strand a fully saved job. The complete call is idempotent for v2 and
   returns HTTP 409 with progress if any file remains unconfirmed. Protocol 1
   completion is unchanged.

The result endpoint is idempotent for duplicate states. A `saved` file cannot
be downgraded. `saving` becomes `uncertain` at the next resume or when an
expired session's progress is read. The server records a client-reported save,
not an independent inspection of the iPhone Photos library.

Per-file result responses contain aggregate counts and the changed file only;
use the separate progress GET for the full file list. This keeps large batches
from transmitting the entire manifest after every photo.

The server emits `shortcut_progress_update` after committed result/complete
changes. A foreground browser must refetch the progress endpoint; WebSocket
messages are hints, not the source of truth.
