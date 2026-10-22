from scripts.migrate_shortcut_url_messages import (
    PLACEHOLDER,
    replace_shortcut_url_lines,
)


def test_replaces_only_standalone_shortcut_url_lines():
    original = (
        "ショートカットを入手\n"
        "https://www.icloud.com/shortcuts/AbC123\n"
        "別のリンク: https://example.com/shortcuts/AbC123\n"
    )
    result, urls = replace_shortcut_url_lines(original)
    assert result == (
        f"ショートカットを入手\n{PLACEHOLDER}\n"
        "別のリンク: https://example.com/shortcuts/AbC123\n"
    )
    assert urls == ["https://www.icloud.com/shortcuts/AbC123"]


def test_preserves_windows_line_endings_and_indentation():
    result, urls = replace_shortcut_url_lines(
        "案内\r\n  https://www.icloud.com/shortcuts/123abc  \r\n"
    )
    assert result == f"案内\r\n  {PLACEHOLDER}  \r\n"
    assert len(urls) == 1


def test_rejects_embedded_shortcut_urls_instead_of_rewriting_unrelated_text():
    try:
        replace_shortcut_url_lines(
            "詳しくは https://www.icloud.com/shortcuts/AbC123 を参照"
        )
    except ValueError:
        pass
    else:
        raise AssertionError("embedded Shortcut URLs must be reviewed manually")
