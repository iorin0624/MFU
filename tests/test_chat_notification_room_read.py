"""Execute the production notification functions against isolated fixtures.

AST extraction avoids starting the production Flask app/background workers.
The room UPDATE is executed as SQL, not merely checked as a source string.
"""
import ast
from datetime import datetime
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


SOURCE = Path(__file__).resolve().parents[1] / 'external_login_user/notifications.py'


def load_function(name, namespace):
    tree = ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(SOURCE), 'exec'), namespace)
    return namespace[name]


class Cursor:
    def __init__(self, connection):
        self.cursor = connection.cursor()

    def execute(self, sql, params):
        self.cursor.execute(sql.replace('%s', '?'), params)

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row else None

    def close(self):
        self.cursor.close()


class RoomReadTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('''CREATE TABLE mfu_notifications (
            id INTEGER PRIMARY KEY, user_kind TEXT, recipient_key TEXT,
            kind TEXT, chat_room_id TEXT, room_id TEXT, read_at TEXT)''')
        self.payload = {'room_id': 'main-1', 'event_id': 1}
        self.emit = Mock()
        self.ns = dict(
            datetime=datetime, _ensure_notification_schema=lambda: None,
            _require_mfu_admin_acl=lambda: ('admin', None),
            request=SimpleNamespace(get_json=lambda **kw: self.payload),
            jsonify=lambda value: value,
            get_db=lambda: SimpleNamespace(cursor=lambda **kw: Cursor(self.connection), commit=self.connection.commit, close=lambda: None),
            _compute_unread_counts_mfu=lambda user: {'total': self.connection.execute("SELECT COUNT(*) FROM mfu_notifications WHERE user_kind='mfu' AND recipient_key=? AND read_at IS NULL", (user,)).fetchone()[0]},
            _emit_notif_unread_mfu=self.emit, current_app=SimpleNamespace(logger=Mock()),
        )
        self.read_room = load_function('api_mfu_notifications_mark_read_by_room', self.ns)

    def tearDown(self):
        self.connection.close()

    def add(self, kind='event_chat', chat_room=None, room='main-1', recipient='admin', user_kind='mfu', read_at=None):
        self.connection.execute('INSERT INTO mfu_notifications VALUES (NULL,?,?,?,?,?,?)', (user_kind, recipient, kind, chat_room, room, read_at))

    def test_legacy_admin_alias_room_id_is_read_and_zero_broadcast(self):
        self.add()
        result = self.read_room()
        self.assertEqual(result['updated_count'], 1)
        self.assertEqual(result['unread_count'], 0)
        self.emit.assert_called_once_with('admin', reason='room_read', latest_id=1)

    def test_canonical_and_empty_legacy_column(self):
        self.add(chat_room='main-1', room=None)
        self.add(chat_room='')
        self.assertEqual(self.read_room()['updated_count'], 2)

    def test_other_rooms_recipients_and_notices_are_not_read(self):
        self.add()
        self.add(room='sub-1')
        self.add(room='main-2')
        self.add(recipient='account-b')
        self.add(user_kind='external')
        self.add(kind='general')
        self.add(chat_room='sub-1', room='main-1')
        result = self.read_room()
        self.assertEqual(result['updated_count'], 1)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM mfu_notifications WHERE read_at IS NULL').fetchone()[0], 6)

    def test_dm_only_reads_requested_dm(self):
        self.add(kind='dm', room='dm:one')
        self.add(kind='dm', room='dm:two')
        self.add()
        self.payload = {'room_id': 'dm:one'}
        self.assertEqual(self.read_room()['updated_count'], 1)

    def test_repeat_is_idempotent(self):
        self.add()
        self.read_room()
        self.assertEqual(self.read_room()['updated_count'], 0)

    def test_missing_room_rejected(self):
        self.payload = {}
        self.assertEqual(self.read_room()[1], 400)

    def test_auth_guard_preserved(self):
        self.ns['_require_mfu_admin_acl'] = lambda: (None, ({'ok': False}, 403))
        self.assertEqual(self.read_room()[1], 403)


class NotificationWriteTests(unittest.TestCase):
    def write(self, kind, room_id=None, chat_room_id=None):
        cursor = Mock(rowcount=1, lastrowid=1)
        db = Mock()
        db.cursor.return_value = cursor
        ns = dict(datetime=datetime, _CHAT_NOTIFICATION_KINDS=('chat_message', 'event_chat', 'dm'),
            _ensure_notification_schema=lambda: None,
            _notification_recipient_key=lambda *a: 'admin',
            _notification_storage_user_id=lambda *a: 1,
            get_db=lambda: db, _emit_notif_unread_mfu=Mock(), _emit_notif_new_mfu=Mock(),
            current_app=SimpleNamespace(logger=Mock()))
        create = load_function('_create_notification_core', ns)
        result = create(user_kind='mfu', user_id=1, recipient_key='admin', kind=kind,
            title='test', body='', target_url='/', dedup_key='test:1', room_id=room_id, chat_room_id=chat_room_id)
        self.assertTrue(result['ok'])
        return cursor.execute.call_args.args[1]

    def test_new_event_and_dm_writes_fill_both_columns(self):
        for kind, room in [('event_chat', 'main-1'), ('dm', 'dm:one'), ('chat_message', 'sub-1')]:
            with self.subTest(kind=kind):
                values = self.write(kind, room_id=room)
                self.assertEqual(values[9], room)
                self.assertEqual(values[11], room)

    def test_existing_canonical_id_preserved(self):
        self.assertEqual(self.write('event_chat', room_id='old', chat_room_id='new')[9], 'new')

    def test_non_chat_room_is_not_made_into_chat(self):
        self.assertIsNone(self.write('general', room_id='other')[9])


if __name__ == '__main__':
    unittest.main()
