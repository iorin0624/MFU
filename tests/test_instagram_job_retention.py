from pathlib import Path


ROUTES = Path(__file__).resolve().parents[1] / "image_viewer" / "routes.py"


def test_completed_instagram_jobs_survive_following_batch_requests():
    source = ROUTES.read_text(encoding="utf-8")

    assert "_INSTAGRAM_JOB_TTL_SECONDS = 6 * 60 * 60" in source
    assert "expired_finished = finished and now - created_at > _INSTAGRAM_JOB_TTL_SECONDS" in source
    assert "if expired_finished or stale_incomplete:" in source
    assert "if finished or stale_incomplete:" not in source
