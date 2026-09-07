from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.invoice.received_services import (
    RECEIVED_STATUSES,
    calculate_item,
)


ROOT = Path(__file__).resolve().parents[1]


def test_received_invoice_tax_calculation_defaults_to_internal_tax():
    item = calculate_item(
        {
            "item_name": "交通費",
            "quantity": "2",
            "unit_price_yen": "500",
            "tax_category": "tax10",
        }
    )
    assert item["quantity"] == Decimal("2.00")
    assert item["line_subtotal_yen"] == 909
    assert item["line_tax_yen"] == 91
    assert item["line_total_yen"] == 1000


def test_received_invoice_external_tax_adds_tax_to_entered_amount():
    item = calculate_item(
        {"item_name": "交通費", "quantity": "2", "unit_price_yen": "500", "tax_category": "tax10"},
        "external",
    )
    assert item["line_subtotal_yen"] == 1000
    assert item["line_tax_yen"] == 100
    assert item["line_total_yen"] == 1100


def test_received_invoice_workflow_statuses_are_available():
    for status in ("draft", "sent", "editing", "submitted", "revision_requested", "accepted", "paid", "cancelled"):
        assert status in RECEIVED_STATUSES


def test_locked_admin_values_are_enforced_by_server_and_public_flow_requires_otp():
    service = (ROOT / "invoice" / "received_services.py").read_text(encoding="utf-8")
    routes = (ROOT / "invoice" / "received_routes.py").read_text(encoding="utf-8")
    assert 'item["source"] == "admin" and int(item["locked_by_admin"])' in service
    assert "_require_grant(row)" in routes
    assert "verify_received_otp" in routes


def test_received_invoice_has_pdf_versions_and_no_freee_sync():
    service = (ROOT / "invoice" / "received_services.py").read_text(encoding="utf-8")
    assert "received_invoice_versions" in service
    assert "freee" not in service.lower()


def test_public_template_supports_expenses_attachments_preview_and_submit():
    template = (ROOT / "invoice" / "template" / "received_invoice_public.html").read_text(encoding="utf-8")
    assert 'name="item_id[]"' in template
    assert 'id="addItem"' in template
    assert 'id="addMemo"' in template
    assert 'name="attachments"' in template
    assert 'id="attachmentPicker"' in template
    assert 'multiple accept=' in template
    assert "let selected=[]" in template
    assert "new DataTransfer()" in template
    assert "data.delete('attachments')" in template
    assert "_mfuAttachments.appendTo(data)" in template
    assert "_mfuAttachments.commitSaved()" in template
    assert 'id="previewSaveStatus"' in template
    assert "fetch(form.getAttribute('action')" in template
    assert "fetch(form.action" not in template
    assert 'p-3 h-100' not in template
    assert 'id="previewInvoice"' in template
    assert 'value="submit"' in template


def test_received_invoice_pdf_reuses_standard_template_and_tax_labels():
    service = (ROOT / "invoice" / "received_services.py").read_text(encoding="utf-8")
    pdf = (ROOT / "invoice" / "pdf.py").read_text(encoding="utf-8")
    template = (ROOT / "invoice" / "template" / "invoice_pdf.html").read_text(encoding="utf-8")
    assert 'render_template("invoice_pdf.html"' in service
    assert "_build_font_css" in service
    assert 'tax_label = "内消費税"' in pdf
    assert "pdf.tax_total_label" in template


def test_every_received_invoice_email_appends_signature():
    service = (ROOT / "invoice" / "received_services.py").read_text(encoding="utf-8")
    assert "append_signature=False" not in service
    assert service.count("append_signature=True") == 7


def test_received_invoice_guidance_templates_are_reusable_and_snapshotted():
    service = (ROOT / "invoice" / "received_services.py").read_text(encoding="utf-8")
    routes = (ROOT / "invoice" / "received_routes.py").read_text(encoding="utf-8")
    form = (ROOT / "invoice" / "template" / "received_invoice_form.html").read_text(encoding="utf-8")
    assert "received_invoice_guidance_templates" in service
    assert "get_default_guidance_template" in service
    assert 'body += f"\\n\\nご案内:' in service
    assert "received_guidance_template_list" in routes
    assert 'id="guidanceTemplateSelect"' in form
    assert 'id="applyGuidanceTemplate"' in form
    assert "guidanceTemplates.find" in form
