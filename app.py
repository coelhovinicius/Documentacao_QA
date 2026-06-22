import streamlit as st
import requests
import json
import os
from io import BytesIO
from PyPDF2 import PdfReader
from docx import Document
from dotenv import load_dotenv

# POR QUE: Carrega as variaveis de ambiente locais (.env). 
# Mantemos as chaves de API e URLs de webhooks fora do controle de versao para seguranca.
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
            
            # Linha mestre do Caso de Teste
            csv_lines.append(f";Test Case;{tc_title};;{tc_pre_conditions};;;Not Automated;{project_name};;Design")
            
            # Linhas filhas (Passos de Execucao)
            for step in test_case.get('passos', []):
                step_num = step.get('numero', '')
                step_action = str(step.get('acao', '')).replace(';', ',')
                step_expected = str(step.get('resultado_esperado', '')).replace(';', ',')
                
                csv_lines.append(f";;;{step_num};;{step_action};{step_expected};;;;")

        return "\n".join(csv_lines)

class WebhookClient:
    def __init__(self, config: AppConfiguration):
        self.config = config

    def _extract_target_payload(self, raw_response: dict, target_key: str) -> list:
        """
        POR QUE: O n8n Langchain envelopa objetos de saida em chaves dinamicas (ex: 'output', 'text').
        Este metodo itera o dicionario recursivamente garantindo a extracao resiliente do JSON real gerado pela IA.
        """
        if not isinstance(raw_response, dict):
            return []

        if target_key in raw_response:
            return raw_response[target_key]
            
        for key, value in raw_response.items():
            if isinstance(value, dict) and target_key in value:
                return value[target_key]
            # Fallback caso a IA falhe no parse e retorne String JSON raw no campo text
            elif isinstance(value, str) and target_key in value:
                try:
                    parsed_str = json.loads(value)
                    if target_key in parsed_str:
                        return parsed_str[target_key]
                except json.JSONDecodeError:
                    continue
        return []

    def trigger_analysis(self, document_text: str, project_name: str) -> dict:
        payload = {
            "document_text": document_text,
            "nome_projeto": project_name
        }
        response = requests.post(self.config.webhook_analysis, json=payload, timeout=120)
        response.raise_for_status()
        
        raw_json = response.json()
        return {"duvidas": self._extract_target_payload(raw_json, "duvidas")}

    def trigger_generation(self, document_text: str, user_answers: dict, project_name: str) -> dict:
        payload = {
            "document_text": document_text,
            "respostas_duvidas": json.dumps(user_answers, ensure_ascii=False),
            "nome_projeto": project_name
        }
        response = requests.post(self.config.webhook_generation, json=payload, timeout=180)
        response.raise_for_status()
        
        raw_json = response.json()
        return {"casos_de_teste": self._extract_target_payload(raw_json, "casos_de_teste")}

class UserInterface:
    def __init__(self):
        st.set_page_config(page_title="QA TestGen - Azure DevOps", page_icon="🧪", layout="wide")
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
        html_header = """
        <div style="background: linear-gradient(135deg, #F15A24 0%, #c94a1a 100%); padding: 1.5rem; border-radius: 8px; margin-bottom: 2rem;">
            <h1 style="color: white; margin: 0;">QA TestGen - Refuturiza Automation</h1>
            <p style="color: white; margin: 0; font-size: 1.1rem;">Gerador Inteligente de Casos de Teste (Azure DevOps Integration)</p>
        </div>
        """
        st.markdown(html_header, unsafe_allow_html=True)

    def view_step_1_upload(self):
        st.subheader("1. Setup de Contexto e Documentação")
        
        project_col, upload_col = st.columns(2)
        
        with project_col:
            project_name_input = st.text_input("Nome do Projeto no Azure DevOps", placeholder="Ex: Passaporte Refuturiza")
            
        with upload_col:
            uploaded_file = st.file_uploader("Documento de Requisitos (PDF, DOCX, TXT)", type=["pdf", "txt", "docx"])

        if uploaded_file and project_name_input:
            if st.button("Executar Análise de Cobertura (IA)", use_container_width=True):
                with st.spinner("Extraindo e roteando dados da documentação para análise de qualidade..."):
                    
                    extracted_text = DocumentProcessor.extract_plain_text(uploaded_file)
                    
                    if not extracted_text.strip():
                        st.error("Falha ao extrair texto estruturado do documento.")
                        return

                    st.session_state.raw_document_text = extracted_text
                    st.session_state.azure_project_name = project_name_input

                    try:
                        n8n_response = self.client.trigger_analysis(extracted_text, project_name_input)
                        st.session_state.identified_questions = n8n_response.get("duvidas") or []
                        st.session_state.current_step = 2
                        st.rerun()
                    except Exception as http_error:
                        st.error(f"Erro no handshake com o nó n8n de análise: {http_error}")

    def view_step_2_human_in_the_loop(self):
        st.subheader("2. Human-in-the-Loop: Alinhamento de Regras de Negócio")
        
        user_responses = {}
        
        if not st.session_state.identified_questions:
            st.success("Análise concluída. O documento base apresentou contexto sólido e nenhuma ambiguidade foi detectada.")
            st.info("Pressione o botão abaixo para seguir com a geração da matriz de testes.")
        else:
            st.warning("A IA identificou ambiguidades. Forneça o contexto técnico/negocial para refinar os testes.")
            for question_obj in st.session_state.identified_questions:
                q_id = str(question_obj.get('id', '0'))
                st.markdown(f"**Dúvida #{q_id}:** {question_obj.get('pergunta', 'N/A')}")
                user_responses[q_id] = st.text_area(f"Resolução para #{q_id}", key=f"q_input_{q_id}")
                st.markdown("---")

        if st.button("Processar e Gerar Matriz de Testes", use_container_width=True):
            with st.spinner("Injetando contexto e gerando matriz de testes..."):
                try:
                    n8n_response = self.client.trigger_generation(
                        document_text=st.session_state.raw_document_text,
                        user_answers=user_responses,
                        project_name=st.session_state.azure_project_name
                    )
                    st.session_state.test_cases = n8n_response.get("casos_de_teste") or []
                    st.session_state.current_step = 3
                    st.rerun()
                except Exception as http_error:
                    st.error(f"Erro no handshake com o nó n8n de geração: {http_error}")

    def view_step_3_review_and_export(self):
        st.subheader("3. Matriz de Testes Gerada")
        
        editable_test_cases = st.session_state.test_cases

        # POR QUE: Previne que o usuario avance e gere um CSV em branco caso a extração da LLM falhe ou retorne vazio.
        if not editable_test_cases:
            st.error("Falha Crítica: A IA não retornou casos de teste válidos no payload JSON esperado.")
            st.warning("Ação corretiva: Tente refazer o upload do documento e rodar o fluxo novamente. Se persistir, os requisitos podem estar ilegíveis para o motor LLM.")
            if st.button("Voltar ao Início", use_container_width=True):
                st.session_state.clear()
                st.rerun()
            return

        st.info("Valide os metadados. Edições feitas aqui refletirão no CSV final.")

        for idx, tc in enumerate(editable_test_cases):
            tc_id = tc.get('id', f'CT-{idx+1:03d}')
            
            with st.expander(f"{tc_id} | {tc.get('titulo', '')}", expanded=(idx == 0)):
                tc['titulo'] = st.text_input("Title", value=tc.get('titulo', ''), key=f"t_{idx}")
                tc['pre_condicoes'] = st.text_area("Pré-condições", value=tc.get('pre_condicoes', ''), key=f"p_{idx}")
                
                for step_idx, step in enumerate(tc.get('passos', [])):
                    col_action, col_expected = st.columns(2)
                    with col_action:
                        step['acao'] = st.text_input(f"Ação {step_idx+1}", value=step.get('acao', ''), key=f"act_{idx}_{step_idx}")
                    with col_expected:
                        step['resultado_esperado'] = st.text_input(f"Resultado {step_idx+1}", value=step.get('resultado_esperado', ''), key=f"res_{idx}_{step_idx}")

        st.session_state.test_cases = editable_test_cases

        if st.button("Gerar Artefato de Exportação (CSV)", use_container_width=True):
            csv_raw_string = AzureCsvFormatter.generate_csv_content(
                st.session_state.test_cases, 
                st.session_state.azure_project_name
            )
            st.session_state.azure_csv_content = csv_raw_string
            st.session_state.current_step = 4
            st.rerun()

    def view_step_4_download(self):
        st.subheader("4. Deploy de Artefatos")
        st.success("Matriz processada com sucesso. Faça o download para integração manual no Azure Boards.")

        csv_bytes = ('\ufeff' + st.session_state.azure_csv_content).encode('utf-8')
        sanitized_filename = f"QA_TC_{st.session_state.azure_project_name.replace(' ', '_')}.csv"

        st.download_button(
            label="Download Dataframe (Azure V1)",
            data=csv_bytes,
            file_name=sanitized_filename,
            mime="text/csv",
            use_container_width=True
        )
        
        st.markdown("<br>", unsafe_allow_html=True)
        
        if st.button("Resetar Pipeline e Iniciar Novo Fluxo"):
            st.session_state.clear()
            st.rerun()

    def execute_flow(self):
        self.render_header()
        
        if st.session_state.current_step == 1:
            self.view_step_1_upload()
        elif st.session_state.current_step == 2:
            self.view_step_2_human_in_the_loop()
        elif st.session_state.current_step == 3:
            self.view_step_3_review_and_export()
        elif st.session_state.current_step == 4:
            self.view_step_4_download()

if __name__ == "__main__":
    app_ui = UserInterface()
    app_ui.execute_flow()