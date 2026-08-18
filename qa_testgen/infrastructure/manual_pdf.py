"""
Gerador de PDF do Manual de Testes (UAT) — layout pensado pra leitura por
usuários leigos (não técnicos): fonte grande, um passo por bloco bem
demarcado, imagens grandes o suficiente pra enxergar detalhe da tela.
"""

import io
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image as RLImage, Table, TableStyle,
    KeepTogether, HRFlowable,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER

from qa_testgen.config import TZ_BR, LOGO_PATH, COR_LARANJA, COR_LARANJA_CLARO, COR_CINZA_ESC, COR_BRANCO

COR_AVISO_FUNDO = colors.HexColor('#FFF4E5')
COR_AVISO_BORDA = colors.HexColor('#F5A623')


class ManualPdfGenerator:

    @staticmethod
    def _esc(text: str) -> str:
        if not text:
            return ""
        return (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    @classmethod
    def _styles(cls):
        base = getSampleStyleSheet()
        return {
            'title': ParagraphStyle('ManualTitle', parent=base['Title'], fontSize=24, leading=28, textColor=COR_CINZA_ESC, alignment=TA_LEFT),
            'subtitle': ParagraphStyle('ManualSubtitle', parent=base['Normal'], fontSize=13, leading=18, textColor=colors.HexColor('#6B6B6B')),
            'intro': ParagraphStyle('ManualIntro', parent=base['Normal'], fontSize=13, leading=19, textColor=COR_CINZA_ESC, spaceAfter=10),
            'passo_titulo': ParagraphStyle('PassoTitulo', parent=base['Normal'], fontSize=16, leading=20, textColor=colors.white, fontName='Helvetica-Bold'),
            'passo_desc': ParagraphStyle('PassoDesc', parent=base['Normal'], fontSize=13, leading=19, textColor=COR_CINZA_ESC),
            'aviso': ParagraphStyle('Aviso', parent=base['Normal'], fontSize=12, leading=17, textColor=colors.HexColor('#8A5A00')),
            'img_caption': ParagraphStyle('ImgCaption', parent=base['Normal'], fontSize=10, leading=13, textColor=colors.HexColor('#7A7A7A'), alignment=TA_CENTER),
        }

    @classmethod
    def _on_page(cls, canvas, doc, titulo_manual, author_name):
        canvas.saveState()
        w, h = A4
        try:
            import os
            if os.path.exists(LOGO_PATH):
                canvas.drawImage(str(LOGO_PATH), 18, h - 46, width=100, height=30, preserveAspectRatio=True, mask='auto')
        except Exception:
            pass
        canvas.setFont('Helvetica', 9)
        canvas.setFillColor(colors.HexColor('#8A8A8A'))
        canvas.drawRightString(w - 18, h - 30, titulo_manual[:60])
        canvas.setStrokeColor(COR_LARANJA)
        canvas.setLineWidth(1.2)
        canvas.line(18, h - 52, w - 18, h - 52)

        canvas.setFont('Helvetica', 9)
        footer = f"Página {doc.page}"
        if author_name:
            footer += f"  —  Gerado por {author_name}"
        canvas.drawCentredString(w / 2, 18, footer)
        canvas.restoreState()

    @classmethod
    def generate(cls, titulo: str, introducao: str, passos: list, passo_images: dict,
                 img_by_filename: dict, author_name: str = "") -> bytes:
        """
        passos: [{"numero","titulo","descricao","aviso"}, ...]
        passo_images: {"1": ["arquivo.jpg", ...], ...} (chave = str(numero))
        img_by_filename: {"arquivo.jpg": bytes, ...}
        """
        buffer = io.BytesIO()
        styles = cls._styles()
        on_page = lambda canvas, doc: cls._on_page(canvas, doc, titulo, author_name)
        doc = SimpleDocTemplate(
            buffer, pagesize=A4,
            leftMargin=2 * cm, rightMargin=2 * cm, topMargin=3.2 * cm, bottomMargin=2 * cm,
            title=titulo, author=author_name or "QA TestGen",
        )
        pw = doc.width
        story = []

        story.append(Paragraph(cls._esc(titulo), styles['title']))
        story.append(Paragraph(
            f"Gerado em {datetime.now(TZ_BR).strftime('%d/%m/%Y')}", styles['subtitle'],
        ))
        story.append(Spacer(1, 14))
        if introducao:
            story.append(Paragraph(cls._esc(introducao).replace(chr(10), '<br/>'), styles['intro']))
        story.append(HRFlowable(width="100%", thickness=1.5, color=COR_LARANJA, spaceAfter=16))

        for passo in passos:
            numero = passo.get('numero')
            bloco = []

            header = Table(
                [[Paragraph(f"PASSO {numero} — {cls._esc(passo.get('titulo', ''))}", styles['passo_titulo'])]],
                colWidths=[pw],
            )
            header.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), COR_LARANJA),
                ('TOPPADDING', (0, 0), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
                ('LEFTPADDING', (0, 0), (-1, -1), 14),
            ]))
            bloco.append(header)
            bloco.append(Spacer(1, 8))

            desc = (passo.get('descricao') or '').replace(chr(10), '<br/>')
            bloco.append(Paragraph(cls._esc(desc), styles['passo_desc']))

            aviso = passo.get('aviso', '')
            if aviso:
                bloco.append(Spacer(1, 8))
                aviso_table = Table(
                    [[Paragraph(f"⚠️ <b>Atenção:</b> {cls._esc(aviso)}", styles['aviso'])]],
                    colWidths=[pw],
                )
                aviso_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, -1), COR_AVISO_FUNDO),
                    ('BOX', (0, 0), (-1, -1), 1, COR_AVISO_BORDA),
                    ('TOPPADDING', (0, 0), (-1, -1), 8),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                    ('LEFTPADDING', (0, 0), (-1, -1), 10),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 10),
                ]))
                bloco.append(aviso_table)

            story.append(KeepTogether(bloco))

            imgs_deste_passo = passo_images.get(str(numero), [])
            for fname in imgs_deste_passo:
                img_bytes = img_by_filename.get(fname)
                if not img_bytes:
                    continue
                try:
                    story.append(Spacer(1, 10))
                    img_buf = io.BytesIO(img_bytes)
                    rl_img = RLImage(img_buf)
                    max_w = pw * 0.85
                    max_h = 11 * cm
                    ratio = min(max_w / rl_img.imageWidth, max_h / rl_img.imageHeight, 1.0)
                    rl_img.drawWidth = rl_img.imageWidth * ratio
                    rl_img.drawHeight = rl_img.imageHeight * ratio
                    rl_img.hAlign = 'CENTER'
                    story.append(rl_img)
                except Exception:
                    pass

            story.append(Spacer(1, 22))

        doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
        return buffer.getvalue()
