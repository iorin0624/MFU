# Album Vue API

The API is served below `/album/api` and is designed for the incremental Vue
migration. The current server-rendered album pages remain available and use the
same database, storage paths and authorization state.

## Authentication and authorization

- Admin and album owners use the normal MFU login session.
- Token albums use `POST /album/api/albums/{album_id}/authenticate` with
  `{"token":"..."}`. Successful authentication grants the existing album
  session permission.
- Event albums never accept an album token. The current external-login user
  must be active and an approved, non-canceled event member.
- Event membership and withdrawal state are checked again on every API request.
- Destructive admin operations use the existing passkey step-up flow.
- Event-linked album names cannot be changed by the album API.
- All POST, PUT, PATCH and DELETE calls require the same-origin CSRF token from
  `GET /album/api/session` in `X-CSRF-Token`.

All responses are JSON. Successful responses contain `"ok": true`; error
responses contain `"ok": false`, a stable `error` code and an appropriate HTTP
status.

The reverse proxy must preserve those error bodies. The production Apache
fragment is tracked at `deploy/apache/mfu-album-api.conf` and is applied to both
IPv4 and IPv6 TLS virtual hosts.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/album/api/session` | Create/read the session CSRF token used by mutation requests |
| GET | `/album/api/albums` | Albums visible to the current admin, owner or event member |
| POST | `/album/api/albums/{album}/authenticate` | Token or event-member authentication |
| GET | `/album/api/albums/{album}` | Album metadata, event summary and permissions |
| PATCH | `/album/api/albums/{album}` | Rename a normal album (admin/owner) |
| DELETE | `/album/api/albums/{album}` | Permanently delete an album (admin/owner) |
| GET | `/album/api/albums/{album}/children` | Child albums and media counts |
| POST | `/album/api/albums/{album}/children` | Create a normal/process/movie child album |
| PATCH | `/album/api/albums/{album}/children/{child}` | Rename a child album |
| DELETE | `/album/api/albums/{album}/children/{child}` | Delete a child album |
| GET | `/album/api/albums/{album}/children/{child}/media` | Paginated photo/video list |
| POST | `/album/api/albums/{album}/children/{child}/media` | Upload one or more files using repeated `file` form fields |
| PATCH | `/album/api/albums/{album}/children/{child}/media/{name}` | Rename media without changing its extension |
| DELETE | `/album/api/albums/{album}/children/{child}/media` | Delete selected media names |
| GET | `/album/api/albums/{album}/children/{child}/processing` | Lock, history, processing targets and statuses |
| PUT | `/album/api/albums/{album}/children/{child}/processing/requests` | Save processing targets and send existing notifications |
| PUT | `/album/api/albums/{album}/children/{child}/processing/members/{user}` | Update one processing status |
| POST | `/album/api/albums/{album}/download-jobs` | Start a ZIP download job |
| GET | `/album/api/albums/{album}/download-jobs/{job}` | Read ZIP progress and completion URL |

### Media pagination

`GET .../media` accepts `page`, `perPage` (maximum 200), and `sort=asc|desc`.
The response contains `page`, `perPage`, `total`, `pages`, `hasNext`, and
`hasPrevious`.

### Permission payload

Album and child responses expose an explicit permission object so the Vue UI
can hide or disable controls without duplicating server policy. The server
still enforces every permission independently of the UI.
