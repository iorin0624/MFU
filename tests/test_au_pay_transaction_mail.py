from pathlib import Path
import importlib.util
import sys


ROOT = Path(__file__).resolve().parents[1]
SENDER_DIR = ROOT / "tools" / "mfu_notification_sender"


def _module():
    sys.path.insert(0, str(SENDER_DIR))
    spec = importlib.util.spec_from_file_location("au_pay_transaction_mail", SENDER_DIR / "au_pay_transaction_mail.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_mail(path: Path, subject: str, body: str, *, authenticated: bool = True):
    authentication = (
        "Authentication-Results: mail.iori0624.jp; spf=pass; dkim=pass "
        "header.d=wallet.auone.jp; dmarc=pass header.from=wallet.auone.jp\n"
        if authenticated else ""
    )
    path.write_text(
        "From: au PAY <info@wallet.auone.jp>\n"
        f"Subject: {subject}\n"
        "Message-ID: <au-pay-test@example.invalid>\n"
        f"{authentication}"
        "Content-Type: text/plain; charset=UTF-8\n\n"
        f"{body}\n",
        encoding="utf-8",
    )


def _fields(event):
    return {row["name"]: row["value"] for row in event["embeds"][0]["fields"]}


def test_au_pay_usage_section_format(tmp_path):
    module = _module()
    eml = tmp_path / "usage.eml"
    _write_mail(eml, "【au PAY】ご利用のお知らせ [メールコードP3001]", """■利用店舗
Steam
■種別
支払い
■利用日時
2026年10月12日(月) 15:45:41
■支払い金額
1,615円
■決済後残高
67,321円""")
    event = module.parse_au_pay_transaction(eml)
    assert event["topic_key"] == "au-pay-usage"
    assert _fields(event) == {
        "利用日時": "2026年10月12日(月) 15:45:41",
        "店舗名": "Steam",
        "利用金額": "1,615円",
        "残高": "67,321円",
    }
    assert "取消" not in event["embeds"][0]["title"]


def test_au_pay_refund_without_balance(tmp_path):
    module = _module()
    eml = tmp_path / "refund.eml"
    _write_mail(eml, "【au PAY】ご利用のお知らせ [メールコードP3006]", """au PAYの返金が完了しました。
■利用店舗
Steam
■種別
支払い取消(全額取消)
■利用日時
2026年10月12日(月) 21:29:59
■支払い取消金額
-1,615円""")
    event = module.parse_au_pay_transaction(eml)
    assert "取消・返金" in event["embeds"][0]["title"]
    assert _fields(event)["利用金額"] == "-1,615円"
    assert _fields(event)["残高"] == "記載なし"


def test_au_pay_prepaid_usage_not_mistaken_for_refund_and_charge(tmp_path):
    module = _module()
    usage = tmp_path / "prepaid.eml"
    _write_mail(usage, "【au PAY】ご利用のお知らせ[メールコードP1104]", """・ご利用日時：2026/10/12 19:54:04
・ご利用店舗：ブランドプリペイド加盟店
・ご利用金額：12,884円
・au PAY 残高：54,437円
※返品をされた場合はご返金に最長45日かかることがございます。""")
    event = module.parse_au_pay_transaction(usage)
    assert "利用速報" in event["embeds"][0]["title"]
    assert _fields(event)["残高"] == "54,437円"

    charge = tmp_path / "charge.eml"
    _write_mail(charge, "【au PAY】チャージ完了のお知らせ[メールコードC1101]", """・チャージ日時：2026/10/12 15:30:58
・チャージ金額：13,000円
・チャージ方法：セブン銀行ATM
・au PAY 残高：68,936円""")
    event = module.parse_au_pay_transaction(charge)
    assert _fields(event) == {
        "日時": "2026/10/12 15:30:58",
        "方法": "セブン銀行ATM",
        "金額": "13,000円",
        "残高": "68,936円",
    }


def test_au_pay_transaction_requires_authenticated_sender(tmp_path):
    module = _module()
    eml = tmp_path / "unauthenticated.eml"
    _write_mail(eml, "【au PAY】ご利用のお知らせ[メールコードP1104]", "", authenticated=False)
    assert module.parse_au_pay_transaction(eml) is None
