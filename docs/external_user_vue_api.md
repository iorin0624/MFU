# Event participant Vue API

The current server-rendered participant pages remain available while the
mobile-first Vue client is developed. All responses are JSON and use the
existing LINE/MFU session cookie.

## Bootstrap

- `GET /external-login/api/vue/session`
- `GET /external-login/api/vue/bootstrap`

Both return the shared top navigation, current session, prerequisite state,
CSRF token and unread notification count. Send the returned token as
`X-CSRF-Token` for every mutation.

## Events

- `GET /external-login/api/vue/events?scope=all|upcoming|past&page=1&perPage=50`
- `GET /external-login/api/vue/events/<event_uuid>`
- `GET /external-login/api/vue/events/<event_uuid>/members`
- `PATCH /external-login/api/vue/events/<event_uuid>/my-role`

Membership, cancellation and event ACL are rechecked on every request. Event
members must be approved and active before chat, album or member-list links are
enabled. The existing payment and chat endpoints remain authoritative.

## Logout

- `POST /external-login/api/vue/logout`

The endpoint clears external-login and event-album authorization state.

## Child album ownership

New child albums store `created_by_ext_user_id`. Existing rows remain `NULL`;
ownership is never inferred from a display name. An active approved event
member may create a child album, then upload to, rename or delete that child
album and delete its media. Other participants remain view/download-only.
`admin`, the album owner and any account present in the event ACL can manage
all children.
