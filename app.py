import streamlit as st
import requests
import json
import os
import io
import uuid
import fitz  # pymupdf
import base64
from PIL import Image
from datetime import datetime
import pytz
from docx import Document
from dotenv import load_dotenv

# ReportLab para geração do PDF
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table,
    TableStyle, PageBreak, HRFlowable, KeepTogether
)

load_dotenv()

# ─── Configuração Global de Timezone (Brasil/Brasília) ────────────────────────
TZ_BR = pytz.timezone('America/Sao_Paulo')

# ─── Cores e Assets Refuturiza ───────────────────────────────────────────────
COR_LARANJA       = colors.HexColor('#F15A24')
COR_CINZA_ESC     = colors.HexColor('#3A3A3A')
COR_CINZA_MED     = colors.HexColor('#6B6B6B')
COR_LARANJA_CLARO = colors.HexColor('#FAE5DC')
COR_CINZA_LIN     = colors.HexColor('#F5F5F5')
COR_BRANCO        = colors.white

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGO_PATH = os.path.join(BASE_DIR, 'logo_refu_1.png')
SIMBOLO_PATH = os.path.join(BASE_DIR, 'simbolo_refu_1.png')


# ═══════════════════════════════════════════════════════════════════════════════
# MODALS & GLOBAL UI COMPONENTS
# ═══════════════════════════════════════════════════════════════════════════════
@st.dialog("⚠️ Confirmação de Exclusão")
def confirm_deletion_modal(list_key: str, index: int):
    """
    True Modal for strict deletion confirmation.
    Used for deleting an entire Matriz row or an entire Test Case.
    Blocks background UI and forces explicit user intent.
    """
    st.markdown("A exclusão deste item é **irreversível**. Tem certeza que deseja remover esta linha?")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🗑️ Sim, Excluir", use_container_width=True, type="primary"):
            st.session_state[list_key].pop(index)
            UserInterface._clear_widget_states()
            st.rerun()
    with c2:
        if st.button("❌ Cancelar", use_container_width=True):
            st.rerun()


@st.dialog("⚠️ Confirmação de Exclusão")
def confirm_step_deletion_modal(steps_state_key: str, step_uid: str):
    """
    True Modal for strict deletion confirmation of an individual test step
    inside an open edit/creation form.
    """
    st.markdown("A exclusão deste step é **irreversível**. Tem certeza que deseja remover este step?")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🗑️ Sim, Excluir", use_container_width=True, type="primary", key="confirm_del_step"):
            steps_list = st.session_state[steps_state_key]
            st.session_state[steps_state_key] = [s for s in steps_list if s["uid"] != step_uid]
            st.rerun()
    with c2:
        if st.button("❌ Cancelar", use_container_width=True, key="cancel_del_step"):
            st.rerun()


@st.dialog("⚠️ Descartar Novo Registro")
def confirm_discard_new_modal(discard_flag_key: str):
    """
    True Modal for confirming discard of an in-progress, never-saved new row
    (Matriz row or Test Case) when the user clicks Cancel.
    """
    st.markdown(
        "Os dados preenchidos neste novo registro ainda **não foram salvos**. "
        "Ao cancelar, essas informações serão **perdidas permanentemente**. "
        "Tem certeza que deseja descartar?"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🗑️ Sim, Descartar", use_container_width=True, type="primary", key="confirm_discard"):
            st.session_state[discard_flag_key] = False
            UserInterface._clear_widget_states()
            st.rerun()
    with c2:
        if st.button("❌ Voltar a Editar", use_container_width=True, key="cancel_discard"):
            st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════
class AppConfiguration:
    def __init__(self):
        self.webhook_analysis = self._get_env_var("N8N_WEBHOOK_URL_ANALYSIS", "http://localhost:5678/webhook/qa-testgen-analysis")
        self.webhook_matrix = self._get_env_var("N8N_WEBHOOK_URL_MATRIX", "http://localhost:5678/webhook/qa-testgen-matrix")
        self.webhook_generation = self._get_env_var("N8N_WEBHOOK_URL_GENERATION", "http://localhost:5678/webhook/qa-testgen-generation")

    def _get_env_var(self, key: str, default: str) -> str:
        val = os.getenv(key)
        if val:
            return val
        try:
            if key in st.secrets:
                return st.secrets[key]
        except Exception:
            pass
        return default


# ═══════════════════════════════════════════════════════════════════════════════
# DOCUMENT PROCESSOR
# ═══════════════════════════════════════════════════════════════════════════════
class DocumentProcessor:
    @staticmethod
    def extract_plain_text(uploaded_file) -> str:
        ext = uploaded_file.name.split('.')[-1].lower()
        text = ""
        try:
            if ext == "pdf":
                data = uploaded_file.read()
                doc = fitz.open(stream=data, filetype="pdf")
                for page in doc:
                    text += page.get_text() + "\n"
                doc.close()
            elif ext == "docx":
                doc = Document(uploaded_file)
                for p in doc.paragraphs:
                    text += p.text + "\n"
            elif ext == "txt":
                text = uploaded_file.getvalue().decode("utf-8")
            return text.strip()
        except Exception as exception:
            st.error(f"Erro ao extrair texto: {exception}")
            return ""


# ═══════════════════════════════════════════════════════════════════════════════
# AZURE DEVOPS CSV FORMATTER
# ═══════════════════════════════════════════════════════════════════════════════
class AzureCsvFormatter:
    @staticmethod
    def generate_csv_content(test_cases: list, project_name: str) -> str:
        header = "ID;Work Item Type;Title;Test Step;Pre condicoes;Step Action;Step Expected;Automation Status;Area Path;Assigned To;State"
        if not test_cases:
            return header
        lines = [header]
        for tc in test_cases:
            titulo = str(tc.get('titulo', '')).replace(';', ',')
            pre    = str(tc.get('pre_condicoes', '')).replace(';', ',')
            lines.append(f";Test Case;{titulo};;{pre};;;Not Automated;{project_name};;Design")
            for step in tc.get('passos', []):
                num  = step.get('numero', '')
                acao = str(step.get('acao', '')).replace(';', ',')
                esp  = str(step.get('resultado_esperado', '')).replace(';', ',')
                lines.append(f";;;{num};;{acao};{esp};;;;")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# PDF REPORT GENERATOR
# ═══════════════════════════════════════════════════════════════════════════════
class PdfReportGenerator:

    @staticmethod
    def _styles():
        base = getSampleStyleSheet()
        return {
            'title': ParagraphStyle('ReTitle', parent=base['Title'],
                fontSize=18, textColor=COR_LARANJA, spaceAfter=4,
                fontName='Helvetica-Bold', alignment=TA_LEFT),
            'subtitle': ParagraphStyle('ReSub', parent=base['Normal'],
                fontSize=9, textColor=COR_CINZA_MED, spaceAfter=14, fontName='Helvetica'),
            'section': ParagraphStyle('ReSection', parent=base['Heading2'],
                fontSize=13, textColor=COR_LARANJA, spaceBefore=18, spaceAfter=8,
                fontName='Helvetica-Bold'),
            'tc_title': ParagraphStyle('ReTCTitle', parent=base['Normal'],
                fontSize=10, textColor=COR_BRANCO, fontName='Helvetica-Bold'),
            'body': ParagraphStyle('ReBody', parent=base['Normal'],
                fontSize=9, textColor=COR_CINZA_ESC, fontName='Helvetica', leading=13),
            'cell': ParagraphStyle('ReCell', parent=base['Normal'],
                fontSize=8, textColor=COR_CINZA_ESC, fontName='Helvetica', leading=11),
            'cell_head': ParagraphStyle('ReCellH', parent=base['Normal'],
                fontSize=8, textColor=COR_BRANCO, fontName='Helvetica-Bold', leading=11),
        }

    @staticmethod
    def _on_page(canvas, doc, project_name):
        canvas.saveState()
        w, h = A4
        canvas.setFillColor(COR_LARANJA)
        canvas.rect(0, h - 52, w, 52, fill=True, stroke=False)
        if os.path.exists(LOGO_PATH):
            canvas.drawImage(LOGO_PATH, 18, h - 46,
                             width=120, height=36,
                             preserveAspectRatio=True, mask='auto')
        canvas.setFont('Helvetica-Bold', 11)
        canvas.setFillColor(COR_BRANCO)
        canvas.drawRightString(w - 18, h - 28, f"QA TestGen  |  {project_name}")
        canvas.setFont('Helvetica', 8)
        canvas.drawRightString(w - 18, h - 42, datetime.now(TZ_BR).strftime('%d/%m/%Y %H:%M'))
        canvas.setFont('Helvetica', 7)
        canvas.setFillColor(COR_CINZA_MED)
        canvas.drawString(18, 20, "Refuturiza – Gerado automaticamente pelo QA TestGen")
        canvas.drawRightString(w - 18, 20, f"Página {doc.page}")
        canvas.setStrokeColor(COR_LARANJA)
        canvas.setLineWidth(0.8)
        canvas.line(18, 32, w - 18, 32)
        canvas.restoreState()

    @classmethod
    def generate(cls, project_name: str, matriz: list, test_cases: list) -> bytes:
        buffer = io.BytesIO()
        styles = cls._styles()
        on_page = lambda c, d: cls._on_page(c, d, project_name)

        doc = SimpleDocTemplate(
            buffer, pagesize=A4,
            leftMargin=1.8*cm, rightMargin=1.8*cm,
            topMargin=3.2*cm, bottomMargin=2.0*cm,
            title=f"QA Report – {project_name}", author="Refuturiza QA TestGen"
        )
        page_width = doc.width
        story = []

        story.append(Spacer(1, 0.4*cm))
        story.append(Paragraph("Documentação QA", styles['title']))
        story.append(Paragraph(
            f"Projeto: <b>{project_name}</b> &nbsp;|&nbsp; "
            f"Gerado em {datetime.now(TZ_BR).strftime('%d/%m/%Y às %H:%M')}",
            styles['subtitle']
        ))
        story.append(HRFlowable(width="100%", thickness=2, color=COR_LARANJA, spaceAfter=14))

        story.append(Paragraph("1. Matriz de Cobertura", styles['section']))
        if matriz:
            headers_cols = ["id","funcionalidade","requisito","cenario",
                            "categoria","prioridade","criticidade","observacoes"]
            labels = ["ID","Funcionalidade","Requisito","Cenário",
                      "Categoria","Prioridade","Criticidade","Observações"]
            widths = [1.4*cm, 3*cm, 2*cm, 4.5*cm, 2.8*cm, 2*cm, 2.2*cm, 3*cm]

            data = [[Paragraph(label, styles['cell_head']) for label in labels]]
            for row in matriz:
                data.append([Paragraph(str(row.get(col, '') or ''), styles['cell']) for col in headers_cols])

            table = Table(data, colWidths=widths, repeatRows=1)
            table.setStyle(TableStyle([
                ('BACKGROUND',    (0,0), (-1,0),  COR_LARANJA),
                ('ROWBACKGROUNDS',(0,1), (-1,-1), [COR_BRANCO, COR_CINZA_LIN]),
                ('GRID',         (0,0), (-1,-1),  0.4, colors.HexColor('#DDDDDD')),
                ('TOPPADDING',   (0,0), (-1,-1),  4),
                ('BOTTOMPADDING',(0,0), (-1,-1),  4),
                ('LEFTPADDING',  (0,0), (-1,-1),  4),
                ('VALIGN',       (0,0), (-1,-1),  'TOP'),
            ]))
            story.append(table)
        else:
            story.append(Paragraph("Nenhuma entrada na Matriz.", styles['body']))

        story.append(PageBreak())

        story.append(Paragraph("2. Casos de Teste", styles['section']))
        for idx, tc in enumerate(test_cases, start=1):
            titulo = tc.get('titulo', f'Caso #{idx}')
            pre    = tc.get('pre_condicoes', '—')
            passos = tc.get('passos', [])

            hdr = Table([[Paragraph(f"TC-{idx:02d} – {titulo}", styles['tc_title'])]], colWidths=[page_width])
            hdr.setStyle(TableStyle([
                ('BACKGROUND',   (0,0),(-1,-1), COR_LARANJA),
                ('TOPPADDING',   (0,0),(-1,-1), 5),
                ('BOTTOMPADDING',(0,0),(-1,-1), 5),
                ('LEFTPADDING',  (0,0),(-1,-1), 8),
            ]))

            pre_t = Table([[Paragraph("<b>Pré-condições:</b>", styles['cell']), Paragraph(pre, styles['cell'])]], colWidths=[3*cm, page_width - 3*cm])
            pre_t.setStyle(TableStyle([
                ('BACKGROUND',   (0,0),(-1,-1), COR_LARANJA_CLARO),
                ('TOPPADDING',   (0,0),(-1,-1), 4),
                ('BOTTOMPADDING',(0,0),(-1,-1), 4),
                ('LEFTPADDING',  (0,0),(-1,-1), 6),
                ('VALIGN',       (0,0),(-1,-1), 'TOP'),
            ]))

            step_data = [[Paragraph("#", styles['cell_head']), Paragraph("Ação", styles['cell_head']), Paragraph("Resultado Esperado", styles['cell_head'])]]
            for step in passos:
                step_data.append([
                    Paragraph(str(step.get('numero','')), styles['cell']),
                    Paragraph(str(step.get('acao','')), styles['cell']),
                    Paragraph(str(step.get('resultado_esperado','')), styles['cell']),
                ])
            st_t = Table(step_data, colWidths=[1*cm, (page_width-1*cm)*0.45, (page_width-1*cm)*0.55], repeatRows=1)
            st_t.setStyle(TableStyle([
                ('BACKGROUND',    (0,0),(-1,0),  COR_CINZA_ESC),
                ('ROWBACKGROUNDS',(0,1),(-1,-1), [COR_BRANCO, COR_CINZA_LIN]),
                ('GRID',         (0,0),(-1,-1),  0.3, colors.HexColor('#CCCCCC')),
                ('TOPPADDING',   (0,0),(-1,-1),  4),
                ('BOTTOMPADDING',(0,0),(-1,-1),  4),
                ('LEFTPADDING',  (0,0),(-1,-1),  5),
                ('VALIGN',       (0,0),(-1,-1),  'TOP'),
            ]))

            story.append(KeepTogether([hdr, pre_t]))
            story.append(st_t)
            story.append(Spacer(1, 14))

        doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
        return buffer.getvalue()


# ═══════════════════════════════════════════════════════════════════════════════
# WEBHOOK CLIENT
# ═══════════════════════════════════════════════════════════════════════════════
class WebhookClient:
    def __init__(self, config: AppConfiguration):
        self.config = config
        api_key = os.getenv("N8N_API_KEY")
        if not api_key:
            try:
                if "N8N_API_KEY" in st.secrets:
                    api_key = st.secrets["N8N_API_KEY"]
            except Exception:
                api_key = ""
        self.headers = {"x-api-key": api_key} if api_key else {}

    def _safe_json_parse(self, response: requests.Response) -> dict:
        raw_text = response.text.strip()
        if not raw_text:
            raise ValueError(
                f"Payload vazio do orquestrador (Status {response.status_code}). "
                "Causa raiz provável: Deadlock no Merge Node do n8n ou falha de roteamento de rede."
            )
        if raw_text.startswith("```json"):
            raw_text = raw_text.replace("```json", "").replace("```", "").strip()
        elif raw_text.startswith("```"):
            raw_text = raw_text.replace("```", "").strip()
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError as decode_error:
            raise ValueError(f"Payload JSON malformado. Resposta bruta: {raw_text[:200]}...") from decode_error

    def _extract(self, raw, key: str) -> list:
        if isinstance(raw, list):
            for item in raw:
                r = self._extract(item, key)
                if r: return r
            return []
        if not isinstance(raw, dict):
            return []
        if key in raw and isinstance(raw[key], list):
            return raw[key]
        for v in raw.values():
            if isinstance(v, dict):
                r = self._extract(v, key)
                if r: return r
            elif isinstance(v, list):
                for item in v:
                    r = self._extract(item, key)
                    if r: return r
            elif isinstance(v, str):
                try:
                    r = self._extract(json.loads(v), key)
                    if r: return r
                except Exception:
                    pass
        return []

    def trigger_analysis(self, doc_text: str, project: str) -> dict:
        response = requests.post(
            self.config.webhook_analysis,
            json={"document_text": doc_text, "nome_projeto": project},
            headers=self.headers, timeout=120
        )
        response.raise_for_status()
        data = self._safe_json_parse(response)
        return {"duvidas": self._extract(data, "duvidas")}

    def trigger_matrix(self, doc_text: str, answers: dict, project: str) -> dict:
        response = requests.post(
            self.config.webhook_matrix,
            json={"document_text": doc_text,
                  "respostas_duvidas": json.dumps(answers, ensure_ascii=False),
                  "nome_projeto": project},
            headers=self.headers, timeout=300
        )
        response.raise_for_status()
        data = self._safe_json_parse(response)
        return {"matriz": self._extract(data, "matriz")}

    def trigger_generation(self, doc_text: str, matriz: list, answers: dict, project: str) -> dict:
        response = requests.post(
            self.config.webhook_generation,
            json={"document_text": doc_text,
                  "matriz_cobertura": json.dumps(matriz, ensure_ascii=False),
                  "respostas_duvidas": json.dumps(answers, ensure_ascii=False),
                  "nome_projeto": project},
            headers=self.headers, timeout=300
        )
        response.raise_for_status()
        data = self._safe_json_parse(response)
        return {"casos_de_teste": self._extract(data, "casos_de_teste")}


# ═══════════════════════════════════════════════════════════════════════════════
# UI PAGE OBJECT
# ═══════════════════════════════════════════════════════════════════════════════
class UserInterface:
    def __init__(self):
        page_icon = "🧪"
        if os.path.exists(SIMBOLO_PATH):
            try:
                page_icon = Image.open(SIMBOLO_PATH)
            except Exception:
                pass

        st.set_page_config(page_title="QA TestGen - Azure DevOps", page_icon=page_icon, layout="wide")
        self._init_state()
        self.config = AppConfiguration()
        self.client = WebhookClient(self.config)

    def _init_state(self):
        defaults = {
            'step': 1, 'doc_text': '', 'project_name': '',
            'questions': [], 'user_answers': {},
            'matriz': [], 'test_cases': [], 'csv_content': '',
            'is_processing': False, 'current_action': None,
            'adding_matriz_row': False, 'adding_test_case': False,
        }
        for k, v in defaults.items():
            if k not in st.session_state:
                st.session_state[k] = v

    def trigger_action(self, action_name: str):
        st.session_state.current_action = action_name
        st.session_state.is_processing = True

    def clear_action(self):
        st.session_state.current_action = None
        st.session_state.is_processing = False

    @staticmethod
    def _clear_widget_states():
        """
        Garante a remoção de State Drift. Desvincula os inputs cacheados
        para que eles não reapareçam em novos índices da tabela.
        """
        prefixes = ("mid_", "mfunc_", "mreq_", "mcen_", "mcat_", "mpri_", "mcrit_", "mobs_", "edit_m_",
                    "tt_", "tp_", "ta_", "te_", "edit_tc_",
                    "newm_", "newtc_", "new_steps_")
        for k in list(st.session_state.keys()):
            if k.startswith(prefixes):
                del st.session_state[k]

    def _render_emergency_reset(self):
        with st.sidebar:
            st.warning("⚠️ Controles de Emergência")
            if st.button("🔄 Resetar Aplicação", use_container_width=True):
                st.session_state.clear()
                st.rerun()

    def _header(self):
        self._render_emergency_reset()

        img_b64 = ""
        if os.path.exists(LOGO_PATH):
            try:
                with open(LOGO_PATH, "rb") as f:
                    img_b64 = base64.b64encode(f.read()).decode("utf-8")
            except Exception:
                pass

        html_content = f"""
        <div style="display: flex; align-items: stretch; margin-bottom: 1.5rem; gap: 1.5rem;">
            <div style="flex: 0 0 200px; display: flex; align-items: center; justify-content: center;">
                <img src="data:image/png;base64,{img_b64}" style="max-width: 100%; max-height: 80px; object-fit: contain; border-radius: 0;">
            </div>
            <div style="flex: 1; background: linear-gradient(135deg, #F15A24, #c94a1a); padding: 1rem 1.5rem; border-radius: 6px; display: flex; flex-direction: column; justify-content: center; min-height: 80px;">
                <h1 style="color: white; margin: 0; font-size: 1.6rem; padding: 0;">🧪 QA TestGen – Automation</h1>
                <p style="color: white; margin: 0.2rem 0 0 0; font-size: 1.05rem; padding: 0;">
                    Gerador Inteligente de Casos de Teste — Azure DevOps Integration
                </p>
            </div>
        </div>
        """
        st.markdown(html_content, unsafe_allow_html=True)

    def _progress(self):
        labels = ["📄 Upload", "💬 Dúvidas", "📊 Matriz", "📋 Casos", "⬇️ Download"]
        cols = st.columns(5)
        for i, (col, label) in enumerate(zip(cols, labels), start=1):
            with col:
                if i < st.session_state.step:
                    st.success(label)
                elif i == st.session_state.step:
                    st.info(f"**{label}**")
                else:
                    st.markdown(
                        f"<div style='padding:.5rem;border-radius:4px;"
                        f"background:#f0f0f0;color:#aaa;text-align:center'>{label}</div>",
                        unsafe_allow_html=True)
        st.divider()

    def _err(self, exception):
        if isinstance(exception, ValueError):
            st.error(f"❌ Erro de Integridade Estrutural: {exception}")
        elif isinstance(exception, requests.exceptions.Timeout):
            st.error("⏱️ Timeout: o n8n demorou demais para responder. Aumente o TTL do request.")
        elif isinstance(exception, requests.exceptions.ConnectionError):
            st.error("🔌 Network Error: Não foi possível conectar ao nó orquestrador (n8n). Verifique URL ou túnel HTTPS.")
        elif isinstance(exception, requests.exceptions.HTTPError):
            st.error(f"❌ HTTP Exception: {exception}")
        else:
            st.error(f"❌ Fatal Error: {exception}")

    @staticmethod
    def _priority_badge(value: str) -> str:
        """Retorna HTML de badge colorido para Prioridade/Criticidade."""
        colors_map = {
            "alta":  ("#c0392b", "#fdecea"),
            "média": ("#d68910", "#fef9e7"),
            "media": ("#d68910", "#fef9e7"),
            "baixa": ("#1e8449", "#eafaf1"),
        }
        key = (value or "").lower()
        fg, bg = colors_map.get(key, ("#555", "#f0f0f0"))
        return (
            f'<span style="background:{bg};color:{fg};padding:2px 10px;'
            f'border-radius:12px;font-size:0.78rem;font-weight:600;'
            f'border:1px solid {fg}33">{value or "—"}</span>'
        )

    @staticmethod
    def _read_only_table(rows: list) -> None:
        """
        Renderiza uma mini tabela HTML (Opção B) com rótulo | valor.
        rows: lista de tuplas (label, value_html)
        """
        html = (
            '<table style="width:100%;border-collapse:collapse;'
            'font-size:0.85rem;margin-top:0.5rem">'
        )
        for label, value in rows:
            html += (
                f'<tr style="border-bottom:1px solid #ececec">'
                f'<td style="padding:6px 10px;color:#888;font-weight:600;'
                f'white-space:nowrap;width:140px">{label}</td>'
                f'<td style="padding:6px 10px;color:#2d2d2d">{value}</td>'
                f'</tr>'
            )
        html += "</table>"
        st.markdown(html, unsafe_allow_html=True)

    @staticmethod
    def _next_matriz_id(matriz: list) -> str:
        """Gera o próximo ID sequencial MC-0XX com base na maior numeração existente."""
        max_n = 0
        for row in matriz:
            rid = str(row.get('id', ''))
            digits = ''.join(ch for ch in rid if ch.isdigit())
            if digits:
                try:
                    max_n = max(max_n, int(digits))
                except ValueError:
                    pass
        return f"MC-{max_n + 1:03d}"

    def step_1(self):
        st.subheader("Passo 1 – Setup e Documentação")
        col1, col2 = st.columns(2)
        with col1:
            project = st.text_input("Nome do Projeto *", placeholder="Ex: Passaporte Refuturiza")
        with col2:
            uploaded = st.file_uploader("Documento de Requisitos (Máx 20MB) *", type=["pdf","txt","docx"])

        if uploaded and uploaded.size > 20 * 1024 * 1024:
            st.error("❌ Operação Bloqueada: O arquivo submetido excede o limite máximo estabelecido de 20MB.")
            return

        if not project or not uploaded:
            st.info("Preencha o nome do projeto e faça o upload do documento para continuar.")
            return

        st.button("🔍 Executar Análise de Cobertura (IA)", use_container_width=True, type="primary",
                  on_click=self.trigger_action, args=("analyze_docs",),
                  disabled=st.session_state.is_processing)

        if st.session_state.current_action == "analyze_docs":
            with st.spinner("Extraindo texto..."):
                text = DocumentProcessor.extract_plain_text(uploaded)
            if not text:
                st.error("Não foi possível extrair texto. Verifique a integridade do artefato.")
                self.clear_action()
            else:
                with st.spinner("Aguarde enquanto a análise é processada…"):
                    try:
                        resp = self.client.trigger_analysis(text, project)
                        st.session_state.doc_text     = text
                        st.session_state.project_name = project
                        st.session_state.questions    = resp.get("duvidas") or []
                        st.session_state.step         = 2
                        self.clear_action()
                        st.rerun()
                    except Exception as e:
                        self._err(e)
                        self.clear_action()

    def step_2(self):
        st.subheader("Passo 2 – Resolução de Conflitos e Ambiguidade")
        questions = st.session_state.questions
        answers   = {}

        if not questions:
            st.success("✅ A IA não identificou ambiguidades. Bypass validado. Prossiga para gerar a Matriz.")
        else:
            st.info(f"A engine de validação identificou **{len(questions)} ponto(s) crítico(s)**.")
            for q in questions:
                qid = str(q.get('id', '0'))
                st.markdown(f"**❓ #{qid}:** {q.get('pergunta', '')}")
                answers[qid] = st.text_area(f"Resposta #{qid}", key=f"q_{qid}",
                                            placeholder="Descreva a regra de negócio consolidada…")

        col1, col2 = st.columns([1, 3])
        with col1:
            if st.button("← Voltar", use_container_width=True, disabled=st.session_state.is_processing):
                st.session_state.step = 1
                st.rerun()
        with col2:
            st.button("📊 Gerar Matriz de Cobertura", use_container_width=True, type="primary",
                      on_click=self.trigger_action, args=("generate_matrix",),
                      disabled=st.session_state.is_processing)

        if st.session_state.current_action == "generate_matrix":
            with st.spinner("Estruturando Matriz de Rastreabilidade…"):
                try:
                    resp = self.client.trigger_matrix(
                        st.session_state.doc_text, answers,
                        st.session_state.project_name
                    )
                    matriz = resp.get("matriz") or []
                    if not matriz:
                        st.error("❌ Matriz vazia. Verifique a saída estruturada do pipeline de Cobertura (n8n).")
                        self.clear_action()
                    else:
                        st.session_state.user_answers = answers
                        st.session_state.matriz       = matriz
                        st.session_state.step         = 3
                        self.clear_action()
                        st.rerun()
                except Exception as e:
                    self._err(e)
                    self.clear_action()

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 3 – Matriz de Cobertura (accordion + Opção B + add row + validation)
    # ──────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _validate_matriz_fields(nid, nfunc, nreq, ncen, ncat, npri, ncrit) -> list:
        """Retorna lista de nomes de campos obrigatórios que estão vazios."""
        missing = []
        if not nid or not nid.strip():     missing.append("ID")
        if not nfunc or not nfunc.strip(): missing.append("Funcionalidade")
        if not nreq or not nreq.strip():   missing.append("Requisito")
        if not ncen or not ncen.strip():   missing.append("Cenário")
        if not ncat or not ncat.strip():   missing.append("Categoria")
        if not npri or not npri.strip():   missing.append("Prioridade")
        if not ncrit or not ncrit.strip(): missing.append("Criticidade")
        return missing

    def _render_matriz_form(self, prefix: str, row: dict):
        """
        Renderiza o formulário (criação OU edição) de uma linha da Matriz.
        prefix: prefixo único de widget key (ex: 'mid_3' ou 'newm')
        row: dict com valores atuais (vazio para criação)
        Retorna o dict com os novos valores (apenas para leitura dos widgets).
        """
        c1, c2, c3 = st.columns(3)
        with c1:
            nid   = st.text_input("ID *", value=row.get('id',''), key=f"{prefix}_id")
            nfunc = st.text_input("Funcionalidade *", value=row.get('funcionalidade',''), key=f"{prefix}_func")
            nreq  = st.text_input("Requisito *", value=row.get('requisito',''), key=f"{prefix}_req")
        with c2:
            ncen  = st.text_area("Cenário *", value=row.get('cenario',''), key=f"{prefix}_cen", height=100)
            ncat  = st.text_input("Categoria *", value=row.get('categoria',''), key=f"{prefix}_cat")
        with c3:
            opts_pri  = ["Alta","Média","Baixa"]
            opts_crit = ["Alta","Média","Baixa"]

            def idx_of(opts, val):
                try: return [opt.lower() for opt in opts].index((val or '').lower())
                except ValueError: return 0

            npri  = st.selectbox("Prioridade *",  opts_pri,  index=idx_of(opts_pri,  row.get('prioridade')),  key=f"{prefix}_pri")
            ncrit = st.selectbox("Criticidade *", opts_crit, index=idx_of(opts_crit, row.get('criticidade')), key=f"{prefix}_crit")
            nobs  = st.text_input("Observações", value=row.get('observacoes',''), key=f"{prefix}_obs")

        return {
            "id": nid, "funcionalidade": nfunc, "requisito": nreq,
            "cenario": ncen, "categoria": ncat, "prioridade": npri,
            "criticidade": ncrit, "observacoes": nobs
        }

    def step_3(self):
        st.subheader("Passo 3 – Refinamento da Matriz de Cobertura")
        matriz = st.session_state.matriz

        if not matriz:
            st.info("A Matriz de Cobertura está vazia.")
        else:
            st.info(
                f"**{len(matriz)} cenário(s) mapeado(s)**. "
                "Clique em uma linha para ver os detalhes e acessar as opções de edição ou exclusão."
            )

        editing_any = False

        for i, row in enumerate(matriz):
            is_editing = st.session_state.get(f"edit_m_{i}", False)
            if is_editing:
                editing_any = True

            expander_label = f"**{row.get('id', f'MC-{i+1:03d}')}** – {row.get('cenario', '')}"

            with st.expander(expander_label, expanded=is_editing):

                if is_editing:
                    with st.container(border=True):
                        new_vals = self._render_matriz_form(f"m{i}", row)

                        col_save, col_cancel = st.columns(2)
                        with col_save:
                            if st.button("💾 Salvar Alterações", key=f"save_m_{i}", type="primary", use_container_width=True):
                                missing = self._validate_matriz_fields(
                                    new_vals["id"], new_vals["funcionalidade"], new_vals["requisito"],
                                    new_vals["cenario"], new_vals["categoria"],
                                    new_vals["prioridade"], new_vals["criticidade"]
                                )
                                if missing:
                                    st.error(
                                        "❌ Não foi possível salvar. Preencha os campos obrigatórios: "
                                        + ", ".join(missing) + "."
                                    )
                                else:
                                    st.session_state.matriz[i] = new_vals
                                    st.session_state[f"edit_m_{i}"] = False
                                    st.rerun()
                        with col_cancel:
                            if st.button("✖ Cancelar", key=f"cancel_m_{i}", use_container_width=True):
                                st.session_state[f"edit_m_{i}"] = False
                                st.rerun()

                else:
                    pri_badge  = self._priority_badge(row.get('prioridade', ''))
                    crit_badge = self._priority_badge(row.get('criticidade', ''))

                    self._read_only_table([
                        ("ID",             row.get('id', '—')),
                        ("Funcionalidade", row.get('funcionalidade', '—')),
                        ("Requisito",      row.get('requisito', '—')),
                        ("Cenário",        row.get('cenario', '—')),
                        ("Categoria",      row.get('categoria', '—')),
                        ("Prioridade",     pri_badge),
                        ("Criticidade",    crit_badge),
                        ("Observações",    row.get('observacoes') or '—'),
                    ])

                    st.markdown("<div style='margin-top:0.75rem'></div>", unsafe_allow_html=True)
                    col_edit, col_del, _ = st.columns([1, 1, 6])
                    with col_edit:
                        if st.button("✏️ Editar", key=f"btn_edit_m_{i}", use_container_width=True):
                            st.session_state[f"edit_m_{i}"] = True
                            st.rerun()
                    with col_del:
                        if st.button("🗑️ Excluir", key=f"btn_del_m_{i}", type="primary", use_container_width=True):
                            confirm_deletion_modal('matriz', i)

        # ── Adicionar nova linha ────────────────────────────────────────────
        st.markdown("<div style='margin-top:0.5rem'></div>", unsafe_allow_html=True)

        if st.session_state.adding_matriz_row:
            with st.expander("**➕ Novo Cenário**", expanded=True):
                with st.container(border=True):
                    blank_row = {"prioridade": "", "criticidade": ""}
                    if "newm_id" not in st.session_state:
                        st.session_state["newm_id"] = self._next_matriz_id(matriz)
                    new_vals = self._render_matriz_form("newm", blank_row)

                    col_save, col_cancel = st.columns(2)
                    with col_save:
                        if st.button("💾 Salvar Novo Cenário", key="save_newm", type="primary", use_container_width=True):
                            missing = self._validate_matriz_fields(
                                new_vals["id"], new_vals["funcionalidade"], new_vals["requisito"],
                                new_vals["cenario"], new_vals["categoria"],
                                new_vals["prioridade"], new_vals["criticidade"]
                            )
                            if missing:
                                st.error(
                                    "❌ Não foi possível salvar. Preencha os campos obrigatórios: "
                                    + ", ".join(missing) + "."
                                )
                            else:
                                st.session_state.matriz.append(new_vals)
                                st.session_state.adding_matriz_row = False
                                self._clear_widget_states()
                                st.rerun()
                    with col_cancel:
                        if st.button("✖ Cancelar", key="cancel_newm", use_container_width=True):
                            confirm_discard_new_modal("adding_matriz_row")
        else:
            if st.button("➕ Adicionar Novo Cenário à Matriz", use_container_width=True,
                        disabled=editing_any or st.session_state.is_processing):
                st.session_state.adding_matriz_row = True
                st.rerun()

        st.divider()

        col1, col2 = st.columns([1, 3])
        with col1:
            if st.button("← Voltar", use_container_width=True, disabled=st.session_state.is_processing):
                st.session_state.step = 2
                st.rerun()
        with col2:
            if editing_any or st.session_state.adding_matriz_row:
                st.warning("⚠️ Salve ou cancele a edição/criação em aberto para prosseguir.")
            else:
                st.button("🚀 Gerar Casos de Teste", use_container_width=True, type="primary",
                          on_click=self.trigger_action, args=("generate_cases",),
                          disabled=st.session_state.is_processing)

        if st.session_state.current_action == "generate_cases":
            with st.spinner("Processando a geração de steps de casos de teste. Isso poderá levar alguns minutos..."):
                try:
                    resp = self.client.trigger_generation(
                        st.session_state.doc_text, st.session_state.matriz,
                        st.session_state.user_answers, st.session_state.project_name
                    )
                    casos = resp.get("casos_de_teste") or []
                    if not casos:
                        st.error("❌ Lista de casos vazia. Valide a chave JSON de saída no n8n.")
                        self.clear_action()
                    else:
                        st.session_state.test_cases = casos
                        st.session_state.step       = 4
                        self.clear_action()
                        st.rerun()
                except Exception as e:
                    self._err(e)
                    self.clear_action()

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 4 – Casos de Teste (accordion + Opção B + add case + dynamic steps)
    # ──────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _validate_tc_fields(titulo, pre, steps_data) -> list:
        """
        Valida campos obrigatórios de um Caso de Teste.
        steps_data: lista de dicts {"acao": str, "resultado_esperado": str}
        """
        missing = []
        if not titulo or not titulo.strip():
            missing.append("Título")
        if not pre or not pre.strip():
            missing.append("Pré-condições")
        if not steps_data:
            missing.append("ao menos 1 Step")
        else:
            for s_idx, step in enumerate(steps_data, start=1):
                if not step.get("acao", "").strip():
                    missing.append(f"Ação do Step {s_idx}")
                if not step.get("resultado_esperado", "").strip():
                    missing.append(f"Resultado Esperado do Step {s_idx}")
        return missing

    def _ensure_steps_state(self, steps_state_key: str, initial_steps: list):
        """Garante que a lista de steps editável (com uid) exista no session_state."""
        if steps_state_key not in st.session_state:
            if initial_steps:
                st.session_state[steps_state_key] = [
                    {"uid": str(uuid.uuid4()), "acao": s.get("acao", ""),
                     "resultado_esperado": s.get("resultado_esperado", "")}
                    for s in initial_steps
                ]
            else:
                st.session_state[steps_state_key] = [
                    {"uid": str(uuid.uuid4()), "acao": "", "resultado_esperado": ""}
                ]

    def _render_steps_editor(self, steps_state_key: str, widget_prefix: str):
        """
        Renderiza os steps editáveis com campos Ação/Esperado e botão de
        remover por step (com modal). Lê os valores atuais dos widgets de
        volta para session_state[steps_state_key] para manter sincronizado.
        Retorna a lista de steps (dicts com acao/resultado_esperado) lida dos widgets.
        """
        steps_list = st.session_state[steps_state_key]
        st.markdown("**Test Steps:**")

        result_steps = []
        for s_idx, step in enumerate(steps_list):
            uid = step["uid"]
            cA, cB, cDel = st.columns([5, 5, 1])
            with cA:
                acao = st.text_area(f"Ação {s_idx+1} *", value=step.get("acao",""),
                                    key=f"{widget_prefix}_acao_{uid}", height=80)
            with cB:
                esp = st.text_area(f"Esperado {s_idx+1} *", value=step.get("resultado_esperado",""),
                                   key=f"{widget_prefix}_esp_{uid}", height=80)
            with cDel:
                st.markdown("<div style='margin-top:1.8rem'></div>", unsafe_allow_html=True)
                if st.button("🗑️", key=f"{widget_prefix}_delstep_{uid}", help="Remover este step",
                            disabled=len(steps_list) <= 1):
                    confirm_step_deletion_modal(steps_state_key, uid)

            result_steps.append({"uid": uid, "acao": acao, "resultado_esperado": esp})

        if len(steps_list) <= 1:
            st.caption("ℹ️ É necessário manter ao menos 1 step. O botão de remover é desativado no último step restante.")

        # sincroniza valores digitados de volta no session_state
        st.session_state[steps_state_key] = result_steps

        if st.button("➕ Adicionar Step", key=f"{widget_prefix}_addstep"):
            st.session_state[steps_state_key].append(
                {"uid": str(uuid.uuid4()), "acao": "", "resultado_esperado": ""}
            )
            st.rerun()

        return [{"acao": s["acao"], "resultado_esperado": s["resultado_esperado"]} for s in result_steps]

    def step_4(self):
        st.subheader("Passo 4 – Console de Casos de Teste")
        test_cases = st.session_state.test_cases

        if not test_cases:
            st.info("Nenhum caso de teste compilado.")
        else:
            st.info(
                f"**{len(test_cases)} script(s)** consolidados. "
                "Clique em um caso para ver os detalhes e acessar as opções de edição ou exclusão."
            )

        editing_any = False

        for idx, tc in enumerate(test_cases):
            is_editing = st.session_state.get(f"edit_tc_{idx}", False)
            if is_editing:
                editing_any = True

            expander_label = f"**TC-{idx+1:02d}** – {tc.get('titulo', '')}"

            with st.expander(expander_label, expanded=is_editing):

                if is_editing:
                    with st.container(border=True):
                        titulo = st.text_input("Título *", value=tc.get('titulo',''), key=f"tt_{idx}")
                        pre    = st.text_area("Pré-condições *", value=tc.get('pre_condicoes',''), key=f"tp_{idx}", height=70)

                        steps_key = f"edit_steps_{idx}"
                        self._ensure_steps_state(steps_key, tc.get('passos', []))
                        current_steps = self._render_steps_editor(steps_key, f"etc{idx}")

                        col_save, col_cancel = st.columns(2)
                        with col_save:
                            if st.button("💾 Salvar Caso de Teste", key=f"save_tc_{idx}", type="primary", use_container_width=True):
                                missing = self._validate_tc_fields(titulo, pre, current_steps)
                                if missing:
                                    st.error(
                                        "❌ Não foi possível salvar. Preencha os campos obrigatórios: "
                                        + ", ".join(missing) + "."
                                    )
                                else:
                                    st.session_state.test_cases[idx] = {
                                        "titulo": titulo, "pre_condicoes": pre,
                                        "passos": [
                                            {"numero": n+1, "acao": s["acao"], "resultado_esperado": s["resultado_esperado"]}
                                            for n, s in enumerate(current_steps)
                                        ]
                                    }
                                    st.session_state[f"edit_tc_{idx}"] = False
                                    del st.session_state[steps_key]
                                    st.rerun()
                        with col_cancel:
                            if st.button("✖ Cancelar", key=f"cancel_tc_{idx}", use_container_width=True):
                                st.session_state[f"edit_tc_{idx}"] = False
                                if steps_key in st.session_state:
                                    del st.session_state[steps_key]
                                st.rerun()

                else:
                    self._read_only_table([
                        ("Pré-condições", tc.get('pre_condicoes') or '—'),
                    ])

                    passos = tc.get('passos', [])
                    if passos:
                        st.markdown("<div style='margin-top:0.6rem'></div>", unsafe_allow_html=True)
                        html = (
                            '<table style="width:100%;border-collapse:collapse;font-size:0.83rem;margin-top:0.3rem">'
                            '<thead><tr style="background:#3A3A3A;color:#fff">'
                            '<th style="padding:6px 10px;text-align:left;width:40px">#</th>'
                            '<th style="padding:6px 10px;text-align:left;width:48%">Ação</th>'
                            '<th style="padding:6px 10px;text-align:left">Resultado Esperado</th>'
                            '</tr></thead><tbody>'
                        )
                        for s_idx, step in enumerate(passos):
                            bg = "#ffffff" if s_idx % 2 == 0 else "#f5f5f5"
                            html += (
                                f'<tr style="background:{bg};border-bottom:1px solid #e0e0e0">'
                                f'<td style="padding:6px 10px;color:#888;font-weight:600">{step.get("numero","")}</td>'
                                f'<td style="padding:6px 10px;color:#2d2d2d">{step.get("acao","")}</td>'
                                f'<td style="padding:6px 10px;color:#2d2d2d">{step.get("resultado_esperado","")}</td>'
                                f'</tr>'
                            )
                        html += "</tbody></table>"
                        st.markdown(html, unsafe_allow_html=True)

                    st.markdown("<div style='margin-top:0.75rem'></div>", unsafe_allow_html=True)
                    col_edit, col_del, _ = st.columns([1, 1, 6])
                    with col_edit:
                        if st.button("✏️ Editar", key=f"btn_edit_tc_{idx}", use_container_width=True):
                            st.session_state[f"edit_tc_{idx}"] = True
                            st.rerun()
                    with col_del:
                        if st.button("🗑️ Excluir", key=f"btn_del_tc_{idx}", type="primary", use_container_width=True):
                            confirm_deletion_modal('test_cases', idx)

        # ── Adicionar novo Caso de Teste ────────────────────────────────────
        st.markdown("<div style='margin-top:0.5rem'></div>", unsafe_allow_html=True)

        if st.session_state.adding_test_case:
            with st.expander("**➕ Novo Caso de Teste**", expanded=True):
                with st.container(border=True):
                    titulo = st.text_input("Título *", value="", key="newtc_titulo")
                    pre    = st.text_area("Pré-condições *", value="", key="newtc_pre", height=70)

                    new_steps_key = "new_steps_tc"
                    self._ensure_steps_state(new_steps_key, [])
                    current_steps = self._render_steps_editor(new_steps_key, "newtc")

                    col_save, col_cancel = st.columns(2)
                    with col_save:
                        if st.button("💾 Salvar Novo Caso de Teste", key="save_newtc", type="primary", use_container_width=True):
                            missing = self._validate_tc_fields(titulo, pre, current_steps)
                            if missing:
                                st.error(
                                    "❌ Não foi possível salvar. Preencha os campos obrigatórios: "
                                    + ", ".join(missing) + "."
                                )
                            else:
                                st.session_state.test_cases.append({
                                    "titulo": titulo, "pre_condicoes": pre,
                                    "passos": [
                                        {"numero": n+1, "acao": s["acao"], "resultado_esperado": s["resultado_esperado"]}
                                        for n, s in enumerate(current_steps)
                                    ]
                                })
                                st.session_state.adding_test_case = False
                                del st.session_state[new_steps_key]
                                self._clear_widget_states()
                                st.rerun()
                    with col_cancel:
                        if st.button("✖ Cancelar", key="cancel_newtc", use_container_width=True):
                            confirm_discard_new_modal("adding_test_case")
        else:
            if st.button("➕ Adicionar Novo Caso de Teste", use_container_width=True,
                        disabled=editing_any or st.session_state.is_processing):
                st.session_state.adding_test_case = True
                st.rerun()

        st.divider()

        col1, col2 = st.columns([1, 3])
        with col1:
            if st.button("← Voltar", use_container_width=True, disabled=st.session_state.is_processing):
                st.session_state.step = 3
                st.rerun()
        with col2:
            if editing_any or st.session_state.adding_test_case:
                st.warning("⚠️ Salve ou cancele a edição/criação em aberto para prosseguir com o Build.")
            else:
                st.button("📥 Consolidar e Construir Artefatos", use_container_width=True, type="primary",
                          on_click=self.trigger_action, args=("build_artifacts",),
                          disabled=st.session_state.is_processing)

        if st.session_state.current_action == "build_artifacts":
            st.session_state.csv_content = AzureCsvFormatter.generate_csv_content(
                st.session_state.test_cases, st.session_state.project_name
            )
            st.session_state.step = 5
            self.clear_action()
            st.rerun()

    def step_5(self):
        st.subheader("Passo 5 – Artefatos Finalizados")
        st.success("🎉 Build concluída sem apontamentos.")

        project   = st.session_state.project_name
        safe_name = project.replace(' ', '_')

        col1, col2 = st.columns(2)
        with col1:
            st.markdown("### 📄 Test Suite – Azure DevOps (CSV)")
            csv_bytes = ('\ufeff' + st.session_state.csv_content).encode('utf-8')
            st.download_button("⬇️ Baixar Test Suite (CSV)", data=csv_bytes,
                               file_name=f"QA_Export_{safe_name}.csv",
                               mime="text/csv", use_container_width=True)
        with col2:
            st.markdown("### 📑 Documentação Técnica – PDF Report")
            with st.spinner("Gerando binários do PDF…"):
                pdf_bytes = PdfReportGenerator.generate(
                    project, st.session_state.matriz, st.session_state.test_cases
                )
            st.download_button("⬇️ Baixar Documentação Técnica (PDF)", data=pdf_bytes,
                               file_name=f"QA_Report_{safe_name}.pdf",
                               mime="application/pdf", use_container_width=True)

        st.divider()
        if st.button("🔄 Flush Session - Nova Análise", use_container_width=True, disabled=st.session_state.is_processing):
            st.session_state.clear()
            st.rerun()

    def run(self):
        self._header()
        self._progress()
        step = st.session_state.step
        if   step == 1: self.step_1()
        elif step == 2: self.step_2()
        elif step == 3: self.step_3()
        elif step == 4: self.step_4()
        elif step == 5: self.step_5()


if __name__ == "__main__":
    UserInterface().run()