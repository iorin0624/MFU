# MFU Chat Desktop

MFU Chat Desktop is a native Windows chat client for the existing MFU Flask-SocketIO chat. It does not embed `/chat` in a WebView. It talks to MFU through `/chat/api/gui/*`, the existing chat HTTP APIs, and the existing Socket.IO events.

## Install

```powershell
cd tools\mfu_chat_desktop
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Environment

Copy `.env.example` to `.env` for local use and adjust:

```env
MFU_BASE_URL=https://mfu.iori0624.jp
MFU_SOCKET_PATH=/socket.io
APP_NAME=MFU Chat Desktop
```

Do not commit `.env`.

## Run

```powershell
python main.py
```

On startup the app calls `GET /chat/api/gui/session`. If the saved cookie is valid, it opens the main window. Otherwise it shows the login dialog and calls `POST /chat/api/gui/login`.

Passwords are only saved when the user opts in, and are stored through `keyring` in Windows Credential Manager. Cookies and app settings are stored under the per-user `platformdirs` config directory.

## Build EXE

```powershell
pyinstaller build/mfu_chat_desktop.spec
```

The generated executable is intended for Windows.

## Server APIs Added

- `GET /chat/api/gui/session`
- `POST /chat/api/gui/login`
- `GET /chat/api/gui/bootstrap`
- `GET /chat/api/gui/events`
- `GET /chat/api/gui/events/<event_id>/snapshot`
- `GET /chat/api/gui/events/<event_id>/messages`
- `GET /chat/api/gui/events/<event_id>/search`
- `GET /chat/api/gui/events/<event_id>/rooms`
- `GET /chat/api/gui/dm/inbox`
- `GET /chat/api/gui/dm/<dm_uuid>/snapshot`
- `GET /chat/api/gui/dm/<dm_uuid>/messages`
- `GET /chat/api/gui/dm/<dm_uuid>/search`

Existing APIs are reused for upload, edit, delete, room management, members, mentions, mute, presence, and thread loading.

## Known Limits

- MFA-enabled MFU accounts return `mfa_required` from the GUI login API. Use an existing browser login/session or extend the desktop app with the existing OTP flow before using those accounts remotely.
- Thread display and room member editing have UI placeholders wired for future expansion; the existing server APIs are preserved and reusable.
- Windows toast click-to-open is dependent on Windows notification registration and may need installer-level AppUserModelID work for production packaging.

## Troubleshooting

- Login fails: verify `MFU_BASE_URL`, credentials, and whether MFA is required.
- Socket does not connect: verify `MFU_SOCKET_PATH` and Apache/gunicorn Socket.IO proxy settings.
- Images fail: check server upload limits and file type. HEIC/HEIF is intentionally blocked in the desktop preview.
- Notifications do not appear: confirm Windows notification settings and the app notification toggle.
