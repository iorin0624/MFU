from pathlib import Path
import importlib.util


ROOT = Path(__file__).resolve().parents[1]
PARSER = ROOT / "tools" / "mfu_notification_sender" / "au_pay_otp_mail.py"


def _module():
    spec = importlib.util.spec_from_file_location("au_pay_otp_mail", PARSER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_mail(path: Path, *, authenticated: bool = True) -> None:
    authentication = (
        "Authentication-Results: mail.iori0624.jp; spf=pass; dkim=pass "
        "header.d=mail-jcn.dnp-cdms.jp; dmarc=pass\n"
        if authenticated else ""
    )
    path.write_text(
        "From: au PAY <info@mail-jcn.dnp-cdms.jp>\n"
        "To: mmz@mail.iori0624.jp\n"
        "Subject: ネットショッピング認証コードのお知らせ\n"
        "Message-ID: <otp-test@example.invalid>\n"
        f"{authentication}"
        "Content-Type: text/plain; charset=UTF-8\n"
        "\n"
        "■発行情報\n"
        "認証コード：686672\n"
        "認証コード有効期限：2026/10/07 11:56:12\n"
        "■ご利用情報\n"
        "ご利用加盟店名：Disney Mobile Order\n"
        "ご利用金額：JPY 400\n"
        "ご利用時刻：2026/10/07 11:46:12\n",
        encoding="utf-8",
    )


def test_au_pay_otp_mail_creates_fast_copy_action_without_exposing_code_to_discord(tmp_path):
    module = _module()
    eml = tmp_path / "otp.eml"
    _write_mail(eml)

    payload = module.parse_au_pay_otp(eml)

    assert payload["kind"] == "au_pay_otp"
    assert payload["actions"] == [{
        "type": "copy",
        "label": "ワンタイムパスワードをコピー",
        "value": "686672",
        "expires_at": "2026-10-07T11:56:12+09:00",
    }]
    serialized_embeds = str(payload["embeds"])
    assert "Disney Mobile Order" in serialized_embeds
    assert "JPY 400" in serialized_embeds
    assert "2026/10/07 11:46:12" in serialized_embeds
    assert "686672" not in serialized_embeds
    assert payload["dedup_key"].startswith("mail:au-pay-otp:")


def test_au_pay_otp_mail_requires_authenticated_sender(tmp_path):
    module = _module()
    eml = tmp_path / "unauthenticated.eml"
    _write_mail(eml, authenticated=False)

    assert module.parse_au_pay_otp(eml) is None
