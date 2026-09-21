from inpa_app.admin_auth import canonical_request


def test_admin_canonical_request_includes_body_hash():
    result = canonical_request("post", "/internal/admin/v1/seasons", "123", "nonce", b'{"a":1}', "admin")
    assert result.decode().splitlines() == [
        "POST", "/internal/admin/v1/seasons", "123", "nonce",
        "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862", "admin",
    ]
