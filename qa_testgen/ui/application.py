import os
import base64
import difflib
import hashlib
import json
import re
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image

from qa_testgen.config import AppConfiguration, LOGO_PATH, SIMBOLO_PATH
from qa_testgen.infrastructure.csv_formatter import AzureCsvFormatter
from qa_testgen.infrastructure.document_processor import DocumentProcessor
from qa_testgen.infrastructure.pdf_report import PdfReportGenerator
from qa_testgen.infrastructure.webhook_client import WebhookClient
from qa_testgen.infrastructure.azure_devops_client import AzureDevOpsClient, AzureDevOpsError
from qa_testgen.application.session import SessionState
from qa_testgen.domain.validators.matrix_validator import MatrixValidator
from qa_testgen.domain.validators.plan_validator import TestPlanValidator
from qa_testgen.domain.validators.testcase_validator import TestCaseValidator
from qa_testgen.ui.dialogs import (
    clear_widget_states,
    confirm_azure_devops_full_push_modal,
    confirm_static_suites_push_modal,
    confirm_reconciliation_push_modal,
    confirm_deletion_modal,
    confirm_discard_new_modal,
    confirm_interrupt_modal,
    confirm_matriz_deletion_modal,
    confirm_navigate_away_modal,
    confirm_suite_deletion_modal,
    confirm_step_deletion_modal,
    confirm_new_analysis_modal,
    confirm_new_report_modal,
    confirm_leave_report_modal,
)
from qa_testgen.ui.auth import (
    require_login, render_logout_control, is_approver, has_permission,
    render_admin_panel, log_action, SESSION_USER_KEY,
)

# Liga/desliga a seção de integração direta com o Azure DevOps no Passo 6.
# Coloque True quando quiser reativar a integração.
AZURE_DEVOPS_INTEGRATION_ENABLED = True


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
        self.ado_client = AzureDevOpsClient(
            self.config.azure_devops_org,
            self.config.azure_devops_project,
            self.config.azure_devops_pat,
        )

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

    def _env_sigla(self) -> str:
        ambiente = self.state.get('ambiente_testes', '')
        return "HML" if ambiente == "Homologação" else ("PROD" if ambiente == "Produção" else "")

    def _format_case_label(self, idx_1based: int, titulo: str) -> str:
        """
        Rótulo padrão de um Caso de Teste: "CT01 HML - <título>" (ou PROD).
        Usado consistentemente na tela, no CSV, no PDF e nos títulos
        criados de verdade no Azure DevOps — computado sempre na hora (não
        gravado no título bruto), pra não ficar desatualizado se os casos
        forem reordenados/editados depois.
        """
        sigla = self._env_sigla()
        prefix = f"CT{idx_1based:02d}" + (f" {sigla}" if sigla else "")
        return f"{prefix} - {titulo}"

    def _next_matriz_id(self, matriz: list) -> str:
        max_n = 0
        for row in matriz:
            digits = ''.join(c for c in str(row.get('id', '')) if c.isdigit())
            if digits:
                try:
                    max_n = max(max_n, int(digits))
                except ValueError:
                    pass
        base = f"MC-{max_n + 1:03d}"
        sigla = self._env_sigla()
        return f"{base} {sigla}" if sigla else base

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

    def _log(self, action_name: str, location: str, details: str = ""):
        username = st.session_state.get(SESSION_USER_KEY, "")
        log_action(self.config, username, action_name, location, details)

    @staticmethod
    def _dedupe_case_assignments(links: dict, ordered_wids: list) -> dict:
        """
        Garante que cada Caso de Teste apareça em, no máximo, UM Work Item.
        Resolve conflito por ordem: o primeiro Work Item (na ordem de
        ordered_wids) que já tinha aquele Caso mantém ele; qualquer Work
        Item posterior perde esse Caso automaticamente da própria seleção.
        """
        claimed = set()
        result = {}
        for wid in ordered_wids:
            titulos = links.get(wid, [])
            livres = [t for t in titulos if t not in claimed]
            result[wid] = livres
            claimed.update(livres)
        return result

    def _navigate_or_confirm(self, pending_state_updates: dict):
        """
        Aplica as mudanças de estado em `pending_state_updates` (ex.: trocar
        de página/sidebar) — a não ser que estejamos na página de Relatório
        de Testes com um PDF já gerado, caso em que primeiro pede
        confirmação (evita perder o relatório sem querer ao clicar em
        qualquer outro botão/menu).
        """
        if self.state.get('show_execution_report_page') and self.state.get('report_pdf_bytes'):
            self.state.set('_pending_navigation_after_report', pending_state_updates)
            self.state.set('show_leave_report_modal', True)
            st.rerun()
        else:
            for key, value in pending_state_updates.items():
                self.state.set(key, value)
            st.rerun()

    def _get_permission_cached(self, permission: str) -> bool:
        """
        Checa uma permissão granular (ex.: 'azure_devops', 'execution_report')
        via n8n, mas só uma vez por sessão — resultado fica em cache. Sem
        isso, cada renderização da barra de progresso faria uma chamada de
        rede, reintroduzindo o mesmo tipo de lentidão que já corrigimos
        antes (algo rodando sem necessidade em toda interação).
        Se o admin conceder/revogar acesso enquanto alguém já está logado,
        essa pessoa só vê a mudança no próximo login — troca aceitável pelo
        ganho de performance.
        """
        cache_key = f'_perm_cache_{permission}'
        if self.state.get(cache_key) is None:
            username = st.session_state.get(SESSION_USER_KEY, "")
            self.state.set(cache_key, has_permission(self.config, username, permission))
        return bool(self.state.get(cache_key))

    def _block_f5_reload(self):
        """
        Tenta impedir F5/Ctrl+R de recarregar a página, pra evitar perda de
        dados não salvos (ex.: Relatório de Testes gerado).

        AVISO HONESTO: isso NÃO é garantido — navegadores modernos
        deliberadamente restringem páginas de bloquear atalhos do próprio
        navegador (F5/Ctrl+R são "chrome" do navegador, não da página), e
        essa proteção pode simplesmente não funcionar dependendo do
        navegador/versão. Os botões "Novo Relatório" e a confirmação antes
        de sair da tela de Relatório são a proteção que realmente sempre
        funciona — isso aqui é só uma tentativa extra.
        """
        components.html(
            """
            <script>
                (function () {
                    try {
                        var doc = window.parent.document;
                        if (doc.__qaF5BlockAttached) { return; }
                        doc.__qaF5BlockAttached = true;
                        doc.addEventListener('keydown', function (e) {
                            var isF5 = e.key === 'F5' || e.keyCode === 116;
                            var isCtrlR = (e.ctrlKey || e.metaKey) && (e.key === 'r' || e.key === 'R');
                            if (isF5 || isCtrlR) {
                                e.preventDefault();
                                e.stopPropagation();
                            }
                        }, true);
                    } catch (err) {
                        // Se o navegador não permitir acessar window.parent
                        // (restrição de segurança), não tem o que fazer.
                    }
                })();
            </script>
            """,
            height=0,
        )

    def _force_sidebar_collapsed(self):
        """
        Recolhe a sidebar automaticamente quando o PASSO muda (navegação
        entre telas), não em toda interação — isso evita injetar um iframe
        com JS (componente caro: cria/destrói um documento HTML próprio) em
        toda troca de dropdown, clique de botão etc., que era a causa real
        da lentidão sentida nas transições do app inteiro.

        `initial_sidebar_state="collapsed"` cuida do primeiro carregamento;
        isso aqui cobre só a recolhida ao trocar de passo, que é quando a
        sidebar realisticamente ficaria aberta sem querer.

        Usa retry porque no momento em que este script roda, a sidebar pode
        ainda não estar montada no DOM (condição de corrida do rerun).
        """
        components.html(
            """
            <script>
                (function () {
                    function tryCollapse(attemptsLeft) {
                        const doc = window.parent.document;
                        const sidebar = doc.querySelector('[data-testid="stSidebar"]');

                        if (sidebar) {
                            const expanded = sidebar.getAttribute('aria-expanded') === 'true';
                            if (!expanded) {
                                return; // já está colapsada, nada a fazer
                            }
                            const collapseBtn = doc.querySelector(
                                '[data-testid="stSidebarCollapseButton"] button'
                            );
                            if (collapseBtn) {
                                collapseBtn.click();
                                return;
                            }
                        }

                        if (attemptsLeft > 0) {
                            setTimeout(function () { tryCollapse(attemptsLeft - 1); }, 150);
                        }
                    }
                    tryCollapse(25); // tenta por ~3.7s antes de desistir
                })();
            </script>
            """,
            height=0,
        )

    def _inject_ui_styles(self):
        st.markdown(
            """
            <style>
                /* Alinha pela base qualquer linha de colunas que contenha um
                   botão "azul" (Azure) — corrige o desalinhamento entre um
                   selectbox/multiselect (que tem rótulo acima) e um botão ao
                   lado (que não tem), sem afetar outros pares de colunas do
                   app que não usam esse tipo de botão. */
                div[data-testid="stHorizontalBlock"]:has(div[class*="st-key-azure_blue_btn_"]) {
                    align-items: flex-end;
                }

                /* Botões "azuis" (padrão Azure) — usados nos fluxos de
                   integração com o Azure DevOps. Qualquer botão dentro de um
                   st.container(key="azure_blue_btn_...") recebe essa cor,
                   independente de type="primary"/"secondary". */
                div[class*="st-key-azure_blue_btn_"] button {
                    background-color: #0078D4 !important;
                    border-color: #0078D4 !important;
                    color: #FFFFFF !important;
                }
                div[class*="st-key-azure_blue_btn_"] button:hover {
                    background-color: #106EBE !important;
                    border-color: #106EBE !important;
                    color: #FFFFFF !important;
                }
                div[class*="st-key-azure_blue_btn_"] button:disabled {
                    background-color: #99C7EA !important;
                    border-color: #99C7EA !important;
                    color: #FFFFFF !important;
                }

                .stMarkdown table, .stMarkdown table th, .stMarkdown table td,
                div[role="main"] table, div[role="main"] table th, div[role="main"] table td {
                    text-align: left !important;
                    vertical-align: top !important;
                }
                table[style] td, table[style] th {
                    text-align: left !important;
                }
                
                /* Container master do botão */
                div[class*="st-key-active_matriz_row"] button,
                div[class*="st-key-active_test_case_row"] button,
                div[class*="st-key-active_test_plan_row"] button {
                    height: auto !important;
                    padding-top: 0.75rem !important;
                    padding-bottom: 0.75rem !important;
                }
                
                /* Container interno flexível do Streamlit */
                div[class*="st-key-active_matriz_row"] button > div,
                div[class*="st-key-active_test_case_row"] button > div,
                div[class*="st-key-active_test_plan_row"] button > div {
                    display: flex !important;
                    width: 100% !important;
                    justify-content: flex-start !important;
                    text-align: left !important;
                }
                
                /* Renderização da fonte */
                div[class*="st-key-active_matriz_row"] button p,
                div[class*="st-key-active_test_case_row"] button p,
                div[class*="st-key-active_test_plan_row"] button p {
                    width: 100% !important;
                    text-align: left !important;
                    white-space: normal !important;
                    line-height: 1.5 !important;
                    margin: 0 !important;
                }
            </style>
            """,
            unsafe_allow_html=True,
        )

    @staticmethod
    @st.cache_data(show_spinner=False)
    def _load_logo_b64(path_str: str) -> str:
        """
        Lê e codifica o logo em base64 uma única vez (cache do Streamlit,
        compartilhado entre sessões) — antes isso rodava do zero em toda
        renderização (2x por vez: sidebar + cabeçalho principal), lendo o
        arquivo do disco sem necessidade.
        """
        if not Path(path_str).exists():
            return ""
        try:
            with open(path_str, 'rb') as f:
                return base64.b64encode(f.read()).decode('utf-8')
        except Exception:
            return ""

    def _header(self):
        with st.sidebar:
            sidebar_logo_b64 = self._load_logo_b64(str(LOGO_PATH))
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
                if st.button("⏹️ Interromper Processamento", use_container_width=True, type="primary", key="btn_interrupt_sidebar"):
                    self.state.set('show_interrupt_modal', True)
                    st.rerun()
            
            if st.button("🔄 Nova Análise", use_container_width=True, type="primary", key="btn_new_sidebar"):
                self.state.set('show_new_analysis_modal', True)
                st.rerun()

            if st.button("🏠 Início", use_container_width=True, disabled=self.state.get('is_processing'), key="btn_home_sidebar"):
                # Diferente de "Nova Análise": só navega pro Passo 1, sem
                # apagar nada — tudo que já foi preenchido continua lá, e
                # dá pra voltar a qualquer passo já feito normalmente.
                if self.state.get('show_execution_report_page') and self.state.get('report_pdf_bytes'):
                    self.state.set('_pending_navigation_after_report', {'show_execution_report_page': False, 'step': 1})
                    self.state.set('show_leave_report_modal', True)
                    st.rerun()
                elif self._has_editing_in_progress():
                    confirm_navigate_away_modal(1)
                else:
                    clear_widget_states()
                    self._set_step(1)
                    st.rerun()

            st.divider()
            if st.button("ℹ️ Sobre o app", use_container_width=True, key="btn_about_sidebar", disabled=self.state.get('is_processing')):
                self._navigate_or_confirm({
                    'show_about_page': True, 'show_admin_page': False,
                    'show_execution_report_page': False, 'show_wi_generation_page': False,
                    'show_wiql_generation_page': False,
                })

            current_username = st.session_state.get(SESSION_USER_KEY, "")
            if self._get_permission_cached("azure_devops"):
                if st.button("🎯 Gerar a partir de Work Items", use_container_width=True, key="btn_wigen_sidebar", disabled=self.state.get('is_processing')):
                    self._navigate_or_confirm({
                        'show_wi_generation_page': True, 'show_about_page': False,
                        'show_admin_page': False, 'show_execution_report_page': False,
                        'show_wiql_generation_page': False,
                    })
                if st.button("🔎 Criar Query com IA", use_container_width=True, key="btn_wiql_sidebar", disabled=self.state.get('is_processing')):
                    self._navigate_or_confirm({
                        'show_wiql_generation_page': True, 'show_about_page': False,
                        'show_admin_page': False, 'show_execution_report_page': False,
                        'show_wi_generation_page': False,
                    })
            if self._get_permission_cached("execution_report"):
                if st.button("📊 Relatório de Testes", use_container_width=True, key="btn_report_sidebar", disabled=self.state.get('is_processing')):
                    # Já estar na própria página de Relatório não conta como
                    # "sair" dela — não precisa do guarda aqui.
                    self.state.set('show_execution_report_page', True)
                    self.state.set('show_about_page', False)
                    self.state.set('show_admin_page', False)
                    self.state.set('show_wi_generation_page', False)
                    self.state.set('show_wiql_generation_page', False)
                    st.rerun()
            if is_approver(self.config, current_username):
                # "Administração" agora fica visível pra qualquer aprovador,
                # não só pro dono — a página em si mostra Solicitações
                # Pendentes pra todo mundo, mas só o dono vê o cadastro de
                # aprovadores/permissões (isso é decidido dentro da própria
                # página, não aqui).
                if st.button("🛡️ Administração", use_container_width=True, key="btn_admin_sidebar", disabled=self.state.get('is_processing')):
                    self._navigate_or_confirm({
                        'show_admin_page': True, 'show_about_page': False,
                        'show_execution_report_page': False, 'show_wi_generation_page': False,
                        'show_wiql_generation_page': False,
                    })

        img_b64 = self._load_logo_b64(str(LOGO_PATH))

        st.markdown(
            f"""
            <div style="display:flex;align-items:stretch;margin-bottom:1.5rem;gap:1.5rem;">
                <div style="flex:0 0 200px;display:flex;align-items:center;justify-content:center;">
                    <img src="data:image/png;base64,{img_b64}"
                         style="max-width:100%;max-height:80px;object-fit:contain;">
                </div>
                <div style="flex:1;background:linear-gradient(135deg,#F15A24,#c94a1a);padding:1rem 1.5rem;border-radius:6px;display:flex;flex-direction:column;justify-content:center;min-height:80px;">
                    <h1 style="color:white;margin:0;font-size:1.6rem;padding:0;">🧪 QA Automation – Azure DevOps</h1>
                    <p style="color:white;margin:0.2rem 0 0 0;font-size:1.05rem;padding:0;">Automação QA com IA - Integração ao Azure DevOps</p>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    def _has_editing_in_progress(self) -> bool:
        state = self.state
        if state.get('adding_matriz_row') or state.get('adding_test_case') or state.get('adding_test_plan'):
            return True
        matriz = state.get('matriz') or []
        for i in range(len(matriz)):
            if state.get(f'edit_m_{i}', False):
                return True
        test_cases = state.get('test_cases') or []
        for i in range(len(test_cases)):
            if state.get(f'edit_tc_{i}', False):
                return True
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
            'fetch_wi': 'Buscando Work Items do Board no Azure DevOps',
            'fetch_wi_step1': 'Buscando Work Items do Board no Azure DevOps',
            'fetch_report_plans': 'Buscando Test Plans do projeto',
            'fetch_existing_plans': 'Buscando Test Plans existentes na Area Path',
            'fetch_recon_plans': 'Buscando Test Plans do projeto',
            'fetch_recon_cases': 'Buscando Casos de Teste do Test Plan anterior',
            'fetch_recon_wi': 'Buscando Work Items do Board',
            'suggest_recon_links': 'Consultando a IA para sugerir vínculos',
            'push_reconciliation': 'Vinculando Casos aos Work Items',
            'generate_execution_report': 'Buscando resultados de execução e gerando o Relatório de Testes',
            'suggest_report_narrative': 'Analisando resultados e gerando sugestão de texto com IA',
            'fetch_orgs': 'Carregando organizações acessíveis a este PAT',
            'fetch_projects': 'Buscando projetos da organização selecionada',
            'fetch_area_paths': 'Buscando Area Paths do projeto selecionado',
            'fetch_area_paths_auto': 'Buscando Area Paths do projeto selecionado',
            'suggest_ado_links': 'Consultando a IA (n8n) para sugerir vínculos',
            'push_azure_devops_full': 'Integrando com o Azure DevOps',
            'check_ado_plan_name': 'Verificando se já existe um Test Plan com esse nome',
        }
        action = labels.get(self.state.get('current_action'), 'Processando informações')
        st.markdown(
            """
            <style>
                /* Garante que o modal nativo do Streamlit (st.dialog) sempre
                   fique acima do overlay de "Processamento em andamento". */
                [data-testid="stDialog"],
                div[role="dialog"],
                [data-testid="stModal"] {
                    z-index: 2147483647 !important;
                }

                /* Barras de progresso (st.progress) — sem isso, elas
                   renderizam ATRÁS do véu escuro durante o processamento
                   (Passo 7, criação de Casos/Suítes/vínculos no Azure
                   DevOps), ficando praticamente invisíveis. */
                [data-testid="stProgress"] {
                    position: relative;
                    z-index: 1001;
                }

                /* pointer-events: auto -> ISSO bloqueia clique de verdade em
                   tudo que estiver embaixo, enquanto durar o processamento.
                   Antes estava "none", ou seja, só era visual — qualquer
                   botão embaixo continuava clicável normalmente.
                   touch-action: pan-y -> libera especificamente o GESTO de
                   rolagem vertical (roda do mouse, trackpad, arrastar no
                   touch) através do véu, sem abrir mão do bloqueio de
                   clique — as duas coisas são independentes no CSS. */
                .qa-processing-shade {
                    position: fixed;
                    inset: 0;
                    background: rgba(20, 24, 31, 0.35);
                    z-index: 999;
                    pointer-events: auto;
                    touch-action: pan-y;
                    overscroll-behavior: contain;
                }

                /* O card fica acima do shade e É a única coisa clicável —
                   por isso ele mesmo tem o botão real de Cancelar dentro. */
                div[class*="st-key-qa_processing_card"] {
                    position: fixed !important;
                    right: 1.5rem;
                    bottom: 1.5rem;
                    z-index: 1000;
                    background: #ffffff;
                    border: 1px solid #f15a24;
                    border-left: 5px solid #f15a24;
                    border-radius: 6px;
                    box-shadow: 0 12px 28px rgba(0,0,0,.25);
                    padding: .9rem 1rem 1rem 1rem;
                    max-width: 380px;
                    pointer-events: auto;
                }
                .qa-processing-title {font-weight: 700;color: #3A3A3A;margin-bottom: .2rem;}
                .qa-processing-text {color: #5b5b5b;font-size: .9rem; margin-bottom: .7rem;}
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
            """,
            unsafe_allow_html=True,
        )
        with st.container(key="qa_processing_card"):
            st.markdown(
                f'<div class="qa-processing-title"><span class="qa-processing-dot"></span>Processamento em andamento</div>'
                f'<div class="qa-processing-text">{action}.<br>Esta é a única ação disponível até finalizar.</div>',
                unsafe_allow_html=True,
            )
            if st.button("⏹️ Cancelar Processamento", key="qa_processing_cancel_btn", use_container_width=True, type="primary"):
                self.state.set('show_interrupt_modal', True)
                st.rerun()

    def _progress(self):
        steps = [
            (1, "📄 Upload"), (2, "💬 Dúvidas"), (3, "📊 Matriz"), (4, "📋 Casos"),
            (5, "📁 Planos"), (6, "⬇️ Download"),
        ]
        if self._get_permission_cached("azure_devops"):
            steps.append((7, "🔗 Azure DevOps"))

        current_step = self.state.get('step')
        max_step = self.state.get('max_step', current_step)
        completed_steps = set(self.state.get('completed_steps') or [])
        is_processing = self.state.get('is_processing')

        with st.container():
            cols = st.columns(len(steps))
            for col, (i, label) in zip(cols, steps):
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

    @staticmethod
    def _suggest_project_name_from_filename(filename: str) -> str:
        """Deriva um nome de Test Plan legível a partir do nome do arquivo (ex.: 'visao_integracao_linkedin.pdf' -> 'Visao Integracao Linkedin')."""
        base = filename.rsplit('.', 1)[0]
        base = base.replace('_', ' ').replace('-', ' ')
        base = ' '.join(base.split())
        return base.title()

    def step_1(self):
        st.subheader("Passo 1 – Setup e Documentação")
        if self.state.get('processing_interrupted'):
            st.info("⚠️ Processamento interrompido. Você pode continuar editando esta etapa.")

        col1, col2 = st.columns(2)
        with col1:
            uploaded_new = st.file_uploader(
                "Documento(s) de Requisitos (Máx 20MB cada) *",
                type=["pdf", "txt", "docx"],
                key='step1_uploaded_file',
                disabled=self.state.get('is_processing'),
                accept_multiple_files=True,
                help="Você pode anexar mais de um documento — o texto de todos será combinado numa única análise. Arraste e solte os arquivos aqui, ou clique para escolher.",
            )
            if uploaded_new:
                self.state.set('uploaded_files', uploaded_new)
                # Sugere o Nome do Test Plan a partir do primeiro documento —
                # só quando o campo ainda está vazio, pra nunca sobrescrever
                # algo que a pessoa já tenha digitado manualmente. Isso
                # PRECISA rodar antes do text_input ser desenhado (coluna
                # seguinte) — o Streamlit não deixa mudar o valor de um
                # widget depois dele já ter sido instanciado na mesma execução.
                if not st.session_state.get('project_name_input', '').strip():
                    suggested = self._suggest_project_name_from_filename(uploaded_new[0].name)
                    st.session_state['project_name_input'] = suggested
                    self.state.set('project_name', suggested)
            uploaded = self.state.get('uploaded_files') or []
        with col2:
            if 'project_name_input' not in st.session_state:
                st.session_state['project_name_input'] = self.state.get('project_name', '')
            project = st.text_input(
                "Nome do Test Plan *",
                key='project_name_input',
                placeholder="Ex: Passaporte Refuturiza",
                disabled=self.state.get('is_processing'),
                help="Preenchido automaticamente a partir do nome do primeiro documento enviado — altere livremente se quiser outro nome.",
            )
            if project:
                self.state.set('project_name', project)

        MAX_FILE_MB = 20
        MAX_TOTAL_MB = 20  # limite total combinado (ex.: client_max_body_size do servidor)

        oversized = [f.name for f in uploaded if f.size > MAX_FILE_MB * 1024 * 1024]
        if oversized:
            st.error(f"❌ Arquivo(s) excedem o limite de {MAX_FILE_MB}MB cada: {', '.join(oversized)}")
            return

        total_mb = sum(f.size for f in uploaded) / (1024 * 1024)
        if total_mb > MAX_TOTAL_MB:
            st.error(
                f"❌ O total dos arquivos anexados ({total_mb:.1f}MB) excede o limite combinado "
                f"de {MAX_TOTAL_MB}MB. Remova algum documento ou divida em análises separadas."
            )
            return

        if uploaded:
            with st.expander(f"📎 {len(uploaded)} documento(s) anexado(s)", expanded=False):
                for f in uploaded:
                    st.caption(f"• {f.name} ({f.size / 1024:.0f} KB)")

        col_amb, col_tipo = st.columns(2)
        with col_amb:
            ambiente = st.radio(
                "Ambiente dos Testes *",
                options=["Homologação", "Produção"],
                index=None,  # sem pré-seleção — obriga a pessoa a escolher conscientemente
                key="ambiente_testes_input",
                disabled=self.state.get('is_processing'),
                horizontal=True,
                help="Define a etiqueta (HML/PROD) usada no nome de cada Caso de Teste, na Matriz e na documentação.",
            )
        if ambiente:
            self.state.set('ambiente_testes', ambiente)

        with col_tipo:
            tipo_documento = st.radio(
                "Tipo de Documento *",
                options=["Visão", "Requisitos Funcionais", "Especificações Funcionais", "Outros"],
                index=None,  # sem pré-seleção — obriga a pessoa a escolher conscientemente
                key="tipo_documento_input",
                disabled=self.state.get('is_processing'),
                horizontal=True,
                help=(
                    "Calibra o nível de detalhe que a IA assume ao gerar Matriz/Casos (Visão = mais "
                    "exploratório, Especificações = mais granular) e sugere o modo de envio pro Azure "
                    "DevOps no Passo 7 (com ou sem vínculo a Work Items)."
                ),
            )
        if tipo_documento:
            self.state.set('tipo_documento', tipo_documento)

        st.divider()
        vincular_wi = st.checkbox(
            "🔗 Vincular cada documento a um Work Item específico do Azure DevOps",
            key="step1_vincular_wi_checkbox",
            disabled=self.state.get('is_processing'),
            help=(
                "Em vez de deixar a IA sugerir os vínculos depois (Passo 7), você já declara aqui "
                "a qual Work Item cada documento se refere. A Matriz e os Casos gerados a partir "
                "desse documento já saem marcados com esse Work Item, entrando pré-vinculados no Passo 7."
            ),
        )
        doc_work_item_map = {}
        if vincular_wi and uploaded:
            conn = self._setup_azure_devops_connection(show_area_path_picker=True)
            if conn is None:
                return  # conexão com o Azure DevOps ainda incompleta — não mostra o resto do Passo 1 até terminar (ou desmarcar a opção)
            ado_client, ado_org, ado_project, area_path = conn

            with st.container(key="azure_blue_btn_fetch_wi_step1"):
                st.button(
                    "🔄 Buscar Work Items do Board",
                    disabled=self.state.get('is_processing'),
                    key="btn_fetch_wi_step1",
                    on_click=self.trigger_action,
                    args=("fetch_wi_step1",),
                )
            if self.state.get('current_action') == 'fetch_wi_step1' and not self.state.get('show_interrupt_modal'):
                try:
                    with st.spinner(f"Buscando Work Items em '{area_path}'..."):
                        items = ado_client.fetch_work_items_by_area_path(area_path)
                    self.state.set('step1_board_items', items)
                    if not items:
                        st.warning("Nenhum Work Item encontrado nesse Area Path.")
                except Exception as error:
                    st.error(f"❌ Não foi possível buscar Work Items: {error}")
                self.clear_action()
                st.rerun()

            board_items = self.state.get('step1_board_items') or []
            if board_items:
                st.caption("Escolha um ou mais Work Items de cada documento (opcional — deixe vazio se preferir):")
                wi_labels = {f"{i['id']} - {i['title']} ({i['type']}, {i['state']})": i for i in board_items}
                doc_cols = st.columns(2)
                for idx, f in enumerate(uploaded):
                    with doc_cols[idx % 2]:
                        chosen_labels = st.multiselect(
                            f"📄 {f.name}",
                            options=list(wi_labels.keys()),
                            key=f"step1_doc_wi_{f.name}",
                            disabled=self.state.get('is_processing'),
                        )
                    items = [wi_labels[label] for label in chosen_labels]
                    if items:
                        doc_work_item_map[f.name] = [{"id": it["id"], "title": it["title"]} for it in items]
                self.state.set('step1_doc_work_item_map', doc_work_item_map)
                st.caption(
                    f"💡 Lembrete: no Passo 7, use uma Area Path que inclua estes Work Items "
                    f"(buscados agora em '{area_path}') pra que o pré-vínculo funcione."
                )
            else:
                st.caption("Busque os Work Items do board acima pra poder vinculá-los aos documentos.")
        else:
            self.state.set('step1_doc_work_item_map', {})

        st.divider()


        if not project or not uploaded:
            st.info("Preencha o nome do projeto e faça o upload de ao menos um documento para continuar.")
            return

        if not ambiente or not tipo_documento:
            st.info("Selecione o Ambiente dos Testes e o Tipo de Documento para continuar.")
            return

        st.button(
            "🔍 Executar Análise de Cobertura (IA)",
            use_container_width=True,
            type="primary",
            on_click=self.trigger_action,
            args=("analyze_docs",),
            disabled=self.state.get('is_processing'),
        )

        if self.state.get('current_action') == 'analyze_docs' and not self.state.get('show_interrupt_modal'):
            with st.spinner("Extraindo texto dos documentos..."):
                text = DocumentProcessor.extract_plain_text_multi(uploaded, self.state.get('step1_doc_work_item_map'))
            if not text:
                st.error("Não foi possível extrair texto.")
                self.clear_action()
            else:
                # Extrai imagens relevantes do corpo dos documentos (ignora
                # cabeçalho/rodapé, ícones pequenos e logos repetidos) e
                # interpreta cada uma via IA, inserindo a descrição de volta
                # no texto, na posição em que a imagem apareceu — assim a
                # IA de análise/geração "vê" o conteúdo visual também.
                img_result = DocumentProcessor.extract_images_with_context(uploaded)
                images = img_result["images"]
                for warn in img_result["warnings"]:
                    st.caption(f"ℹ️ {warn}")

                if images:
                    text += "\n\n===== DESCRIÇÕES DE IMAGENS DO DOCUMENTO (geradas por IA) =====\n"
                    progress = st.progress(0, text=f"Interpretando imagens do documento... (0/{len(images)})")
                    for idx, img in enumerate(images, start=1):
                        try:
                            descricao = self.client.interpret_image(
                                img["bytes"], img["mime"], img["context"], project,
                                source_file=img["source_file"], location=img["location"],
                            )
                            text += (
                                f"\n[IMAGEM — {img['source_file']}, {img['location']}]: {descricao}\n"
                            )
                        except Exception as error:
                            st.caption(
                                f"⚠️ Não foi possível interpretar uma imagem de {img['source_file']} "
                                f"({img['location']}), pulada: {error}"
                            )
                        progress.progress(idx / len(images), text=f"Interpretando imagens do documento... ({idx}/{len(images)})")

                self._run_analysis(text, project)

    def _run_analysis(self, text: str, project: str):
        """
        Roda a análise de IA (mesma do Passo 1) e navega pro Passo 2 se der
        certo. Reutilizado tanto pelo Passo 1 (documento enviado) quanto
        pela geração a partir de Work Items do Azure DevOps.
        """
        with st.spinner("Aguarde enquanto a análise é processada… Isso pode levar alguns minutos..."):
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

        if self.state.get('current_action') == 'generate_matrix' and not self.state.get('show_interrupt_modal'):
            with st.spinner("Estruturando Matriz de Rastreabilidade… Aguarde um momento..."):
                try:
                    resp = self.client.trigger_matrix(
                        self.state.get('doc_text'),
                        self.state.get('step_2_answers', answers),
                        self.state.get('project_name'),
                        self.state.get('tipo_documento', ''),
                    )
                    matriz = resp.get('matriz') or []
                    if not matriz:
                        st.error("❌ Matriz vazia.")
                        self.clear_action()
                    else:
                        sigla = self._env_sigla()
                        if sigla:
                            for row in matriz:
                                base_id = str(row.get('id', '')).strip()
                                if base_id and not base_id.endswith(f" {sigla}"):
                                    row['id'] = f"{base_id} {sigla}"
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
            row_uid = row.get('id') or f"idx{i}"
            with st.container(key=f"matriz_row_{row_uid}"):
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

        if self.state.get('current_action') == 'generate_cases' and not self.state.get('show_interrupt_modal'):
            with st.spinner("Gerando Casos de Teste… Aguarde um momento..."):
                try:
                    resp = self.client.trigger_generation(
                        self.state.get('doc_text'),
                        self.state.get('matriz'),
                        self.state.get('user_answers'),
                        self.state.get('project_name'),
                        self.state.get('tipo_documento', ''),
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
            label = self._format_case_label(idx + 1, tc.get('titulo', ''))
            with st.container(key=f"tc_row_{idx}"):
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
                                            **test_cases[idx],  # preserva campos extras (work_item_relacionado, requisitos_relacionados, id) que não são editados aqui
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
                        wi_relacionado = str(tc.get('work_item_relacionado') or '').strip()
                        if wi_relacionado:
                            st.caption(f"🔗 Work Item relacionado (vindo do Passo 1): #{wi_relacionado}")
                        elif self.state.get('step1_doc_work_item_map'):
                            # Só mostra esse aviso se a pessoa realmente usou a
                            # vinculação no Passo 1 — senão é ruído pra quem
                            # nunca pediu esse recurso.
                            st.caption("⚪ Nenhum Work Item relacionado veio da IA para este Caso.")
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

        if self.state.get('current_action') == 'generate_plans' and not self.state.get('show_interrupt_modal'):
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
            label = f"Plano {i + 1:02d} – {plan.get('nome', '')}  ·  Suites: {suite_names}"

            with st.container(key=f"plan_row_{i}"):
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

        if self.state.get('current_action') == 'build_artifacts' and not self.state.get('show_interrupt_modal'):
            ambiente = self.state.get('ambiente_testes', '')
            self.state.set('csv_cases', AzureCsvFormatter.cases_only(self.state.get('test_cases'), self.state.get('project_name'), ambiente))
            self.state.set('csv_plans', AzureCsvFormatter.plans_suites_cases(
                self.state.get('test_plans'), self.state.get('test_cases'), self.state.get('project_name'), ambiente
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
        author_name = st.text_input(
            "Nome de quem está gerando este relatório",
            value=self.state.get('author_name', ''),
            key="author_name_input",
            help="Aparece no rodapé do PDF. Se você já validou seu PAT no Passo 7 antes, isso é preenchido automaticamente.",
        )
        self.state.set('author_name', author_name)

        st.divider()
        col_pdf, col_azure = st.columns(2)
        with col_pdf:
            st.markdown("### 📑 Documentação Técnica – PDF Report")
            st.caption("Relatório completo: Matriz de Cobertura, Planos de Teste e Casos de Teste.")
            # Gerar PDF é um trabalho relativamente pesado (várias tabelas,
            # ReportLab) — antes rodava de novo em TODA interação nessa tela
            # (até digitar uma letra no campo de nome já disparava tudo de
            # novo). Agora só regenera se o conteúdo realmente mudou.
            fingerprint = hashlib.md5(
                json.dumps(
                    [project, self.state.get('matriz'), self.state.get('test_plans'),
                     self.state.get('test_cases'), author_name, self.state.get('ambiente_testes')],
                    sort_keys=True, default=str,
                ).encode('utf-8')
            ).hexdigest()
            if self.state.get('pdf_report_fingerprint') != fingerprint:
                with st.spinner("Gerando binários do PDF… Aguarde um momento..."):
                    pdf_bytes = PdfReportGenerator.generate(
                        project,
                        self.state.get('matriz'),
                        self.state.get('test_plans'),
                        self.state.get('test_cases'),
                        author_name=author_name,
                        ambiente=self.state.get('ambiente_testes', ''),
                    )
                self.state.set('pdf_report_bytes', pdf_bytes)
                self.state.set('pdf_report_fingerprint', fingerprint)
            else:
                pdf_bytes = self.state.get('pdf_report_bytes')
            st.download_button(
                "⬇️ Baixar Documentação Técnica (PDF)",
                data=pdf_bytes,
                file_name=f"QA_Report_{safe_name}.pdf",
                mime="application/pdf",
                use_container_width=True,
                type="primary",
            )
        with col_azure:
            st.markdown("### 🔗 Azure DevOps")
            st.caption("Envie os artefatos gerados direto para o seu projeto no Azure DevOps.")
            with st.container(key="azure_blue_btn_goto_step7"):
                if st.button("🔗 Ir para Integração com Azure DevOps →", use_container_width=True, disabled=self.state.get('is_processing'), key="btn_goto_step7"):
                    self._set_step(7, allow_during_processing=True)
                    st.rerun()

        st.divider()
        c1, c2 = st.columns(2)
        with c1:
            if st.button("← Voltar", use_container_width=True, disabled=self.state.get('is_processing'), key="btn_back_step6"):
                self._set_step(5)
                st.rerun()
        with c2:
            if st.button("🔄 Nova Análise", use_container_width=True, type="primary", disabled=self.state.get('is_processing'), key="btn_new_step6"):
                self.state.set('show_new_analysis_modal', True)
                st.rerun()

    def _render_step7_static_suite_mode(self, ado_client, ado_project: str, fallback_area_path: str):
        """
        Modo alternativo de envio: usa os Planos/Suítes/Casos já gerados
        pelo próprio app (Passo 5) e cria Suítes ESTÁTICAS no Azure DevOps
        — sem depender de nenhum Work Item existir. Indicado pra projetos
        no início (só Documento de Visão), onde o máximo que existe no
        board é um Épico/Backlog genérico, se tanto.
        """
        test_plans = self.state.get('test_plans') or []
        test_cases = self.state.get('test_cases') or []

        if not test_plans or not test_cases:
            st.warning("Nenhum Plano de Teste gerado ainda — volte ao Passo 5 antes de usar este modo.")
            return

        st.markdown("### 📋 Test Plan (destino no Azure DevOps)")
        st.caption(
            f"Os **{len(test_plans)} Plano(s)** e suas Suítes, gerados no Passo 5, serão criados "
            "como Suítes Estáticas no Azure DevOps, com os Casos de Teste vinculados diretamente "
            "— sem depender de nenhum Work Item."
        )
        with st.expander("Ver os Planos que serão enviados"):
            for plan in test_plans:
                suites = plan.get('suites', [])
                total_casos = sum(len(s.get('casos', [])) for s in suites)
                st.write(f"**{plan.get('nome', '')}** — {len(suites)} Suíte(s), {total_casos} Caso(s) no total")

        with st.container(key="azure_blue_btn_fetch_static_plans"):
            st.button(
                "🔍 Buscar Test Plans existentes",
                disabled=self.state.get('is_processing'),
                key="btn_fetch_static_plans",
                on_click=self.trigger_action,
                args=("fetch_static_plans",),
            )
        if self.state.get('current_action') == 'fetch_static_plans' and not self.state.get('show_interrupt_modal'):
            try:
                with st.spinner(f"Buscando Test Plans em '{fallback_area_path}'..."):
                    existing = ado_client.list_test_plans_for_area_path(fallback_area_path)
                self.state.set('ado_static_existing_plans', existing)
                self.state.set('ado_static_existing_plans_path', fallback_area_path)
            except AzureDevOpsError as error:
                st.error(f"❌ Não foi possível buscar Test Plans existentes: {error}")
            except Exception as error:
                st.error(f"❌ Erro inesperado: {error}")
            self.clear_action()
            st.rerun()

        existing_plans = (
            self.state.get('ado_static_existing_plans') or []
            if self.state.get('ado_static_existing_plans_path') == fallback_area_path
            else []
        )
        plan_mode_options = ["Criar novo Test Plan"]
        if existing_plans:
            plan_mode_options.append("Usar um Test Plan existente (adicionar Suites/Casos nele)")
        plan_mode = st.radio(
            "O que você quer fazer?",
            options=plan_mode_options,
            disabled=self.state.get('is_processing'),
            key="ado_static_plan_mode_radio",
        )

        col_plan, col_state = st.columns(2)
        existing_plan_id = None
        if plan_mode.startswith("Usar"):
            plan_labels = {f"{p['id']} - {p['name']}": p for p in existing_plans}
            with col_plan:
                chosen_label = st.selectbox(
                    "Test Plan existente", options=list(plan_labels.keys()),
                    disabled=self.state.get('is_processing'), key="ado_static_existing_plan_select",
                    help="Suítes com o mesmo nome que já existirem neste plano não são duplicadas — só recebem os Casos novos.",
                )
            existing_plan_id = plan_labels[chosen_label]["id"]
            plan_name = plan_labels[chosen_label]["name"]
        else:
            default_name = f"{self.state.get('project_name') or 'QA TestGen'} - QA TestGen"
            with col_plan:
                plan_name = st.text_input(
                    "Nome do Test Plan a ser criado", value=self.state.get('ado_static_plan_name') or default_name,
                    disabled=self.state.get('is_processing'), key="ado_static_plan_name_input",
                )
            self.state.set('ado_static_plan_name', plan_name)

        with col_state:
            initial_state_label = st.selectbox(
                "Estado inicial dos Casos de Teste criados",
                options=["Design (revisar manualmente antes de rodar)", "Ready (pronto para execução)"],
                index=1 if self.state.get('ado_tc_initial_state', 'Ready') == 'Ready' else 0,
                disabled=self.state.get('is_processing'), key="ado_static_tc_initial_state_select",
            )
        initial_state = "Ready" if initial_state_label.startswith("Ready") else "Design"

        st.divider()
        with st.container(key="azure_blue_btn_confirm_static"):
            if st.button(
                "🔗 Confirmar e Integrar com Azure DevOps",
                type="primary", use_container_width=True,
                disabled=self.state.get('is_processing') or not plan_name.strip(),
                key="btn_confirm_static_push",
            ):
                existing_case_ids = self.state.get('ado_test_case_ids') or {}
                excluded_titles = set(self.state.get('ado_excluded_case_titles') or [])
                duplicate_titles_now = set(self.state.get('ado_duplicate_case_titles') or [])
                titulos_necessarios = set()
                suites_display = []
                for plan in test_plans:
                    for suite in plan.get('suites', []):
                        casos_titulos = [t for t in suite.get('casos', []) if t not in excluded_titles]
                        titulos_necessarios.update(casos_titulos)
                        if casos_titulos:
                            suites_display.append((suite.get('nome', ''), casos_titulos))
                cases_to_create_titles = [
                    tc.get('titulo') for tc in test_cases
                    if tc.get('titulo') in titulos_necessarios
                    and tc.get('titulo') not in existing_case_ids
                    and tc.get('titulo') not in duplicate_titles_now
                ]
                self.state.set('ado_static_confirm_modal_params', (cases_to_create_titles, suites_display, plan_name.strip(), bool(existing_plan_id)))
                self.state.set('show_static_confirm_modal', True)
                st.rerun()

        if self.state.get('show_static_confirm_modal'):
            params = self.state.get('ado_static_confirm_modal_params') or ([], [], plan_name, False)
            confirm_static_suites_push_modal(*params)

        if self.state.get('current_action') == 'push_static_suites' and not self.state.get('show_interrupt_modal'):
            self._push_static_suites_azure_devops(ado_client, fallback_area_path, plan_name.strip(), initial_state, existing_plan_id)

        log = self.state.get('ado_static_push_log') or []
        if log:
            st.markdown("#### 📋 Resultado da integração")
            for line in log:
                st.write(line)

    def _push_static_suites_azure_devops(self, ado_client, area_path: str, plan_name: str,
                                           initial_state: str, existing_plan_id: int = None):
        """
        Push do modo "Sem Work Items": cria (ou reaproveita) o Test Plan,
        cria/reaproveita uma Suíte Estática por Suíte gerada no Passo 5
        (por nome, sem duplicar), cria os Casos de Teste que ainda não
        existem no Azure DevOps, e adiciona cada um na Suíte
        correspondente diretamente — sem nenhum vínculo a Work Item.
        """
        test_plans = self.state.get('test_plans') or []
        test_cases = self.state.get('test_cases') or []
        case_ids = dict(self.state.get('ado_test_case_ids') or {})
        excluded_titles = set(self.state.get('ado_excluded_case_titles') or [])
        duplicate_titles = set(self.state.get('ado_duplicate_case_titles') or [])
        titled = AzureCsvFormatter._titled(test_cases, self.state.get('ambiente_testes', ''))
        log = []

        # 1) Test Plan: cria novo ou reaproveita existente.
        if existing_plan_id:
            plan_id = existing_plan_id
            try:
                with st.spinner(f"Buscando suite raiz do Test Plan existente '{plan_name}'..."):
                    root_suite_id = ado_client.get_test_plan_root_suite(plan_id)
                log.append(f"♻️ Reaproveitando Test Plan existente: **{plan_name}** (ID {plan_id})")
                with st.spinner("Verificando Suítes já existentes neste Test Plan (evita duplicar)..."):
                    existing_suite_by_name = ado_client.get_existing_static_suite_ids_by_name(plan_id)
            except Exception as error:
                log.append(f"❌ Falha ao preparar o Test Plan existente: {error}")
                self.state.set('ado_static_push_log', log)
                self.clear_action()
                st.rerun()
                return
        else:
            try:
                plan = ado_client.create_test_plan(plan_name, f"Gerado automaticamente pelo QA TestGen (modo sem Work Items)")
                plan_id = plan["id"]
                root_suite_id = plan.get("root_suite_id")
                log.append(f"✅ Test Plan criado: **{plan_name}** (ID {plan_id})")
            except Exception as error:
                log.append(f"❌ Falha ao criar Test Plan: {error}")
                self.state.set('ado_static_push_log', log)
                self.clear_action()
                st.rerun()
                return
            existing_suite_by_name = {}

        if not root_suite_id:
            log.append("⚠️ Não recebi o ID da suite raiz do plano — não é possível continuar.")
            self.state.set('ado_static_push_log', log)
            self.clear_action()
            st.rerun()
            return

        # 2) Garante que todos os Casos de Teste necessários existem no
        # Azure DevOps (os já existentes/duplicados/excluídos são pulados —
        # mesma regra do modo com Work Items).
        titulos_necessarios = set()
        for plan in test_plans:
            for suite in plan.get('suites', []):
                titulos_necessarios.update(suite.get('casos', []))

        cases_to_create = [
            tc for tc in test_cases
            if tc.get('titulo') in titulos_necessarios
            and tc.get('titulo') not in case_ids
            and tc.get('titulo') not in duplicate_titles
            and tc.get('titulo') not in excluded_titles
        ]
        if cases_to_create:
            total = len(cases_to_create)
            progress = st.progress(0, text=f"Criando Test Cases no Azure DevOps... (0/{total})")
            done = 0

            def _create_case(tc):
                titulo = tc.get('titulo')
                titulo_prefixado = titled.get(titulo, titulo)
                result = ado_client.create_test_case(
                    titulo_prefixado, tc.get('pre_condicoes', ''), tc.get('passos', []), area_path, initial_state
                )
                return titulo, result["id"]

            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = {executor.submit(_create_case, tc): tc for tc in cases_to_create}
                for future in as_completed(futures):
                    try:
                        titulo, new_id = future.result()
                        case_ids[titulo] = new_id
                        log.append(f"✅ Caso de Teste criado: {titulo} (ID {new_id})")
                    except Exception as error:
                        log.append(f"❌ Falha ao criar um Caso de Teste: {error}")
                    done += 1
                    progress.progress(done / total, text=f"Criando Test Cases no Azure DevOps... ({done}/{total})")
            self.state.set('ado_test_case_ids', case_ids)

        # 3) Suítes Estáticas — uma por Suíte gerada, reaproveitando por
        # nome se já existir (regra do "merge"), e adicionando os Casos
        # diretamente nela (Static Suite não "puxa" sozinha como a
        # Requirement Suite — precisa do vínculo explícito).
        suite_tasks = []
        for plan in test_plans:
            for suite in plan.get('suites', []):
                nome_suite = suite.get('nome', '')
                casos_titulos = [t for t in suite.get('casos', []) if t not in excluded_titles]
                case_id_list = [case_ids[t] for t in casos_titulos if t in case_ids]
                if nome_suite and case_id_list:
                    suite_tasks.append((nome_suite, case_id_list))

        if suite_tasks:
            total_suites = len(suite_tasks)
            progress2 = st.progress(0, text=f"Criando/atualizando Suítes no Azure DevOps... (0/{total_suites})")
            for idx, (nome_suite, case_id_list) in enumerate(suite_tasks, start=1):
                nome_norm = nome_suite.strip().lower()
                try:
                    if nome_norm in existing_suite_by_name:
                        suite_id = existing_suite_by_name[nome_norm]
                        log.append(f"♻️ Suíte '{nome_suite}' já existia neste Test Plan (ID {suite_id}) — Casos novos adicionados nela.")
                    else:
                        suite_id = ado_client.create_test_suite(plan_id, root_suite_id, nome_suite)
                        log.append(f"✅ Suíte criada: '{nome_suite}' (ID {suite_id})")
                    ado_client.add_cases_to_suite(plan_id, suite_id, case_id_list)
                    log.append(f"　　→ {len(case_id_list)} Caso(s) vinculado(s) à Suíte '{nome_suite}'.")
                except AzureDevOpsError as error:
                    log.append(f"❌ Falha ao processar a Suíte '{nome_suite}': {error}")
                except Exception as error:
                    log.append(f"❌ Erro inesperado na Suíte '{nome_suite}': {error}")
                progress2.progress(idx / total_suites, text=f"Criando/atualizando Suítes no Azure DevOps... ({idx}/{total_suites})")

        log.append(f"\n🔗 Confira o Test Plan completo: {ado_client.test_plan_url(plan_id)}")
        self._log(
            "Integração com Azure DevOps (sem Work Items)", "Passo 7",
            f"Test Plan '{plan_name}' — {len(cases_to_create)} caso(s) criado(s), {len(suite_tasks)} suíte(s) processada(s)",
        )
        self.state.set('ado_static_push_log', log)
        self.clear_action()
        st.rerun()

    def _render_step7_reconciliation_mode(self, ado_client, ado_project: str, area_paths: list):
        """
        Modo alternativo de envio: liga Casos de Teste que JÁ EXISTEM num
        Test Plan anterior (feito no modo "Sem Work Items", quando só havia
        um Documento de Visão) a Work Items que foram criados depois. Não
        cria Caso de Teste novo nenhum — só cria a Requirement Suite (se
        ainda não existir) e o vínculo "Tests" entre o Caso já existente e
        o Work Item novo.
        """
        st.markdown("### 📋 1. Escolha o Test Plan anterior")
        st.caption("O Test Plan que já tem os Casos de Teste criados (do fluxo 'Sem Work Items').")

        with st.container(key="azure_blue_btn_fetch_recon_plans"):
            st.button(
                "🔍 Buscar Test Plans do Projeto",
                disabled=self.state.get('is_processing'),
                key="btn_fetch_recon_plans",
                on_click=self.trigger_action,
                args=("fetch_recon_plans",),
            )
        if self.state.get('current_action') == 'fetch_recon_plans' and not self.state.get('show_interrupt_modal'):
            try:
                with st.spinner("Buscando Test Plans..."):
                    plans = ado_client.list_test_plans()
                self.state.set('ado_recon_available_plans', plans)
            except Exception as error:
                st.error(f"❌ Não foi possível buscar Test Plans: {error}")
            self.clear_action()
            st.rerun()

        available_plans = self.state.get('ado_recon_available_plans') or []
        if not available_plans:
            return

        plan_labels = {f"{p['id']} - {p['name']}": p for p in available_plans}
        col_plan, col_btn_cases = st.columns(2)
        with col_plan:
            chosen_label = st.selectbox(
                "Test Plan anterior", options=list(plan_labels.keys()),
                disabled=self.state.get('is_processing'), key="ado_recon_plan_select",
            )
        old_plan = plan_labels[chosen_label]
        old_plan_id = old_plan["id"]

        with col_btn_cases:
            with st.container(key="azure_blue_btn_fetch_recon_cases"):
                st.button(
                    "🔍 Buscar Casos de Teste deste Test Plan",
                    disabled=self.state.get('is_processing'),
                    key="btn_fetch_recon_cases",
                    on_click=self.trigger_action,
                    args=("fetch_recon_cases",),
                    use_container_width=True,
                )
        if self.state.get('current_action') == 'fetch_recon_cases' and not self.state.get('show_interrupt_modal'):
            try:
                with st.spinner(f"Buscando Casos de Teste do Test Plan '{old_plan['name']}'..."):
                    summary = ado_client.get_test_plan_execution_summary(old_plan_id)
                seen = {}
                for point in summary.get("points", []):
                    cid = point.get("case_id")
                    if cid and cid not in seen:
                        seen[cid] = point.get("case_title", f"Caso #{cid}")
                old_cases = [{"id": cid, "titulo": titulo} for cid, titulo in seen.items()]
                self.state.set('ado_recon_old_plan_id', old_plan_id)
                self.state.set('ado_recon_old_cases', old_cases)
                if not old_cases:
                    st.warning("Nenhum Caso de Teste encontrado nesse Test Plan.")
            except Exception as error:
                st.error(f"❌ Não foi possível buscar os Casos de Teste: {error}")
            self.clear_action()
            st.rerun()

        old_cases = (
            self.state.get('ado_recon_old_cases') or []
            if self.state.get('ado_recon_old_plan_id') == old_plan_id
            else []
        )
        if not old_cases:
            st.caption("Busque os Casos de Teste do Test Plan escolhido pra continuar.")
            return
        st.caption(f"✅ {len(old_cases)} Caso(s) de Teste encontrados neste Test Plan, prontos pra vincular.")

        st.divider()
        st.markdown("### 🎯 2. Busque os Work Items novos")
        with st.container(key="azure_blue_btn_fetch_recon_wi"):
            st.button(
                "🔄 Buscar Work Items do Board",
                disabled=self.state.get('is_processing'),
                key="btn_fetch_recon_wi",
                on_click=self.trigger_action,
                args=("fetch_recon_wi",),
            )
        if self.state.get('current_action') == 'fetch_recon_wi' and not self.state.get('show_interrupt_modal'):
            try:
                paths_to_search = area_paths or [ado_project]
                with st.spinner(f"Buscando Work Items em {len(paths_to_search)} Area Path(s)..."):
                    items_by_id = {}
                    for ap in paths_to_search:
                        for item in ado_client.fetch_work_items_by_area_path(ap):
                            items_by_id[item["id"]] = item
                self.state.set('ado_recon_board_items', list(items_by_id.values()))
                self.state.set('ado_recon_wi_case_links', {})
            except Exception as error:
                st.error(f"❌ Não foi possível buscar Work Items: {error}")
            self.clear_action()
            st.rerun()

        board_items = self.state.get('ado_recon_board_items') or []
        if not board_items:
            return

        st.divider()
        st.markdown("### 🤖 3. Sugestão automática com IA")
        st.caption(
            "Compara os títulos dos Casos já existentes no Test Plan anterior com os Work Items "
            "novos — a IA baseia a sugestão só no título de cada caso (não tem acesso aos passos "
            "detalhados), então revise com atenção antes de confirmar."
        )
        wi_labels = {f"{i['id']} - {i['title']} ({i['type']}, {i['state']})": i for i in board_items}
        selected_labels = st.multiselect(
            "🎯 Work Items considerados na análise da IA",
            options=list(wi_labels.keys()),
            disabled=self.state.get('is_processing'),
            key="ado_recon_wi_multiselect",
        )
        selected_items = [wi_labels[l] for l in selected_labels]

        with st.container(key="azure_blue_btn_suggest_recon"):
            st.button(
                "🤖 Sugerir Vínculos com IA", type="primary",
                disabled=self.state.get('is_processing') or not selected_items,
                key="btn_suggest_recon_links",
                on_click=self.trigger_action,
                args=("suggest_recon_links",),
            )
        if self.state.get('current_action') == 'suggest_recon_links' and not self.state.get('show_interrupt_modal'):
            try:
                payload_cases = [{"titulo": c["titulo"]} for c in old_cases]
                with st.spinner("Consultando a IA para sugerir os vínculos..."):
                    result = self.client.trigger_matching(selected_items, payload_cases, self.state.get('project_name'))
                links = {}
                for vinculo in result.get("vinculos", []):
                    wid = str(vinculo.get("work_item_id"))
                    links[wid] = vinculo.get("casos", [])
                self.state.set('ado_recon_wi_case_links', links)
                for item in selected_items:
                    widget_key = f"ado_recon_multiselect_{item['id']}"
                    st.session_state[widget_key] = [c for c in links.get(str(item['id']), []) if c in [oc['titulo'] for oc in old_cases]]
            except Exception as error:
                st.error(f"❌ Não foi possível obter a sugestão da IA: {error}")
            self.clear_action()
            st.rerun()

        st.divider()
        st.markdown("### ✏️ 4. Revisar e confirmar")
        st.caption("Cada Caso só pode ser vinculado a UM Work Item — se já estiver escolhido em outro, some das opções aqui.")
        links = dict(self.state.get('ado_recon_wi_case_links') or {})
        ordered_wids = [str(item['id']) for item in selected_items]
        links = self._dedupe_case_assignments(links, ordered_wids)
        case_titles = [c["titulo"] for c in old_cases]
        claimed_so_far = set()
        for item in selected_items:
            wid_key = str(item['id'])
            widget_key = f"ado_recon_multiselect_{item['id']}"
            if widget_key not in st.session_state:
                st.session_state[widget_key] = [c for c in links.get(wid_key, []) if c in case_titles]
            available_options = [c for c in case_titles if c not in claimed_so_far or c in st.session_state[widget_key]]
            st.session_state[widget_key] = [c for c in st.session_state[widget_key] if c in available_options]
            selected = st.multiselect(
                f"{item['id']} - {item['title']} ({item['type']}, {item['state']})",
                options=available_options, key=widget_key, disabled=self.state.get('is_processing'),
                help="Casos já vinculados a outro Work Item não aparecem aqui.",
            )
            links[wid_key] = selected
            claimed_so_far.update(selected)
        self.state.set('ado_recon_wi_case_links', links)

        total_links = sum(len(c) for c in links.values())
        st.divider()
        with st.container(key="azure_blue_btn_confirm_recon"):
            if st.button(
                "🔗 Confirmar e Vincular no Azure DevOps", type="primary", use_container_width=True,
                disabled=self.state.get('is_processing') or total_links == 0,
                key="btn_confirm_recon",
            ):
                items_by_id_lookup = {item['id']: item for item in selected_items}
                items_display = []
                for wid_str, casos in links.items():
                    if not casos:
                        continue
                    item = items_by_id_lookup.get(int(wid_str))
                    label = f"{wid_str} - {item['title']} ({item['type']}, {item['state']})" if item else wid_str
                    items_display.append((label, casos))
                self.state.set('ado_recon_confirm_modal_params', (items_display, old_plan['name']))
                self.state.set('show_recon_confirm_modal', True)
                st.rerun()
        if total_links == 0:
            st.caption("Selecione ao menos um vínculo acima pra habilitar a confirmação.")

        if self.state.get('show_recon_confirm_modal'):
            params = self.state.get('ado_recon_confirm_modal_params') or ([], old_plan['name'])
            confirm_reconciliation_push_modal(*params)

        if self.state.get('current_action') == 'push_reconciliation' and not self.state.get('show_interrupt_modal'):
            self._push_reconciliation(ado_client, old_plan_id, old_cases, links)

        log = self.state.get('ado_recon_push_log') or []
        if log:
            st.markdown("#### 📋 Resultado da reconciliação")
            for line in log:
                st.write(line)

    def _push_reconciliation(self, ado_client, old_plan_id: int, old_cases: list, links: dict):
        """Cria (se preciso) a Requirement Suite de cada Work Item e vincula os Casos já existentes a ele."""
        case_id_by_title = {c["titulo"]: c["id"] for c in old_cases}
        log = []
        try:
            with st.spinner("Verificando Suítes já existentes neste Test Plan..."):
                root_suite_id = ado_client.get_test_plan_root_suite(old_plan_id)
                existing_suite_by_wi = ado_client.get_existing_requirement_suite_ids(old_plan_id)
        except Exception as error:
            log.append(f"❌ Falha ao preparar o Test Plan: {error}")
            self.state.set('ado_recon_push_log', log)
            self.clear_action()
            st.rerun()
            return

        tasks = [(int(wid), titulos) for wid, titulos in links.items() if titulos]
        total = len(tasks)
        if total:
            progress = st.progress(0, text=f"Vinculando Casos aos Work Items... (0/{total})")
            for idx, (work_item_id, titulos) in enumerate(tasks, start=1):
                try:
                    if work_item_id in existing_suite_by_wi:
                        suite_id = existing_suite_by_wi[work_item_id]
                        log.append(f"♻️ Work Item {work_item_id} já tinha Suite (ID {suite_id}).")
                    else:
                        suite_id = ado_client.create_requirement_based_suite(old_plan_id, root_suite_id, work_item_id)
                        log.append(f"✅ Suite criada para Work Item {work_item_id} (ID {suite_id}).")
                    for titulo in titulos:
                        case_id = case_id_by_title.get(titulo)
                        if not case_id:
                            continue
                        try:
                            ado_client.link_test_case_to_work_item(case_id, work_item_id)
                            log.append(f"　　→ '{titulo}' vinculado ao Work Item {work_item_id}.")
                        except AzureDevOpsError as error:
                            log.append(f"　　❌ Falha ao vincular '{titulo}': {error}")
                except AzureDevOpsError as error:
                    log.append(f"❌ Falha no Work Item {work_item_id}: {error}")
                except Exception as error:
                    log.append(f"❌ Erro inesperado no Work Item {work_item_id}: {error}")
                progress.progress(idx / total, text=f"Vinculando Casos aos Work Items... ({idx}/{total})")

        self._log(
            "Reconciliação de Test Plan Anterior", "Passo 7",
            f"Test Plan {old_plan_id} — {total} Work Item(s) processado(s)",
        )
        self.state.set('ado_recon_push_log', log)
        self.clear_action()
        st.rerun()

    def _render_step7_back_and_new(self, key_suffix: str, back_step: int = 6):
        c1, c2 = st.columns(2)
        with c1:
            if st.button(
                "← Voltar", use_container_width=True,
                disabled=self.state.get('is_processing'), key=f"btn_back_step7_{key_suffix}",
            ):
                self._set_step(back_step)
                st.rerun()
        with c2:
            if st.button(
                "🔄 Nova Análise", use_container_width=True, type="primary",
                disabled=self.state.get('is_processing'), key=f"btn_new_step7_{key_suffix}",
            ):
                self.state.set('show_new_analysis_modal', True)
                st.rerun()

    def _setup_azure_devops_connection(self, show_area_path_picker: bool = True):
        """
        Renderiza PAT + Organização + Projeto (+ Area Path, se
        `show_area_path_picker=True`) — compartilhado entre o Passo 7
        (Integração) e o Relatório de Testes, já que os dois precisam da
        mesma conexão com o Azure DevOps. Como só um step roda por vez,
        reaproveitar as mesmas widget keys aqui é seguro (nunca os dois
        renderizam na mesma execução do script).

        O Relatório de Testes tem seu PRÓPRIO seletor de Area Path(s)
        (multiseleção) fora deste método — por isso ele chama com
        `show_area_path_picker=False`, pra não mostrar dois seletores de
        Area Path na mesma tela (um aqui de 1 só, outro dele de vários).

        Retorna (ado_client, ado_org, ado_project, area_path) quando tudo
        está pronto, ou None se ainda falta algo — nesse caso, a mensagem
        já foi mostrada, e quem chamou só precisa dar `return` (o botão
        Voltar/Nova Análise fica por conta de quem chamou). Quando
        `show_area_path_picker=False`, `area_path` sempre volta como
        `ado_project` (equivalente a "raiz do projeto").
        """
        st.markdown("#### 🔧 Configuração do Azure DevOps")
        st.caption(
            "Organização, Projeto e Area Path vêm direto do Azure DevOps — nada aqui é digitado livremente."
        )

        st.markdown("##### 🔑 Seu Personal Access Token (PAT)")
        st.caption(
            "Use o **seu próprio** PAT do Azure DevOps aqui — não é mais um token único compartilhado "
            "por todo mundo. Isso garante que as ações feitas no Azure DevOps (criar Test Cases, Test "
            "Plans, vínculos) fiquem registradas em seu nome, não no de outra pessoa. O token não é "
            "salvo em nenhum lugar — vale só para esta sessão."
        )
        with st.expander("❓ Como criar meu próprio PAT no Azure DevOps"):
            st.markdown(
                """
1. Acesse `https://dev.azure.com/{sua-organização}/_usersSettings/tokens`
   (troque `{sua-organização}` pelo nome real, ex.: `refuturiza`)
2. Clique em **"+ New Token"**
3. Dê um nome (ex.: `qa-testgen-<seu-usuário>`)
4. Em **Organization**, escolha a organização certa (ex.: `refuturiza`) — ou
   **"All accessible organizations"** se você usa mais de uma
5. Em **Expiration**, escolha um prazo (ex.: 90 dias) — anote a data pra
   lembrar de renovar depois
6. Em **Scopes**, clique em **"Show all scopes"** e marque:
   - **Work Items** → Read & Write
   - **Test Management** → Read & Write
7. Clique em **Create**, e **copie o token imediatamente** — o Azure DevOps
   só mostra ele uma vez; se perder, precisa criar outro
8. Cole o token no campo abaixo
                """
            )
        user_pat = st.text_input(
            "Personal Access Token (PAT)",
            type="password",
            value=self.state.get('ado_user_pat', ''),
            disabled=self.state.get('is_processing'),
            key="ado_user_pat_input",
            help="Nunca é salvo em disco — fica só na memória desta sessão.",
        )
        self.state.set('ado_user_pat', user_pat)

        if not user_pat:
            st.info("Informe seu PAT acima para continuar.")
            return None

        # 1) Organizações — busca automática (é só 1 chamada rápida, ou nem
        # isso quando cai no fallback abaixo), então não precisa de um botão
        # manual só pra isso. Busca uma vez por sessão, e de novo sempre que
        # o PAT digitado mudar.
        if self.state.get('ado_last_validated_pat') != user_pat:
            self.state.set('ado_orgs_fetch_done', False)
            self.state.set('ado_pat_validated', None)

        if not self.state.get('ado_orgs_fetch_done'):
            probe_org = AzureDevOpsClient("", "", user_pat)
            try:
                with st.spinner("Carregando organizações acessíveis a este PAT..."):
                    orgs = probe_org.list_accessible_organizations()
                self.state.set('ado_accessible_orgs', orgs)
                self.state.set('ado_orgs_fetch_error', None)
                self.state.set('ado_pat_validated', True)
            except Exception as error:
                # Esse endpoint específico (app.vssps.visualstudio.com) só
                # funciona com PATs criados com escopo "All accessible
                # organizations". Um PAT restrito a uma única organização
                # também recebe 401 aqui — igual a um PAT inválido de
                # verdade. Pra diferenciar os dois casos, tenta uma chamada
                # ORG-SCOPED de verdade (list_projects) contra a organização
                # padrão configurada — só essa chamada confirma se o PAT é
                # válido ou não.
                self.state.set('ado_accessible_orgs', [])
                self.state.set('ado_orgs_fetch_error', str(error))
                fallback_org = self.config.azure_devops_org
                pat_ok = False
                if fallback_org:
                    try:
                        with st.spinner(f"Validando PAT em '{fallback_org}'..."):
                            AzureDevOpsClient(fallback_org, "", user_pat).list_projects()
                        pat_ok = True
                    except Exception:
                        pat_ok = False
                self.state.set('ado_pat_validated', pat_ok)
            self.state.set('ado_orgs_fetch_done', True)
            self.state.set('ado_last_validated_pat', user_pat)

            # Aproveita o PAT já validado pra preencher automaticamente o
            # nome de quem está gerando os relatórios (não sobrescreve se a
            # pessoa já digitou um nome manualmente antes, no Passo 6).
            if self.state.get('ado_pat_validated') and not self.state.get('author_name'):
                try:
                    probe_name = AzureDevOpsClient("", "", user_pat)
                    display_name = probe_name.get_profile_display_name()
                    if display_name:
                        self.state.set('author_name', display_name)
                except Exception:
                    pass  # não crítico — a pessoa sempre pode digitar manualmente no Passo 6

            if self.state.get('ado_pat_validated'):
                self._log("PAT Validado", "Azure DevOps", "PAT pessoal validado com sucesso")

        if not self.state.get('ado_pat_validated'):
            st.error(
                "❌ Não foi possível validar esse PAT. Confira se ele está correto, não "
                "expirou, e tem os escopos **Work Items (Read & Write)** e **Test Management "
                "(Read & Write)**."
            )
            fetch_error = self.state.get('ado_orgs_fetch_error')
            if fetch_error:
                st.caption(f"Detalhe do erro: {fetch_error}")
            return None

        orgs = self.state.get('ado_accessible_orgs') or []

        if not orgs and self.config.azure_devops_org:
            # Fallback: não conseguimos listar dinamicamente (PAT restrito a
            # uma única organização, já validado acima como funcional), mas
            # há uma organização padrão configurada — usa ela como única
            # opção válida do dropdown.
            orgs = [self.config.azure_devops_org]
            st.caption(
                f"ℹ️ Não foi possível listar organizações dinamicamente (PAT provavelmente restrito "
                f"a uma única organização) — usando **{self.config.azure_devops_org}**, configurada "
                f"no `secrets.toml`, como única opção."
            )

        if not orgs:
            st.warning("Nenhuma organização encontrada para o PAT informado.")
            if st.button("🔄 Tentar novamente", disabled=self.state.get('is_processing'), key="btn_retry_orgs"):
                self.state.set('ado_orgs_fetch_done', False)
                st.rerun()
            return None

        previous_org = self.state.get('ado_org_override')
        default_org = previous_org if previous_org in orgs else (
            self.config.azure_devops_org if self.config.azure_devops_org in orgs else orgs[0]
        )

        col_org, col_proj = st.columns(2)
        with col_org:
            ado_org = st.selectbox(
                "Organização *",
                options=orgs,
                index=orgs.index(default_org),
                disabled=self.state.get('is_processing'),
                key="ado_org_select",
                help="Lista vem direto do Azure DevOps — só as organizações que esse PAT consegue acessar.",
            )
        if ado_org != previous_org:
            # Organização mudou — projetos/Area Paths buscados antes eram de
            # outra org, não faz sentido continuar mostrando eles.
            self.state.set('ado_available_projects', [])
            self.state.set('ado_projects_org', '')
            self.state.set('ado_project_override', '')
            self.state.set('ado_available_area_paths', [])
            self.state.set('ado_area_paths_project', '')
            self.state.set('ado_area_path', '')
        self.state.set('ado_org_override', ado_org)

        # 2) Projetos — só buscados quando o usuário pedir explicitamente.
        projects = self.state.get('ado_available_projects') or []
        need_fetch_projects = not projects or self.state.get('ado_projects_org') != ado_org

        with col_proj:
            if need_fetch_projects:
                with st.container(key="azure_blue_btn_fetch_projects"):
                    st.button(
                        "🔍 Buscar Projetos desta Organização",
                        disabled=self.state.get('is_processing'),
                        key="btn_fetch_projects",
                        on_click=self.trigger_action,
                        args=("fetch_projects",),
                        use_container_width=True,
                    )

        if self.state.get('current_action') == 'fetch_projects' and not self.state.get('show_interrupt_modal'):
            probe_proj = AzureDevOpsClient(ado_org, "", user_pat)
            try:
                with st.spinner(f"Buscando projetos em '{ado_org}'..."):
                    projects = probe_proj.list_projects()
                self.state.set('ado_available_projects', projects)
                self.state.set('ado_projects_org', ado_org)
            except AzureDevOpsError as error:
                st.error(f"❌ Não foi possível listar projetos de '{ado_org}': {error}")
                self.state.set('ado_available_projects', [])
            except Exception as error:
                st.error(f"❌ Erro inesperado ao listar projetos: {error}")
                self.state.set('ado_available_projects', [])
            self.clear_action()
            st.rerun()

        projects = self.state.get('ado_available_projects') or []

        if not projects or self.state.get('ado_projects_org') != ado_org:
            # Ainda não buscou projetos desta organização — a tela para por
            # aqui de propósito: só Organização + botão ficam visíveis.
            return None

        PLACEHOLDER = "---"
        project_options = [PLACEHOLDER] + projects
        current_project = self.state.get('ado_project_override') or PLACEHOLDER
        if current_project not in project_options:
            current_project = PLACEHOLDER
        with col_proj:
            ado_project_choice = st.selectbox(
                "Projeto *",
                options=project_options,
                index=project_options.index(current_project),
                disabled=self.state.get('is_processing'),
                key="ado_project_select",
                help="Lista vem direto do Azure DevOps — só os projetos visíveis a esse PAT dentro da organização selecionada.",
            )
        if ado_project_choice != self.state.get('ado_project_override'):
            self.state.set('ado_available_area_paths', [])
            self.state.set('ado_area_paths_project', '')
            self.state.set('ado_area_path', '')
            self.state.set('ado_area_path_choice', PLACEHOLDER)
        self.state.set('ado_project_override', '' if ado_project_choice == PLACEHOLDER else ado_project_choice)

        ado_project = self.state.get('ado_project_override')

        if not ado_project:
            # Nenhum projeto real escolhido ainda (ainda em "---") — não
            # revela Area Path/Work Items até o usuário escolher de verdade.
            return None

        ado_client = AzureDevOpsClient(ado_org, ado_project, user_pat)

        if not ado_client.is_configured():
            st.info(
                "Preencha Organização e Projeto acima. Além disso, `AZURE_DEVOPS_PAT` precisa "
                "estar configurado no `secrets.toml` (não é editável nesta tela, por segurança)."
            )
            return None

        if not show_area_path_picker:
            return ado_client, ado_org, ado_project, ado_project

        st.divider()

        # 3) Area Path do Board — busca automática assim que um Projeto real é
        # escolhido (sem botão), mas o dropdown SEMPRE começa em "---" (nada
        # pré-selecionado) — se ficar em "---", usamos a raiz do projeto por
        # baixo dos panos. É opcional. Passa pelo padrão trigger_action pra
        # travar a tela durante a busca, igual a qualquer outra chamada de rede.
        if self.state.get('ado_area_paths_project') != ado_project and self.state.get('current_action') != 'fetch_area_paths_auto':
            self.trigger_action('fetch_area_paths_auto')
            st.rerun()

        if self.state.get('current_action') == 'fetch_area_paths_auto' and not self.state.get('show_interrupt_modal'):
            try:
                with st.spinner(f"Buscando Area Paths em '{ado_project}'..."):
                    area_paths = ado_client.list_area_paths()
                self.state.set('ado_available_area_paths', area_paths)
            except AzureDevOpsError as error:
                st.error(f"❌ Não foi possível listar os Area Paths de '{ado_project}': {error}")
                self.state.set('ado_available_area_paths', [])
            except Exception as error:
                st.error(f"❌ Erro inesperado ao listar Area Paths: {error}")
                self.state.set('ado_available_area_paths', [])
            self.state.set('ado_area_paths_project', ado_project)
            self.state.set('ado_area_path_choice', PLACEHOLDER)
            self.clear_action()
            st.rerun()

        st.markdown("##### 📁 Area Path do Board no Azure DevOps")
        st.caption(
            f"Opcional — se deixar em **\"{PLACEHOLDER}\"**, uso a raiz do projeto "
            f"(**{ado_project}**). Preciso do Area Path exato do board certo pra encontrar os "
            "Work Items dele, então escolha aqui se o board que você quer usar fica numa Area "
            "diferente da raiz."
        )

        available_area_paths = self.state.get('ado_available_area_paths') or []
        area_path_options = [PLACEHOLDER] + available_area_paths
        current_area_choice = self.state.get('ado_area_path_choice') or PLACEHOLDER
        if current_area_choice not in area_path_options:
            current_area_choice = PLACEHOLDER
        area_path_choice = st.selectbox(
            "Area Path do Board",
            options=area_path_options,
            index=area_path_options.index(current_area_choice),
            help="Lista vem direto do Azure DevOps — todos os Area Paths que existem no projeto selecionado.",
            disabled=self.state.get('is_processing'),
            key="ado_area_path_select",
        )
        self.state.set('ado_area_path_choice', area_path_choice)
        area_path = ado_project if area_path_choice == PLACEHOLDER else area_path_choice
        self.state.set('ado_area_path', area_path)

        return ado_client, ado_org, ado_project, area_path

    def step_7(self):
        st.subheader("Passo 7 – Integração com Azure DevOps")

        if not self._get_permission_cached("azure_devops"):
            st.error("❌ Você não tem permissão para acessar a integração com o Azure DevOps.")
            st.divider()
            self._render_step7_back_and_new("no_permission")
            return

        if not AZURE_DEVOPS_INTEGRATION_ENABLED:
            st.info("🛠️ Em breve! Essa integração está temporariamente desativada.")
            st.divider()
            self._render_step7_back_and_new("disabled")
            return

        conn = self._setup_azure_devops_connection(show_area_path_picker=False)
        if conn is None:
            st.divider()
            self._render_step7_back_and_new("incomplete_setup")
            return
        ado_client, ado_org, ado_project, _default_area_path = conn

        st.markdown("##### 📁 Area Path(s) do Board no Azure DevOps")
        st.caption(
            "Opcional — deixe vazio pra considerar o projeto inteiro. Selecione uma ou mais pra "
            "restringir a busca de Work Items a boards específicos."
        )
        if self.state.get('ado_available_area_paths') and self.state.get('ado_area_paths_project') == ado_project:
            area_path_options = self.state.get('ado_available_area_paths') or []
        else:
            try:
                with st.spinner("Buscando Area Paths do projeto..."):
                    area_path_options = ado_client.list_area_paths()
                self.state.set('ado_available_area_paths', area_path_options)
                self.state.set('ado_area_paths_project', ado_project)
            except Exception as error:
                st.error(f"❌ Não foi possível buscar Area Paths: {error}")
                area_path_options = []

        col_ap, col_btn = st.columns(2)
        with col_ap:
            area_paths = st.multiselect(
                "Area Path(s)",
                options=area_path_options,
                disabled=self.state.get('is_processing'),
                key="ado_area_paths_select_s7",
                help="Selecione uma ou mais — a busca de Work Items considera todas juntas.",
            )
        # Área usada como fallback pra Casos sem nenhum Work Item vinculado
        # (esses precisam de UMA Area Path pra existir no Azure DevOps —
        # usa a primeira escolhida, ou a raiz do projeto se nenhuma foi selecionada).
        fallback_area_path = area_paths[0] if area_paths else ado_project

        with col_btn:
            with st.container(key="azure_blue_btn_fetch_wi"):
                st.button(
                    "🔄 Buscar Work Items do Board",
                    disabled=self.state.get('is_processing'),
                    key="btn_fetch_wi_s7",
                    on_click=self.trigger_action,
                    args=("fetch_wi",),
                    use_container_width=True,
                )

        if self.state.get('current_action') == 'fetch_wi' and not self.state.get('show_interrupt_modal'):
            try:
                paths_to_search = area_paths or [ado_project]
                with st.spinner(f"Buscando Work Items em {len(paths_to_search)} Area Path(s)..."):
                    items_by_id = {}
                    for ap in paths_to_search:
                        for item in ado_client.fetch_work_items_by_area_path(ap):
                            items_by_id[item["id"]] = item
                    items = list(items_by_id.values())
                self.state.set('ado_board_items', items)
                # Nova busca -> reseta a seleção de "quais entram no matching"
                # pra não arrastar uma seleção antiga de um board diferente.
                # None (não []) -> sinaliza "ainda não escolhida nesta busca",
                # pra depois pré-selecionar automaticamente os Work Items com
                # Caso pré-vinculado do Passo 1.
                self.state.set('ado_wi_matching_selected_ids', None)
                if 'ado_wi_matching_multiselect' in st.session_state:
                    del st.session_state['ado_wi_matching_multiselect']
                if not items:
                    st.warning("Nenhum Work Item encontrado" + (" nessas Area Paths (além de Test Cases)." if area_paths else " nesse projeto (além de Test Cases)."))
            except AzureDevOpsError as error:
                st.error(f"❌ {error}")
            except Exception as error:
                st.error(f"❌ Erro inesperado: {error}")
            self.clear_action()
            st.rerun()

        board_items_fetched = self.state.get('ado_board_items') or []
        if not board_items_fetched:
            st.info("Busque os Work Items do Board (botão acima) antes de escolher o Modo de Envio.")
            st.divider()
            self._render_step7_back_and_new("waiting_wi")
            return

        st.divider()
        st.markdown("##### 🔀 Modo de Envio")
        modo_options = [
            "🔗 Vincular a Work Items (Requirement Suites)",
            "📋 Sem Work Items (Suítes Estáticas, a partir dos Planos gerados)",
            "🔄 Reconciliar Test Plan Anterior (ligar Casos já criados a Work Items novos)",
        ]
        # Sugestão de modo baseada no Tipo de Documento escolhido no Passo 1
        # — "Visão" geralmente significa que ainda não há Work Item pra
        # vincular caso a caso (só um Épico/Backlog no máximo). A pessoa
        # sempre pode trocar manualmente.
        tipo_doc = self.state.get('tipo_documento', '')
        default_modo_idx = 1 if tipo_doc == "Visão" else 0
        modo_envio = st.radio(
            "Como os Casos de Teste devem entrar no Azure DevOps?",
            options=modo_options,
            index=default_modo_idx,
            disabled=self.state.get('is_processing'),
            key="ado_modo_envio_radio",
            help=(
                "Sugestão baseada no Tipo de Documento do Passo 1 — mude se não fizer sentido pro seu caso. "
                "'Sem Work Items' é indicado quando o projeto ainda não tem Work Items (ex.: só um Documento "
                "de Visão). 'Reconciliar' é pra quando você já usou 'Sem Work Items' antes, e agora os Work "
                "Items foram criados — liga os Casos já existentes a eles, sem duplicar."
            ),
        )

        if modo_envio == modo_options[1]:
            st.divider()
            self._render_step7_static_suite_mode(ado_client, ado_project, fallback_area_path)
            st.divider()
            self._render_step7_back_and_new("main")
            return

        if modo_envio == modo_options[2]:
            st.divider()
            self._render_step7_reconciliation_mode(ado_client, ado_project, area_paths)
            st.divider()
            self._render_step7_back_and_new("main")
            return

        board_items = self.state.get('ado_board_items') or []
        test_cases = self.state.get('test_cases') or []

        # Pré-vínculos declarados no Passo 1 (documento já marcado com um
        # Work Item) aparecem aqui IMEDIATAMENTE, assim que os Work Items
        # são buscados — sem precisar clicar em "Sugerir Vínculos com IA"
        # pra isso. Roda só uma vez por busca de board_items (não sobrescreve
        # depois se você editar manualmente ou pedir uma sugestão da IA).
        board_ids_tuple = tuple(sorted(item['id'] for item in board_items))
        prelink_diag = {"com_marcacao": 0, "vinculados": 0, "nao_bateu": []}
        if board_items and test_cases and self.state.get('ado_wi_prelinked_marker') != board_ids_tuple:
            board_ids_set = set(board_ids_tuple)
            existing_links = dict(self.state.get('ado_wi_case_links') or {})
            for tc in test_cases:
                wi_raw_full = str(tc.get("work_item_relacionado") or "").strip()
                if not wi_raw_full:
                    continue
                prelink_diag["com_marcacao"] += 1
                # Extrai só os dígitos — robusto a variações de formatação da
                # IA (ex.: "#1234", "Work Item 1234", "1234.0").
                digits = re.sub(r"[^\d]", "", wi_raw_full)
                if not digits:
                    prelink_diag["nao_bateu"].append(f"{tc.get('titulo', '')} (valor: '{wi_raw_full}')")
                    continue
                wi_id = int(digits)
                if wi_id not in board_ids_set:
                    prelink_diag["nao_bateu"].append(f"{tc.get('titulo', '')} (Work Item #{wi_id}, fora dos buscados agora)")
                    continue
                titulo = tc.get("titulo", "")
                wid_key = str(wi_id)
                existing_links.setdefault(wid_key, [])
                if titulo not in existing_links[wid_key]:
                    existing_links[wid_key].append(titulo)
                    prelink_diag["vinculados"] += 1
            self.state.set('ado_wi_case_links', existing_links)
            self.state.set('ado_wi_prelinked_marker', board_ids_tuple)
            self.state.set('ado_wi_prelink_diag', prelink_diag)
        else:
            prelink_diag = self.state.get('ado_wi_prelink_diag') or prelink_diag

        if not board_items:
            st.info("Busque os Work Items do Board (botão acima) antes de continuar.")
        elif not test_cases:
            st.info("Nenhum Caso de Teste foi gerado ainda nesta análise.")
        else:
            st.divider()
            st.markdown("### 🤖 Sugestão automática de vínculos com IA")
            st.caption(
                "Envia os Work Items selecionados abaixo e os Casos de Teste gerados pro n8n, "
                "que devolve uma sugestão de quais casos se relacionam a quais Work Items. Você "
                "pode ajustar tudo manualmente depois, antes de confirmar."
            )

            wi_labels = {
                f"{item['id']} - {item['title']} ({item['type']}, {item['state']})": item
                for item in board_items
            }
            selected_ids = self.state.get('ado_wi_matching_selected_ids')
            if selected_ids is None:
                # Primeira vez nesta busca de board — pré-seleciona
                # automaticamente os Work Items que já têm Caso pré-vinculado
                # do Passo 1 (não faz sentido pedir pra escolher de novo algo
                # que a pessoa já declarou lá).
                pre_linked_wids = {
                    int(wid) for wid, casos in (self.state.get('ado_wi_case_links') or {}).items()
                    if casos
                }
                selected_ids = [item['id'] for item in board_items if item['id'] in pre_linked_wids]
                self.state.set('ado_wi_matching_selected_ids', selected_ids)
            label_by_id = {item['id']: label for label, item in wi_labels.items()}
            current_labels = [label_by_id[wid] for wid in selected_ids if wid in label_by_id]

            selected_labels = st.multiselect(
                "🎯 Work Items considerados na análise da IA",
                options=list(wi_labels.keys()),
                default=current_labels,
                disabled=self.state.get('is_processing'),
                key="ado_wi_matching_multiselect",
                help="Work Items com Caso pré-vinculado do Passo 1 já vêm marcados automaticamente. Clique em quantos quiser — não precisa segurar Ctrl/Shift.",
            )
            selected_ids = [wi_labels[label]['id'] for label in selected_labels]
            self.state.set('ado_wi_matching_selected_ids', selected_ids)

            if not selected_labels:
                st.caption("Nenhum Work Item selecionado ainda — escolha acima.")

            selected_board_items = [wi_labels[label] for label in selected_labels]

            with st.container(key="azure_blue_btn_suggest"):
                st.button(
                    "🤖 Sugerir Vínculos com IA",
                    type="primary",
                    disabled=self.state.get('is_processing') or not selected_board_items,
                    key="btn_suggest_links",
                    on_click=self.trigger_action,
                    args=("suggest_ado_links",),
                )
            if not selected_board_items:
                st.caption("Selecione ao menos 1 Work Item acima para habilitar a sugestão da IA.")
            if self.state.get('current_action') == 'suggest_ado_links' and not self.state.get('show_interrupt_modal'):
                self._suggest_ado_links(ado_client, selected_board_items, test_cases)
                self.clear_action()
                st.rerun()

            suggest_msg = self.state.get('ado_suggest_message')
            if suggest_msg:
                level, text = suggest_msg
                {"success": st.success, "warning": st.warning, "error": st.error}.get(level, st.info)(text)

            duplicate_titles = self.state.get('ado_duplicate_case_titles') or []
            if duplicate_titles:
                with st.expander(f"🔁 {len(duplicate_titles)} Caso(s) considerados duplicados (não serão criados/vinculados)"):
                    st.caption(
                        "Esses casos pareceram muito parecidos com Casos de Teste que já existem no "
                        "Work Item correspondente no Azure DevOps. Se algum desses NÃO for realmente "
                        "duplicado, você pode vinculá-lo manualmente na revisão abaixo — só ele não vai "
                        "aparecer pré-selecionado."
                    )
                    for t in duplicate_titles:
                        st.write(f"- {t}")

            st.divider()
            st.markdown("### ✏️ Revisar e confirmar vínculos")
            st.caption(
                "Adicione ou remova Casos de Teste livremente pra cada Work Item. Cada Caso só "
                "pode ser vinculado a UM Work Item — se ele já estiver escolhido em outro, some "
                "das opções aqui. Work Items sem nenhum caso selecionado não geram Suite no Azure DevOps."
            )

            diag = self.state.get('ado_wi_prelink_diag') or {}
            if diag.get("vinculados"):
                st.success(
                    f"✅ {diag['vinculados']} Caso(s) já vieram pré-vinculados desde o Passo 1 "
                    "(documento marcado com Work Item) — já aparecem selecionados abaixo, sem "
                    "precisar de sugestão da IA."
                )
            elif diag.get("com_marcacao"):
                # Tem marcação, mas nada bateu — mostra o motivo, em vez de
                # falhar silenciosamente (ajuda a diagnosticar rápido).
                st.warning(
                    f"⚠️ {diag['com_marcacao']} Caso(s) vieram com marcação de Work Item do Passo 1, "
                    "mas nenhum bateu com os Work Items buscados agora nesta tela."
                )
                if diag.get("nao_bateu"):
                    with st.expander("Ver detalhes"):
                        for linha in diag["nao_bateu"]:
                            st.caption(f"• {linha}")
                        st.caption(
                            "Confira se a Area Path escolhida aqui no Passo 7 é a mesma (ou inclui) "
                            "a Area Path usada no Passo 1 pra buscar os Work Items."
                        )

            ignored_ids = [item['id'] for item in board_items if item['id'] not in selected_ids]
            if ignored_ids:
                st.caption(
                    f"ℹ️ {len(ignored_ids)} Work Item(s) do board foram ignorados nesta análise "
                    f"(não selecionados): {', '.join(str(i) for i in ignored_ids)}."
                )

            links = dict(self.state.get('ado_wi_case_links') or {})
            ordered_wids = [str(item['id']) for item in selected_board_items]
            links = self._dedupe_case_assignments(links, ordered_wids)
            case_titles = [tc.get('titulo', f'Caso #{i}') for i, tc in enumerate(test_cases, start=1)]

            claimed_so_far = set()
            for item in selected_board_items:
                wid_key = str(item['id'])
                widget_key = f"ado_wi_multiselect_{item['id']}"
                # O Streamlit só respeita "default" na primeiríssima renderização
                # do widget; depois disso, quem manda é o valor em session_state.
                # Por isso, sempre que houver uma sugestão nova (da IA ou de um
                # rerun anterior) ainda não refletida no widget, semeamos o
                # session_state diretamente antes de desenhar o multiselect.
                if widget_key not in st.session_state:
                    st.session_state[widget_key] = [c for c in links.get(wid_key, []) if c in case_titles]

                # Um Caso já reivindicado por um Work Item ANTERIOR nesta
                # mesma lista não aparece como opção aqui — exclusividade.
                available_options = [c for c in case_titles if c not in claimed_so_far or c in st.session_state[widget_key]]
                st.session_state[widget_key] = [c for c in st.session_state[widget_key] if c in available_options]

                selected = st.multiselect(
                    f"{item['id']} - {item['title']} ({item['type']}, {item['state']})",
                    options=available_options,
                    key=widget_key,
                    disabled=self.state.get('is_processing'),
                    help="Casos já vinculados a outro Work Item não aparecem aqui — um Caso pertence a só um Work Item por vez.",
                )
                links[wid_key] = selected
                claimed_so_far.update(selected)
            self.state.set('ado_wi_case_links', links)

            assigned_titles = set()
            for casos in links.values():
                assigned_titles.update(casos)
            unassigned = [t for t in case_titles if t not in assigned_titles]
            if unassigned:
                with st.expander(f"⚠️ {len(unassigned)} Caso(s) sem nenhum Work Item vinculado (marcados por padrão pra não subir — veja abaixo)"):
                    for t in unassigned:
                        st.write(f"- {t}")

            st.divider()
            st.markdown("### 🚫 Excluir Casos de Teste do Envio")
            st.caption(
                "Escolha aqui os Casos de Teste que você **não** quer enviar pro Azure DevOps "
                "nesta integração — nem como Caso avulso, nem vinculados a nenhum Work Item. "
                "Casos sem nenhum Work Item vinculado já vêm marcados por padrão — desmarque "
                "aqui se quiser enviar algum deles mesmo assim."
            )
            # Semeia a exclusão só na primeira vez que o widget aparece nesta
            # sessão — depois disso, respeita o que a pessoa escolher
            # manualmente (não fica reforçando o padrão a cada rerun).
            if "ado_excluded_case_titles_select" not in st.session_state:
                st.session_state["ado_excluded_case_titles_select"] = list(unassigned)
            excluded_titles = st.multiselect(
                "Casos a excluir",
                options=case_titles,
                disabled=self.state.get('is_processing'),
                key="ado_excluded_case_titles_select",
                help="Os excluídos aqui não sobem de jeito nenhum, mesmo que estejam vinculados a um Work Item acima.",
            )
            self.state.set('ado_excluded_case_titles', excluded_titles)
            if excluded_titles:
                st.caption(f"ℹ️ {len(excluded_titles)} Caso(s) marcados pra não subir.")

            items_with_cases = {
                wid: [c for c in casos if c not in excluded_titles]
                for wid, casos in links.items()
            }
            items_with_cases = {wid: casos for wid, casos in items_with_cases.items() if casos}
            total_links = sum(len(c) for c in items_with_cases.values())

            st.divider()
            st.markdown("### 📋 Test Plan")

            existing_plans_label = (
                f"🔍 Buscar Test Plans existentes ({len(area_paths)} Area Path(s))" if area_paths
                else "🔍 Buscar Test Plans existentes no Projeto"
            )
            with st.container(key="azure_blue_btn_fetch_existing_plans"):
                st.button(
                    existing_plans_label,
                    disabled=self.state.get('is_processing'),
                    key="btn_fetch_existing_plans",
                    on_click=self.trigger_action,
                    args=("fetch_existing_plans",),
                )
            if self.state.get('current_action') == 'fetch_existing_plans' and not self.state.get('show_interrupt_modal'):
                try:
                    paths_key = tuple(sorted(area_paths)) if area_paths else (ado_project,)
                    with st.spinner(f"Buscando Test Plans em {len(paths_key)} Area Path(s)..."):
                        plans_by_id = {}
                        for ap in paths_key:
                            for p in ado_client.list_test_plans_for_area_path(ap):
                                plans_by_id[p["id"]] = p
                        existing = list(plans_by_id.values())
                    self.state.set('ado_existing_plans_in_path', existing)
                    self.state.set('ado_existing_plans_area_path', paths_key)
                except AzureDevOpsError as error:
                    st.error(f"❌ Não foi possível buscar Test Plans existentes: {error}")
                except Exception as error:
                    st.error(f"❌ Erro inesperado: {error}")
                self.clear_action()
                st.rerun()

            current_paths_key = tuple(sorted(area_paths)) if area_paths else (ado_project,)
            existing_plans = (
                self.state.get('ado_existing_plans_in_path') or []
                if self.state.get('ado_existing_plans_area_path') == current_paths_key
                else []
            )

            plan_mode_options = ["Criar novo Test Plan"]
            if existing_plans:
                plan_mode_options.append("Usar um Test Plan existente (adicionar Suites/Casos nele)")
            else:
                st.caption(
                    "Nenhum Test Plan encontrado ainda nesta Area Path — clique em \"Buscar\" acima "
                    "se você espera que já exista um (ou simplesmente crie um novo abaixo)."
                )

            plan_mode = st.radio(
                "O que você quer fazer?",
                options=plan_mode_options,
                disabled=self.state.get('is_processing'),
                key="ado_plan_mode_radio",
                horizontal=False,
            )

            col_plan, col_state = st.columns(2)
            existing_plan_id = None
            if plan_mode.startswith("Usar"):
                plan_labels = {f"{p['id']} - {p['name']}": p for p in existing_plans}
                with col_plan:
                    chosen_plan_label = st.selectbox(
                        "Test Plan existente",
                        options=list(plan_labels.keys()),
                        disabled=self.state.get('is_processing'),
                        key="ado_existing_plan_select",
                        help="Os Work Items selecionados que já tiverem uma Suite neste plano não geram Suite "
                             "duplicada — os Casos de Teste novos só entram na Suite já existente.",
                    )
                existing_plan_id = plan_labels[chosen_plan_label]["id"]
                plan_name = plan_labels[chosen_plan_label]["name"]
                self.state.set('ado_plan_name_error', None)
            else:
                default_plan_name = f"{self.state.get('project_name') or 'QA TestGen'} - QA TestGen"
                with col_plan:
                    plan_name = st.text_input(
                        "Nome do Test Plan a ser criado no Azure DevOps",
                        value=self.state.get('ado_test_plan_name') or default_plan_name,
                        disabled=self.state.get('is_processing'),
                        key="ado_test_plan_name_input",
                        help="Precisa ser único no projeto — não pode repetir o nome de um Test Plan já existente.",
                    )
                self.state.set('ado_test_plan_name', plan_name)

            with col_state:
                initial_state_label = st.selectbox(
                    "Estado inicial dos Casos de Teste criados",
                    options=["Design (revisar manualmente antes de rodar)", "Ready (pronto para execução)"],
                    index=1 if self.state.get('ado_tc_initial_state', 'Ready') == 'Ready' else 0,
                    disabled=self.state.get('is_processing'),
                    key="ado_tc_initial_state_select",
                )
            initial_state = "Ready" if initial_state_label.startswith("Ready") else "Design"
            self.state.set('ado_tc_initial_state', initial_state)

            plan_name_error = self.state.get('ado_plan_name_error')
            if plan_name_error:
                st.error(plan_name_error)

            st.divider()
            if items_with_cases:
                with st.container(key="azure_blue_btn_confirm"):
                    if st.button(
                        "🔗 Confirmar e Integrar com Azure DevOps",
                        type="primary",
                        use_container_width=True,
                        disabled=self.state.get('is_processing'),
                        key="btn_open_ado_full_confirm",
                    ):
                        if not plan_name.strip():
                            self.state.set('ado_plan_name_error', "❌ Informe um nome para o Test Plan antes de continuar.")
                            st.rerun()
                        else:
                            existing_case_ids = self.state.get('ado_test_case_ids') or {}
                            duplicate_titles_now = set(self.state.get('ado_duplicate_case_titles') or [])
                            cases_to_create_titles = [
                                tc.get('titulo') for tc in test_cases
                                if tc.get('titulo') not in excluded_titles
                                and tc.get('titulo') not in existing_case_ids
                                and tc.get('titulo') not in duplicate_titles_now
                            ]
                            items_by_id_lookup = {item['id']: item for item in board_items}
                            items_display = []
                            for wid_str, casos in items_with_cases.items():
                                item = items_by_id_lookup.get(int(wid_str))
                                label = f"{wid_str} - {item['title']} ({item['type']}, {item['state']})" if item else wid_str
                                items_display.append((label, casos))
                            self.state.set('ado_confirm_modal_params', (cases_to_create_titles, items_display, plan_name.strip(), bool(existing_plan_id)))
                            self.state.set('ado_existing_plan_id_chosen', existing_plan_id)
                            if existing_plan_id:
                                self.state.set('ado_plan_name_error', None)
                                self.state.set('show_ado_confirm_modal', True)
                            else:
                                self.trigger_action("check_ado_plan_name")
                            st.rerun()
            else:
                st.info("Vincule pelo menos um Caso de Teste a um Work Item antes de continuar.")

            if self.state.get('current_action') == 'check_ado_plan_name' and not self.state.get('show_interrupt_modal'):
                try:
                    with st.spinner("Verificando se já existe um Test Plan com esse nome..."):
                        duplicate = ado_client.test_plan_name_exists(plan_name.strip())
                    if duplicate:
                        self.state.set(
                            'ado_plan_name_error',
                            f"❌ Já existe um Test Plan chamado **{plan_name.strip()}** neste projeto do "
                            "Azure DevOps. Escolha um nome diferente, ou use a opção \"Usar um Test Plan "
                            "existente\" acima pra reaproveitar esse mesmo plano.",
                        )
                    else:
                        self.state.set('ado_plan_name_error', None)
                        self.state.set('show_ado_confirm_modal', True)
                except AzureDevOpsError as error:
                    self.state.set('ado_plan_name_error', f"❌ Não foi possível checar duplicidade: {error}")
                except Exception as error:
                    self.state.set('ado_plan_name_error', f"❌ Erro inesperado ao checar duplicidade: {error}")
                self.clear_action()
                st.rerun()

            if self.state.get('show_ado_confirm_modal'):
                params = self.state.get('ado_confirm_modal_params') or ([], [], plan_name, False)
                confirm_azure_devops_full_push_modal(*params)

            if self.state.get('current_action') == 'push_azure_devops_full' and not self.state.get('show_interrupt_modal'):
                self._push_full_azure_devops(ado_client, fallback_area_path, plan_name.strip(), initial_state, self.state.get('ado_existing_plan_id_chosen'))

            log = self.state.get('ado_full_push_log') or []
            if log:
                st.markdown("#### 📋 Resultado da integração")
                for line in log:
                    st.write(line)

        st.divider()
        self._render_step7_back_and_new("main")

    def _execution_report_page(self):
        st.subheader("📊 Relatório de Testes (execução)")
        st.caption(
            "Documenta o que já foi EXECUTADO no Azure DevOps — não depende de terminar o "
            "assistente de geração, só busca dados que já existem lá."
        )

        c_back, c_new = st.columns(2)
        with c_back:
            if st.button("← Voltar", key="btn_report_back_top", use_container_width=True):
                self._navigate_or_confirm({'show_execution_report_page': False})
        with c_new:
            if st.button("🔄 Novo Relatório", key="btn_new_report_top", use_container_width=True, disabled=not self.state.get('report_pdf_bytes')):
                self.state.set('show_new_report_modal', True)
                st.rerun()
        if self.state.get('show_new_report_modal'):
            confirm_new_report_modal()
        if self.state.get('show_leave_report_modal'):
            confirm_leave_report_modal()

        if not self._get_permission_cached("execution_report"):
            st.error("❌ Você não tem permissão para acessar o Relatório de Testes.")
            return

        conn = self._setup_azure_devops_connection(show_area_path_picker=False)
        if conn is None:
            return
        ado_client, ado_org, ado_project, _default_area_path = conn

        st.markdown("##### 📁 Area Path(s) para este relatório")
        st.caption(
            "Opcional — filtra quais Test Plans aparecem pra escolher abaixo, e vira o \"nome "
            "do projeto\" mostrado no relatório. Deixe vazio pra considerar o projeto inteiro."
        )
        if self.state.get('ado_available_area_paths') and self.state.get('ado_area_paths_project') == ado_project:
            area_path_options = self.state.get('ado_available_area_paths') or []
        else:
            try:
                with st.spinner("Buscando Area Paths do projeto..."):
                    area_path_options = ado_client.list_area_paths()
                self.state.set('ado_available_area_paths', area_path_options)
                self.state.set('ado_area_paths_project', ado_project)
            except Exception as error:
                st.error(f"❌ Não foi possível buscar Area Paths: {error}")
                area_path_options = []

        area_paths = st.multiselect(
            "Area Path(s)",
            options=area_path_options,
            disabled=self.state.get('is_processing'),
            key="report_area_paths_select",
            help="Selecione uma ou mais — os Test Plans mostrados abaixo ficam restritos a elas.",
        )

        st.divider()
        self._render_execution_report_section(ado_client, ado_project, area_paths)

        st.divider()
        if st.button("← Voltar", key="btn_report_back_bottom"):
            self._navigate_or_confirm({'show_execution_report_page': False})

    def _wi_generation_page(self):
        st.subheader("🎯 Gerar Casos de Teste a partir de Work Items")
        st.caption(
            "Escolhe Work Items existentes no Azure DevOps pra usar como especificação, no lugar "
            "de enviar um documento — a Descrição e os Critérios de Aceite de cada um viram o "
            "texto de entrada, e o resto do processo segue igual ao Passo 1 (Dúvidas → Matriz → "
            "Casos → Planos)."
        )

        if st.button("← Voltar", key="btn_wigen_back_top"):
            self.state.set('show_wi_generation_page', False)
            st.rerun()

        if not self._get_permission_cached("azure_devops"):
            st.error("❌ Você não tem permissão para acessar Work Items do Azure DevOps.")
            return

        conn = self._setup_azure_devops_connection(show_area_path_picker=False)
        if conn is None:
            return
        ado_client, ado_org, ado_project, _default_area_path = conn

        st.markdown("##### 📁 Area Path(s) do Board")
        st.caption(
            "Opcional — deixe vazio pra considerar o projeto inteiro. Selecione uma ou mais pra "
            "restringir a busca de Work Items a boards específicos."
        )
        if self.state.get('ado_available_area_paths') and self.state.get('ado_area_paths_project') == ado_project:
            area_path_options = self.state.get('ado_available_area_paths') or []
        else:
            try:
                with st.spinner("Buscando Area Paths do projeto..."):
                    area_path_options = ado_client.list_area_paths()
                self.state.set('ado_available_area_paths', area_path_options)
                self.state.set('ado_area_paths_project', ado_project)
            except Exception as error:
                st.error(f"❌ Não foi possível buscar Area Paths: {error}")
                area_path_options = []

        col_ap, col_btn = st.columns(2)
        with col_ap:
            area_paths = st.multiselect(
                "Area Path(s)",
                options=area_path_options,
                disabled=self.state.get('is_processing'),
                key="wigen_area_paths_select",
                help="Selecione uma ou mais — a busca de Work Items considera todas juntas.",
            )
        with col_btn:
            with st.container(key="azure_blue_btn_fetch_wi_gen"):
                st.button(
                    "🔄 Buscar Work Items do Board",
                    disabled=self.state.get('is_processing'),
                    key="btn_fetch_wi_gen",
                    on_click=self.trigger_action,
                    args=("fetch_wi_gen",),
                    use_container_width=True,
                )

        if self.state.get('current_action') == 'fetch_wi_gen' and not self.state.get('show_interrupt_modal'):
            try:
                paths_to_search = area_paths or [ado_project]
                with st.spinner(f"Buscando Work Items em {len(paths_to_search)} Area Path(s)..."):
                    items_by_id = {}
                    for ap in paths_to_search:
                        for item in ado_client.fetch_work_items_by_area_path(ap):
                            items_by_id[item["id"]] = item
                    items = list(items_by_id.values())
                self.state.set('wigen_board_items', items)
                self.state.set('wigen_selected_ids', [])
                if 'wigen_multiselect' in st.session_state:
                    del st.session_state['wigen_multiselect']
                if not items:
                    st.warning("Nenhum Work Item encontrado" + (" nessas Area Paths." if area_paths else " neste projeto."))
            except AzureDevOpsError as error:
                st.error(f"❌ {error}")
                self.state.set('wigen_board_items', [])
            except Exception as error:
                st.error(f"❌ Erro inesperado: {error}")
                self.state.set('wigen_board_items', [])
            self.clear_action()
            st.rerun()

        board_items = self.state.get('wigen_board_items') or []
        if not board_items:
            return

        wi_labels = {
            f"{item['id']} - {item['title']} ({item['type']}, {item['state']})": item
            for item in board_items
        }
        selected_ids = self.state.get('wigen_selected_ids') or []
        label_by_id = {item['id']: label for label, item in wi_labels.items()}
        current_labels = [label_by_id[wid] for wid in selected_ids if wid in label_by_id]

        selected_labels = st.multiselect(
            "🎯 Work Items para usar como especificação",
            options=list(wi_labels.keys()),
            default=current_labels,
            disabled=self.state.get('is_processing'),
            key="wigen_multiselect",
            help="Selecione quantos quiser — clique em vários seguidos, sem precisar segurar Ctrl/Shift.",
        )
        selected_ids = [wi_labels[label]['id'] for label in selected_labels]
        self.state.set('wigen_selected_ids', selected_ids)

        if not selected_labels:
            st.caption("Nenhum Work Item selecionado ainda — escolha acima.")
            return

        col_name, col_amb = st.columns(2)
        with col_name:
            project_name = st.text_input(
                "Nome do Test Plan *",
                value=self.state.get('project_name') or ado_project,
                key="wigen_project_name_input",
                disabled=self.state.get('is_processing'),
            )
        with col_amb:
            ambiente = st.radio(
                "Ambiente dos Testes *",
                options=["Homologação", "Produção"],
                index=None,
                key="wigen_ambiente_input",
                disabled=self.state.get('is_processing'),
                horizontal=True,
                help="Define a etiqueta (HML/PROD) usada no nome de cada Caso de Teste, na Matriz e na documentação.",
            )
        if ambiente:
            self.state.set('ambiente_testes', ambiente)

        with st.container(key="azure_blue_btn_confirm_wigen"):
            st.button(
                "✅ Confirmar e Gerar Especificação",
                type="primary",
                use_container_width=True,
                disabled=self.state.get('is_processing') or not project_name.strip() or not ambiente,
                key="btn_confirm_wigen",
                on_click=self.trigger_action,
                args=("confirm_wigen",),
            )
        if not ambiente:
            st.caption("Selecione o Ambiente dos Testes para habilitar a confirmação.")

        if self.state.get('current_action') == 'confirm_wigen' and not self.state.get('show_interrupt_modal'):
            try:
                with st.spinner(f"Buscando detalhes completos de {len(selected_ids)} Work Item(s)..."):
                    details = ado_client.get_work_items_full_details(selected_ids)
                if not details:
                    st.error("❌ Não foi possível buscar os detalhes dos Work Items selecionados.")
                    self.clear_action()
                else:
                    text_parts = []
                    for wi in details:
                        part = f"===== WORK ITEM {wi['id']} - {wi['title']} ({wi['type']}) =====\n"
                        if wi.get('description'):
                            part += f"Descrição:\n{wi['description']}\n"
                        if wi.get('acceptance_criteria'):
                            part += f"\nCritérios de Aceite:\n{wi['acceptance_criteria']}\n"
                        if not wi.get('description') and not wi.get('acceptance_criteria'):
                            part += "(Sem descrição ou critérios de aceite preenchidos neste Work Item)\n"
                        part += f"===== FIM DO WORK ITEM {wi['id']} ====="
                        text_parts.append(part)
                    text = "\n\n".join(text_parts)

                    self._log(
                        "Gerar a partir de Work Items", "Gerar a partir de Work Items",
                        f"Projeto '{project_name.strip()}' — {len(details)} Work Item(s): {', '.join(str(wi['id']) for wi in details)}",
                    )
                    self.state.set('show_wi_generation_page', False)
                    self._run_analysis(text, project_name.strip())
            except Exception as error:
                st.error(f"❌ Erro ao buscar detalhes dos Work Items: {error}")
                self.clear_action()

    def _wiql_generation_page(self):
        st.subheader("🔎 Criar Query no Azure DevOps com IA")
        st.caption(
            "Descreve em português o que você quer consultar — a IA traduz pra WIQL (a "
            "linguagem de query do Azure DevOps). Antes de criar qualquer coisa de verdade, "
            "você vê um preview de quantos itens a query traria, pra confirmar que é isso mesmo."
        )

        if st.button("← Voltar", key="btn_wiql_back_top"):
            self.state.set('show_wiql_generation_page', False)
            st.rerun()

        if not self._get_permission_cached("azure_devops"):
            st.error("❌ Você não tem permissão para acessar o Azure DevOps.")
            return

        conn = self._setup_azure_devops_connection()
        if conn is None:
            return
        ado_client, ado_org, ado_project, area_path = conn

        st.divider()
        descricao = st.text_area(
            "O que você quer consultar?",
            value=self.state.get('wiql_descricao', ''),
            key="wiql_descricao_input",
            height=100,
            placeholder="Ex.: Bugs abertos atribuídos a mim, criados nos últimos 30 dias",
            disabled=self.state.get('is_processing'),
        )
        self.state.set('wiql_descricao', descricao)

        with st.container(key="azure_blue_btn_generate_wiql"):
            st.button(
                "🤖 Gerar Query com IA",
                type="primary",
                use_container_width=True,
                disabled=self.state.get('is_processing') or not descricao.strip(),
                key="btn_generate_wiql",
                on_click=self.trigger_action,
                args=("generate_wiql",),
            )

        if self.state.get('current_action') == 'generate_wiql' and not self.state.get('show_interrupt_modal'):
            try:
                with st.spinner("Traduzindo sua descrição em uma query WIQL..."):
                    resp = self.client.trigger_wiql_generation(descricao.strip(), self.state.get('project_name') or ado_project)
                self.state.set('wiql_generated', resp)
                self.state.set('wiql_preview_result', None)
                st.session_state['wiql_titulo_input'] = resp.get('titulo_sugerido', '')
                st.session_state['wiql_text_input'] = resp.get('wiql', '')
            except Exception as error:
                st.error(f"❌ Não foi possível gerar a query: {error}")
            self.clear_action()
            st.rerun()

        generated = self.state.get('wiql_generated')
        if not generated:
            return

        st.divider()
        st.markdown("#### 📝 Revise antes de criar")
        st.info(f"**O que a IA entendeu:** {generated.get('explicacao', '—')}")

        if "wiql_titulo_input" not in st.session_state:
            st.session_state["wiql_titulo_input"] = generated.get('titulo_sugerido', '')
        titulo = st.text_input(
            "Nome da query",
            key="wiql_titulo_input",
            disabled=self.state.get('is_processing'),
        )
        if "wiql_text_input" not in st.session_state:
            st.session_state["wiql_text_input"] = generated.get('wiql', '')
        wiql_text = st.text_area(
            "Query WIQL (pode editar à mão se quiser ajustar algo)",
            key="wiql_text_input",
            height=150,
            disabled=self.state.get('is_processing'),
        )
        folder = st.selectbox(
            "Onde salvar",
            options=["My Queries", "Shared Queries"],
            key="wiql_folder_select",
            disabled=self.state.get('is_processing'),
            help="'My Queries' é pessoal, sempre funciona. 'Shared Queries' fica visível pro time todo, mas exige permissão de escrita nessa pasta compartilhada.",
        )

        with st.container(key="azure_blue_btn_preview_wiql"):
            st.button(
                "🔍 Testar Query (preview, não cria nada ainda)",
                use_container_width=True,
                disabled=self.state.get('is_processing') or not wiql_text.strip(),
                key="btn_preview_wiql",
                on_click=self.trigger_action,
                args=("preview_wiql",),
            )

        if self.state.get('current_action') == 'preview_wiql' and not self.state.get('show_interrupt_modal'):
            try:
                with st.spinner("Executando a query como teste..."):
                    preview = ado_client.run_wiql_query(wiql_text.strip())
                    ids_to_show = [item['id'] for item in preview['items'][:50]]
                    preview['details'] = ado_client.get_work_items_basic_fields(ids_to_show) if ids_to_show else []
                self.state.set('wiql_preview_result', preview)
            except AzureDevOpsError as error:
                st.error(f"❌ Erro na query: {error}")
                self.state.set('wiql_preview_result', None)
            except Exception as error:
                st.error(f"❌ Erro inesperado: {error}")
                self.state.set('wiql_preview_result', None)
            self.clear_action()
            st.rerun()

        preview = self.state.get('wiql_preview_result')
        if preview:
            st.success(f"✅ Essa query traria **{preview['count']}** Work Item(s). Nada foi salvo no Azure DevOps ainda.")

            details = preview.get('details') or []
            if details:
                rows = [
                    {"ID": d["id"], "Título": d["title"], "Tipo": d["type"], "Estado": d["state"]}
                    for d in details
                ]
                st.dataframe(rows, use_container_width=True, hide_index=True)
                if preview['count'] > len(details):
                    st.caption(f"Mostrando os primeiros {len(details)} de {preview['count']} itens.")

            with st.container(key="azure_blue_btn_confirm_wiql"):
                st.button(
                    "✅ Confirmar e Criar Query no Azure DevOps",
                    type="primary",
                    use_container_width=True,
                    disabled=self.state.get('is_processing') or not titulo.strip(),
                    key="btn_confirm_wiql",
                    on_click=self.trigger_action,
                    args=("confirm_wiql",),
                )

            if self.state.get('current_action') == 'confirm_wiql' and not self.state.get('show_interrupt_modal'):
                try:
                    with st.spinner(f"Criando a query '{titulo.strip()}' no Azure DevOps..."):
                        result = ado_client.create_shared_query(titulo.strip(), wiql_text.strip(), folder)
                    self._log(
                        "Criar Query WIQL", "Criar Query no Azure DevOps",
                        f"Projeto '{ado_project}' — query '{titulo.strip()}' em '{folder}'",
                    )
                    st.success(f"🎉 Query criada com sucesso em '{folder}'!")
                    if result.get('url'):
                        st.markdown(f"[Abrir a query no Azure DevOps]({result['url']})")
                    self.state.set('wiql_generated', None)
                    self.state.set('wiql_preview_result', None)
                    self.state.set('wiql_descricao', '')
                except AzureDevOpsError as error:
                    st.error(f"❌ Não foi possível criar a query: {error}")
                except Exception as error:
                    st.error(f"❌ Erro inesperado: {error}")
                self.clear_action()
        else:
            st.caption("Testa a query acima antes de poder confirmar a criação.")

    def _render_execution_report_section(self, ado_client, ado_project: str = "", area_paths: list = None):
        area_paths = area_paths or []
        st.markdown("### 📊 Relatório de Testes (execução)")
        st.caption(
            "Documenta o que foi EXECUTADO no Azure DevOps (diferente do PDF do Passo 6, que "
            "documenta o que foi planejado). Busca o Test Plan escolhido, os resultados de "
            "execução e as evidências (anexos) direto do Azure DevOps."
        )

        fetch_label = (
            f"🔍 Buscar Test Plans de {len(area_paths)} Area Path(s)" if area_paths
            else "🔍 Buscar Test Plans deste Projeto"
        )
        with st.container(key="azure_blue_btn_fetch_report_plans"):
            st.button(
                fetch_label,
                disabled=self.state.get('is_processing'),
                key="btn_fetch_report_plans",
                on_click=self.trigger_action,
                args=("fetch_report_plans",),
            )
        if self.state.get('current_action') == 'fetch_report_plans' and not self.state.get('show_interrupt_modal'):
            try:
                if area_paths:
                    with st.spinner(f"Buscando Test Plans em {len(area_paths)} Area Path(s)..."):
                        plans_by_id = {}
                        for ap in area_paths:
                            for p in ado_client.list_test_plans_for_area_path(ap):
                                plans_by_id[p["id"]] = p
                        plans = list(plans_by_id.values())
                else:
                    with st.spinner("Buscando Test Plans..."):
                        plans = ado_client.list_test_plans()
                self.state.set('report_available_plans', plans)
                if not plans:
                    st.warning("Nenhum Test Plan encontrado" + (" nessas Area Paths." if area_paths else " neste projeto."))
            except AzureDevOpsError as error:
                st.error(f"❌ {error}")
                self.state.set('report_available_plans', [])
            except Exception as error:
                st.error(f"❌ Erro inesperado: {error}")
                self.state.set('report_available_plans', [])
            self.clear_action()
            st.rerun()

        available_plans = self.state.get('report_available_plans') or []
        if not available_plans:
            return

        plan_labels = {f"{p['id']} - {p['name']}": p for p in available_plans}
        chosen_labels = st.multiselect(
            "Test Plan(s) a reportar",
            options=list(plan_labels.keys()),
            disabled=self.state.get('is_processing'),
            key="report_plan_select",
            help="Selecione um ou mais Test Plans — os resultados de todos entram juntos no mesmo relatório.",
        )
        chosen_plans = [plan_labels[label] for label in chosen_labels]

        if not chosen_plans:
            st.caption("Nenhum Test Plan selecionado ainda — escolha acima.")
            return

        with st.container(key="azure_blue_btn_suggest_narrative"):
            st.button(
                "🤖 Sugerir Contexto/Escopo/Conclusão com IA",
                disabled=self.state.get('is_processing'),
                key="btn_suggest_report_narrative",
                on_click=self.trigger_action,
                args=("suggest_report_narrative",),
                help="A IA analisa os resultados desses Test Plans e sugere os textos abaixo — você revisa e edita antes de gerar o PDF.",
            )
        if self.state.get('current_action') == 'suggest_report_narrative' and not self.state.get('show_interrupt_modal'):
            self._suggest_report_narrative(ado_client, chosen_plans)

        st.caption("Os campos abaixo já vêm com sugestão da IA (se você clicou no botão acima) — revise e edite livremente antes de gerar o PDF.")

        col1, col2 = st.columns(2)
        with col1:
            if "report_contexto_input" not in st.session_state:
                st.session_state["report_contexto_input"] = self.state.get('report_contexto', '')
            contexto = st.text_input(
                "Contexto",
                key="report_contexto_input",
                help="Ex.: 'Testes de regressão pós-deploy da Sprint 14'",
            )
        with col2:
            ambiente_opts = ["Homologação", "Produção"]
            session_ambiente = self.state.get('ambiente_testes', '')
            if session_ambiente in ambiente_opts:
                ambiente_default = ambiente_opts.index(session_ambiente)
                help_text = "Pré-selecionado com base no Ambiente escolhido na geração desta sessão — confirme antes de gerar."
            else:
                ambiente_default = 1 if any("prod" in p["name"].lower() for p in chosen_plans) else 0
                help_text = "Pré-selecionado por um palpite a partir do nome do Test Plan — confirme antes de gerar."
            ambiente = st.selectbox(
                "Ambiente",
                options=ambiente_opts,
                index=ambiente_default,
                disabled=self.state.get('is_processing'),
                key="report_ambiente_select",
                help=help_text,
            )
        self.state.set('report_contexto', contexto)

        if "report_escopo_input" not in st.session_state:
            st.session_state["report_escopo_input"] = self.state.get('report_escopo', '')
        escopo_proposito = st.text_area(
            "Escopo e Propósito",
            key="report_escopo_input",
            help="Explique brevemente o escopo e o propósito dos testes executados.",
            height=100,
        )
        self.state.set('report_escopo', escopo_proposito)

        if "report_conclusao_input" not in st.session_state:
            st.session_state["report_conclusao_input"] = self.state.get('report_conclusao', '')
        conclusao = st.text_area(
            "Conclusão",
            key="report_conclusao_input",
            height=100,
        )
        self.state.set('report_conclusao', conclusao)

        if "report_proximos_input" not in st.session_state:
            st.session_state["report_proximos_input"] = self.state.get('report_proximos', '')
        proximos_passos = st.text_area(
            "Próximos Passos e Sugestões (opcional)",
            key="report_proximos_input",
            height=80,
        )
        self.state.set('report_proximos', proximos_passos)

        status_manual = st.radio(
            "Status do Relatório *",
            options=["Aprovado", "Cancelado", "Pendente"],
            index=None,
            key="report_status_manual_select",
            disabled=self.state.get('is_processing'),
            horizontal=True,
            help="Você define o status final do relatório diretamente — não depende mais do cálculo automático pela coluna do board.",
        )

        with st.container(key="azure_blue_btn_generate_report"):
            st.button(
                "📊 Buscar Resultados e Gerar Relatório",
                type="primary",
                use_container_width=True,
                disabled=self.state.get('is_processing') or not contexto or not escopo_proposito or not conclusao or not status_manual,
                key="btn_generate_execution_report",
                on_click=self.trigger_action,
                args=("generate_execution_report",),
            )
        if not (contexto and escopo_proposito and conclusao and status_manual):
            st.caption("Preencha Contexto, Escopo e Propósito, Conclusão, e escolha o Status para habilitar a geração.")

        if self.state.get('current_action') == 'generate_execution_report' and not self.state.get('show_interrupt_modal'):
            self._generate_execution_report(ado_client, chosen_plans, contexto, ambiente, escopo_proposito, conclusao, proximos_passos, ado_project, area_paths, status_manual)

        report_bytes = self.state.get('report_pdf_bytes')
        if report_bytes:
            safe_name = (self.state.get('project_name') or 'projeto').replace(' ', '_')
            st.download_button(
                "⬇️ Baixar Relatório de Testes (PDF)",
                data=report_bytes,
                file_name=f"Relatorio_Testes_{safe_name}.pdf",
                mime="application/pdf",
                use_container_width=True,
                type="primary",
            )
            for warn in self.state.get('report_warnings') or []:
                st.caption(f"ℹ️ {warn}")

    def _suggest_report_narrative(self, ado_client, plans: list):
        EXCLUDE_TYPES = {"test plan", "test suite", "test case"}
        # Vocabulário real da coluna "Outcome" da aba Execute das Suites de
        # Teste no Azure DevOps — usado tal e qual, sem lumping genérico.
        OUTCOME_LABELS = {
            "Passed": "Aprovado", "Failed": "Reprovado", "Active": "Ativo (não iniciado)",
            "Paused": "Pausado", "Blocked": "Bloqueado", "NotApplicable": "Não Aplicável",
            "Not Run": "Não Executado",
        }
        try:
            plan_names = ", ".join(p['name'] for p in plans)
            with st.spinner("Analisando resultados dos Test Plans para sugerir os textos..."):
                total = 0
                by_outcome = {}       # outcome bruto -> contagem
                titles_by_outcome = {}  # outcome bruto -> [títulos dos casos]
                all_case_ids = []
                for plan in plans:
                    summary = ado_client.get_test_plan_execution_summary(plan["id"])
                    points = summary.get("points", [])
                    total += len(points)
                    for p in points:
                        outcome = p.get("outcome") or "Not Run"
                        by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
                        titles_by_outcome.setdefault(outcome, []).append(p.get("case_title", ""))
                    all_case_ids.extend(p.get("case_id") for p in points if p.get("case_id"))

                linhas_resumo = [f"{total} casos de teste no(s) Test Plan(s) '{plan_names}', por status (aba Execute das Suítes):"]
                for outcome_raw, count in sorted(by_outcome.items(), key=lambda x: -x[1]):
                    label = OUTCOME_LABELS.get(outcome_raw, outcome_raw)
                    titulos = titles_by_outcome.get(outcome_raw, [])
                    # Lista até 5 títulos por status, pra IA poder citar casos
                    # específicos (ex.: qual bug está bloqueando o quê) sem
                    # o prompt virar uma lista infinita em Test Plans grandes.
                    amostra = "; ".join(t for t in titulos[:5] if t)
                    extra = f" (e mais {len(titulos) - 5})" if len(titulos) > 5 else ""
                    linha = f"- {label} ({outcome_raw}): {count} caso(s)"
                    if amostra:
                        linha += f" — ex.: {amostra}{extra}"
                    linhas_resumo.append(linha)
                resumo_resultados = "\n".join(linhas_resumo)

                # Contexto deve ser baseado na descrição real dos Work Items
                # testados (User Stories, Bugs, Features etc.) — não em Test
                # Plan/Suite/Case, que não descrevem negócio nenhum.
                wi_ids = set()
                for case_id in all_case_ids:
                    try:
                        wi_ids.update(ado_client.get_tested_work_item_ids(case_id))
                    except Exception:
                        pass  # um caso sem vínculo não deve travar a sugestão inteira

                descricoes_texto = ""
                if wi_ids:
                    details = ado_client.get_work_items_full_details(list(wi_ids))
                    relevantes = [d for d in details if (d.get('type') or '').strip().lower() not in EXCLUDE_TYPES]
                    partes = []
                    for d in relevantes:
                        desc = (d.get('description') or '').strip()
                        if desc:
                            partes.append(f"[{d.get('type')}] {d.get('title')}: {desc[:500]}")
                    descricoes_texto = "\n\n".join(partes)

                resp = self.client.trigger_execution_report_narrative(
                    nome_projeto=self.state.get('project_name') or plan_names,
                    nome_plano=plan_names,
                    resumo_resultados=resumo_resultados,
                    matriz=self.state.get('matriz') or [],
                    descricoes_work_items=descricoes_texto,
                )

            contexto = resp.get('contexto', '')
            escopo = resp.get('escopo_proposito', '')
            conclusao = resp.get('conclusao', '')
            proximos = resp.get('proximos_passos', '')

            self.state.set('report_contexto', contexto)
            self.state.set('report_escopo', escopo)
            self.state.set('report_conclusao', conclusao)
            self.state.set('report_proximos', proximos)

            # Streamlit só respeita "value=" na primeira renderização do
            # widget — depois disso, precisa sobrescrever o session_state
            # do próprio widget diretamente pra sugestão da IA aparecer.
            st.session_state['report_contexto_input'] = contexto
            st.session_state['report_escopo_input'] = escopo
            st.session_state['report_conclusao_input'] = conclusao
            st.session_state['report_proximos_input'] = proximos
        except Exception as error:
            st.error(f"❌ Não foi possível gerar a sugestão da IA: {error}")

        self.clear_action()
        st.rerun()

    def _generate_execution_report(self, ado_client, plans: list, contexto: str, ambiente: str,
                                     escopo_proposito: str, conclusao: str, proximos_passos: str,
                                     ado_project: str = "", area_paths: list = None, status_manual: str = ""):
        area_paths = area_paths or []
        warnings = []
        evidencias_por_caso = {}
        casos = []  # [{"titulo", "outcome", "suite_name"}] — direto do Azure DevOps
        plan_names = ", ".join(p['name'] for p in plans)

        # Nome usado na documentação: se uma ou mais Area Paths DE VERDADE
        # foram escolhidas, usa o(s) nome(s) delas — só cai pro nome do
        # Projeto se nenhuma Area Path foi selecionada.
        if area_paths:
            report_project_name = ", ".join(area_paths)
        else:
            report_project_name = self.state.get('project_name') or ado_project or plan_names

        try:
            all_points = []
            with st.spinner(f"Buscando Casos de Teste de {len(plans)} Test Plan(s)..."):
                for plan in plans:
                    summary = ado_client.get_test_plan_execution_summary(plan["id"])
                    warnings.extend(summary.get("warnings", []))
                    all_points.extend(summary.get("points", []))

            with st.spinner(f"Consultando status de QA (coluna do board) de {len(all_points)} caso(s)..."):
                statuses_seen = set()
                all_wi_ids = set()
                for point in all_points:
                    titulo = point.get("case_title") or f"Caso #{point.get('case_id')}"
                    case_id = point.get("case_id")

                    # Status vem da coluna do board do(s) Work Item(s) que
                    # esse Caso de Teste testa — não do outcome de execução
                    # do Test Point (que nem sempre reflete a realidade de
                    # como o time trabalha).
                    qa_status = "Desconhecido"
                    if case_id:
                        try:
                            wi_ids = ado_client.get_tested_work_item_ids(case_id)
                            all_wi_ids.update(wi_ids)
                            wi_statuses = set()
                            for wi_id in wi_ids:
                                try:
                                    wi_statuses.add(ado_client.get_work_item_qa_status(wi_id))
                                except Exception as error:
                                    warnings.append(f"Falha ao checar status do Work Item {wi_id} (caso '{titulo}'): {error}")
                            for prioridade in ["Cancelado", "Reprovado", "Aprovado", "Pendente"]:
                                if prioridade in wi_statuses:
                                    qa_status = prioridade
                                    break
                            if not wi_ids:
                                warnings.append(f"Caso '{titulo}': nenhum Work Item vinculado encontrado — status ficou 'Desconhecido'.")
                        except Exception as error:
                            warnings.append(f"Falha ao buscar Work Items vinculados ao caso '{titulo}': {error}")

                    casos.append({
                        "titulo": titulo,
                        "outcome": qa_status,
                        "suite_name": point.get("suite_name", "—"),
                    })
                    statuses_seen.add(qa_status)

                    if case_id:
                        try:
                            imgs, img_warnings = ado_client.get_test_case_attachments(case_id)
                            if imgs:
                                evidencias_por_caso[titulo] = imgs
                            warnings.extend(img_warnings)
                        except Exception as error:
                            warnings.append(f"Falha ao buscar imagens dos steps de '{titulo}': {error}")

            # Status GERAL do relatório: agora é escolhido manualmente por
            # você antes de gerar (o cálculo automático pela coluna do
            # board continua alimentando o status de CADA caso individual
            # na seção "Casos de Teste", só o resumo geral do topo é que
            # passou a ser sua decisão direta).
            status_geral = status_manual or "Pendente"

            # A Matriz de Cobertura nunca é enviada pro Azure DevOps, então
            # só existe se ESTA sessão gerou o mesmo projeto que está sendo
            # reportado. Confirma isso batendo pelo menos um título de caso
            # em comum antes de incluir — evita mostrar a Matriz errada de
            # um projeto diferente.
            session_matriz = self.state.get('matriz') or []
            session_case_titles = {tc.get('titulo', '') for tc in (self.state.get('test_cases') or [])}
            azure_case_titles = {c['titulo'] for c in casos}
            matriz_to_use = session_matriz if (session_matriz and session_case_titles & azure_case_titles) else []
            if session_matriz and not matriz_to_use:
                warnings.append(
                    "A Matriz de Cobertura desta sessão parece ser de um projeto diferente do Test "
                    "Plan selecionado — não foi incluída no relatório."
                )

            # Sem Matriz de sessão disponível? Monta uma Matriz INDEPENDENTE
            # a partir dos Work Items de verdade vinculados aos Casos de
            # Teste no Azure DevOps — funciona mesmo sem nada ter sido
            # gerado nesta sessão do app.
            if not matriz_to_use and all_wi_ids:
                try:
                    with st.spinner(f"Montando Matriz de Cobertura a partir de {len(all_wi_ids)} Work Item(s) vinculado(s)..."):
                        details = ado_client.get_work_items_full_details(list(all_wi_ids))
                        EXCLUDE_TYPES_MTX = {"test plan", "test suite", "test case"}
                        relevantes = [d for d in details if (d.get('type') or '').strip().lower() not in EXCLUDE_TYPES_MTX]
                        CATEGORIA_POR_TIPO = {
                            "bug": "Correção de Defeito",
                            "user story": "Fluxo Funcional",
                            "product backlog item": "Fluxo Funcional",
                            "feature": "Fluxo Funcional",
                            "task": "Tarefa Técnica",
                        }
                        sigla = self._env_sigla()
                        matriz_independente = []
                        for idx, wi in enumerate(relevantes, start=1):
                            mc_id = f"MC-{idx:03d}" + (f" {sigla}" if sigla else "")
                            wi_type_label = wi.get('type', '') or ''
                            categoria = CATEGORIA_POR_TIPO.get(wi_type_label.strip().lower(), wi_type_label or "—")
                            descricao = (wi.get('description') or '').strip()
                            matriz_independente.append({
                                "id": mc_id,
                                "funcionalidade": wi.get('title', '') or '—',
                                "requisito": f"{wi_type_label} #{wi.get('id', '')}".strip(),
                                "cenario": (descricao[:200] + "…") if len(descricao) > 200 else (descricao or "—"),
                                "categoria": categoria,
                                "prioridade": "—",
                                "criticidade": "—",
                            })
                    if matriz_independente:
                        matriz_to_use = matriz_independente
                        warnings.append(
                            f"Matriz de Cobertura montada de forma independente, a partir de "
                            f"{len(matriz_independente)} Work Item(s) vinculado(s) diretamente no Azure "
                            f"DevOps (não depende de nada ter sido gerado nesta sessão)."
                        )
                except Exception as error:
                    warnings.append(f"Não foi possível montar a Matriz independente: {error}")

            with st.spinner("Gerando o PDF do Relatório de Testes..."):
                pdf_bytes = PdfReportGenerator.generate_execution_report(
                    project_name=report_project_name,
                    contexto=contexto,
                    ambiente=ambiente,
                    status_geral=status_geral,
                    escopo_proposito=escopo_proposito,
                    casos=casos,
                    evidencias_por_caso=evidencias_por_caso,
                    conclusao=conclusao,
                    proximos_passos=proximos_passos,
                    matriz=matriz_to_use,
                    author_name=self.state.get('author_name', ''),
                )
            self.state.set('report_pdf_bytes', pdf_bytes)
            self.state.set('report_warnings', warnings)
            self._log("Gerar Relatório de Testes", "Relatório de Testes", f"Test Plan(s) '{plan_names}' — status: {status_geral}")
        except AzureDevOpsError as error:
            st.error(f"❌ {error}")
        except Exception as error:
            st.error(f"❌ Erro inesperado ao gerar o relatório: {error}")

        self.clear_action()
        st.rerun()

    def _suggest_ado_links(self, ado_client, board_items: list, test_cases: list):
        # Casos que já vieram marcados no Passo 1 (documento vinculado
        # diretamente a um Work Item) não precisam de sugestão da IA — o
        # vínculo já é conhecido, então pré-preenche direto.
        board_ids = {item["id"] for item in board_items}
        pre_linked = {}
        pre_linked_titles = set()
        for tc in test_cases:
            wi_raw = str(tc.get("work_item_relacionado") or "").strip()
            if not wi_raw:
                continue
            try:
                wi_id = int(wi_raw)
            except ValueError:
                continue
            if wi_id not in board_ids:
                continue  # marcado pra um Work Item que não está nesta busca — ignora
            titulo = tc.get("titulo", "")
            pre_linked.setdefault(str(wi_id), []).append(titulo)
            pre_linked_titles.add(titulo)

        # Busca, pra cada Work Item, quais Casos de Teste JÁ estão vinculados
        # a ele no Azure DevOps — isso vai como contexto pro n8n, pra IA
        # evitar sugerir um caso novo que já é essencialmente o que já existe.
        existing_by_wid = {}
        try:
            with st.spinner("Verificando Casos de Teste já existentes nos Work Items..."):
                for item in board_items:
                    try:
                        existing_by_wid[item["id"]] = ado_client.get_existing_test_case_titles(item["id"])
                    except AzureDevOpsError:
                        existing_by_wid[item["id"]] = []
        except Exception:
            existing_by_wid = {item["id"]: [] for item in board_items}

        payload_items = [
            {
                "id": item["id"],
                "title": item["title"],
                "type": item["type"],
                "state": item["state"],
                "casos_existentes": existing_by_wid.get(item["id"], []),
            }
            for item in board_items
        ]
        # Só manda pra IA os Casos que AINDA NÃO têm um Work Item conhecido
        # — não faz sentido pedir sugestão pra algo que já foi declarado
        # explicitamente lá no Passo 1.
        payload_cases = [
            {
                "titulo": tc.get("titulo", ""),
                "pre_condicoes": tc.get("pre_condicoes", ""),
                "passos": tc.get("passos", []),
            }
            for tc in test_cases
            if tc.get("titulo", "") not in pre_linked_titles
        ]
        try:
            if payload_cases:
                with st.spinner("Consultando a IA (n8n) para sugerir os vínculos..."):
                    result = self.client.trigger_matching(payload_items, payload_cases, self.state.get('project_name'))
            else:
                result = {"vinculos": []}
            links = {}
            skipped = 0
            for vinculo in result.get("vinculos", []):
                # A IA às vezes devolve o item como string JSON em vez de objeto —
                # tenta decodificar antes de desistir dele.
                if isinstance(vinculo, str):
                    try:
                        vinculo = json.loads(vinculo)
                    except (ValueError, TypeError):
                        skipped += 1
                        continue
                if not isinstance(vinculo, dict):
                    skipped += 1
                    continue

                wid = vinculo.get("work_item_id")
                casos = vinculo.get("casos", [])
                if isinstance(casos, str):
                    try:
                        casos = json.loads(casos)
                    except (ValueError, TypeError):
                        casos = [casos]
                if not isinstance(casos, list):
                    casos = []

                if wid is None:
                    skipped += 1
                    continue
                try:
                    wid_int = int(str(wid).strip())
                except (ValueError, TypeError):
                    skipped += 1
                    continue
                links[str(wid_int)] = casos

            # Rede de segurança: mesmo com o prompt ajustado, a IA ainda pode
            # devolver o mesmo Caso de Teste vinculado a vários Work Items.
            # Por padrão, cada caso deve pertencer a só 1 Work Item — mantém
            # só a PRIMEIRA ocorrência (na ordem em que a IA respondeu, que
            # tende a ser o vínculo mais forte) e remove o caso dos demais.
            # O usuário ainda pode adicionar vínculos extras manualmente na
            # revisão abaixo, se for um caso genuinamente excepcional.
            seen_cases = set()
            deduped_links = {}
            duplicates_removed = 0
            for wid_key, casos in links.items():
                kept = []
                for c in casos:
                    if c in seen_cases:
                        duplicates_removed += 1
                        continue
                    seen_cases.add(c)
                    kept.append(c)
                if kept:
                    deduped_links[wid_key] = kept
            links = deduped_links

            # Rede de segurança #2: remove sugestões de casos muito
            # parecidos com um Caso de Teste QUE JÁ EXISTE naquele Work Item
            # no Azure DevOps (buscado no início desta função). Evita duplicar
            # cobertura de teste que já foi feita antes.
            SIMILARITY_THRESHOLD = 0.80
            duplicate_case_titles = set()
            final_links = {}
            for wid_key, casos in links.items():
                existentes = existing_by_wid.get(int(wid_key), [])
                kept = []
                for c in casos:
                    c_norm = c.strip().lower()
                    is_dup = any(
                        difflib.SequenceMatcher(None, c_norm, e.strip().lower()).ratio() >= SIMILARITY_THRESHOLD
                        for e in existentes
                    )
                    if is_dup:
                        duplicate_case_titles.add(c)
                    else:
                        kept.append(c)
                if kept:
                    final_links[wid_key] = kept
            links = final_links

            # Mescla os vínculos já conhecidos desde o Passo 1 (Casos vindos
            # de documento marcado com Work Item) — esses não passaram pela
            # IA, então entram direto, sem risco de conflito de exclusividade
            # (já foram excluídos do que a IA recebeu pra sugerir).
            for wid_key, titulos in pre_linked.items():
                links.setdefault(wid_key, [])
                for t in titulos:
                    if t not in links[wid_key]:
                        links[wid_key].append(t)

            self.state.set('ado_duplicate_case_titles', sorted(duplicate_case_titles))

            self.state.set('ado_wi_case_links', links)

            # Força a atualização visual dos multiselects: como eles já foram
            # renderizados antes (vazios), só mudar o estado "lógico" acima não
            # é o suficiente — precisa sobrescrever o session_state de cada
            # widget diretamente pra sugestão da IA aparecer nos campos.
            case_titles_valid = {tc.get("titulo", "") for tc in test_cases}
            for item in board_items:
                wid_key = str(item["id"])
                st.session_state[f"ado_wi_multiselect_{item['id']}"] = [
                    c for c in links.get(wid_key, []) if c in case_titles_valid
                ]

            if links:
                msg = ("success", f"✅ IA sugeriu vínculos para {len(links)} Work Item(s). Revise abaixo antes de confirmar.")
            else:
                msg = ("warning", "⚠️ A IA não sugeriu nenhum vínculo válido. Você pode montar manualmente abaixo.")
            if pre_linked_titles:
                msg = (msg[0], msg[1] + f" ({len(pre_linked_titles)} Caso(s) já vieram pré-vinculados do Passo 1, sem precisar da IA.)")
            if skipped:
                msg = (msg[0], msg[1] + f" ({skipped} item(ns) da resposta da IA vieram em formato inesperado e foram ignorados.)")
            if duplicates_removed:
                msg = (
                    msg[0],
                    msg[1] + f" ({duplicates_removed} vínculo(s) duplicado(s) — mesmo caso em vários Work "
                    "Items — foram reduzidos a 1 vínculo por padrão; adicione manualmente na revisão abaixo se for exceção real.)",
                )
            if duplicate_case_titles:
                msg = (
                    msg[0],
                    msg[1] + f" ⚠️ {len(duplicate_case_titles)} caso(s) parecem duplicar Casos de Teste que JÁ "
                    "existem no Work Item correspondente no Azure DevOps — não serão vinculados nem criados "
                    "(veja a lista abaixo).",
                )
            self.state.set('ado_suggest_message', msg)
        except ValueError as error:
            self.state.set('ado_suggest_message', ("error", f"❌ {error}"))
        except Exception as error:
            self.state.set('ado_suggest_message', ("error", f"❌ Erro inesperado ao consultar sugestão da IA: {error}"))

    def _push_full_azure_devops(self, ado_client, area_path: str, plan_name: str, initial_state: str = None, existing_plan_id: int = None):
        test_cases = self.state.get('test_cases') or []
        project_name = self.state.get('project_name') or "QA TestGen"
        case_ids = dict(self.state.get('ado_test_case_ids') or {})
        case_links = dict(self.state.get('ado_case_links') or {})
        links = self.state.get('ado_wi_case_links') or {}
        excluded_titles = set(self.state.get('ado_excluded_case_titles') or [])
        log = []

        if excluded_titles:
            log.append(f"🚫 {len(excluded_titles)} Caso(s) excluído(s) do envio, por escolha sua: {', '.join(excluded_titles)}")

        items_with_cases = {
            wid: [c for c in casos if c not in excluded_titles]
            for wid, casos in links.items()
        }
        items_with_cases = {wid: casos for wid, casos in items_with_cases.items() if casos}

        # Mesma numeração CT01, CT02... usada nos CSVs, aplicada também aqui —
        # a chave interna (case_ids, links, etc.) continua sendo o título
        # ORIGINAL do caso; só o texto enviado como Title pro Azure DevOps é
        # que leva o prefixo.
        titled = AzureCsvFormatter._titled(test_cases, self.state.get('ambiente_testes', ''))
        MAX_WORKERS = 4  # nº de chamadas simultâneas à API do Azure DevOps (reduzido — 8 causava reset de conexão)

        # 1) Garante que TODOS os Casos de Teste gerados existem no Azure DevOps,
        # vinculados a algum Work Item ou não — casos sem vínculo são criados
        # normalmente, só não entram em nenhuma Suite depois. Casos marcados
        # como duplicados de algo que já existe no Azure DevOps (checagem
        # feita em _suggest_ado_links), ou marcados como excluídos por você,
        # são pulados — não sobem pro Azure.
        # As criações são independentes entre si, então rodam em paralelo.
        duplicate_titles = set(self.state.get('ado_duplicate_case_titles') or [])
        skipped_as_duplicate = [tc.get('titulo') for tc in test_cases if tc.get('titulo') in duplicate_titles]
        if skipped_as_duplicate:
            log.append(
                f"🔁 {len(skipped_as_duplicate)} Caso(s) não foram criados por parecerem duplicados de "
                f"algo já existente no Azure DevOps: {', '.join(skipped_as_duplicate)}"
            )
        cases_to_create = [
            tc for tc in test_cases
            if tc.get('titulo') not in case_ids
            and tc.get('titulo') not in duplicate_titles
            and tc.get('titulo') not in excluded_titles
        ]
        if cases_to_create:
            total = len(cases_to_create)
            done = 0
            progress = st.progress(0, text=f"Criando Test Cases no Azure DevOps... (0/{total})")

            def _create_case(tc):
                titulo = tc.get('titulo')
                titulo_prefixado = titled.get(titulo, titulo)
                result = ado_client.create_test_case(
                    titulo_prefixado, tc.get('pre_condicoes', ''), tc.get('passos', []), area_path, initial_state
                )
                return titulo, titulo_prefixado, result["id"], result.get("state_warning")

            with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, total)) as executor:
                futures = {executor.submit(_create_case, tc): tc for tc in cases_to_create}
                for future in as_completed(futures):
                    tc = futures[future]
                    titulo_original = tc.get('titulo')
                    titulo_prefixado = titled.get(titulo_original, titulo_original)
                    try:
                        titulo, _, wid, state_warning = future.result()
                        case_ids[titulo] = wid
                        log.append(f"✅ Test Case criado: **{titulo_prefixado}** (ID {wid})")
                        if state_warning:
                            log.append(f"&nbsp;&nbsp;⚠️ {state_warning}")
                    except AzureDevOpsError as error:
                        log.append(f"❌ Falha ao criar Test Case '{titulo_prefixado}': {error}")
                    except Exception as error:
                        log.append(f"❌ Erro inesperado ao criar Test Case '{titulo_prefixado}': {error}")
                    done += 1
                    progress.progress(done / total, text=f"Criando Test Cases no Azure DevOps... ({done}/{total})")
            self.state.set('ado_test_case_ids', case_ids)

        # 2) Test Plan: cria um novo, ou reaproveita um já existente (modo
        # "merge" — a pessoa escolheu isso no Passo 7).
        if existing_plan_id:
            plan_id = existing_plan_id
            try:
                with st.spinner(f"Buscando suite raiz do Test Plan existente '{plan_name}'..."):
                    root_suite_id = ado_client.get_test_plan_root_suite(plan_id)
                log.append(f"♻️ Reaproveitando Test Plan existente: **{plan_name}** (ID {plan_id})")
            except AzureDevOpsError as error:
                log.append(f"❌ Falha ao buscar detalhes do Test Plan existente: {error}")
                self.state.set('ado_full_push_log', log)
                self.clear_action()
                st.rerun()
                return
            except Exception as error:
                log.append(f"❌ Erro inesperado ao buscar Test Plan existente: {error}")
                self.state.set('ado_full_push_log', log)
                self.clear_action()
                st.rerun()
                return

            try:
                with st.spinner("Verificando Suites já existentes neste Test Plan (evita duplicar)..."):
                    existing_suite_by_wi = ado_client.get_existing_requirement_suite_ids(plan_id)
            except Exception as error:
                log.append(f"⚠️ Não foi possível checar Suites já existentes — pode gerar Suite duplicada: {error}")
                existing_suite_by_wi = {}
        else:
            try:
                plan = ado_client.create_test_plan(plan_name, f"Gerado automaticamente pelo QA TestGen para {project_name}")
                plan_id = plan["id"]
                root_suite_id = plan.get("root_suite_id")
                log.append(f"✅ Test Plan criado: **{plan_name}** (ID {plan_id})")
            except AzureDevOpsError as error:
                log.append(f"❌ Falha ao criar Test Plan: {error}")
                self.state.set('ado_full_push_log', log)
                self.clear_action()
                st.rerun()
                return
            except Exception as error:
                log.append(f"❌ Erro inesperado ao criar Test Plan: {error}")
                self.state.set('ado_full_push_log', log)
                self.clear_action()
                st.rerun()
                return
            existing_suite_by_wi = {}

        if not root_suite_id:
            log.append("⚠️ Não recebi o ID da suite raiz do plano — não é possível criar as Requirement Suites.")
            self.state.set('ado_full_push_log', log)
            self.clear_action()
            st.rerun()
            return

        # 3a) Cria as Requirement Suites, uma por Work Item com casos — mas
        # só pros Work Items que AINDA NÃO têm uma Suite neste plano (regra
        # do "merge"). A Suite que já existe continua funcionando sozinha:
        # como ela já "puxa" qualquer Caso de Teste vinculado ("Tests") ao
        # Work Item dela, os Casos NOVOS aparecem lá automaticamente assim
        # que o vínculo é criado no passo 3b — não precisa mexer na Suite.
        # IMPORTANTE: criação de Suite precisa ser SEQUENCIAL — todas são
        # filhas do mesmo Suite raiz do plano, e criar várias ao mesmo tempo
        # em paralelo faz o Azure DevOps rejeitar com erro de concorrência
        # (TF26071: "changed by someone else since you opened it"), porque
        # múltiplas escritas concorrentes tentam atualizar o mesmo pai.
        suite_tasks = list(items_with_cases.items())  # [(work_item_id_str, [titulos]), ...]
        if suite_tasks:
            total_suites = len(suite_tasks)
            progress2 = st.progress(0, text=f"Verificando/criando Suites no Azure DevOps... (0/{total_suites})")
            for idx, (wid_str, _casos) in enumerate(suite_tasks, start=1):
                work_item_id = int(wid_str)
                if work_item_id in existing_suite_by_wi:
                    log.append(
                        f"♻️ Work Item {work_item_id} já tinha Suite neste Test Plan "
                        f"(Suite ID {existing_suite_by_wi[work_item_id]}) — Casos novos entram nela automaticamente."
                    )
                else:
                    try:
                        suite_id = ado_client.create_requirement_based_suite(plan_id, root_suite_id, work_item_id)
                        log.append(f"✅ Suite criada para Work Item {work_item_id} (Suite ID {suite_id})")
                    except AzureDevOpsError as error:
                        log.append(f"❌ Falha ao criar Suite para Work Item {work_item_id}: {error}")
                    except Exception as error:
                        log.append(f"❌ Erro inesperado ao criar Suite para Work Item {work_item_id}: {error}")
                progress2.progress(idx / total_suites, text=f"Verificando/criando Suites no Azure DevOps... ({idx}/{total_suites})")

        # 3b) Vincula os Casos de Teste aos Work Items (link "Tests", não
        # depende da Suite existir). Isso é seguro em paralelo ENTRE casos
        # diferentes (cada um é um Work Item distinto) — mas vínculos do
        # MESMO caso (quando ele vai pra mais de um Work Item) escrevem no
        # mesmo Test Case, então esses ficam agrupados e rodam em sequência
        # entre si pra não colidir.
        links_by_case = {}
        for wid_str, casos in items_with_cases.items():
            work_item_id = int(wid_str)
            for titulo in casos:
                case_id = case_ids.get(titulo)
                if not case_id:
                    log.append(f"⚠️ Caso '{titulo}' não existe no Azure DevOps, pulando vínculo com Work Item {work_item_id}.")
                    continue
                if work_item_id in case_links.get(titulo, []):
                    log.append(f"↪️ '{titulo}' já estava vinculado ao Work Item {work_item_id}")
                    continue
                links_by_case.setdefault(titulo, []).append((work_item_id, case_id))

        if links_by_case:
            total_cases_to_link = len(links_by_case)
            done = 0
            progress3 = st.progress(0, text=f"Vinculando Casos de Teste no Azure DevOps... (0/{total_cases_to_link})")

            def _link_one_case(titulo, pares):
                # pares = [(work_item_id, case_id), ...] — mesmo case_id em todos,
                # processados em sequência entre si (mesmo Test Case sendo escrito).
                resultados = []
                for work_item_id, case_id in pares:
                    try:
                        ado_client.link_test_case_to_work_item(case_id, work_item_id)
                        resultados.append((work_item_id, case_id, None))
                    except AzureDevOpsError as error:
                        resultados.append((work_item_id, case_id, error))
                    except Exception as error:
                        resultados.append((work_item_id, case_id, error))
                return titulo, resultados

            with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, total_cases_to_link)) as executor:
                futures = {
                    executor.submit(_link_one_case, titulo, pares): titulo
                    for titulo, pares in links_by_case.items()
                }
                for future in as_completed(futures):
                    titulo, resultados = future.result()
                    for work_item_id, case_id, error in resultados:
                        if error is None:
                            case_links.setdefault(titulo, []).append(work_item_id)
                            log.append(f"↳ '{titulo}' (Caso {case_id}) vinculado ao Work Item {work_item_id}")
                        else:
                            log.append(f"❌ Falha ao vincular '{titulo}' ao Work Item {work_item_id}: {error}")
                    done += 1
                    progress3.progress(
                        done / total_cases_to_link,
                        text=f"Vinculando Casos de Teste no Azure DevOps... ({done}/{total_cases_to_link})",
                    )

        self.state.set('ado_case_links', case_links)
        log.append(f"\n🔗 Confira o Test Plan completo: {ado_client.test_plan_url(plan_id)}")
        self.state.set('ado_full_push_log', log)
        self._log(
            "Integração com Azure DevOps", "Passo 7",
            f"Projeto '{project_name}' → Test Plan '{plan_name}' ({len(titled)} caso(s) de teste)",
        )
        self.clear_action()
        st.rerun()


    @staticmethod
    def _flatten_html(html: str) -> str:
        """
        Remove a indentação de cada linha antes de mandar pro st.markdown.
        Sem isso, linhas com 4+ espaços à esquerda são interpretadas pelo
        parser de Markdown como bloco de código — mesmo com
        unsafe_allow_html=True — e o SVG aparece como texto bruto na tela
        em vez de ser renderizado como imagem.
        """
        return "\n".join(line.strip() for line in html.strip().split("\n"))

    def _about_page(self):
        st.subheader("ℹ️ Sobre o App")
        st.caption(
            "Uma visão geral de como o QA Automation funciona hoje — do envio do documento até "
            "a integração com o Azure DevOps, passando pelo controle de acesso e pelos recursos "
            "extras disponíveis na barra lateral."
        )

        if st.button("← Voltar", key="btn_about_back"):
            self.state.set('show_about_page', False)
            st.rerun()

        st.markdown("#### 🧭 Arquitetura geral")
        st.caption(
            "O acesso passa por aprovação antes de entrar. Depois disso, o time de QA usa o "
            "app, que aciona o n8n (onde a IA gera o conteúdo, e onde o controle de acesso/logs "
            "também vivem) e integra tudo direto no Azure DevOps, usando o PAT pessoal de cada "
            "pessoa — não mais um token único compartilhado."
        )
        st.markdown(self._flatten_html(self._svg_architecture_diagram()), unsafe_allow_html=True)

        st.divider()

        st.markdown("#### 📋 Os 7 passos do assistente")
        st.caption(
            "Do upload do documento até a integração com o Azure DevOps. No Passo 1, além do "
            "Ambiente (Homologação/Produção), agora também se escolhe o Tipo de Documento — "
            "isso calibra o nível de detalhe que a IA assume ao gerar Matriz e Casos."
        )
        st.markdown(self._flatten_html(self._svg_steps_diagram()), unsafe_allow_html=True)

        st.divider()

        st.markdown("#### 🔀 Passo 7 — os 3 modos de envio")
        st.caption(
            "Escolhidos na hora, com uma sugestão automática baseada no Tipo de Documento do "
            "Passo 1 — mas sempre trocável manualmente."
        )
        st.markdown(self._flatten_html(self._svg_modes_diagram()), unsafe_allow_html=True)
        st.markdown(
            "- **🔗 Vincular a Work Items**: o fluxo clássico — Casos novos são criados e "
            "vinculados a Work Items já existentes no board, com a IA sugerindo os pares e "
            "você revisando antes de confirmar\n"
            "- **📋 Sem Work Items**: pra projetos no início (só um Documento de Visão, sem "
            "Work Item ainda) — cria o Test Plan com Suítes Estáticas, a partir dos Planos que "
            "o próprio Passo 5 gerou, sem depender de nenhum Work Item\n"
            "- **🔄 Reconciliar Test Plan Anterior**: pra quando os Work Items finalmente forem "
            "criados depois de um envio \"Sem Work Items\" — liga os Casos que **já existem** no "
            "Azure DevOps aos Work Items novos, sem duplicar nenhum Caso"
        )
        st.info(
            "**Regra importante nos 3 modos**: um mesmo Caso de Teste só pode ficar vinculado a "
            "**um** Work Item por vez — se ele já estiver escolhido em algum, some das opções "
            "dos outros. E antes de qualquer chamada real ao Azure DevOps, o app sempre mostra "
            "uma **lista detalhada** do que vai ser criado/vinculado, pra você revisar."
        )

        st.divider()

        st.markdown("#### 🧩 Recursos adicionais (barra lateral)")
        st.caption(
            "Não fazem parte da sequência dos 7 passos — ficam sempre disponíveis na sidebar, "
            "cada um liberado só pra quem tem a permissão certa (concedida em Administração)."
        )
        st.markdown(self._flatten_html(self._svg_extras_diagram()), unsafe_allow_html=True)

        st.divider()

        st.markdown("#### 🔐 Controle de acesso e governança")
        st.markdown(
            "- **Login com aprovação**: só o dono do app entra direto — qualquer outra pessoa "
            "precisa ser aprovada por você ou por um aprovador cadastrado, a cada nova sessão\n"
            "- **Sessão via ID opaco na URL**: o link de sessão não revela usuário nem senha "
            "nenhuma — o dado real fica guardado no n8n, e pode ser revogado remotamente a "
            "qualquer momento (a sua própria sessão, ou a de outra pessoa) em Administração\n"
            "- **PAT pessoal**: cada pessoa usa o próprio token do Azure DevOps — as ações ficam "
            "registradas no nome de quem fez, não de uma conta compartilhada, e o token nunca é "
            "salvo em disco\n"
            "- **Permissões granulares**: acesso à Integração com Azure DevOps e ao Relatório de "
            "Testes são liberados individualmente — quem não tem permissão nem vê o botão\n"
            "- **Logs de auditoria**: os últimos 500 eventos do app (login, aprovações, "
            "integrações, relatórios gerados, sessões revogadas) ficam visíveis só pro dono, em "
            "Administração"
        )

        st.divider()
        if st.button("← Voltar", key="btn_about_back_bottom"):
            self.state.set('show_about_page', False)
            st.rerun()

    @staticmethod
    def _svg_architecture_diagram() -> str:
        box = "fill='#ffffff' stroke='#d8d8d8' stroke-width='1'"
        title_style = "font-family:sans-serif;font-size:15px;font-weight:600;fill:#2d2d2d"
        sub_style = "font-family:sans-serif;font-size:12px;fill:#7a7a7a"
        arrow = "stroke='#F15A24' stroke-width='2' marker-end='url(#arch_arrow)'"

        def node(y, title, sub):
            return f"""
            <rect x="230" y="{y}" width="220" height="56" rx="8" {box} />
            <text x="340" y="{y+24}" text-anchor="middle" style="{title_style}">{title}</text>
            <text x="340" y="{y+44}" text-anchor="middle" style="{sub_style}">{sub}</text>
            """

        return f"""
        <div style="width:100%;overflow-x:auto;background:#fdfcf8;border-radius:8px;padding:8px 0;">
        <svg width="100%" viewBox="0 0 680 600" style="max-width:520px;display:block;margin:0 auto;">
            <defs>
                <marker id="arch_arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                    <path d="M2 1L8 5L2 9" fill="none" stroke="#F15A24" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                </marker>
            </defs>
            {node(40, "Usuário", "Time de QA")}
            <line x1="340" y1="96" x2="340" y2="156" {arrow} />
            {node(156, "Login", "Aprovação + PAT pessoal")}
            <line x1="340" y1="212" x2="340" y2="272" {arrow} />
            {node(272, "App QA Automation", "Streamlit")}
            <line x1="340" y1="328" x2="340" y2="388" {arrow} />
            {node(388, "n8n", "IA + Controle de Acesso")}
            <line x1="340" y1="444" x2="340" y2="504" {arrow} />
            {node(504, "Azure DevOps", "Board, Test Plans, Queries")}
        </svg>
        </div>
        """

    @staticmethod
    def _svg_steps_diagram() -> str:
        box = "fill='#ffffff' stroke='#d8d8d8' stroke-width='1'"
        title_style = "font-family:sans-serif;font-size:14px;font-weight:600;fill:#2d2d2d"
        sub_style = "font-family:sans-serif;font-size:11px;fill:#7a7a7a"
        arrow = "stroke='#F15A24' stroke-width='2' marker-end='url(#steps_arrow)'"

        def node(x, y, w, title, sub):
            cx = x + w / 2
            return f"""
            <rect x="{x}" y="{y}" width="{w}" height="56" rx="8" {box} />
            <text x="{cx}" y="{y+24}" text-anchor="middle" style="{title_style}">{title}</text>
            <text x="{cx}" y="{y+44}" text-anchor="middle" style="{sub_style}">{sub}</text>
            """

        return f"""
        <div style="width:100%;overflow-x:auto;background:#fdfcf8;border-radius:8px;padding:8px 0;">
        <svg width="100%" viewBox="0 0 680 346" style="max-width:680px;display:block;margin:0 auto;">
            <defs>
                <marker id="steps_arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                    <path d="M2 1L8 5L2 9" fill="none" stroke="#F15A24" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                </marker>
            </defs>
            {node(40, 90, 135, "1. Upload", "Documento + imagens")}
            {node(195, 90, 135, "2. Dúvidas", "Perguntas da IA")}
            {node(350, 90, 135, "3. Matriz", "Com etiqueta HML/PROD")}
            {node(505, 90, 135, "4. Casos", "CT01 HML/PROD - título")}
            <line x1="175" y1="118" x2="195" y2="118" {arrow} />
            <line x1="330" y1="118" x2="350" y2="118" {arrow} />
            <line x1="485" y1="118" x2="505" y2="118" {arrow} />
            <path d="M572.5 146 L572.5 195 L135.5 195 L135.5 250" fill="none" {arrow} />
            {node(43, 250, 185, "5. Planos", "Organiza em suítes")}
            {node(248, 250, 185, "6. Download", "CSV e PDF prontos")}
            {node(453, 250, 185, "7. Azure DevOps", "PAT pessoal + merge")}
            <line x1="228" y1="278" x2="248" y2="278" {arrow} />
            <line x1="433" y1="278" x2="453" y2="278" {arrow} />
        </svg>
        </div>
        """

    @staticmethod
    def _svg_modes_diagram() -> str:
        box = "fill='#ffffff' stroke='#d8d8d8' stroke-width='1'"
        title_style = "font-family:sans-serif;font-size:13px;font-weight:600;fill:#2d2d2d"
        sub_style = "font-family:sans-serif;font-size:11px;fill:#7a7a7a"

        def node(x, y, w, title, sub):
            cx = x + w / 2
            return f"""
            <rect x="{x}" y="{y}" width="{w}" height="64" rx="8" {box} />
            <text x="{cx}" y="{y+24}" text-anchor="middle" style="{title_style}">{title}</text>
            <text x="{cx}" y="{y+42}" text-anchor="middle" style="{sub_style}">{sub[0]}</text>
            <text x="{cx}" y="{y+58}" text-anchor="middle" style="{sub_style}">{sub[1] if len(sub) > 1 else ''}</text>
            """

        return f"""
        <div style="width:100%;overflow-x:auto;background:#fdfcf8;border-radius:8px;padding:8px 0;">
        <svg width="100%" viewBox="0 0 680 140" style="max-width:680px;display:block;margin:0 auto;">
            {node(20, 30, 210, "🔗 Vincular a Work Items", ["Work Items já existem", "IA sugere os pares"])}
            {node(240, 30, 210, "📋 Sem Work Items", ["Só Documento de Visão", "Suítes Estáticas"])}
            {node(460, 30, 210, "🔄 Reconciliar Anterior", ["Work Items criados depois", "Liga Casos já existentes"])}
        </svg>
        </div>
        """

    @staticmethod
    def _svg_extras_diagram() -> str:
        box = "fill='#ffffff' stroke='#d8d8d8' stroke-width='1'"
        title_style = "font-family:sans-serif;font-size:13px;font-weight:600;fill:#2d2d2d"
        sub_style = "font-family:sans-serif;font-size:11px;fill:#7a7a7a"

        def node(x, y, w, title, sub):
            cx = x + w / 2
            return f"""
            <rect x="{x}" y="{y}" width="{w}" height="60" rx="8" {box} />
            <text x="{cx}" y="{y+24}" text-anchor="middle" style="{title_style}">{title}</text>
            <text x="{cx}" y="{y+44}" text-anchor="middle" style="{sub_style}">{sub}</text>
            """

        return f"""
        <div style="width:100%;overflow-x:auto;background:#fdfcf8;border-radius:8px;padding:8px 0;">
        <svg width="100%" viewBox="0 0 680 130" style="max-width:680px;display:block;margin:0 auto;">
            {node(40, 40, 135, "📊 Relatório", "Status real do board")}
            {node(195, 40, 135, "🎯 Via Work Items", "Gera a partir do Azure")}
            {node(350, 40, 135, "🔎 Query com IA", "WIQL por descrição")}
            {node(505, 40, 135, "🛡️ Administração", "Permissões e Logs")}
        </svg>
        </div>
        """

    def run(self):
        if not require_login(self.config):
            return

        if not self.state.get('_f5_block_injected'):
            self.state.set('_f5_block_injected', True)
            self._block_f5_reload()

        self._inject_ui_styles()
        self._header()
        render_logout_control(self.config)
        
        # Scroll Viewport to Top Tracking System
        current_step = self.state.get('step')
        if current_step != self.state.get('last_viewed_step'):
            self.state.set('last_viewed_step', current_step)
            self._force_sidebar_collapsed()
            st.markdown(
                """
                <svg onload="
                    window.parent.scrollTo({top: 0, behavior: 'smooth'}); 
                    var m = window.parent.document.querySelector('.main'); 
                    if(m) m.scrollTo({top: 0, behavior: 'smooth'});
                " style="display:none;"></svg>
                """,
                unsafe_allow_html=True
            )

        if self.state.get('show_interrupt_modal'):
            confirm_interrupt_modal()
            
        if self.state.get('show_new_analysis_modal'):
            confirm_new_analysis_modal(self.config)

        if self.state.get('show_about_page'):
            self._about_page()
            return

        if self.state.get('show_admin_page'):
            if st.button("← Voltar", key="btn_admin_back"):
                self.state.set('show_admin_page', False)
                st.rerun()
            render_admin_panel(self.config)
            return

        if self.state.get('show_execution_report_page'):
            self._execution_report_page()
            return

        if self.state.get('show_wi_generation_page'):
            self._wi_generation_page()
            return

        if self.state.get('show_wiql_generation_page'):
            self._wiql_generation_page()
            return

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
        elif step == 7:
            self.step_7()
