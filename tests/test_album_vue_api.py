import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_PATH = ROOT / "albums" / "api.py"
ROUTES_PATH = ROOT / "albums" / "routes.py"
APP_PATH = ROOT / "__init__.py"


def function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    node = next(
        item
        for item in ast.walk(tree)
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
    )
    return ast.get_source_segment(source, node) or ""


class AlbumVueApiSourceTests(unittest.TestCase):
    def test_required_endpoints_are_registered(self):
        source = API_PATH.read_text(encoding="utf-8-sig")
        expected = (
            '"/api/session"',
            '"/api/albums"',
            '"/api/albums/<album_id>/authenticate"',
            '"/api/albums/<album_id>/children"',
            '"/api/albums/<album_id>/children/<child_id>/media"',
            '"/api/albums/<album_id>/children/<child_id>/processing"',
            '"/api/albums/<album_id>/children/<child_id>/processing/begin"',
            '"/api/albums/<album_id>/children/<child_id>/processing/latest"',
            '"/api/albums/<album_id>/children/<child_id>/processing/unlock"',
            '"/api/albums/<album_id>/children/<child_id>/processing/force-unlock"',
            '"/api/albums/<album_id>/download-jobs"',
        )
        for route in expected:
            self.assertIn(route, source)

    def test_event_access_is_rechecked_and_withdrawal_revokes_album_session(self):
        source = function_source(API_PATH, "_album_context")
        self.assertIn("_is_event_member_approved", source)
        self.assertIn("_revoke_album_auth(album_id)", source)
        self.assertIn('access_mode == "event"', source)

    def test_authenticate_never_accepts_token_for_event_album(self):
        source = function_source(API_PATH, "api_album_authenticate")
        event_branch = source.index('ctx["gate"].get("access_mode") == "event"')
        token_branch = source.index('payload = request.get_json')
        self.assertLess(event_branch, token_branch)
        self.assertIn("_is_event_member_approved", source[event_branch:token_branch])

    def test_album_delete_keeps_management_and_admin_step_up(self):
        source = function_source(API_PATH, "api_album_delete")
        self.assertIn("_require_manage(ctx)", source)
        self.assertIn("require_admin_passkey", source)

    def test_child_destructive_routes_use_child_permissions_and_admin_step_up(self):
        expectations = {
            "api_album_child_delete": "canDeleteChild",
            "api_album_media_delete": "canDeleteMedia",
        }
        for name, permission in expectations.items():
            source = function_source(API_PATH, name)
            self.assertIn("_require_child_permission", source)
            self.assertIn(permission, source)
            self.assertIn('ctx["is_admin"]', source)
            self.assertIn("require_admin_passkey", source)

    def test_child_creator_permissions_require_current_active_event_member(self):
        source = function_source(API_PATH, "_child_permissions")
        self.assertIn("created_by_ext_user_id", source)
        self.assertIn("current_ext_user_id", source)
        self.assertIn('ctx.get("event_member")', source)

    def test_event_member_child_creation_records_external_user_id(self):
        source = function_source(API_PATH, "api_album_child_create")
        self.assertIn('ctx["can_create_child"]', source)
        self.assertIn("created_by_ext_user_id=creator_id", source)

    def test_existing_workflows_are_reused_for_upload_processing_and_zip(self):
        self.assertIn("upload_child(album_id, child_id)", function_source(API_PATH, "api_album_media_upload"))
        self.assertIn("request_process(album_id, child_id)", function_source(API_PATH, "api_album_processing_requests"))
        self.assertIn("update_process_status(album_id, child_id, payload)", function_source(API_PATH, "api_album_processing_member"))
        self.assertIn("start_zip_entries_job", function_source(API_PATH, "api_album_download_job_create"))

    def test_event_member_can_only_update_own_completion_state(self):
        source = function_source(API_PATH, "api_album_processing_member")
        self.assertIn('not ctx["can_manage"] and current_ext_user_id != ext_user_id', source)
        self.assertIn("_fetch_event_process_members", source)

    def test_processing_latest_filename_cannot_be_renamed(self):
        source = function_source(API_PATH, "api_album_media_rename")
        self.assertIn('child.get("mode") == "process"', source)
        self.assertIn("process_media_rename_not_allowed", source)

    def test_process_replacement_upload_requires_current_lock_holder(self):
        source = function_source(API_PATH, "api_album_media_upload")
        self.assertIn('child.get("mode") == "process"', source)
        self.assertIn("currentUserHoldsLock", source)
        self.assertIn("processing_lock_required", source)

    def test_processing_begin_reuses_lock_and_latest_media_helpers(self):
        source = function_source(API_PATH, "api_album_processing_begin")
        self.assertIn("find_latest_filename", source)
        self.assertIn("try_acquire_lock_db", source)
        self.assertIn("LOCK_TTL_SEC", source)

    def test_processing_latest_requires_lock_or_album_manager(self):
        source = function_source(API_PATH, "api_album_processing_latest")
        self.assertIn("currentUserHoldsLock", source)
        self.assertIn('ctx["can_manage"]', source)

    def test_processing_unlock_and_force_unlock_have_distinct_permissions(self):
        normal = function_source(API_PATH, "api_album_processing_unlock")
        force = function_source(API_PATH, "api_album_processing_force_unlock")
        self.assertIn("canUnlock", normal)
        self.assertIn('ctx["is_admin"]', force)

    def test_withdrawn_users_are_hidden_from_processing_history(self):
        source = function_source(API_PATH, "_processing_payload")
        self.assertIn("is_withdrawn_ext_user(item)", source)

    def test_event_before_request_returns_json_for_all_api_routes(self):
        source = function_source(ROUTES_PATH, "_enforce_event_album_access")
        self.assertIn('request.path.startswith("/album/api/")', source)
        self.assertIn('"album.api_album_authenticate"', source)

    def test_page_size_is_bounded(self):
        source = function_source(API_PATH, "_paginate")
        self.assertIn("API_MAX_PAGE_SIZE", source)
        self.assertIn("hasNext", source)
        self.assertIn("hasPrevious", source)

    def test_mutations_are_csrf_protected_and_errors_remain_json(self):
        source = APP_PATH.read_text(encoding="utf-8-sig")
        self.assertIn('"/album/api/",', source)
        error_source = function_source(APP_PATH, "_is_json_error_response")
        self.assertIn('request.path.startswith("/album/api/")', error_source)
        session_source = function_source(API_PATH, "api_album_session")
        self.assertIn("csrfToken=_csrf_token()", session_source)


if __name__ == "__main__":
    unittest.main()
