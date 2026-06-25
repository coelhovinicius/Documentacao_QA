import streamlit as st
import requests
import json
import os
import base64
import zipfile
import pandas as pd
import fitz  # PyMuPDF
from fpdf import FPDF
from docx import Document
from dotenv import load_dotenv

load_dotenv()

class AppConfiguration:
    def __init__(self):
        # Por que: Centralização das URLs de webhook permite failover ou alteração de ambiente via .env sem tocar no código compilado.
        self.webhook_analysis = os.getenv("N8N_WEBHOOK_URL_ANALYSIS", "http://localhost:5678/webhook/qa-testgen-analysis")
        self.webhook_coverage = os.getenv("N8N_WEBHOOK_URL_COVERAGE", "http://localhost:5678/webhook/qa-testgen-coverage")
        self.webhook_generation = os.getenv("N8N_WEBHOOK_URL_GENERATION", "http://localhost:5678/webhook/qa-testgen-generation")
        self.logo_path = os.getenv("APP_LOGO_PATH", "logo.png")


class DocumentProcessor:
    @staticmethod
    def extract_plain_text(uploaded_file) -> str:
        # Por que: Fallback seguro para texto. Mantemos PyMuPDF para PDF por ser mais performático em sistemas Windows.
        file_extension = uploaded_file.name.split('.')[-1].lower()
        extracted_text = ""
        uploaded_file.seek(0)
        try:
            if file_extension == "pdf":
                pdf_doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
                for page in pdf_doc:
                    extracted_text += page.get_text() + "\n"
            elif file_extension == "docx":
                doc = Document(uploaded_file)
                for paragraph in doc.paragraphs:
                    extracted_text += paragraph.text + "\n"
            elif file_extension == "txt":
                extracted_text = uploaded_file.getvalue().decode("utf-8")
            return extracted_text
        except Exception as exception:
            st.error(f"Erro ao extrair o texto do arquivo: {exception}")
            return ""

    @staticmethod
    def extract_images_as_base64(uploaded_file) -> list:
        # Por que: Converte imagens embutidas em Base64 para consumo direto pelo LLM via Vision API (ex: Gemini 1.5).
        file_extension = uploaded_file.name.split('.')[-1].lower()
        images_b64 = []
        uploaded_file.seek(0)
        
        try:
            if file_extension == "pdf":
                pdf_doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
                for page in pdf_doc:
                    for img in page.get_images(full=True):
                        xref = img[0]
                        base_image = pdf_doc.extract_image(xref)
                        image_bytes = base_image["image"]
                        images_b64.append(base64.b64encode(image_bytes).decode("utf-8"))
            
            elif file_extension == "docx":
                # Por que: Arquivos DOCX são containers ZIP. Acessar word/media/ é a forma mais direta de extrair os binários de imagem.
                with zipfile.ZipFile(uploaded_file) as docx_zip:
                    for item in docx_zip.namelist():
                        if item.startswith('word/media/'):
                            image_bytes = docx_zip.read(item)
                            images_b64.append(base64.b64encode(image_bytes).decode("utf-8"))
                            
            return images_b64
        except Exception as exception:
            st.warning(f"Aviso: Falha ao tentar extrair imagens do documento: {exception}")
            return []


class DataTransformer:
    @staticmethod
    def flatten_test_cases(test_cases: list) -> pd.DataFrame:
        # Por que: Grids UI (st.data_editor) não suportam arrays aninhados eficientemente. Achatar a estrutura permite edição em lote (CRUD).
        flat_list = []
        for tc in test_cases:
            tc_id = tc.get('id', '')
            tc_title = tc.get('titulo', '')
            tc_pre = tc.get('pre_condicoes', '')
            passos = tc.get('passos', [])
            
            if not passos:
                flat_list.append({
                    "TC_ID": tc_id, "Titulo": tc_title, "Pre_Condicoes": tc_pre,
                    "Passo": 1, "Acao": "", "Esperado": ""
                })
            else:
                for step in passos:
                    flat_list.append({
                        "TC_ID": tc_id, "Titulo": tc_title, "Pre_Condicoes": tc_pre,
                        "Passo": step.get('numero', ''),
                        "Acao": step.get('acao', ''),
                        "Esperado": step.get('resultado_esperado', '')
                    })
        return pd.DataFrame(flat_list)

    @staticmethod
    def nest_test_cases(flat_df: pd.DataFrame) -> list:
        # Por que: Restaura a estrutura hierárquica do JSON (BDD) exigida para formatação final do artefato exportado.
        nested_dict = {}
        for _, row in flat_df.iterrows():
            tc_id = str(row.get('TC_ID', ''))
            if tc_id not in nested_dict:
                nested_dict[tc_id] = {
                    "id": tc_id,
                    "titulo": str(row.get('Titulo', '')),
                    "pre_condicoes": str(row.get('Pre_Condicoes', '')),
                    "passos": []
                }
            nested_dict[tc_id]['passos'].append({
                "numero": row.get('Passo', 0),
                "acao": str(row.get('Acao', '')),
                "resultado_esperado": str(row.get('Esperado', ''))
            })
        return list(nested_dict.values())


class PdfReportExporter:
    @staticmethod
    def generate_pdf(project_name: str, coverage_df: pd.DataFrame, test_cases: list, logo_path: str = None) -> bytes:
        # Por que: fpdf2 permite geração dinâmica e programática sem dependências externas complexas de SO.
        pdf = FPDF()
        pdf.add_page()
        pdf.set_auto_page_break(auto=True, margin=15)
        
        # Header
        pdf.set_font("helvetica", "B", 16)
        if logo_path and os.path.exists(logo_path):
            try:
                pdf.image(logo_path, x=10, y=8, w=30)
            except Exception:
                pass # Fail silently if logo is corrupted
        
        pdf.cell(0, 10, f"Documentacao de Qualidade - {project_name}", new_x="LMARGIN", new_y="NEXT", align="C")
        pdf.ln(10)
        
        # Coverage Matrix Section
        pdf.set_font("helvetica", "B", 14)
        pdf.cell(0, 10, "Matriz de Cobertura", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("helvetica", size=10)
        
        for _, row in coverage_df.iterrows():
            pdf.set_font("helvetica", "B", 10)
            pdf.multi_cell(0, 6, f"ID: {row.get('ID', '')} | Funcionalidade: {row.get('Funcionalidade', '')}")
            pdf.set_font("helvetica", size=10)
            pdf.multi_cell(0, 6, f"Cenario: {row.get('Cenario', '')}")
            pdf.multi_cell(0, 6, f"Regra: {row.get('Regra_Negocio', '')}")
            pdf.ln(2)
            
        pdf.ln(5)
        
        # Test Cases Section
        pdf.set_font("helvetica", "B", 14)
        pdf.cell(0, 10, "Casos de Teste (BDD)", new_x="LMARGIN", new_y="NEXT")
        
        for tc in test_cases:
            pdf.set_font("helvetica", "B", 11)
            pdf.multi_cell(0, 8, f"TC-{tc.get('id', '')}: {tc.get('titulo', '')}")
            pdf.set_font("helvetica", "I", 10)
            pdf.multi_cell(0, 6, f"Pre-condicoes: {tc.get('pre_condicoes', '')}")
            pdf.set_font("helvetica", size=10)
            
            for step in tc.get('passos', []):
                pdf.multi_cell(0, 6, f"Passo {step.get('numero', '')}: {step.get('acao', '')}")
                pdf.multi_cell(0, 6, f"Esperado: {step.get('resultado_esperado', '')}")
            pdf.ln(4)
            
        return pdf.output(dest="S")


class AzureCsvFormatter:
    @staticmethod
    def generate_csv_content(test_cases: list, project_name: str) -> str:
        if not test_cases:
            return "ID;Work Item Type;Title;Test Step;Pre condicoes;Step Action;Step Expected;Automation Status;Area Path;Assigned To;State"

        csv_header = "ID;Work Item Type;Title;Test Step;Pre condicoes;Step Action;Step Expected;Automation Status;Area Path;Assigned To;State"
        csv_lines = [csv_header]

        for test_case in test_cases:
            tc_title = str(test_case.get('titulo', '')).replace(';', ',')
            tc_pre_conditions = str(test_case.get('pre_condicoes', '')).replace(';', ',')
            csv_lines.append(
                f";Test Case;{tc_title};;{tc_pre_conditions};;;Not Automated;{project_name};;Design"
            )
            for step in test_case.get('passos', []):
                step_num = step.get('numero', '')
                step_action = str(step.get('acao', '')).replace(';', ',').replace('\n', ' ')
                step_expected = str(step.get('resultado_esperado', '')).replace(';', ',').replace('\n', ' ')
                csv_lines.append(f";;;{step_num};;{step_action};{step_expected};;;;")

        return "\n".join(csv_lines)


class WebhookClient:
    def __init__(self, config: AppConfiguration):
        self.config = config
        self.headers = {"x-api-key": st.secrets["N8N_API_KEY"]} if "N8N_API_KEY" in st.secrets else {}

    def _extract_target_payload(self, raw_response, target_key: str) -> list:
        if isinstance(raw_response, list):
            for item in raw_response:
                result = self._extract_target_payload(item, target_key)
                if result: return result
            return []
        if not isinstance(raw_response, dict):
            return []
        if target_key in raw_response:
            value = raw_response[target_key]
            if isinstance(value, list): return value
        for key, value in raw_response.items():
            if isinstance(value, dict):
                result = self._extract_target_payload(value, target_key)
                if result: return result
            elif isinstance(value, list):
                for item in value:
                    result = self._extract_target_payload(item, target_key)
                    if result: return result
            elif isinstance(value, str):
                try:
                    parsed = json.loads(value)
                    result = self._extract_target_payload(parsed, target_key)
                    if result: return result
                except (json.JSONDecodeError, TypeError):
                    continue
        return []

    def trigger_analysis(self, document_text: str, images_b64: list, project_name: str) -> dict:
        payload = {"document_text": document_text, "images_base64": images_b64, "nome_projeto": project_name}
        response = requests.post(self.config.webhook_analysis, json=payload, headers=self.headers, timeout=120)
        response.raise_for_status()
        return {"duvidas": self._extract_target_payload(response.json(), "duvidas")}

    def trigger_coverage(self, document_text: str, images_b64: list, user_answers: dict, project_name: str) -> dict:
        payload = {
            "document_text": document_text,
            "images_base64": images_b64,
            "respostas_duvidas": json.dumps(user_answers, ensure_ascii=False),
            "nome_projeto": project_name
        }
        response = requests.post(self.config.webhook_coverage, json=payload, headers=self.headers, timeout=240)
        response.raise_for_status()
        return {"matriz_cobertura": self._extract_target_payload(response.json(), "matriz_cobertura")}

    def trigger_generation(self, coverage_matrix: list, project_name: str) -> dict:
        payload = {
            "matriz_cobertura": json.dumps(coverage_matrix, ensure_ascii=False),
            "nome_projeto": project_name
        }
        response = requests.post(self.config.webhook_generation, json=payload, headers=self.headers, timeout=300)
        response.raise_for_status()
        return {"casos_de_teste": self._extract_target_payload(response.json(), "casos_de_teste")}


class UserInterface:
    def __init__(self):
        st.set_page_config(page_title="QA TestGen - Refuturiza", page_icon="🧪", layout="wide")
        self.config = AppConfiguration()
        self.client = WebhookClient(self.config)
        self.initialize_state()

    def initialize_state(self):
        default_states = {
            'current_step': 1,
            'raw_document_text': '',
            'raw_images_b64': [],
            'azure_project_name': '',
            'identified_questions': [],
            'coverage_matrix_df': pd.DataFrame(),
            'test_cases_df': pd.DataFrame(),
            'final_test_cases': [],
            'azure_csv_content': '',
            'pdf_bytes': None
        }
        for key, value in default_states.items():
            if key not in st.session_state:
                st.session_state[key] = value

    def render_header(self):
        col1, col2 = st.columns([1, 10])
        with col1:
            if os.path.exists(self.config.logo_path):
                st.image(self.config.logo_path, width=80)
        with col2:
            st.markdown("""
            <div style="background: linear-gradient(135deg, #F15A24 0%, #c94a1a 100%);
                        padding: 1.5rem; border-radius: 8px; margin-bottom: 2rem;">
                <h1 style="color: white; margin: 0;">🧪 QA TestGen – Refuturiza Automation</h1>
                <p style="color: white; margin: 0.3rem 0 0 0; font-size: 1.1rem;">
                    BDD Test Cases & Coverage Matrix Generator
                </p>
            </div>
            """, unsafe_allow_html=True)

    def render_progress(self):
        steps = ["📄 Upload", "💬 Dúvidas", "🎯 Matriz", "📋 Revisão Testes", "⬇️ Exportar"]
        cols = st.columns(len(steps))
        for i, (col, label) in enumerate(zip(cols, steps), start=1):
            with col:
                if i < st.session_state.current_step:
                    st.success(label)
                elif i == st.session_state.current_step:
                    st.info(f"**{label}**")
                else:
                    st.markdown(
                        f"<div style='padding:0.5rem; border-radius:4px; background:#f0f0f0; color:#999; text-align:center'>{label}</div>", 
                        unsafe_allow_html=True
                    )
        st.divider()

    def view_step_1_upload(self):
        st.subheader("Passo 1 – Setup de Contexto e Documentação")
        col1, col2 = st.columns(2)
        with col1:
            project_name = st.text_input("Nome do Projeto *", placeholder="Ex: Passaporte Refuturiza")
        with col2:
            uploaded_file = st.file_uploader("Documento de Requisitos *", type=["pdf", "txt", "docx"])

        if st.button("🔍 Extrair e Analisar (Texto + Imagens)", use_container_width=True, disabled=not (project_name and uploaded_file)):
            with st.spinner("Processando texto e imagens do documento..."):
                text = DocumentProcessor.extract_plain_text(uploaded_file)
                images = DocumentProcessor.extract_images_as_base64(uploaded_file)
                
            if not text.strip() and not images:
                st.error("Não foi possível extrair conteúdo útil do arquivo.")
                return

            with st.spinner("Analisando contexto funcional no n8n..."):
                try:
                    resp = self.client.trigger_analysis(text, images, project_name)
                    st.session_state.raw_document_text = text
                    st.session_state.raw_images_b64 = images
                    st.session_state.azure_project_name = project_name
                    st.session_state.identified_questions = resp.get("duvidas") or []
                    st.session_state.current_step = 2
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ Erro de Integração (Analysis): {e}")

    def view_step_2_human_in_the_loop(self):
        st.subheader("Passo 2 – Resolução de Ambiguidade")
        questions = st.session_state.identified_questions
        user_responses = {}

        if not questions:
            st.success("✅ A IA não identificou pontos de falha na Governança das regras de negócio. Siga para a Matriz.")
        else:
            for q in questions:
                q_id = str(q.get('id', '0'))
                st.markdown(f"**❓ Dúvida #{q_id}:** {q.get('pergunta', 'N/A')}")
                user_responses[q_id] = st.text_area("Resposta", key=f"q_{q_id}")

        col1, col2 = st.columns([1, 3])
        with col1:
            if st.button("← Voltar", use_container_width=True):
                st.session_state.current_step = 1
                st.rerun()
        with col2:
            if st.button("🧩 Gerar Matriz de Cobertura", use_container_width=True, type="primary"):
                with st.spinner("Processando arquitetura de cobertura de testes..."):
                    try:
                        resp = self.client.trigger_coverage(
                            st.session_state.raw_document_text,
                            st.session_state.raw_images_b64,
                            user_responses,
                            st.session_state.azure_project_name
                        )
                        matriz_json = resp.get("matriz_cobertura") or []
                        st.session_state.coverage_matrix_df = pd.DataFrame(matriz_json)
                        st.session_state.current_step = 3
                        st.rerun()
                    except Exception as e:
                        st.error(f"❌ Erro de Integração (Coverage): {e}")

    def view_step_3_coverage_matrix(self):
        st.subheader("Passo 3 – Revisão da Matriz de Cobertura")
        st.info("Edite os campos diretamente na tabela abaixo. Adicione ou remova linhas se necessário.")
        
        # Grid Interativo
        edited_matrix_df = st.data_editor(
            st.session_state.coverage_matrix_df, 
            num_rows="dynamic", 
            use_container_width=True,
            key="matrix_editor"
        )

        col1, col2 = st.columns([1, 3])
        with col1:
            if st.button("← Voltar", use_container_width=True):
                st.session_state.current_step = 2
                st.rerun()
        with col2:
            if st.button("🚀 Gerar Casos de Teste (BDD)", use_container_width=True, type="primary"):
                # Salva estado editado para o pipeline
                st.session_state.coverage_matrix_df = edited_matrix_df
                
                with st.spinner("Transpilando Matriz para Cenários BDD..."):
                    try:
                        resp = self.client.trigger_generation(
                            edited_matrix_df.to_dict('records'),
                            st.session_state.azure_project_name
                        )
                        raw_cases = resp.get("casos_de_teste") or []
                        st.session_state.test_cases_df = DataTransformer.flatten_test_cases(raw_cases)
                        st.session_state.current_step = 4
                        st.rerun()
                    except Exception as e:
                        st.error(f"❌ Erro de Integração (Generation): {e}")

    def view_step_4_review_test_cases(self):
        st.subheader("Passo 4 – Revisão e Edição dos Casos de Teste")
        st.info("Abaixo está a visão flat (passo-a-passo) dos casos de teste. Edite, exclua ou adicione steps.")

        edited_cases_df = st.data_editor(
            st.session_state.test_cases_df,
            num_rows="dynamic",
            use_container_width=True,
            height=500,
            key="test_cases_editor"
        )

        col1, col2 = st.columns([1, 3])
        with col1:
            if st.button("← Voltar", use_container_width=True):
                st.session_state.current_step = 3
                st.rerun()
        with col2:
            if st.button("⚙️ Consolidar e Exportar", use_container_width=True, type="primary"):
                final_nested_cases = DataTransformer.nest_test_cases(edited_cases_df)
                st.session_state.final_test_cases = final_nested_cases
                
                # Geração Assíncrona de Artefatos em Memória
                st.session_state.azure_csv_content = AzureCsvFormatter.generate_csv_content(
                    final_nested_cases, st.session_state.azure_project_name
                )
                
                st.session_state.pdf_bytes = PdfReportExporter.generate_pdf(
                    st.session_state.azure_project_name,
                    st.session_state.coverage_matrix_df,
                    final_nested_cases,
                    self.config.logo_path
                )
                
                st.session_state.current_step = 5
                st.rerun()

    def view_step_5_download(self):
        st.subheader("Passo 5 – Download de Artefatos")
        st.success("🎉 Processamento finalizado! Baixe os pacotes abaixo.")

        col1, col2 = st.columns(2)
        
        csv_name = f"QA_Azure_{st.session_state.azure_project_name.replace(' ', '_')}.csv"
        pdf_name = f"QA_Report_{st.session_state.azure_project_name.replace(' ', '_')}.pdf"

        with col1:
            st.download_button(
                label="⬇️ Baixar Planilha CSV (Azure DevOps)",
                data=('\ufeff' + st.session_state.azure_csv_content).encode('utf-8'),
                file_name=csv_name,
                mime="text/csv",
                use_container_width=True
            )
            
        with col2:
            if st.session_state.pdf_bytes:
                st.download_button(
                    label="⬇️ Baixar Relatório PDF (Matriz + BDD)",
                    data=st.session_state.pdf_bytes,
                    file_name=pdf_name,
                    mime="application/pdf",
                    use_container_width=True
                )

        st.divider()
        if st.button("🔄 Reiniciar Pipeline", use_container_width=True):
            st.session_state.clear()
            st.rerun()

    def execute_flow(self):
        self.render_header()
        self.render_progress()

        step = st.session_state.current_step
        if step == 1: self.view_step_1_upload()
        elif step == 2: self.view_step_2_human_in_the_loop()
        elif step == 3: self.view_step_3_coverage_matrix()
        elif step == 4: self.view_step_4_review_test_cases()
        elif step == 5: self.view_step_5_download()


if __name__ == "__main__":
    app_ui = UserInterface()
    app_ui.execute_flow()


''' ÚLTIMA VERSÃO FUNCIONAL
import streamlit as st
import requests
import json
import os
from PyPDF2 import PdfReader
from docx import Document
from dotenv import load_dotenv

load_dotenv()


class AppConfiguration:
    def __init__(self):
        self.webhook_analysis = os.getenv(
            "N8N_WEBHOOK_URL_ANALYSIS",
            "http://localhost:5678/webhook/qa-testgen-analysis"
        )
        self.webhook_generation = os.getenv(
            "N8N_WEBHOOK_URL_GENERATION",
            "http://localhost:5678/webhook/qa-testgen-generation"
        )


class DocumentProcessor:
    @staticmethod
    def extract_plain_text(uploaded_file) -> str:
        file_extension = uploaded_file.name.split('.')[-1].lower()
        extracted_text = ""
        try:
            if file_extension == "pdf":
                pdf_reader = PdfReader(uploaded_file)
                for page in pdf_reader.pages:
                    page_text = page.extract_text()
                    if page_text:
                        extracted_text += page_text + "\n"
            elif file_extension == "docx":
                doc = Document(uploaded_file)
                for paragraph in doc.paragraphs:
                    extracted_text += paragraph.text + "\n"
            elif file_extension == "txt":
                extracted_text = uploaded_file.getvalue().decode("utf-8")
            return extracted_text
        except Exception as exception:
            st.error(f"Erro ao extrair o texto do arquivo: {exception}")
            return ""


class AzureCsvFormatter:
    @staticmethod
    def generate_csv_content(test_cases: list, project_name: str) -> str:
        if not test_cases:
            return "ID;Work Item Type;Title;Test Step;Pre condicoes;Step Action;Step Expected;Automation Status;Area Path;Assigned To;State"

        csv_header = "ID;Work Item Type;Title;Test Step;Pre condicoes;Step Action;Step Expected;Automation Status;Area Path;Assigned To;State"
        csv_lines = [csv_header]

        for test_case in test_cases:
            tc_title = str(test_case.get('titulo', '')).replace(';', ',')
            tc_pre_conditions = str(test_case.get('pre_condicoes', '')).replace(';', ',')
            csv_lines.append(
                f";Test Case;{tc_title};;{tc_pre_conditions};;;Not Automated;{project_name};;Design"
            )
            for step in test_case.get('passos', []):
                step_num = step.get('numero', '')
                step_action = str(step.get('acao', '')).replace(';', ',')
                step_expected = str(step.get('resultado_esperado', '')).replace(';', ',')
                csv_lines.append(f";;;{step_num};;{step_action};{step_expected};;;;")

        return "\n".join(csv_lines)


class WebhookClient:
    def __init__(self, config: AppConfiguration):
        self.config = config
        self.headers = {"x-api-key": st.secrets["N8N_API_KEY"]}

    def _extract_target_payload(self, raw_response, target_key: str) -> list:
        """
        Navega recursivamente pela resposta do n8n para encontrar a chave alvo.
        O n8n pode retornar: dict simples, lista de dicts, ou JSON aninhado em string.
        """
        # n8n às vezes retorna uma lista no nível raiz
        if isinstance(raw_response, list):
            for item in raw_response:
                result = self._extract_target_payload(item, target_key)
                if result:
                    return result
            return []

        if not isinstance(raw_response, dict):
            return []

        # Chave encontrada diretamente
        if target_key in raw_response:
            value = raw_response[target_key]
            if isinstance(value, list):
                return value

        # Busca aninhada em sub-dicts e strings JSON
        for key, value in raw_response.items():
            if isinstance(value, dict):
                result = self._extract_target_payload(value, target_key)
                if result:
                    return result
            elif isinstance(value, list):
                # Pode ser a própria lista (ex: output do parser estruturado)
                for item in value:
                    result = self._extract_target_payload(item, target_key)
                    if result:
                        return result
            elif isinstance(value, str):
                try:
                    parsed = json.loads(value)
                    result = self._extract_target_payload(parsed, target_key)
                    if result:
                        return result
                except (json.JSONDecodeError, TypeError):
                    continue

        return []

    def trigger_analysis(self, document_text: str, project_name: str) -> dict:
        payload = {"document_text": document_text, "nome_projeto": project_name}
        response = requests.post(
            self.config.webhook_analysis,
            json=payload,
            headers=self.headers,
            timeout=120
        )
        response.raise_for_status()

        raw = response.json()
        # DEBUG: descomente se precisar investigar a resposta crua do n8n
        # st.write("DEBUG analysis raw:", raw)

        duvidas = self._extract_target_payload(raw, "duvidas")
        return {"duvidas": duvidas}

    def trigger_generation(self, document_text: str, user_answers: dict, project_name: str) -> dict:
        payload = {
            "document_text": document_text,
            "respostas_duvidas": json.dumps(user_answers, ensure_ascii=False),
            "nome_projeto": project_name
        }
        response = requests.post(
            self.config.webhook_generation,
            json=payload,
            headers=self.headers,
            timeout=300
        )
        response.raise_for_status()

        raw = response.json()
        # DEBUG: descomente se precisar investigar a resposta crua do n8n
        # st.write("DEBUG generation raw:", raw)

        casos = self._extract_target_payload(raw, "casos_de_teste")
        return {"casos_de_teste": casos}


class UserInterface:
    def __init__(self):
        st.set_page_config(
            page_title="QA TestGen - Azure DevOps",
            page_icon="🧪",
            layout="wide"
        )
        self.initialize_state()
        self.config = AppConfiguration()
        self.client = WebhookClient(self.config)

    def initialize_state(self):
        default_states = {
            'current_step': 1,
            'raw_document_text': '',
            'identified_questions': [],
            'test_cases': [],
            'azure_csv_content': '',
            'azure_project_name': ''
        }
        for key, value in default_states.items():
            if key not in st.session_state:
                st.session_state[key] = value

    def render_header(self):
        st.markdown("""
        <div style="background: linear-gradient(135deg, #F15A24 0%, #c94a1a 100%);
                    padding: 1.5rem; border-radius: 8px; margin-bottom: 2rem;">
            <h1 style="color: white; margin: 0;">🧪 QA TestGen – Refuturiza Automation</h1>
            <p style="color: white; margin: 0.3rem 0 0 0; font-size: 1.1rem;">
                Gerador Inteligente de Casos de Teste (Azure DevOps Integration)
            </p>
        </div>
        """, unsafe_allow_html=True)

    def render_progress(self):
        steps = ["📄 Upload", "💬 Dúvidas", "📋 Revisão", "⬇️ Download"]
        cols = st.columns(4)
        for i, (col, label) in enumerate(zip(cols, steps), start=1):
            with col:
                if i < st.session_state.current_step:
                    st.success(label)
                elif i == st.session_state.current_step:
                    st.info(f"**{label}**")
                else:
                    st.markdown(
                        f"<div style='padding:0.5rem; border-radius:4px; "
                        f"background:#f0f0f0; color:#999; text-align:center'>{label}</div>",
                        unsafe_allow_html=True
                    )
        st.divider()

    # ─────────────────────────────────────────────
    # STEP 1: Upload e Análise
    # ─────────────────────────────────────────────
    def view_step_1_upload(self):
        st.subheader("Passo 1 – Setup de Contexto e Documentação")
        #st.write("URL Analysis:", self.config.webhook_analysis)
        #st.write("URL Generation:", self.config.webhook_generation)

        col1, col2 = st.columns(2)
        with col1:
            project_name = st.text_input(
                "Nome do Projeto *",
                placeholder="Ex: Passaporte Refuturiza"
            )
        with col2:
            uploaded_file = st.file_uploader(
                "Documento de Requisitos *",
                type=["pdf", "txt", "docx"]
            )

        if not project_name or not uploaded_file:
            st.info("Preencha o nome do projeto e faça o upload do documento para continuar.")
            return

        if st.button("🔍 Executar Análise de Cobertura (IA)", use_container_width=True):
            with st.spinner("Extraindo texto do documento..."):
                text = DocumentProcessor.extract_plain_text(uploaded_file)

            if not text.strip():
                st.error("Não foi possível extrair texto do arquivo. Verifique se o PDF não está protegido ou escaneado.")
                return

            with st.spinner("Analisando documento com IA… isso pode levar alguns segundos."):
                try:
                    resp = self.client.trigger_analysis(text, project_name)
                    st.session_state.raw_document_text = text
                    st.session_state.azure_project_name = project_name
                    st.session_state.identified_questions = resp.get("duvidas") or []
                    st.session_state.current_step = 2
                    st.rerun()
                except requests.exceptions.Timeout:
                    st.error("⏱️ Timeout: o webhook demorou mais que 120s. Tente novamente ou verifique o n8n.")
                except requests.exceptions.ConnectionError:
                    st.error("🔌 Não foi possível conectar ao n8n. Verifique se o serviço está rodando e a URL está correta.")
                except requests.exceptions.HTTPError as e:
                    st.error(f"❌ Erro HTTP do n8n: {e}")
                except Exception as e:
                    st.error(f"❌ Erro inesperado: {e}")

    # ─────────────────────────────────────────────
    # STEP 2: Human-in-the-Loop
    # ─────────────────────────────────────────────
    def view_step_2_human_in_the_loop(self):
        st.subheader("Passo 2 – Human-in-the-Loop")

        questions = st.session_state.identified_questions
        user_responses = {}

        if not questions:
            # ✅ FIX: Se a IA não gerou dúvidas, avança direto sem travar
            st.success("✅ A IA não identificou ambiguidades no documento. Você pode gerar os casos de teste diretamente.")
        else:
            st.info(f"A IA identificou **{len(questions)} ponto(s)** que precisam de esclarecimento antes de gerar os testes.")
            for q in questions:
                q_id = str(q.get('id', '0'))
                st.markdown(f"**❓ Dúvida #{q_id}:** {q.get('pergunta', 'N/A')}")
                user_responses[q_id] = st.text_area(
                    f"Sua resposta para a dúvida #{q_id}",
                    key=f"q_{q_id}",
                    placeholder="Descreva a regra de negócio ou decisão para este ponto…"
                )

        col1, col2 = st.columns([1, 3])
        with col1:
            if st.button("← Voltar", use_container_width=True):
                st.session_state.current_step = 1
                st.rerun()
        with col2:
            if st.button("🚀 Gerar Matriz de Testes", use_container_width=True, type="primary"):
                with st.spinner("Gerando casos de teste com IA… pode levar até 3 minutos para documentos grandes."):
                    try:
                        resp = self.client.trigger_generation(
                            st.session_state.raw_document_text,
                            user_responses,
                            st.session_state.azure_project_name
                        )
                        casos = resp.get("casos_de_teste") or []
                        if not casos:
                            st.error(
                                "❌ A IA retornou uma lista vazia de casos de teste. "
                                "Verifique os logs do n8n para ver o que foi retornado."
                            )
                            return
                        st.session_state.test_cases = casos
                        st.session_state.current_step = 3
                        st.rerun()
                    except requests.exceptions.Timeout:
                        st.error("⏱️ Timeout: o webhook de geração demorou mais que 300s.")
                    except requests.exceptions.ConnectionError:
                        st.error("🔌 Não foi possível conectar ao n8n.")
                    except requests.exceptions.HTTPError as e:
                        st.error(f"❌ Erro HTTP do n8n: {e}")
                    except Exception as e:
                        st.error(f"❌ Erro inesperado: {e}")

    # ─────────────────────────────────────────────
    # STEP 3: Revisão dos casos de teste
    # ─────────────────────────────────────────────
    def view_step_3_review_and_export(self):
        st.subheader("Passo 3 – Revisão da Matriz de Testes")

        test_cases = st.session_state.test_cases
        st.success(f"✅ {len(test_cases)} caso(s) de teste gerado(s) com sucesso!")

        for idx, tc in enumerate(test_cases, start=1):
            titulo = tc.get('titulo', f'Caso #{idx}')
            pre = tc.get('pre_condicoes', '—')
            passos = tc.get('passos', [])

            with st.expander(f"**TC-{idx:02d}** – {titulo}", expanded=(idx == 1)):
                st.markdown(f"**Pré-condições:** {pre}")
                if passos:
                    st.markdown("**Passos:**")
                    for step in passos:
                        num = step.get('numero', '')
                        acao = step.get('acao', '')
                        esperado = step.get('resultado_esperado', '')
                        st.markdown(
                            f"&nbsp;&nbsp;`{num}.` **Ação:** {acao}  \n"
                            f"&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;**Esperado:** {esperado}"
                        )
                else:
                    st.warning("Nenhum passo encontrado para este caso de teste.")

        st.divider()
        col1, col2 = st.columns([1, 3])
        with col1:
            if st.button("← Voltar", use_container_width=True):
                st.session_state.current_step = 2
                st.rerun()
        with col2:
            if st.button("📥 Gerar CSV para Azure DevOps", use_container_width=True, type="primary"):
                csv_content = AzureCsvFormatter.generate_csv_content(
                    test_cases,
                    st.session_state.azure_project_name
                )
                st.session_state.azure_csv_content = csv_content
                st.session_state.current_step = 4
                st.rerun()

    # ─────────────────────────────────────────────
    # STEP 4: Download
    # ─────────────────────────────────────────────
    def view_step_4_download(self):
        st.subheader("Passo 4 – Download")
        st.success("🎉 CSV gerado com sucesso e pronto para importar no Azure DevOps!")

        csv_bytes = ('\ufeff' + st.session_state.azure_csv_content).encode('utf-8')
        file_name = f"QA_Export_{st.session_state.azure_project_name.replace(' ', '_')}.csv"

        st.download_button(
            label="⬇️ Baixar CSV (Azure DevOps)",
            data=csv_bytes,
            file_name=file_name,
            mime="text/csv",
            use_container_width=True
        )

        st.divider()
        with st.expander("👀 Pré-visualização do CSV"):
            st.code(st.session_state.azure_csv_content[:3000], language="text")

        if st.button("🔄 Iniciar Nova Análise", use_container_width=True):
            st.session_state.clear()
            st.rerun()

    # ─────────────────────────────────────────────
    # Runner
    # ─────────────────────────────────────────────
    def execute_flow(self):
        self.render_header()
        self.render_progress()

        step = st.session_state.current_step
        if step == 1:
            self.view_step_1_upload()
        elif step == 2:
            self.view_step_2_human_in_the_loop()
        elif step == 3:
            self.view_step_3_review_and_export()
        elif step == 4:
            self.view_step_4_download()


if __name__ == "__main__":
    app_ui = UserInterface()
    app_ui.execute_flow()
'''