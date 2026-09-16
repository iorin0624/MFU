from utils.shortcut_version import evaluate_shortcut_version


def evaluate(value, *, current=3, minimum=1, allow_unversioned=True):
    return evaluate_shortcut_version(
        value,
        current_version=current,
        minimum_supported_version=minimum,
        allow_unversioned=allow_unversioned,
    )


def test_unversioned_shortcut_can_be_temporarily_allowed():
    result = evaluate(None)
    assert result["ok"] is True
    assert result["client_version"] == 0
    assert result["reason"] == "legacy_allowed"


def test_unversioned_shortcut_can_be_rejected():
    result = evaluate(None, allow_unversioned=False)
    assert result["ok"] is False
    assert result["reason"] == "version_required"


def test_supported_range_is_inclusive():
    assert evaluate("1")["ok"] is True
    assert evaluate(3)["ok"] is True


def test_versions_outside_supported_range_are_rejected():
    assert evaluate("0", allow_unversioned=False)["reason"] == "version_too_old"
    assert evaluate("4")["reason"] == "version_too_new"
    assert evaluate("not-a-number")["reason"] == "invalid_version"
