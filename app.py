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