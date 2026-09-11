from unittest.mock import Mock, patch

from app.etc_accounting import browser_session


def test_existing_x_socket_is_ready_even_if_supplied_pid_ended():
    socket_path = Mock()
    socket_path.exists.return_value = True
    with (
        patch.object(browser_session, "Path", return_value=socket_path),
        patch.object(browser_session, "_process_alive", return_value=False),
    ):
        browser_session._wait_xvfb_ready(999999, timeout=0.1)
