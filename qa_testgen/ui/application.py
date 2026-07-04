import os
import base64
import uuid
from pathlib import Path

import requests
import streamlit as st
from PIL import Image

from qa_testgen.config import AppConfiguration, LOGO_PATH, SIMBOLO_PATH
from qa_testgen.infrastructure.csv_formatter import AzureCsvFormatter
from qa_testgen.infrastructure.document_processor import DocumentProcessor
from qa_testgen.infrastructure.pdf_report import PdfReportGenerator
from qa_testgen.infrastructure.webhook_client import WebhookClient
from qa_testgen.application.session import SessionState
from qa_testgen.domain.validators.matrix_validator import MatrixValidator
from qa_testgen.domain.validators.plan_validator import TestPlanValidator
from qa_testgen.domain.validators.testcase_validator import TestCaseValidator
from qa_testgen.ui.dialogs import (
    clear_widget_states,
    confirm_deletion_modal,
    confirm_discard_new_modal,
    confirm_interrupt_modal,
    confirm_matriz_deletion_modal,
    confirm_navigate_away_modal,
    confirm_suite_deletion_modal,
    confirm_step_deletion_modal,
)


class UserInterface:
    def __init__(self):
        page_icon = "🧪"
        if Path(SIMBOLO_PATH).exists():
            try:
                page_icon = Image.open(SIMBOLO_PATH)
            except Exception:
                pass

        st.set_page_config(page_title="QA TestGen - Azure DevOps", page_icon=page_icon, layout="wide", initial_sidebar_state="collapsed")
        self.state = SessionState()
        self.config = AppConfiguration()
        self.client = WebhookClient(self.config)

    def trigger_action(self, action_name: str):
        self.state.set('current_action', action_name)
        self.state.set('is_processing', True)
        self.state.set('processing_interrupted', False)

    def clear_action(self):
        self.state.set('current_action', None)
        self.state.set('is_processing', False)
        self.state.set('processing_interrupted', False)

    def interrupt_processing(self):
        self.state.set('current_action', None)
        self.state.set('is_processing', False)
        self.state.set('processing_interrupted', True)
        st.rerun()

    def _set_step(self, target_step: int, allow_during_processing: bool = False):
        if self.state.get('is_processing') and not allow_during_processing:
            return False

        current_step = self.state.get('step', 1)
        if target_step != current_step:
            completed_steps = set(self.state.get('completed_steps') or [])
            completed_steps.add(current_step)
            self.state.set('completed_steps', sorted(completed_steps))

        self.state.set('step', target_step)
        self.state.set('max_step', max(self.state.get('max_step', 1), target_step))
        return True

    @staticmethod
    def can_access_step(target_step, current_step, max_step, completed_steps, is_processing):
        if is_processing:
            return False
        if target_step == current_step:
            return False
        if target_step <= max_step:
            return True
        return target_step in set(completed_steps or [])

    @staticmethod
    def _priority_badge(value: str) -> str:
        colors = {
            'alta': ('#c0392b', '#fdecea'),
            'média': ('#d68910', '#fef9e7'),
            'media': ('#d68910', '#fef9e7'),
            'baixa': ('#1e8449', '#eafaf1'),
        }
        fg, bg = colors.get((value or '').lower(), ('#555', '#f0f0f0'))
        return (
            f'<span style="background:{bg};color:{fg};padding:2px 10px;'
            f'border-radius:12px;font-size:0.78rem;font-weight:600;'
            f'border:1px solid {fg}33">{value or "—"}</span>'
        )

    @staticmethod
    def _read_only_table(rows: list) -> None:
        html = '<table style="width:100%;border-collapse:collapse;font-size:0.85rem;margin-top:0.5rem">'
        for label, value in rows:
            html += (
                f'<tr style="border-bottom:1px solid #ececec">'
                f'<td style="padding:6px 10px;color:#888;font-weight:600;white-space:nowrap;width:160px">{label}</td>'
                f'<td style="padding:6px 10px;color:#2d2d2d">{value}</td></tr>'
            )
        html += '</table>'
        st.markdown(html, unsafe_allow_html=True)

    @staticmethod
    def _next_matriz_id(matriz: list) -> str:
        max_n = 0
        for row in matriz:
            digits = ''.join(c for c in str(row.get('id', '')) if c.isdigit())
            if digits:
                try:
                    max_n = max(max_n, int(digits))
                except ValueError:
                    pass
        return f"MC-{max_n + 1:03d}"

    def _err(self, error: Exception):
        if isinstance(error, ValueError):
            st.error(f"❌ Erro de Integridade Estrutural: {error}")
        elif isinstance(error, requests.exceptions.Timeout):
            st.error("⏱️ Timeout: o n8n demorou demais para responder.")
        elif isinstance(error, requests.exceptions.ConnectionError):
            st.error("🔌 Network Error: não foi possível conectar ao n8n.")
        elif isinstance(error, requests.exceptions.HTTPError):
            st.error(f"❌ HTTP Exception: {error}")
        else:
            st.error(f"❌ Fatal Error: {error}")


    def _inject_ui_styles(self):
        st.markdown(
            """
            <style>
                div[data-testid="stButton"] > button {
                    justify-content: flex-start;
                    text-align: left;
                }
                div[data-testid="stButton"] > button p {
                    text-align: left;
                }
            </style>
            """,
            unsafe_allow_html=True,
        )

    def _header(self):
        with st.sidebar:
            sidebar_logo_b64 = ""
            if Path(LOGO_PATH).exists():
                try:
                    with open(LOGO_PATH, 'rb') as f:
                        sidebar_logo_b64 = base64.b64encode(f.read()).decode('utf-8')
                except Exception:
                    pass
            if sidebar_logo_b64:
                st.markdown(
                    f"""
                    <div style="width:100%;padding:0 0 .75rem 0;">
                        <img src="data:image/png;base64,{sidebar_logo_b64}"
                             style="width:100%;height:auto;object-fit:contain;border-radius:0;display:block;">
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

            st.divider()
            st.warning("⚠️ Controles")
            if self.state.get('is_processing'):
                st.info("Processamento em andamento. Aguarde a conclusão ou solicite a interrupção.")
                if st.button("⏹️ Interromper Processamento", use_container_width=True, type="primary"):
                    confirm_interrupt_modal()
            if st.button("🔄 Nova Análise", use_container_width=True, type="primary"):
                self.state.clear()
                st.rerun()

        img_b64 = ""
        if Path(LOGO_PATH).exists():
            try:
                with open(LOGO_PATH, 'rb') as f:
                    img_b64 = base64.b64encode(f.read()).decode('utf-8')
            except Exception:
                pass

        st.markdown(
            f"""
            <div style="display:flex;align-items:stretch;margin-bottom:1.5rem;gap:1.5rem;">
                <div style="flex:0 0 200px;display:flex;align-items:center;justify-content:center;">
                    <img src="data:image/png;base64,{img_b64}"
                         style="max-width:100%;max-height:80px;object-fit:contain;">
                </div>
                <div style="flex:1;background:linear-gradient(135deg,#F15A24,#c94a1a);padding:1rem 1.5rem;border-radius:6px;display:flex;flex-direction:column;justify-content:center;min-height:80px;">
                    <h1 style="color:white;margin:0;font-size:1.6rem;padding:0;">🧪 QA TestGen – Automation</h1>
                    <p style="color:white;margin:0.2rem 0 0 0;font-size:1.05rem;padding:0;">Gerador Inteligente de Casos de Teste — Azure DevOps Integration</p>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    def _has_editing_in_progress(self) -> bool:
        """Retorna True se houver qualquer formulário de edição ou criação em aberto."""
        state = self.state
        # Criação em aberto
        if state.get('adding_matriz_row') or state.get('adding_test_case') or state.get('adding_test_plan'):
            return True
        # Edição de linha da Matriz
        matriz = state.get('matriz') or []
        for i in range(len(matriz)):
            if state.get(f'edit_m_{i}', False):
                return True
        # Edição de Caso de Teste
        test_cases = state.get('test_cases') or []
        for i in range(len(test_cases)):
            if state.get(f'edit_tc_{i}', False):
                return True
        # Edição de Plano de Teste
        test_plans = state.get('test_plans') or []
        for i in range(len(test_plans)):
            if state.get(f'edit_p_{i}', False):
                return True
        return False



    def _render_row_toggle(self, active_key: str, index: int, label: str, disabled: bool = False) -> bool:
        is_active = self.state.get(active_key) == index
        marker = "▼" if is_active else "▶"
        if st.button(f"{marker} {label}", key=f"{active_key}_{index}", use_container_width=True, disabled=disabled):
            self.state.set(active_key, None if is_active else index)
            st.rerun()
        return is_active

    def _normalize_active_row(self, active_key: str, total: int):
        active = self.state.get(active_key)
        if not isinstance(active, int) or active < 0 or active >= total:
            self.state.set(active_key, None)

    def _processing_banner(self):
        if not self.state.get('is_processing'):
            return
        labels = {
            'analyze_docs': 'Analisando a documentação com IA',
            'generate_matrix': 'Gerando a Matriz de Cobertura',
            'generate_cases': 'Gerando os Casos de Teste',
            'generate_plans': 'Gerando os Planos de Teste',
            'build_artifacts': 'Construindo os artefatos finais',
        }
        action = labels.get(self.state.get('current_action'), 'Processando informações')
        st.markdown(
            """
            <style>
                .qa-processing-shade {
                    position: fixed;
                    inset: 0;
                    background: rgba(20, 24, 31, 0.18);
                    z-index: 999;
                    pointer-events: none;
                }
                .qa-processing-card {
                    position: fixed;
                    right: 1.5rem;
                    bottom: 1.5rem;
                    z-index: 1000;
                    background: #ffffff;
                    border: 1px solid #f15a24;
                    border-left: 5px solid #f15a24;
                    border-radius: 6px;
                    box-shadow: 0 12px 28px rgba(0,0,0,.18);
                    padding: .9rem 1rem;
                    max-width: 380px;
                    pointer-events: none;
                }
                .qa-processing-title {font-weight: 700;color: #3A3A3A;margin-bottom: .2rem;}
                .qa-processing-text {color: #5b5b5b;font-size: .9rem;}
                .qa-processing-dot {
                    display: inline-block;
                    width: .6rem;
                    height: .6rem;
                    margin-right: .45rem;
                    border-radius: 50%;
                    background: #f15a24;
                    animation: qaPulse 1s infinite ease-in-out;
                }
                @keyframes qaPulse {0%, 100% {opacity: .25; transform: scale(.85);} 50% {opacity: 1; transform: scale(1.1);}}
            </style>
            <div class="qa-processing-shade"></div>
            <div class="qa-processing-card">
                <div class="qa-processing-title"><span class="qa-processing-dot"></span>Processamento em andamento</div>
                <div class="qa-processing-text">Aguarde a conclusão. Evite navegar ou atualizar a página durante esta etapa.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.warning(f"⏳ {action}. Aguarde a conclusão para continuar.")
        if st.button("⏹️ Solicitar interrupção", key="request_interrupt_main", type="primary", use_container_width=True):
            confirm_interrupt_modal()

    def _progress(self):
        """Barra de progresso com navegação restrita aos passos liberados."""
        labels = ["📄 Upload", "💬 Dúvidas", "📊 Matriz", "📋 Casos", "📁 Planos", "⬇️ Download"]
        cols = st.columns(6)
        current_step = self.state.get('step')
        max_step = self.state.get('max_step', current_step)
        completed_steps = set(self.state.get('completed_steps') or [])
        is_processing = self.state.get('is_processing')

        for i, (col, label) in enumerate(zip(cols, labels), start=1):
            with col:
                is_current = i == current_step
                is_accessible = self.can_access_step(i, current_step, max_step, completed_steps, is_processing)

                if is_current:
                    st.markdown(
                        f"<div style='padding:.45rem .5rem;border-radius:4px;background:#d0e8ff;"
                        f"color:#0a4f8a;text-align:center;font-weight:700;border:1.5px solid #4A90D9'>"
                        f"{label}</div>",
                        unsafe_allow_html=True,
                    )
                elif is_accessible:
                    if st.button(label, key=f"nav_step_{i}", use_container_width=True, disabled=is_processing):
                        if self._has_editing_in_progress():
                            confirm_navigate_away_modal(i)
                        else:
                            clear_widget_states()
                            self._set_step(i)
                            st.rerun()
                else:
                    st.button(label, key=f"nav_step_{i}", use_container_width=True, disabled=True)
        st.divider()

    def _ensure_steps_state(self, key: str, initial: list):
        if key not in self.state:
            if initial:
                self.state.set(key, [
                    {"uid": str(uuid.uuid4()), "acao": s.get('acao', ''), "resultado_esperado": s.get('resultado_esperado', '')}
                    for s in initial
                ])
            else:
                self.state.set(key, [{"uid": str(uuid.uuid4()), "acao": "", "resultado_esperado": ""}])

    def _render_steps_editor(self, steps_key: str, prefix: str) -> list:
        steps_list = self.state.get(steps_key)
        st.markdown("**Test Steps:**")
        result = []
        for index, step in enumerate(steps_list):
            uid = step['uid']
            cA, cB, cDel = st.columns([5, 5, 1])
            with cA:
                acao = st.text_area(
                    f"Ação {index + 1} *",
                    value=step.get('acao', ''),
                    key=f"{prefix}_acao_{uid}",
                    height=80,
                )
            with cB:
                esp = st.text_area(
                    f"Esperado {index + 1} *",
                    value=step.get('resultado_esperado', ''),
                    key=f"{prefix}_esp_{uid}",
                    height=80,
                )
            with cDel:
                st.markdown("<div style='margin-top:1.8rem'></div>", unsafe_allow_html=True)
                if st.button("🗑️", key=f"{prefix}_delstep_{uid}", disabled=len(steps_list) <= 1):
                    confirm_step_deletion_modal(steps_key, uid)
            result.append({"uid": uid, "acao": acao, "resultado_esperado": esp})

        if len(steps_list) <= 1:
            st.caption("ℹ️ É necessário manter ao menos 1 step.")
        self.state.set(steps_key, result)
        if st.button("➕ Adicionar Step", key=f"{prefix}_addstep"):
            updated = self.state.get(steps_key)
            updated.append({"uid": str(uuid.uuid4()), "acao": "", "resultado_esperado": ""})
            self.state.set(steps_key, updated)
            st.rerun()
        return [{"acao": s['acao'], "resultado_esperado": s['resultado_esperado']} for s in result]

    def _ensure_suites_state(self, key: str, initial: list):
        if key not in self.state:
            if initial:
                self.state.set(key, [
                    {
                        "uid": str(uuid.uuid4()),
                        "nome": s.get('nome', ''),
                        "descricao": s.get('descricao', ''),
                        "casos": s.get('casos', []),
                    }
                    for s in initial
                ])
            else:
                self.state.set(key, [{"uid": str(uuid.uuid4()), "nome": "", "descricao": "", "casos": []}])

    def _render_suites_editor(self, suites_key: str, prefix: str, available_cases: list) -> list:
        suites_list = self.state.get(suites_key)
        st.markdown("**Test Suites:**")
        result = []
        for index, suite in enumerate(suites_list):
            uid = suite['uid']
            with st.container(border=True):
                col_hdr, col_del = st.columns([11, 1])
                with col_hdr:
                    st.markdown(f"**Suite {index + 1}**")
                with col_del:
                    if st.button("🗑️", key=f"{prefix}_delsuite_{uid}", help="Remover esta Suite", disabled=len(suites_list) <= 1):
                        confirm_suite_deletion_modal(suites_key, uid)

                nome = st.text_input(
                    f"Nome da Suite {index + 1} *",
                    value=suite.get('nome', ''),
                    key=f"{prefix}_sname_{uid}",
                )
                desc = st.text_input(
                    f"Descrição da Suite {index + 1}",
                    value=suite.get('descricao', ''),
                    key=f"{prefix}_sdesc_{uid}",
                )
                casos_sel = st.multiselect(
                    f"Casos de Teste vinculados à Suite {index + 1} *",
                    options=available_cases,
                    default=[c for c in suite.get('casos', []) if c in available_cases],
                    key=f"{prefix}_scasos_{uid}",
                )
            result.append({"uid": uid, "nome": nome, "descricao": desc, "casos": casos_sel})

        if len(suites_list) <= 1:
            st.caption("ℹ️ É necessário manter ao menos 1 Suite.")

        self.state.set(suites_key, result)
        if st.button("➕ Adicionar Suite", key=f"{prefix}_addsuite"):
            updated = self.state.get(suites_key)
            updated.append({"uid": str(uuid.uuid4()), "nome": "", "descricao": "", "casos": []})
            self.state.set(suites_key, updated)
            st.rerun()
        return [{"nome": s['nome'], "descricao": s['descricao'], "casos": s['casos']} for s in result]

    @staticmethod
    def _validate_matriz(nid: str, nfunc: str, nreq: str, ncen: str, ncat: str, npri: str, ncrit: str) -> list:
        return MatrixValidator.validate(
            type('Row', (), {
                'id': nid,
                'funcionalidade': nfunc,
                'requisito': nreq,
                'cenario': ncen,
                'categoria': ncat,
                'prioridade': npri,
                'criticidade': ncrit,
            })
        )

    @staticmethod
    def _validate_tc(titulo: str, pre: str, steps: list) -> list:
        from types import SimpleNamespace

        test_case = SimpleNamespace(titulo=titulo, pre_condicoes=pre, passos=[
            SimpleNamespace(acao=s['acao'], resultado_esperado=s['resultado_esperado'])
            for s in steps
        ])
        return TestCaseValidator.validate(test_case)

    @staticmethod
    def _validate_plan(nome: str, suites: list) -> list:
        from types import SimpleNamespace

        test_plan = SimpleNamespace(nome=nome, suites=[
            SimpleNamespace(nome=s['nome'], casos=s['casos']) for s in suites
        ])
        return TestPlanValidator.validate(test_plan)

    def _render_matriz_form(self, prefix: str, row: dict) -> dict:
        c1, c2, c3 = st.columns(3)
        with c1:
            nid = st.text_input("ID *", value=row.get('id', ''), key=f"{prefix}_id")
            nfunc = st.text_input("Funcionalidade *", value=row.get('funcionalidade', ''), key=f"{prefix}_func")
            nreq = st.text_input("Requisito *", value=row.get('requisito', ''), key=f"{prefix}_req")
        with c2:
            ncen = st.text_area("Cenário *", value=row.get('cenario', ''), key=f"{prefix}_cen", height=100)
            ncat = st.text_input("Categoria *", value=row.get('categoria', ''), key=f"{prefix}_cat")
        with c3:
            opts = ["Alta", "Média", "Baixa"]
            def idx(o, v):
                try:
                    return [x.lower() for x in o].index((v or '').lower())
                except ValueError:
                    return 0
            npri = st.selectbox("Prioridade *", opts, index=idx(opts, row.get('prioridade')), key=f"{prefix}_pri")
            ncrit = st.selectbox("Criticidade *", opts, index=idx(opts, row.get('criticidade')), key=f"{prefix}_crit")
            nobs = st.text_input("Observações", value=row.get('observacoes', ''), key=f"{prefix}_obs")
        return {
            'id': nid,
            'funcionalidade': nfunc,
            'requisito': nreq,
            'cenario': ncen,
            'categoria': ncat,
            'prioridade': npri,
            'criticidade': ncrit,
            'observacoes': nobs,
        }

    def step_1(self):
        st.subheader("Passo 1 – Setup e Documentação")
        if self.state.get('processing_interrupted'):
            st.info("⚠️ Processamento interrompido. Você pode continuar editando esta etapa.")

        col1, col2 = st.columns(2)
        with col1:
            project = st.text_input(
                "Nome do Projeto *",
                value=self.state.get('project_name', ''),
                key='project_name_input',
                placeholder="Ex: Passaporte Refuturiza",
            )
            if project:
                self.state.set('project_name', project)
        with col2:
            uploaded = st.file_uploader(
                "Documento de Requisitos (Máx 20MB) *",
                type=["pdf", "txt", "docx"],
                key='step1_uploaded_file',
                disabled=self.state.get('is_processing'),
            ) or self.state.get('uploaded_file')
            if uploaded is not None:
                self.state.set('uploaded_file', uploaded)

        if uploaded and uploaded.size > 20 * 1024 * 1024:
            st.error("❌ Arquivo excede o limite máximo de 20MB.")
            return

        if not project or not uploaded:
            st.info("Preencha o nome do projeto e faça o upload do documento para continuar.")
            return

        st.button(
            "🔍 Executar Análise de Cobertura (IA)",
            use_container_width=True,
            type="primary",
            on_click=self.trigger_action,
            args=("analyze_docs",),
            disabled=self.state.get('is_processing'),
        )

        if self.state.get('current_action') == 'analyze_docs':
            with st.spinner("Extraindo texto..."):
                text = DocumentProcessor.extract_plain_text(uploaded)
            if not text:
                st.error("Não foi possível extrair texto.")
                self.clear_action()
            else:
                with st.spinner("Aguarde enquanto a análise é processada… Isso pode levar alguns minutos, dependendo do tamanho do documento..."):
                    try:
                        resp = self.client.trigger_analysis(text, project)
                        self.state.set('doc_text', text)
                        self.state.set('project_name', project)
                        self.state.set('questions', resp.get('duvidas') or [])
                        self._set_step(2, allow_during_processing=True)
                        self.clear_action()
                        st.rerun()
                    except Exception as error:
                        self._err(error)
                        self.clear_action()

    def step_2(self):
        st.subheader("Passo 2 – Resolução de Conflitos e Ambiguidade")
        questions = self.state.get('questions')
        answers = {}
        existing_answers = self.state.get('step_2_answers', {})
        is_generating_matrix = self.state.get('current_action') == 'generate_matrix' or self.state.get('is_processing')
        if not questions:
            st.success("✅ A IA não identificou ambiguidades. Prossiga para gerar a Matriz.")
        else:
            st.info(f"A engine de validação identificou **{len(questions)} ponto(s) crítico(s)**.")
            for question in questions:
                qid = str(question.get('id', '0'))
                st.markdown(f"**❓ #{qid}:** {question.get('pergunta', '')}")
                answers[qid] = st.text_area(
                    f"Resposta #{qid}",
                    key=f"q_{qid}",
                    value=existing_answers.get(qid, ''),
                    placeholder="Descreva a regra de negócio consolidada…",
                    disabled=is_generating_matrix,
                )
        if is_generating_matrix:
            answers = existing_answers
        else:
            self.state.set('step_2_answers', answers)

        c1, c2 = st.columns([1, 3])
        with c1:
            if st.button("← Voltar", use_container_width=True, disabled=self.state.get('is_processing')):
                self._set_step(1)
                st.rerun()
        with c2:
            st.button(
                "📊 Gerar Matriz de Cobertura",
                use_container_width=True,
                type="primary",
                on_click=self.trigger_action,
                args=("generate_matrix",),
                disabled=self.state.get('is_processing'),
            )

        if self.state.get('current_action') == 'generate_matrix':
            with st.spinner("Estruturando Matriz de Rastreabilidade… Aguarde um momento..."):
                try:
                    resp = self.client.trigger_matrix(
                        self.state.get('doc_text'),
                        self.state.get('step_2_answers', answers),
                        self.state.get('project_name'),
                    )
                    matriz = resp.get('matriz') or []
                    if not matriz:
                        st.error("❌ Matriz vazia.")
                        self.clear_action()
                    else:
                        self.state.set('user_answers', self.state.get('step_2_answers', answers))
                        self.state.set('matriz', matriz)
                        self._set_step(3, allow_during_processing=True)
                        self.clear_action()
                        st.rerun()
                except Exception as error:
                    self._err(error)
                    self.clear_action()

    def step_3(self):
        st.subheader("Passo 3 – Refinamento da Matriz de Cobertura")
        matriz = self.state.get('matriz')

        if not matriz:
            st.info("A Matriz de Cobertura está vazia.")
        else:
            st.info(f"**{len(matriz)} cenário(s) mapeado(s)**. Clique em uma linha para ver os detalhes.")

        editing_any = any(self.state.get(f'edit_m_{j}', False) for j in range(len(matriz)))
        self._normalize_active_row('active_matriz_row', len(matriz))

        for i, row in enumerate(matriz):
            is_editing = self.state.get(f"edit_m_{i}", False)
            if is_editing:
                editing_any = True
            label = f"{row.get('id', f'MC-{i+1:03d}')} - {row.get('cenario', '')}"
            if self._render_row_toggle('active_matriz_row', i, label, disabled=self.state.get('is_processing') or (editing_any and not is_editing)):
                if is_editing:
                    with st.container(border=True):
                        vals = self._render_matriz_form(f"m{i}", row)
                        cs, cc = st.columns(2)
                        with cs:
                            if st.button("💾 Salvar Alterações", key=f"save_m_{i}", type="primary", use_container_width=True):
                                missing = self._validate_matriz(
                                    vals['id'], vals['funcionalidade'], vals['requisito'],
                                    vals['cenario'], vals['categoria'], vals['prioridade'], vals['criticidade'],
                                )
                                if missing:
                                    st.error("❌ Campos obrigatórios faltando: " + ", ".join(missing) + ".")
                                else:
                                    matriz[i] = vals
                                    self.state.set('matriz', matriz)
                                    self.state.set(f"edit_m_{i}", False)
                                    st.rerun()
                        with cc:
                            if st.button("✖ Cancelar", key=f"cancel_m_{i}", use_container_width=True):
                                self.state.set(f"edit_m_{i}", False)
                                st.rerun()
                else:
                    self._read_only_table([
                        ("ID", row.get('id', '—')),
                        ("Funcionalidade", row.get('funcionalidade', '—')),
                        ("Requisito", row.get('requisito', '—')),
                        ("Cenário", row.get('cenario', '—')),
                        ("Categoria", row.get('categoria', '—')),
                        ("Prioridade", self._priority_badge(row.get('prioridade', ''))),
                        ("Criticidade", self._priority_badge(row.get('criticidade', ''))),
                        ("Observações", row.get('observacoes') or '—'),
                    ])
                    st.markdown("<div style='margin-top:.75rem'></div>", unsafe_allow_html=True)
                    ce, cd, _ = st.columns([1, 1, 6])
                    with ce:
                        if st.button("✏️ Editar", key=f"btn_edit_m_{i}", use_container_width=True, disabled=self.state.get('is_processing')):
                            self.state.set(f"edit_m_{i}", True)
                            self.state.set('active_matriz_row', i)
                            st.rerun()
                    with cd:
                        if st.button("🗑️ Excluir", key=f"btn_del_m_{i}", type="primary", use_container_width=True, disabled=self.state.get('is_processing')):
                            confirm_matriz_deletion_modal(i)

        st.markdown("<div style='margin-top:.5rem'></div>", unsafe_allow_html=True)
        if self.state.get('adding_matriz_row'):
            with st.expander("**➕ Novo Cenário**", expanded=True):
                with st.container(border=True):
                    blank = {'prioridade': '', 'criticidade': ''}
                    if 'newm_id' not in st.session_state:
                        st.session_state['newm_id'] = self._next_matriz_id(matriz)
                    vals = self._render_matriz_form('newm', blank)
                    cs, cc = st.columns(2)
                    with cs:
                        if st.button("💾 Salvar Novo Cenário", key="save_newm", type="primary", use_container_width=True):
                            missing = self._validate_matriz(
                                vals['id'], vals['funcionalidade'], vals['requisito'],
                                vals['cenario'], vals['categoria'], vals['prioridade'], vals['criticidade'],
                            )
                            if missing:
                                st.error("❌ Campos obrigatórios faltando: " + ", ".join(missing) + ".")
                            else:
                                matriz.append(vals)
                                self.state.set('matriz', matriz)
                                self.state.set('adding_matriz_row', False)
                                clear_widget_states()
                                st.rerun()
                    with cc:
                        if st.button("✖ Cancelar", key="cancel_newm", use_container_width=True):
                            confirm_discard_new_modal('adding_matriz_row')
        else:
            if st.button("➕ Adicionar Novo Cenário à Matriz", use_container_width=True, disabled=editing_any or self.state.get('is_processing')):
                self.state.set('active_matriz_row', None)
                self.state.set('adding_matriz_row', True)
                st.rerun()

        st.divider()
        c1, c2 = st.columns([1, 3])
        with c1:
            if st.button("← Voltar", use_container_width=True, disabled=self.state.get('is_processing')):
                self._set_step(2)
                st.rerun()
        with c2:
            if editing_any or self.state.get('adding_matriz_row'):
                st.warning("⚠️ Salve ou cancele a edição/criação em aberto para prosseguir.")
            else:
                st.button(
                    "🚀 Gerar Casos de Teste",
                    use_container_width=True,
                    type="primary",
                    on_click=self.trigger_action,
                    args=("generate_cases",),
                    disabled=self.state.get('is_processing'),
                )

        if self.state.get('current_action') == 'generate_cases':
            with st.spinner("Gerando Casos de Teste… Aguarde um momento..."):
                try:
                    resp = self.client.trigger_generation(
                        self.state.get('doc_text'),
                        self.state.get('matriz'),
                        self.state.get('user_answers'),
                        self.state.get('project_name'),
                    )
                    casos = resp.get('casos_de_teste') or []
                    if not casos:
                        st.error("❌ Lista de casos vazia.")
                        self.clear_action()
                    else:
                        self.state.set('test_cases', casos)
                        self._set_step(4, allow_during_processing=True)
                        self.clear_action()
                        st.rerun()
                except Exception as error:
                    self._err(error)
                    self.clear_action()

    def step_4(self):
        st.subheader("Passo 4 – Console de Casos de Teste")
        test_cases = self.state.get('test_cases')

        if not test_cases:
            st.info("Nenhum caso de teste compilado.")
        else:
            st.info(f"**{len(test_cases)} script(s)** consolidados. Clique em um caso para ver os detalhes.")

        editing_any = any(self.state.get(f'edit_tc_{j}', False) for j in range(len(test_cases)))
        self._normalize_active_row('active_test_case_row', len(test_cases))

        for idx, tc in enumerate(test_cases):
            is_editing = self.state.get(f"edit_tc_{idx}", False)
            if is_editing:
                editing_any = True
            label = f"TC-{idx + 1:02d} - {tc.get('titulo', '')}"
            if self._render_row_toggle('active_test_case_row', idx, label, disabled=self.state.get('is_processing') or (editing_any and not is_editing)):
                if is_editing:
                    with st.container(border=True):
                        titulo = st.text_input("Título *", value=tc.get('titulo', ''), key=f"tt_{idx}")
                        pre = st.text_area("Pré-condições *", value=tc.get('pre_condicoes', ''), key=f"tp_{idx}", height=70)
                        sk = f"edit_steps_{idx}"
                        self._ensure_steps_state(sk, tc.get('passos', []))
                        steps = self._render_steps_editor(sk, f"etc{idx}")
                        cs, cc = st.columns(2)
                        with cs:
                            if st.button("💾 Salvar Caso de Teste", key=f"save_tc_{idx}", type="primary", use_container_width=True):
                                missing = self._validate_tc(titulo, pre, steps)
                                if missing:
                                    st.error("❌ Campos obrigatórios faltando: " + ", ".join(missing) + ".")
                                else:
                                    test_cases[idx] = {
                                        'titulo': titulo,
                                        'pre_condicoes': pre,
                                        'passos': [
                                            {'numero': n + 1, 'acao': step['acao'], 'resultado_esperado': step['resultado_esperado']}
                                            for n, step in enumerate(steps)
                                        ],
                                    }
                                    self.state.set('test_cases', test_cases)
                                    self.state.set(f"edit_tc_{idx}", False)
                                    self.state.delete(sk)
                                    st.rerun()
                        with cc:
                            if st.button("✖ Cancelar", key=f"cancel_tc_{idx}", use_container_width=True):
                                self.state.set(f"edit_tc_{idx}", False)
                                self.state.delete(sk)
                                st.rerun()
                else:
                    self._read_only_table([("Pré-condições", tc.get('pre_condicoes') or '—')])
                    passos = tc.get('passos', [])
                    if passos:
                        html = (
                            '<table style="width:100%;border-collapse:collapse;font-size:.83rem;margin-top:.6rem">'
                            '<thead><tr style="background:#3A3A3A;color:#fff">'
                            '<th style="padding:6px 10px;width:40px">#</th>'
                            '<th style="padding:6px 10px;width:48%">Ação</th>'
                            '<th style="padding:6px 10px">Resultado Esperado</th>'
                            '</tr></thead><tbody>'
                        )
                        for si, step in enumerate(passos):
                            bg = '#fff' if si % 2 == 0 else '#f5f5f5'
                            html += (
                                f'<tr style="background:{bg};border-bottom:1px solid #e0e0e0">'
                                f'<td style="padding:6px 10px;color:#888;font-weight:600">{step.get("numero", "")}</td>'
                                f'<td style="padding:6px 10px;color:#2d2d2d">{step.get("acao", "")}</td>'
                                f'<td style="padding:6px 10px;color:#2d2d2d">{step.get("resultado_esperado", "")}</td></tr>'
                            )
                        html += '</tbody></table>'
                        st.markdown(html, unsafe_allow_html=True)
                    st.markdown("<div style='margin-top:.75rem'></div>", unsafe_allow_html=True)
                    ce, cd, _ = st.columns([1, 1, 6])
                    with ce:
                        if st.button("✏️ Editar", key=f"btn_edit_tc_{idx}", use_container_width=True, disabled=self.state.get('is_processing')):
                            self.state.set(f"edit_tc_{idx}", True)
                            self.state.set('active_test_case_row', idx)
                            st.rerun()
                    with cd:
                        if st.button("🗑️ Excluir", key=f"btn_del_tc_{idx}", type="primary", use_container_width=True, disabled=self.state.get('is_processing')):
                            confirm_deletion_modal('test_cases', idx)

        st.markdown("<div style='margin-top:.5rem'></div>", unsafe_allow_html=True)
        if self.state.get('adding_test_case'):
            with st.expander("**➕ Novo Caso de Teste**", expanded=True):
                with st.container(border=True):
                    titulo = st.text_input("Título *", key="newtc_titulo")
                    pre = st.text_area("Pré-condições *", key="newtc_pre", height=70)
                    sk = "new_steps_tc"
                    self._ensure_steps_state(sk, [])
                    steps = self._render_steps_editor(sk, "newtc")
                    cs, cc = st.columns(2)
                    with cs:
                        if st.button("💾 Salvar Novo Caso de Teste", key="save_newtc", type="primary", use_container_width=True):
                            missing = self._validate_tc(titulo, pre, steps)
                            if missing:
                                st.error("❌ Campos obrigatórios faltando: " + ", ".join(missing) + ".")
                            else:
                                test_cases.append({
                                    'titulo': titulo,
                                    'pre_condicoes': pre,
                                    'passos': [
                                        {'numero': n + 1, 'acao': step['acao'], 'resultado_esperado': step['resultado_esperado']}
                                        for n, step in enumerate(steps)
                                    ],
                                })
                                self.state.set('test_cases', test_cases)
                                self.state.set('adding_test_case', False)
                                self.state.delete(sk)
                                clear_widget_states()
                                st.rerun()
                    with cc:
                        if st.button("✖ Cancelar", key="cancel_newtc", use_container_width=True):
                            confirm_discard_new_modal('adding_test_case')
        else:
            if st.button("➕ Adicionar Novo Caso de Teste", use_container_width=True, disabled=editing_any or self.state.get('is_processing')):
                self.state.set('active_test_case_row', None)
                self.state.set('adding_test_case', True)
                st.rerun()

        st.divider()
        c1, c2 = st.columns([1, 3])
        with c1:
            if st.button("← Voltar", use_container_width=True, disabled=self.state.get('is_processing')):
                self._set_step(3)
                st.rerun()
        with c2:
            if editing_any or self.state.get('adding_test_case'):
                st.warning("⚠️ Salve ou cancele a edição/criação em aberto para prosseguir.")
            else:
                st.button(
                    "📁 Gerar Planos de Teste",
                    use_container_width=True,
                    type="primary",
                    on_click=self.trigger_action,
                    args=("generate_plans",),
                    disabled=self.state.get('is_processing'),
                )

        if self.state.get('current_action') == 'generate_plans':
            with st.spinner("Gerando Planos de Teste com a IA… isso pode levar alguns minutos."):
                try:
                    resp = self.client.trigger_plans(
                        self.state.get('doc_text'),
                        self.state.get('matriz'),
                        self.state.get('test_cases'),
                        self.state.get('user_answers'),
                        self.state.get('project_name'),
                    )
                    plans = resp.get('planos_de_teste') or []
                    if not plans:
                        st.error("❌ Nenhum Plano de Teste retornado. Valide a chave JSON de saída no n8n.")
                        self.clear_action()
                    else:
                        self.state.set('test_plans', plans)
                        self._set_step(5, allow_during_processing=True)
                        self.clear_action()
                        st.rerun()
                except Exception as error:
                    self._err(error)
                    self.clear_action()

    def step_5(self):
        st.subheader("Passo 5 – Refinamento dos Planos de Teste")
        test_plans = self.state.get('test_plans')
        available_cases = [tc.get('titulo', '') for tc in self.state.get('test_cases')]

        if not test_plans:
            st.info("Nenhum Plano de Teste gerado.")
        else:
            st.info(
                f"**{len(test_plans)} Plano(s)** gerado(s). "
                "Cada Plano contém Suites que agrupam os Casos de Teste. "
                "Clique em um Plano para ver os detalhes."
            )

        editing_any = any(self.state.get(f'edit_p_{j}', False) for j in range(len(test_plans)))
        self._normalize_active_row('active_test_plan_row', len(test_plans))

        for i, plan in enumerate(test_plans):
            is_editing = self.state.get(f"edit_p_{i}", False)
            if is_editing:
                editing_any = True

            suites = plan.get('suites', [])
            suite_names = ", ".join(s.get('nome', '') for s in suites) if suites else "Sem suites"
            label = f"**Plano {i + 1:02d}** – {plan.get('nome', '')}  ·  Suites: {suite_names}"

            if self._render_row_toggle('active_test_plan_row', i, label, disabled=self.state.get('is_processing') or (editing_any and not is_editing)):
                if is_editing:
                    with st.container(border=True):
                        nome = st.text_input("Nome do Plano *", value=plan.get('nome', ''), key=f"pn_{i}")
                        desc = st.text_input("Descrição", value=plan.get('descricao', ''), key=f"pd_{i}")

                        sk = f"suites_edit_{i}"
                        self._ensure_suites_state(sk, plan.get('suites', []))
                        suites_vals = self._render_suites_editor(sk, f"ep{i}", available_cases)

                        cs, cc = st.columns(2)
                        with cs:
                            if st.button("💾 Salvar Plano", key=f"save_p_{i}", type="primary", use_container_width=True):
                                missing = self._validate_plan(nome, suites_vals)
                                if missing:
                                    st.error("❌ Campos obrigatórios faltando: " + ", ".join(missing) + ".")
                                else:
                                    test_plans[i] = {'nome': nome, 'descricao': desc, 'suites': suites_vals}
                                    self.state.set('test_plans', test_plans)
                                    self.state.set(f"edit_p_{i}", False)
                                    self.state.delete(sk)
                                    st.rerun()
                        with cc:
                            if st.button("✖ Cancelar", key=f"cancel_p_{i}", use_container_width=True):
                                self.state.set(f"edit_p_{i}", False)
                                self.state.delete(sk)
                                st.rerun()
                else:
                    self._read_only_table([
                        ("Nome", plan.get('nome', '—')),
                        ("Descrição", plan.get('descricao') or '—'),
                    ])
                    if suites:
                        st.markdown("<div style='margin-top:.6rem'></div>", unsafe_allow_html=True)
                        for s_idx, suite in enumerate(suites, start=1):
                            casos = suite.get('casos', [])
                            st.markdown(
                                f"<div style='background:#f0f4ff;border-left:3px solid #4A90D9;"
                                f"padding:6px 12px;margin:4px 0;border-radius:3px;font-size:.85rem'>"
                                f"<b>Suite {s_idx}: {suite.get('nome', '')}</b>"
                                + (f" — {suite.get('descricao', '')}" if suite.get('descricao') else "")
                                + f"<br><span style='color:#555'>Casos vinculados ({len(casos)}): "
                                + (", ".join(casos) if casos else "Nenhum")
                                + "</span></div>",
                                unsafe_allow_html=True,
                            )

                    st.markdown("<div style='margin-top:.75rem'></div>", unsafe_allow_html=True)
                    ce, cd, _ = st.columns([1, 1, 6])
                    with ce:
                        if st.button("✏️ Editar", key=f"btn_edit_p_{i}", use_container_width=True):
                            self.state.set(f"edit_p_{i}", True)
                            self.state.set('active_test_plan_row', i)
                            st.rerun()
                    with cd:
                        if st.button("🗑️ Excluir", key=f"btn_del_p_{i}", type="primary", use_container_width=True):
                            confirm_deletion_modal('test_plans', i)

        st.markdown("<div style='margin-top:.5rem'></div>", unsafe_allow_html=True)
        if self.state.get('adding_test_plan'):
            with st.expander("**➕ Novo Plano de Teste**", expanded=True):
                with st.container(border=True):
                    nome = st.text_input("Nome do Plano *", key="newp_nome")
                    desc = st.text_input("Descrição", key="newp_desc")
                    sk = "new_suites_plan"
                    self._ensure_suites_state(sk, [])
                    suites_vals = self._render_suites_editor(sk, "newp", available_cases)
                    cs, cc = st.columns(2)
                    with cs:
                        if st.button("💾 Salvar Novo Plano", key="save_newp", type="primary", use_container_width=True):
                            missing = self._validate_plan(nome, suites_vals)
                            if missing:
                                st.error("❌ Campos obrigatórios faltando: " + ", ".join(missing) + ".")
                            else:
                                test_plans.append({'nome': nome, 'descricao': desc, 'suites': suites_vals})
                                self.state.set('test_plans', test_plans)
                                self.state.set('adding_test_plan', False)
                                self.state.delete(sk)
                                clear_widget_states()
                                st.rerun()
                    with cc:
                        if st.button("✖ Cancelar", key="cancel_newp", use_container_width=True):
                            confirm_discard_new_modal('adding_test_plan')
        else:
            if st.button("➕ Adicionar Novo Plano de Teste", use_container_width=True, disabled=editing_any or self.state.get('is_processing')):
                self.state.set('active_test_plan_row', None)
                self.state.set('adding_test_plan', True)
                st.rerun()

        st.divider()
        c1, c2 = st.columns([1, 3])
        with c1:
            if st.button("← Voltar", use_container_width=True, disabled=self.state.get('is_processing')):
                self._set_step(4)
                st.rerun()
        with c2:
            if editing_any or self.state.get('adding_test_plan'):
                st.warning("⚠️ Salve ou cancele a edição/criação em aberto para prosseguir.")
            else:
                st.button(
                    "📥 Consolidar e Construir Artefatos",
                    use_container_width=True,
                    type="primary",
                    on_click=self.trigger_action,
                    args=("build_artifacts",),
                    disabled=self.state.get('is_processing'),
                )

        if self.state.get('current_action') == 'build_artifacts':
            self.state.set('csv_cases', AzureCsvFormatter.cases_only(self.state.get('test_cases'), self.state.get('project_name')))
            self.state.set('csv_plans', AzureCsvFormatter.plans_suites_cases(
                self.state.get('test_plans'), self.state.get('test_cases'), self.state.get('project_name')
            ))
            self._set_step(6, allow_during_processing=True)
            self.clear_action()
            st.rerun()

    def step_6(self):
        st.subheader("Passo 6 – Artefatos Finalizados")
        st.success("🎉 Build concluída sem apontamentos.")

        project = self.state.get('project_name')
        safe_name = project.replace(' ', '_')

        st.markdown("### 📄 Exportações CSV – Azure DevOps")
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Test Cases - Azure DevOps**")
            st.caption("CSV no layout usado para importação manual de Test Cases no Azure DevOps.")
            csv_cases = ('\ufeff' + self.state.get('csv_cases')).encode('utf-8')
            st.download_button(
                "⬇️ Baixar Test Cases (CSV)",
                data=csv_cases,
                file_name=f"QA_Cases_{safe_name}.csv",
                mime="text/csv",
                use_container_width=True,
                type="primary",
            )
        with col2:
            st.markdown("**Planos + Suites + Cases**")
            st.caption("CSV com Plan/Suite/Case para apoiar a organização manual no Azure DevOps.")
            csv_plans = ('\ufeff' + self.state.get('csv_plans')).encode('utf-8')
            st.download_button(
                "⬇️ Baixar Test Plans (CSV)",
                data=csv_plans,
                file_name=f"QA_Plans_{safe_name}.csv",
                mime="text/csv",
                use_container_width=True,
                type="primary",
            )

        st.divider()
        st.markdown("### 📑 Documentação Técnica – PDF Report")
        st.caption("Relatório completo: Matriz de Cobertura, Planos de Teste e Casos de Teste.")
        with st.spinner("Gerando binários do PDF… Aguarde um momento..."):
            pdf_bytes = PdfReportGenerator.generate(
                project,
                self.state.get('matriz'),
                self.state.get('test_plans'),
                self.state.get('test_cases'),
            )
        st.download_button(
            "⬇️ Baixar Documentação Técnica (PDF)",
            data=pdf_bytes,
            file_name=f"QA_Report_{safe_name}.pdf",
            mime="application/pdf",
            use_container_width=True,
            type="primary",
        )

        st.divider()
        if st.button("🔄 Nova Análise", use_container_width=True, type="primary", disabled=self.state.get('is_processing')):
            self.state.clear()
            st.rerun()

    def run(self):
        self._inject_ui_styles()
        self._header()
        self._progress()
        self._processing_banner()
        step = self.state.get('step')
        if step == 1:
            self.step_1()
        elif step == 2:
            self.step_2()
        elif step == 3:
            self.step_3()
        elif step == 4:
            self.step_4()
        elif step == 5:
            self.step_5()
        elif step == 6:
            self.step_6()
