import streamlit as st
import requests
import json
import os
import io
import fitz  # pymupdf
from datetime import datetime
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
    TableStyle, PageBreak, Image, HRFlowable, KeepTogether
)

load_dotenv()

# ─── Cores Refuturiza ────────────────────────────────────────────────────────
COR_LARANJA       = colors.HexColor('#F15A24')
COR_CINZA_ESC     = colors.HexColor('#3A3A3A')
COR_CINZA_MED     = colors.HexColor('#6B6B6B')
COR_LARANJA_CLARO = colors.HexColor('#FAE5DC')
COR_CINZA_LIN     = colors.HexColor('#F5F5F5')
COR_BRANCO        = colors.white

LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logo_refu_1.png')


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════
class AppConfiguration:
    def __init__(self):
        self.webhook_analysis = os.getenv(
            "N8N_WEBHOOK_URL_ANALYSIS",
            "http://localhost:5678/webhook/qa-testgen-analysis"
        )
        self.webhook_matrix = os.getenv(
            "N8N_WEBHOOK_URL_MATRIX",
            "http://localhost:5678/webhook/qa-testgen-matrix"
        )
        self.webhook_generation = os.getenv(
            "N8N_WEBHOOK_URL_GENERATION",
            "http://localhost:5678/webhook/qa-testgen-generation"
        )


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
        canvas.drawRightString(w - 18, h - 42, datetime.now().strftime('%d/%m/%Y %H:%M'))
        
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
        story.append(Paragraph("Relatório de QA", styles['title']))
        story.append(Paragraph(
            f"Projeto: <b>{project_name}</b> &nbsp;|&nbsp; "
            f"Gerado em {datetime.now().strftime('%d/%m/%Y às %H:%M')}",
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
                ('BACKGROUND',   (0,0), (-1,0),  COR_LARANJA),
                ('ROWBACKGROUNDS',(0,1),(-1,-1),  [COR_BRANCO, COR_CINZA_LIN]),
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
                ('ROWBACKGROUNDS',(0,1),(-1,-1),  [COR_BRANCO, COR_CINZA_LIN]),
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
        # Higienização e failover para aquisição de chave de API
        api_key = os.getenv("N8N_API_KEY")
        if not api_key:
            try:
                api_key = st.secrets.get("N8N_API_KEY", "")
            except Exception:
                api_key = ""
        self.headers = {"x-api-key": api_key} if api_key else {}

    def _safe_json_parse(self, response: requests.Response) -> dict:
        # Resolve o erro de JSONDecodeError limpando anomalias Markdown (```json ... ```) ou texto puro
        try:
            return response.json()
        except json.JSONDecodeError:
            raw_text = response.text.strip()
            if raw_text.startswith("```json"):
                raw_text = raw_text.replace("```json", "").replace("```", "").strip()
            elif raw_text.startswith("```"):
                raw_text = raw_text.replace("```", "").strip()
            try:
                return json.loads(raw_text)
            except json.JSONDecodeError as decode_error:
                raise ValueError(f"Payload de resposta inválido (não é JSON). Resposta bruta: {response.text[:500]}") from decode_error

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
        st.set_page_config(page_title="QA TestGen - Azure DevOps", page_icon="🧪", layout="wide")
        self._init_state()
        self.config = AppConfiguration()
        self.client = WebhookClient(self.config)

    def _init_state(self):
        defaults = {
            'step': 1, 'doc_text': '', 'project_name': '',
            'questions': [], 'user_answers': {},
            'matriz': [], 'test_cases': [], 'csv_content': '',
        }
        for k, v in defaults.items():
            if k not in st.session_state:
                st.session_state[k] = v

    def _header(self):
        st.markdown("""
        <div style="background:linear-gradient(135deg,#F15A24,#c94a1a);
                    padding:1.5rem;border-radius:8px;margin-bottom:1.5rem;">
            <h1 style="color:white;margin:0;">🧪 QA TestGen – Refuturiza Automation</h1>
            <p style="color:white;margin:.3rem 0 0;font-size:1.05rem;">
                Gerador Inteligente de Casos de Teste — Azure DevOps Integration
            </p>
        </div>""", unsafe_allow_html=True)

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
            st.error(f"❌ Erro de Formato de Resposta (JSONDecode): {exception}")
        elif isinstance(exception, requests.exceptions.Timeout):
            st.error("⏱️ Timeout: o n8n demorou demais para responder.")
        elif isinstance(exception, requests.exceptions.ConnectionError):
            st.error("🔌 Não foi possível conectar ao n8n.")
        elif isinstance(exception, requests.exceptions.HTTPError):
            st.error(f"❌ Erro HTTP do n8n: {exception}")
        else:
            st.error(f"❌ Erro inesperado: {exception}")

    def step_1(self):
        st.subheader("Passo 1 – Setup e Documentação")
        col1, col2 = st.columns(2)
        with col1:
            project = st.text_input("Nome do Projeto *", placeholder="Ex: Passaporte Refuturiza")
        with col2:
            uploaded = st.file_uploader("Documento de Requisitos *", type=["pdf","txt","docx"])

        if not project or not uploaded:
            st.info("Preencha o nome do projeto e faça o upload do documento para continuar.")
            return

        if st.button("🔍 Executar Análise de Cobertura (IA)", use_container_width=True, type="primary"):
            with st.spinner("Extraindo texto..."):
                text = DocumentProcessor.extract_plain_text(uploaded)
            if not text:
                st.error("Não foi possível extrair texto. Verifique o arquivo.")
                return
            with st.spinner("Analisando com IA…"):
                try:
                    resp = self.client.trigger_analysis(text, project)
                    st.session_state.doc_text     = text
                    st.session_state.project_name = project
                    st.session_state.questions    = resp.get("duvidas") or []
                    st.session_state.step         = 2
                    st.rerun()
                except Exception as e:
                    self._err(e)

    def step_2(self):
        st.subheader("Passo 2 – Esclarecimentos")
        questions = st.session_state.questions
        answers   = {}

        if not questions:
            st.success("✅ A IA não identificou ambiguidades. Prossiga para gerar a Matriz.")
        else:
            st.info(f"A IA identificou **{len(questions)} ponto(s) crítico(s)**.")
            for q in questions:
                qid = str(q.get('id', '0'))
                st.markdown(f"**❓ #{qid}:** {q.get('pergunta', '')}")
                answers[qid] = st.text_area(f"Resposta #{qid}", key=f"q_{qid}",
                                            placeholder="Descreva a regra de negócio ou decisão…")

        col1, col2 = st.columns([1, 3])
        with col1:
            if st.button("← Voltar", use_container_width=True):
                st.session_state.step = 1
                st.rerun()
        with col2:
            if st.button("📊 Gerar Matriz de Cobertura", use_container_width=True, type="primary"):
                with st.spinner("Gerando Matriz com IA…"):
                    try:
                        resp = self.client.trigger_matrix(
                            st.session_state.doc_text, answers,
                            st.session_state.project_name
                        )
                        matriz = resp.get("matriz") or []
                        if not matriz:
                            st.error("❌ Matriz vazia. Verifique a saída estruturada do n8n.")
                            return
                        st.session_state.user_answers = answers
                        st.session_state.matriz       = matriz
                        st.session_state.step         = 3
                        st.rerun()
                    except Exception as e:
                        self._err(e)

    def step_3(self):
        st.subheader("Passo 3 – Matriz de Cobertura")
        matriz = st.session_state.matriz
        st.info(f"**{len(matriz)} cenário(s)**. Edite os campos abaixo se necessário.")

        headers_cols = ["id","funcionalidade","requisito","cenario",
                        "categoria","prioridade","criticidade","observacoes"]

        def norm(row):
            aliases = {"scenario":"cenario","feature":"funcionalidade",
                       "requirement":"requisito","category":"categoria",
                       "priority":"prioridade","criticality":"criticidade",
                       "notes":"observacoes","observations":"observacoes"}
            out = {aliases.get(k.lower(), k.lower()): v for k,v in row.items()}
            return {col: out.get(col,'') for col in headers_cols}

        normalized   = [norm(row) for row in matriz]
        edited_matriz = []
        opts_pri  = ["Alta","Média","Baixa"]
        opts_crit = ["Alta","Média","Baixa"]

        def idx_of(opts, val):
            try: return [opt.lower() for opt in opts].index((val or '').lower())
            except Exception: return 0

        for i, row in enumerate(normalized):
            with st.expander(f"**{row['id'] or f'MC-{i+1:03d}'}** – {row['cenario']}", expanded=(i==0)):
                col1, col2, col3 = st.columns(3)
                with col1:
                    nid   = st.text_input("ID",             value=row['id'],             key=f"mid_{i}")
                    nfunc = st.text_input("Funcionalidade", value=row['funcionalidade'], key=f"mfunc_{i}")
                    nreq  = st.text_input("Requisito",      value=row['requisito'],      key=f"mreq_{i}")
                with col2:
                    ncen  = st.text_area("Cenário",         value=row['cenario'],        key=f"mcen_{i}", height=100)
                    ncat  = st.text_input("Categoria",      value=row['categoria'],      key=f"mcat_{i}")
                with col3:
                    npri  = st.selectbox("Prioridade",      opts_pri,  index=idx_of(opts_pri, row['prioridade']),  key=f"mpri_{i}")
                    ncrit = st.selectbox("Criticidade",     opts_crit, index=idx_of(opts_crit,row['criticidade']), key=f"mcrit_{i}")
                    nobs  = st.text_input("Observações",    value=row['observacoes'],    key=f"mobs_{i}")

                edited_matriz.append({"id":nid,"funcionalidade":nfunc,"requisito":nreq,
                                      "cenario":ncen,"categoria":ncat,"prioridade":npri,
                                      "criticidade":ncrit,"observacoes":nobs})

        col1, col2 = st.columns([1, 3])
        with col1:
            if st.button("← Voltar", use_container_width=True):
                st.session_state.step = 2
                st.rerun()
        with col2:
            if st.button("🚀 Gerar Casos de Teste", use_container_width=True, type="primary"):
                st.session_state.matriz = edited_matriz
                with st.spinner("Gerando Casos de Teste com IA… pode levar alguns minutos."):
                    try:
                        resp = self.client.trigger_generation(
                            st.session_state.doc_text, edited_matriz,
                            st.session_state.user_answers, st.session_state.project_name
                        )
                        casos = resp.get("casos_de_teste") or []
                        if not casos:
                            st.error("❌ Lista de casos vazia. Verifique a saída estruturada do n8n.")
                            return
                        st.session_state.test_cases = casos
                        st.session_state.step       = 4
                        st.rerun()
                    except Exception as e:
                        self._err(e)

    def step_4(self):
        st.subheader("Passo 4 – Revisão dos Casos de Teste")
        test_cases = st.session_state.test_cases
        st.info(f"**{len(test_cases)} caso(s)** gerado(s). Edite se necessário.")

        edited = []
        for idx, tc in enumerate(test_cases):
            with st.expander(f"**TC-{idx+1:02d}** – {tc.get('titulo','')}", expanded=(idx==0)):
                titulo = st.text_input("Título",       value=tc.get('titulo',''),        key=f"tt_{idx}")
                pre    = st.text_area("Pré-condições", value=tc.get('pre_condicoes',''), key=f"tp_{idx}", height=70)
                passos = tc.get('passos', [])
                novos  = []
                if passos:
                    st.markdown("**Passos:**")
                    for s, step in enumerate(passos):
                        colA, colB = st.columns(2)
                        with colA:
                            acao = st.text_area(f"Ação {step.get('numero',s+1)}",
                                                value=step.get('acao',''),
                                                key=f"ta_{idx}_{s}", height=80)
                        with colB:
                            esp = st.text_area(f"Esperado {step.get('numero',s+1)}",
                                               value=step.get('resultado_esperado',''),
                                               key=f"te_{idx}_{s}", height=80)
                        novos.append({"numero":step.get('numero',s+1),"acao":acao,"resultado_esperado":esp})
                edited.append({"titulo":titulo,"pre_condicoes":pre,"passos":novos})

        col1, col2 = st.columns([1, 3])
        with col1:
            if st.button("← Voltar", use_container_width=True):
                st.session_state.step = 3
                st.rerun()
        with col2:
            if st.button("📥 Gerar Exportações", use_container_width=True, type="primary"):
                st.session_state.test_cases  = edited
                st.session_state.csv_content = AzureCsvFormatter.generate_csv_content(
                    edited, st.session_state.project_name
                )
                st.session_state.step = 5
                st.rerun()

    def step_5(self):
        st.subheader("Passo 5 – Download")
        st.success("🎉 Exportações prontas!")

        project   = st.session_state.project_name
        safe_name = project.replace(' ', '_')

        col1, col2 = st.columns(2)
        with col1:
            st.markdown("### 📄 CSV – Azure DevOps")
            csv_bytes = ('\ufeff' + st.session_state.csv_content).encode('utf-8')
            st.download_button("⬇️ Baixar CSV", data=csv_bytes,
                               file_name=f"QA_Export_{safe_name}.csv",
                               mime="text/csv", use_container_width=True)
        with col2:
            st.markdown("### 📑 PDF – Relatório Completo")
            with st.spinner("Gerando PDF…"):
                pdf_bytes = PdfReportGenerator.generate(
                    project, st.session_state.matriz, st.session_state.test_cases
                )
            st.download_button("⬇️ Baixar PDF", data=pdf_bytes,
                               file_name=f"QA_Report_{safe_name}.pdf",
                               mime="application/pdf", use_container_width=True)

        st.divider()
        with st.expander("👀 Pré-visualização do CSV"):
            st.code(st.session_state.csv_content[:3000], language="text")

        if st.button("🔄 Iniciar Nova Análise", use_container_width=True):
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