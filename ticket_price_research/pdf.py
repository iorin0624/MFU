from __future__ import annotations

from pathlib import Path

from flask import current_app


class TicketPricePdfError(RuntimeError):
    pass


def _font_paths() -> tuple[Path, Path]:
    font_dir = Path(current_app.root_path) / "PDF_Font"
    regular = font_dir / "BIZUDPGothic-Regular.ttf"
    bold = font_dir / "BIZUDPGothic-Bold.ttf"
    missing = [str(path) for path in (regular, bold) if not path.is_file()]
    if missing:
        raise TicketPricePdfError(
            "チケット価格PDF用フォントが見つかりません: " + ", ".join(missing)
        )
    return regular.resolve(), bold.resolve()


def render_disney_ticket_pdf(payload: dict) -> bytes:
    if not payload.get("ok") or not payload.get("items"):
        raise TicketPricePdfError("チケット価格情報がないためPDFを生成できません")
    try:
        from weasyprint import CSS, HTML
        from weasyprint.text.fonts import FontConfiguration
    except Exception as exc:  # pragma: no cover - dependency boundary
        raise TicketPricePdfError("WeasyPrintを利用できません") from exc

    regular, bold = _font_paths()
    html = current_app.jinja_env.get_template(
        "ticket_price_research/disney_pdf.html"
    ).render(payload=payload)
    font_config = FontConfiguration()
    font_css = CSS(
        string=f"""
        @font-face {{
          font-family: 'TicketPricePdf';
          src: url('{regular.as_uri()}');
          font-weight: 400;
        }}
        @font-face {{
          font-family: 'TicketPricePdf';
          src: url('{bold.as_uri()}');
          font-weight: 700;
        }}
        html, body {{ font-family: 'TicketPricePdf', sans-serif; }}
        """,
        font_config=font_config,
    )
    base_url = Path(current_app.root_path).resolve().as_uri() + "/"
    return HTML(string=html, base_url=base_url).write_pdf(
        stylesheets=[font_css],
        font_config=font_config,
    )
