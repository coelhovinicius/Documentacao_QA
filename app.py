import streamlit as st
import requests
import json
import os
from io import BytesIO
from PyPDF2 import PdfReader
from docx import Document
from dotenv import load_dotenv

# POR QUE: Carrega as variaveis de ambiente locais (.env). 
load_dotenv()

class AppConfiguration:
    def __init__(self):
        self.webhook_analysis = os.getenv("N8N_WEBHOOK_URL_ANALYSIS", "http://localhost:5678/webhook/qa-testgen-analysis")
        self.webhook_generation = os.getenv("N8N_WEBHOOK_URL_GENERATION", "http://localhost:5678/webhook/qa-testgen-generation")

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
        csv_header = "ID;Work Item Type;Title;Test Step;Pre condicoes;Step Action;Step Expected;Automation Status;Area Path;Assigned To;State"
        csv_lines = [csv_header]

        for test_case in test_cases:
            tc_title = str(test_case.get('titulo', '')).replace(';', ',')
            tc_pre_conditions = str(test_case.get('pre_condicoes', '')).replace(';', ',')
            
            csv_lines.append(f";Test Case;{tc_title};;{tc_pre_conditions};;;Not Automated;{project_name};;Design")
            
            for step in test_case.get('passos', []):
                step_num = step.get('numero', '')
                step_action = str(step.get('acao', '')).replace(';', ',')
                step_expected = str(step.get('resultado_esperado', '')).replace(';', ',')
                
                csv_lines.append(f";;;{step_num};;{step_action};{step_expected};;;;")

        return "\n".join(csv_lines)

class WebhookClient:
    def __init__(self, config: AppConfiguration):
        self.config = config
        # CENTRALIZAÇÃO DA SEGURANÇA: Todos os requests usarão este header
        self.headers = {"x-api-key": st.secrets["N8N_API_KEY"]}

    def _extract_target_payload(self, raw_response: dict, target_key: str) -> list:
        if not isinstance(raw_response, dict):
            return []
        if target_key in raw_response:
            return raw_response[target_key]
        for key, value in raw_response.items():
            if isinstance(value, dict) and target_key in value:
                return value[target_key]
            elif isinstance(value, str) and target_key in value:
                try:
                    parsed_str = json.loads(value)
                    if target_key in parsed_str:
                        return parsed_str[target_key]
                except json.JSONDecodeError:
                    continue
        return []

    def trigger_analysis(self, document_text: str, project_name: str) -> dict:
        payload = {"document_text": document_text, "nome_projeto": project_name}
        response = requests.post(self.config.webhook_analysis, json=payload, headers=self.headers, timeout=120)
        response.raise_for_status()
        return {"duvidas": self._extract_target_payload(response.json(), "duvidas")}

    def trigger_generation(self, document_text: str, user_answers: dict, project_name: str) -> dict:
        payload = {
            "document_text": document_text,
            "respostas_duvidas": json.dumps(user_answers, ensure_ascii=False),
            "nome_projeto": project_name
        }
        response = requests.post(self.config.webhook_generation, json=payload, headers=self.headers, timeout=180)
        response.raise_for_status()
        return {"casos_de_teste": self._extract_target_payload(response.json(), "casos_de_teste")}

class UserInterface:
    def __init__(self):
        st.set_page_config(page_title="QA TestGen - Azure DevOps", page_icon="🧪", layout="wide")
        self.initialize_state()
        self.config = AppConfiguration()
        self.client = WebhookClient(self.config)

    def initialize_state(self):
        default_states = {
            'current_step': 1, 'raw_document_text': '', 'identified_questions': [],
            'test_cases': [], 'azure_csv_content': '', 'azure_project_name': ''
        }
        for key, value in default_states.items():
            if key not in st.session_state:
                st.session_state[key] = value

    def render_header(self):
        st.markdown("""
        <div style="background: linear-gradient(135deg, #F15A24 0%, #c94a1a 100%); padding: 1.5rem; border-radius: 8px; margin-bottom: 2rem;">
            <h1 style="color: white; margin: 0;">QA TestGen - Refuturiza Automation</h1>
            <p style="color: white; margin: 0; font-size: 1.1rem;">Gerador Inteligente de Casos de Teste (Azure DevOps Integration)</p>
        </div>
        """, unsafe_allow_html=True)

    def view_step_1_upload(self):
        st.subheader("1. Setup de Contexto e Documentação")
        col1, col2 = st.columns(2)
        with col1: project_name = st.text_input("Nome do Projeto", placeholder="Ex: Passaporte Refuturiza")
        with col2: uploaded_file = st.file_uploader("Documento de Requisitos", type=["pdf", "txt", "docx"])

        if uploaded_file and project_name:
            if st.button("Executar Análise de Cobertura (IA)"):
                with st.spinner("Analisando..."):
                    text = DocumentProcessor.extract_plain_text(uploaded_file)
                    try:
                        resp = self.client.trigger_analysis(text, project_name)
                        st.session_state.raw_document_text = text
                        st.session_state.azure_project_name = project_name
                        st.session_state.identified_questions = resp.get("duvidas") or []
                        st.session_state.current_step = 2
                        st.rerun()
                    except Exception as e:
                        st.error(f"Erro: {e}")

    def view_step_2_human_in_the_loop(self):
        st.subheader("2. Human-in-the-Loop")
        user_responses = {}
        for q in st.session_state.identified_questions:
            q_id = str(q.get('id', '0'))
            st.markdown(f"**Dúvida #{q_id}:** {q.get('pergunta', 'N/A')}")
            user_responses[q_id] = st.text_area(f"Resolução #{q_id}", key=f"q_{q_id}")
        
        if st.button("Gerar Matriz de Testes"):
            try:
                resp = self.client.trigger_generation(st.session_state.raw_document_text, user_responses, st.session_state.azure_project_name)
                st.session_state.test_cases = resp.get("casos_de_teste") or []
                st.session_state.current_step = 3
                st.rerun()
            except Exception as e:
                st.error(f"Erro: {e}")

    def view_step_3_review_and_export(self):
        st.subheader("3. Matriz de Testes")
        # Logica de edição...
        if st.button("Gerar CSV"):
            st.session_state.azure_csv_content = AzureCsvFormatter.generate_csv_content(st.session_state.test_cases, st.session_state.azure_project_name)
            st.session_state.current_step = 4
            st.rerun()

    def view_step_4_download(self):
        st.success("Concluído!")
        st.download_button("Download CSV", data=('\ufeff' + st.session_state.azure_csv_content).encode('utf-8'), file_name="QA_Export.csv", mime="text/csv")
        if st.button("Reiniciar"):
            st.session_state.clear()
            st.rerun()

    def execute_flow(self):
        self.render_header()
        if st.session_state.current_step == 1: self.view_step_1_upload()
        elif st.session_state.current_step == 2: self.view_step_2_human_in_the_loop()
        elif st.session_state.current_step == 3: self.view_step_3_review_and_export()
        elif st.session_state.current_step == 4: self.view_step_4_download()

if __name__ == "__main__":
    app_ui = UserInterface()
    app_ui.execute_flow()