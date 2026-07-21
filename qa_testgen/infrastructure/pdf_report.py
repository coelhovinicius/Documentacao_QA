import io
import os
from datetime import datetime
from xml.sax.saxutils import escape as _xml_escape
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.enums import TA_LEFT
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle, PageBreak, HRFlowable, KeepTogether, SimpleDocTemplate

from qa_testgen.config import TZ_BR, LOGO_PATH, COR_LARANJA, COR_CINZA_ESC, COR_CINZA_MED, COR_LARANJA_CLARO, COR_AZUL_CLARO, COR_CINZA_LIN, COR_BRANCO

class PdfReportGenerator:
    @staticmethod
    def _esc(value) -> str:
        """
        Escapa &, < e > antes de qualquer texto entrar num Paragraph do
        ReportLab. O Paragraph interpreta o texto como uma mini-linguagem de
        marcação (parecida com XML); sem isso, conteúdo gerado pela IA que
        contenha esses caracteres (ex.: "valor > 100", "clique em <Salvar>")
        quebra o parser com "unclosed tags".
        """
        return _xml_escape("" if value is None else str(value))

    @staticmethod
    def _styles():
        base = getSampleStyleSheet()
        return {
            'title': ParagraphStyle(
                'ReTitle', parent=base['Title'], fontSize=18, textColor=COR_LARANJA,
                spaceAfter=4, fontName='Helvetica-Bold', alignment=TA_LEFT,
            ),
            'subtitle': ParagraphStyle(
                'ReSub', parent=base['Normal'], fontSize=9, textColor=COR_CINZA_MED,
                spaceAfter=14, fontName='Helvetica',
            ),
            'section': ParagraphStyle(
                'ReSection', parent=base['Heading2'], fontSize=13, textColor=COR_LARANJA,
                spaceBefore=18, spaceAfter=8, fontName='Helvetica-Bold',
            ),
            'subsection': ParagraphStyle(
                'ReSub2', parent=base['Heading3'], fontSize=10, textColor=COR_CINZA_ESC,
                spaceBefore=10, spaceAfter=4, fontName='Helvetica-Bold',
            ),
            'tc_title': ParagraphStyle(
                'ReTCTitle', parent=base['Normal'], fontSize=10, textColor=COR_BRANCO,
                fontName='Helvetica-Bold',
            ),
            'plan_title': ParagraphStyle(
                'RePTitle', parent=base['Normal'], fontSize=10, textColor=COR_BRANCO,
                fontName='Helvetica-Bold',
            ),
            'body': ParagraphStyle(
                'ReBody', parent=base['Normal'], fontSize=9, textColor=COR_CINZA_ESC,
                fontName='Helvetica', leading=13,
            ),
            'cell': ParagraphStyle(
                'ReCell', parent=base['Normal'], fontSize=8, textColor=COR_CINZA_ESC,
                fontName='Helvetica', leading=11,
            ),
            'cell_head': ParagraphStyle(
                'ReCellH', parent=base['Normal'], fontSize=8, textColor=COR_BRANCO,
                fontName='Helvetica-Bold', leading=11,
            ),
        }

    @staticmethod
    def _on_page(canvas, doc, project_name):
        canvas.saveState()
        w, h = A4
        canvas.setFillColor(COR_BRANCO)
        canvas.rect(0, h - 52, w, 52, fill=True, stroke=False)
        if os.path.exists(LOGO_PATH):
            canvas.drawImage(LOGO_PATH, 18, h - 46, width=120, height=36, preserveAspectRatio=True, mask='auto')
        canvas.setFont('Helvetica-Bold', 11)
        canvas.setFillColor(COR_LARANJA)
        canvas.drawRightString(w - 18, h - 28, f"QA TestGen |  {PdfReportGenerator._esc(project_name)}")
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(COR_LARANJA)
        canvas.drawRightString(w - 18, h - 42, datetime.now(TZ_BR).strftime('%d/%m/%Y %H:%M'))
        canvas.setStrokeColor(COR_LARANJA)
        canvas.setLineWidth(1.2)
        canvas.line(0, h - 52, w, h - 52)
        canvas.setFont('Helvetica', 7)
        canvas.setFillColor(COR_CINZA_MED)
        canvas.drawString(18, 20, "Refuturiza – Gerado automaticamente pelo QA TestGen")
        canvas.drawRightString(w - 18, 20, f"Página {doc.page}")
        canvas.setStrokeColor(COR_LARANJA)
        canvas.setLineWidth(0.8)
        canvas.line(18, 32, w - 18, 32)
        canvas.restoreState()

    @classmethod
    def generate(cls, project_name: str, matriz: list, test_plans: list, test_cases: list) -> bytes:
        buffer = io.BytesIO()
        styles = cls._styles()
        on_page = lambda canvas, doc: cls._on_page(canvas, doc, project_name)
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=1.8 * cm,
            rightMargin=1.8 * cm,
            topMargin=3.2 * cm,
            bottomMargin=2.0 * cm,
            title=f"QA Report – {project_name}",
            author="Refuturiza QA TestGen",
        )
        pw = doc.width
        story = []

        # Numeração TC-XX consistente entre a Matriz, os Planos e os Casos,
        # e um índice reverso (requisito da Matriz -> quais TC-XX o cobrem)
        # usado no resumo de rastreabilidade logo após a Matriz.
        tc_numbers = {tc.get('titulo', ''): idx for idx, tc in enumerate(test_cases or [], start=1)}
        cases_by_titulo = {tc.get('titulo', ''): tc for tc in test_cases or []}
        coverage_by_mc_id = {}
        for tc in test_cases or []:
            tc_label = f"TC-{tc_numbers.get(tc.get('titulo', ''), 0):02d}"
            for mc_id in (tc.get('requisitos_relacionados') or []):
                coverage_by_mc_id.setdefault(str(mc_id), []).append(tc_label)

        story.append(Spacer(1, 0.4 * cm))
        story.append(Paragraph("Documentação QA", styles['title']))
        story.append(Paragraph(
            f"Projeto: <b>{cls._esc(project_name)}</b> &nbsp;|&nbsp; "
            f"Gerado em {datetime.now(TZ_BR).strftime('%d/%m/%Y às %H:%M')}",
            styles['subtitle'],
        ))
        story.append(HRFlowable(width="100%", thickness=2, color=COR_LARANJA, spaceAfter=14))

        story.append(Paragraph("1. Matriz de Cobertura", styles['section']))
        if matriz:
            hcols = ["id", "funcionalidade", "requisito", "cenario", "categoria", "prioridade", "criticidade", "observacoes"]
            labels = ["ID", "Funcionalidade", "Requisito", "Cenário", "Categoria", "Prioridade", "Criticidade", "Observações"]
            widths = [1.4 * cm, 3 * cm, 2 * cm, 4.5 * cm, 2.8 * cm, 2 * cm, 2.2 * cm, 3 * cm]
            data = [[Paragraph(label, styles['cell_head']) for label in labels]]
            for row in matriz:
                data.append([Paragraph(cls._esc(row.get(col, '') or ''), styles['cell']) for col in hcols])
            table = Table(data, colWidths=widths, repeatRows=1)
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), COR_LARANJA),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [COR_BRANCO, COR_CINZA_LIN]),
                ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#DDDDDD')),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('LEFTPADDING', (0, 0), (-1, -1), 4),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ]))
            story.append(table)

            story.append(Spacer(1, 12))
            story.append(Paragraph("1.1 Resumo de Rastreabilidade", styles['subsection']))
            cov_data = [[
                Paragraph("ID", styles['cell_head']),
                Paragraph("Requisito", styles['cell_head']),
                Paragraph("Casos de Teste que Cobrem", styles['cell_head']),
            ]]
            cov_row_colors = [COR_BRANCO]  # cabeçalho, cor não usada mas mantém índice alinhado
            for row in matriz:
                mc_id = str(row.get('id', '') or '')
                requisito = str(row.get('requisito', '') or '')
                covering = coverage_by_mc_id.get(mc_id, [])
                if covering:
                    cov_text = ", ".join(covering)
                    row_color = COR_BRANCO
                else:
                    cov_text = "⚠ Sem cobertura"
                    row_color = colors.HexColor('#FDEAEA')
                cov_row_colors.append(row_color)
                cov_data.append([
                    Paragraph(cls._esc(mc_id), styles['cell']),
                    Paragraph(cls._esc(requisito), styles['cell']),
                    Paragraph(cls._esc(cov_text), styles['cell']),
                ])
            cov_table = Table(cov_data, colWidths=[2 * cm, 3 * cm, pw - 5 * cm], repeatRows=1)
            cov_style = [
                ('BACKGROUND', (0, 0), (-1, 0), COR_CINZA_ESC),
                ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#DDDDDD')),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('LEFTPADDING', (0, 0), (-1, -1), 4),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ]
            for r_idx, r_color in enumerate(cov_row_colors):
                if r_idx == 0:
                    continue
                cov_style.append(('BACKGROUND', (0, r_idx), (-1, r_idx), r_color))
            cov_table.setStyle(TableStyle(cov_style))
            story.append(cov_table)
        else:
            story.append(Paragraph("Nenhuma entrada na Matriz.", styles['body']))

        story.append(PageBreak())

        story.append(Paragraph("2. Planos de Teste", styles['section']))
        if test_plans:
            for p_idx, plan in enumerate(test_plans, start=1):
                plan_name = plan.get('nome', f'Plano #{p_idx}')
                plan_desc = plan.get('descricao', '')
                suites = plan.get('suites', [])

                phdr = Table([[Paragraph(f"Plano {p_idx:02d} – {cls._esc(plan_name)}", styles['plan_title'])]], colWidths=[pw])
                phdr.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, -1), COR_LARANJA),
                    ('TOPPADDING', (0, 0), (-1, -1), 5),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
                    ('LEFTPADDING', (0, 0), (-1, -1), 8),
                ]))
                story.append(phdr)

                if plan_desc:
                    desc_t = Table(
                        [[Paragraph("<b>Descrição:</b>", styles['cell']), Paragraph(cls._esc(plan_desc), styles['cell'])]],
                        colWidths=[2.8 * cm, pw - 2.8 * cm],
                    )
                    desc_t.setStyle(TableStyle([
                        ('BACKGROUND', (0, 0), (-1, -1), COR_AZUL_CLARO),
                        ('TOPPADDING', (0, 0), (-1, -1), 4),
                        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                        ('LEFTPADDING', (0, 0), (-1, -1), 6),
                        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ]))
                    story.append(desc_t)

                for s_idx, suite in enumerate(suites, start=1):
                    story.append(Paragraph(f"&nbsp;&nbsp;&nbsp;Suite {s_idx}: {cls._esc(suite.get('nome',''))}", styles['subsection']))
                    if suite.get('descricao'):
                        story.append(Paragraph(cls._esc(suite['descricao']), styles['body']))

                    casos = suite.get('casos', [])
                    if casos:
                        suite_data = [[
                            Paragraph("#", styles['cell_head']),
                            Paragraph("Caso de Teste", styles['cell_head']),
                            Paragraph("Requisitos", styles['cell_head']),
                        ]]
                        for c_idx, caso in enumerate(casos, start=1):
                            tc_num = tc_numbers.get(caso)
                            caso_label = f"TC-{tc_num:02d} – {caso}" if tc_num else caso
                            reqs = (cases_by_titulo.get(caso, {}) or {}).get('requisitos_relacionados') or []
                            reqs_text = ", ".join(str(r) for r in reqs) if reqs else "—"
                            suite_data.append([
                                Paragraph(str(c_idx), styles['cell']),
                                Paragraph(cls._esc(caso_label), styles['cell']),
                                Paragraph(cls._esc(reqs_text), styles['cell']),
                            ])
                        st_t = Table(suite_data, colWidths=[1 * cm, (pw - 1 * cm) * 0.65, (pw - 1 * cm) * 0.35], repeatRows=1)
                        st_t.setStyle(TableStyle([
                            ('BACKGROUND', (0, 0), (-1, 0), COR_CINZA_ESC),
                            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [COR_BRANCO, COR_CINZA_LIN]),
                            ('GRID', (0, 0), (-1, -1), 0.3, colors.HexColor('#CCCCCC')),
                            ('TOPPADDING', (0, 0), (-1, -1), 4),
                            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                            ('LEFTPADDING', (0, 0), (-1, -1), 5),
                            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                        ]))
                        story.append(st_t)
                    story.append(Spacer(1, 8))
                story.append(Spacer(1, 14))
        else:
            story.append(Paragraph("Nenhum Plano de Teste gerado.", styles['body']))

        story.append(PageBreak())

        story.append(Paragraph("3. Casos de Teste", styles['section']))
        for idx, tc in enumerate(test_cases, start=1):
            titulo = tc.get('titulo', f'Caso #{idx}')
            pre = tc.get('pre_condicoes', '—')
            passos = tc.get('passos', [])
            reqs = tc.get('requisitos_relacionados') or []
            reqs_text = ", ".join(str(r) for r in reqs) if reqs else "—"

            hdr = Table([[Paragraph(f"TC-{idx:02d} – {cls._esc(titulo)}", styles['tc_title'])]], colWidths=[pw])
            hdr.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), COR_LARANJA),
                ('TOPPADDING', (0, 0), (-1, -1), 5),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
                ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ]))
            pre_t = Table(
                [
                    [Paragraph("<b>Pré-condições:</b>", styles['cell']), Paragraph(cls._esc(pre), styles['cell'])],
                    [Paragraph("<b>Rastreabilidade:</b>", styles['cell']), Paragraph(cls._esc(reqs_text), styles['cell'])],
                ],
                colWidths=[3 * cm, pw - 3 * cm],
            )
            pre_t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), COR_LARANJA_CLARO),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('LEFTPADDING', (0, 0), (-1, -1), 6),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ]))
            step_data = [[
                Paragraph("#", styles['cell_head']),
                Paragraph("Ação", styles['cell_head']),
                Paragraph("Resultado Esperado", styles['cell_head']),
            ]]
            for step in passos:
                step_data.append([
                    Paragraph(cls._esc(step.get('numero', '')), styles['cell']),
                    Paragraph(cls._esc(step.get('acao', '')), styles['cell']),
                    Paragraph(cls._esc(step.get('resultado_esperado', '')), styles['cell']),
                ])
            st_t = Table(step_data, colWidths=[1 * cm, (pw - 1 * cm) * 0.45, (pw - 1 * cm) * 0.55], repeatRows=1)
            st_t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), COR_CINZA_ESC),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [COR_BRANCO, COR_CINZA_LIN]),
                ('GRID', (0, 0), (-1, -1), 0.3, colors.HexColor('#CCCCCC')),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('LEFTPADDING', (0, 0), (-1, -1), 5),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ]))
            story.append(KeepTogether([hdr, pre_t]))
            story.append(st_t)
            story.append(Spacer(1, 14))

        doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
        return buffer.getvalue()
