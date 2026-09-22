import os
import base64
import difflib
import hashlib
import html
import json
import re
import tempfile
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image

from qa_testgen.config import AppConfiguration, LOGO_PATH, SIMBOLO_PATH, TZ_BR
from qa_testgen.infrastructure.csv_formatter import AzureCsvFormatter
from qa_testgen.infrastructure.document_processor import DocumentProcessor
from qa_testgen.infrastructure.pdf_report import PdfReportGenerator
from qa_testgen.infrastructure.manual_pdf import ManualPdfGenerator
from qa_testgen.infrastructure.document_store import (
    DocumentStore, DocumentStoreError, AppSettingsStore, CONFIG_IDENTIDADES_AZURE,
)
from qa_testgen.infrastructure.webhook_client import WebhookClient
from qa_testgen.infrastructure.azure_devops_client import AzureDevOpsClient, AzureDevOpsError
from qa_testgen.infrastructure.access_control_client import AccessControlClient
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
    aviso_pat_compartilhado_modal,
)
from qa_testgen.ui.api_tests_page import ApiTestsPageMixin
from qa_testgen.ui.ia_retry import IaRetryMixin
from qa_testgen.ui.work_item_batch_page import WorkItemBatchMixin
from qa_testgen.ui.auth import (
    require_login, render_logout_control, is_approver, has_permission,
    render_admin_panel, log_action, SESSION_USER_KEY,
)

# Textos do expander "ℹ️ O que é e como usar esta área", um por área. Escritos
# pra quem entende o negócio mas não o app: o que é, quando usar, passo a
# passo e onde clicar. Mesmo formato em todas as telas.
AREA_HELP = {
    "assistente": (
        "**O que é:** o fluxo principal do app. Transforma uma especificação (documento, Work Items "
        "ou uma query do Azure DevOps) em **Matriz de Cobertura → Casos de Teste → Planos de Teste**, "
        "com IA gerando o conteúdo e você revisando cada etapa.\n\n"
        "**Quando usar:** sempre que precisar documentar testes de uma funcionalidade nova ou de um "
        "conjunto de Work Items.\n\n"
        "**Passo a passo:**\n"
        "1. **Upload** — escolha a origem (documento, Work Items do board ou query salva), o Ambiente "
        "e o Tipo de Documento (calibra o nível de detalhe da IA).\n"
        "2. **Dúvidas** — a IA pergunta o que ficou ambíguo; responda o que fizer sentido.\n"
        "3. **Matriz** → 4. **Casos** → 5. **Planos** — gerados pela IA, editáveis (adicionar, "
        "alterar, excluir).\n"
        "6. **Download** — CSV e PDF \"Documentação QA\".\n"
        "7. **Azure DevOps** (quem tem permissão) — envia tudo pro Azure: vinculando a Work Items, "
        "sem Work Items (suítes estáticas) ou reconciliando um Test Plan anterior.\n\n"
        "💡 A barra no topo mostra em que passo você está; passos já concluídos podem ser reabertos. "
        "\"🔄 Nova Análise\" (barra lateral) recomeça do zero — pede confirmação antes."
    ),
    "relatorio": (
        "**O que é:** um relatório em PDF do que foi **executado** no Azure DevOps (diferente do PDF "
        "do Passo 6, que documenta o que foi *planejado*).\n\n"
        "**Quando usar:** ao fechar um ciclo de testes, pra entregar o status real ao time/gestão.\n\n"
        "**Passo a passo:**\n"
        "1. Conecte ao Azure DevOps (Organização, Projeto e, se quiser, Area Path).\n"
        "2. Escolha um ou mais Test Plans e clique em **Buscar Resultados**.\n"
        "3. O status de cada caso vem da **coluna do board** do Work Item vinculado (não do Test "
        "Point). Escolha o **Status geral** manualmente.\n"
        "4. Use **Sugerir com IA** pra preencher Contexto, Escopo, Conclusão e Próximos Passos, "
        "ajuste o texto e clique em **Gerar Relatório**.\n"
        "5. Baixe o PDF (ou salve em Documentos Armazenados).\n\n"
        "💡 Se sair da tela com um relatório gerado e não baixado, o app avisa antes."
    ),
    "wiql": (
        "**O que é:** cria uma **query salva dentro do Azure DevOps** a partir de uma descrição em "
        "português — a IA traduz pra WIQL (a linguagem de consulta do Azure).\n\n"
        "**Quando usar:** pra montar consultas que você vai reutilizar lá (dashboards, widgets, "
        "acompanhamento) sem escrever WIQL na mão.\n\n"
        "**Passo a passo:**\n"
        "1. Conecte ao Azure DevOps.\n"
        "2. Descreva o que quer (ex.: *bugs abertos atribuídos a mim nos últimos 30 dias*) e clique "
        "em **Gerar Query com IA**.\n"
        "3. Clique em **Testar Query** pra ver os resultados reais antes de salvar.\n"
        "4. Dê um nome, escolha a pasta (My Queries / Shared Queries) e **salve**.\n\n"
        "💡 Os atalhos **Usar pra Gerar Testes** e **Usar pra Criar Manual** pulam o salvar e já "
        "levam o resultado pro Passo 1 ou pro Manual de Testes. Isto é diferente do modo "
        "\"Gerar a partir de uma Query\" do Passo 1, que parte de uma query que você **já tem** salva."
    ),
    "manual": (
        "**O que é:** um **manual de reprodução em linguagem simples**, pra quem não é de TI "
        "(Produto, Marketing) testar em UAT sem confundir passo executado errado com bug.\n\n"
        "**Quando usar:** antes de liberar uma funcionalidade pra aceite por times não técnicos.\n\n"
        "**Passo a passo:**\n"
        "1. Escolha a origem do conteúdo: Documentos, Work Items do Azure DevOps (board ou query "
        "salva) ou Mesclado.\n"
        "2. Anexe/busque o material e clique em **Gerar Manual com IA**.\n"
        "3. Revise o texto na tela (editável) e baixe o PDF.\n\n"
        "💡 O app **não tira print ao vivo** — só reaproveita imagens que já existam nos documentos "
        "ou nos Work Items."
    ),
    "documentos": (
        "**O que é:** um arquivo de tudo que o app já gerou (CSV, PDF, relatórios, evidências de "
        "Testes de API), guardado num banco próprio e organizado por grupo.\n\n"
        "**Quando usar:** pra recuperar uma documentação sem precisar gerar de novo, ou pra "
        "consultar o que outra pessoa do time gerou.\n\n"
        "**Como usar:** filtre pelo fluxo de origem, abra o grupo e clique no arquivo pra baixar. "
        "Pra salvar algo aqui, use o botão \"Salvar em Documentos Armazenados\" que aparece nas "
        "telas que geram arquivos (Passo 6, Relatório de Testes, Testes de API).\n\n"
        "💡 Qualquer pessoa com a permissão salva e vê; **excluir** um grupo é só do dono do app."
    ),
    "mapa": (
        "**O que é:** uma visualização em árvore — **Work Item → Suítes → Casos de Teste** — "
        "navegável e com zoom.\n\n"
        "**Quando usar:** pra apresentar ou revisar a cobertura de um conjunto de Work Items de "
        "forma visual.\n\n"
        "**Passo a passo:**\n"
        "1. Escolha a origem: a sessão atual (Planos gerados no Passo 5), um grupo de Documentos "
        "Armazenados ou Work Items do Azure DevOps.\n"
        "2. Clique em **Gerar Mapa Mental**.\n"
        "3. Expanda/recolha os nós na tela; exporte em **SVG** ou **PDF** — o arquivo sai sempre "
        "com tudo expandido."
    ),
    "bug": (
        "**O que é:** cria um **Bug direto no Azure DevOps**, sem sair do app.\n\n"
        "**Quando usar:** ao encontrar um defeito durante a execução dos testes.\n\n"
        "**Passo a passo:**\n"
        "1. Conecte ao Azure DevOps e escolha Projeto e Area Path.\n"
        "2. Modo **livre** (preenche tudo) ou **a partir de um Work Item** com Casos de Teste "
        "relacionados — vinculando a um Caso específico (título e Passos de Reprodução já vêm "
        "preenchidos) ou direto no Work Item.\n"
        "3. Preencha Título, Passos de Reprodução e, se quiser, System Info, Acceptance Criteria, "
        "Discussion e **evidências em imagem** (sobem como anexo e ficam embutidas no System Info).\n"
        "4. Revise a confirmação e clique em **Criar Bug**.\n\n"
        "💡 Nada criado no Azure DevOps pode ser desfeito pelo app — corrija lá se precisar."
    ),
    "work_item": (
        "**O que é:** cria **Work Items de qualquer tipo** que o processo do projeto permita "
        "(User Story, Epic, Feature, Task, Spike, tipos customizados...).\n\n"
        "**Quando usar:** pra registrar itens de backlog sem abrir o Azure DevOps, ou pra quebrar "
        "uma User Story em várias Tasks de uma vez.\n\n"
        "**Passo a passo:**\n"
        "1. Conecte ao Azure DevOps e escolha o Projeto (Area Path, Sprint e coluna são opcionais).\n"
        "2. Escolha o **tipo** — o formulário é montado com os campos do próprio projeto; os "
        "obrigatórios aparecem em destaque, o resto fica em \"Outros campos\".\n"
        "3. Modo **Um Work Item** (com pai opcional), **Vários filhos de um Work Item**, ou "
        "**Fila / planilha** — vários itens de qualquer tipo enviados de uma vez.\n"
        "4. Tags (existentes ou novas), responsável, evidências em imagem → revise e clique em "
        "**Criar Work Item** (ou **➕ Adicionar à fila** pra enviar depois, junto com outros).\n\n"
        "**Fila / planilha:** baixe o **modelo** (.xlsx ou .csv, gerado com os tipos, Area Paths, "
        "Iterations e pessoas deste projeto — a aba *Instruções* explica cada coluna), preencha uma "
        "linha por item e suba o arquivo; o app valida linha a linha e mostra o que corrigir. "
        "Na coluna **Pai** use o ID de um item existente ou `#Ref` de outra linha (o pai é criado "
        "antes). Nada é criado até você clicar em **Enviar** e confirmar. Ao terminar, aparece no topo "
        "o **resumo do envio**: tabela com ID, tipo e título de cada item criado, link direto pra cada um, "
        "link pra ver todos juntos numa consulta do Azure DevOps e download do resumo em .md; o que "
        "falhar fica na fila com o motivo, pra corrigir e reenviar."
    ),
}

# Liga/desliga a seção de integração direta com o Azure DevOps no Passo 6.
# Coloque True quando quiser reativar a integração.
AZURE_DEVOPS_INTEGRATION_ENABLED = True

# Liga/desliga TODOS os pontos de envio de documento do app (Passo 1,
# complementares do fluxo de Work Items e Manual de Reprodução), sem remover a
# funcionalidade — reativado apos corrigir as 4 causas raiz do travamento/
# Test Plan vazio (chave de estado errada, rate limit de IA sem espera entre
# lotes, descasamento de titulo Plano<->Caso, e checagem final de sucesso
# baseada em planejamento em vez do resultado real da API). Coloque False de
# novo se aparecer qualquer problema durante o reteste.
DOCUMENT_UPLOAD_ENABLED = True
DOCUMENT_UPLOAD_DISABLED_MSG = (
    "📄 O envio de documentos está temporariamente desativado enquanto corrigimos um "
    "problema no processamento. Use as outras origens de especificação (Work Items/Query "
    "do Azure DevOps) enquanto isso."
)


class UserInterface(ApiTestsPageMixin, WorkItemBatchMixin, IaRetryMixin):
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

    def _identidade_azure(self, username: str = None) -> str:
        """
        Nome que identifica a pessoa NO AZURE (tag criado-por). Por padrão é o
        próprio usuário do login, mas o dono pode mapear outro em
        Administração > Configurações (ex.: login "admin" -> "vinicius"), já
        que o PAT é compartilhado e o Azure mostra sempre a conta do token.
        Lido uma vez por sessão (o mapa é global, no Turso).
        """
        # st.session_state não existe dentro de ThreadPoolExecutor — quem cria
        # itens em thread já passa o nome pronto; aqui só não deixamos estourar.
        try:
            if username is None:
                username = st.session_state.get(SESSION_USER_KEY, "") or ""
            mapa = st.session_state.get('identidades_azure_cache')
        except Exception:
            return username or ""
        if mapa is None:
            mapa = {}
            if getattr(self.config, 'turso_database_url', ''):
                try:
                    store = AppSettingsStore(self.config.turso_database_url, self.config.turso_auth_token)
                    store.ensure_schema()
                    mapa = json.loads(store.get(CONFIG_IDENTIDADES_AZURE, "") or "{}")
                except Exception:
                    mapa = {}
            try:
                st.session_state['identidades_azure_cache'] = mapa
            except Exception:
                pass
        return (mapa.get(username) or username).strip()

    def _tag_criado_por(self, tags_existentes: str = None, username: str = None) -> str:
        """
        Acrescenta "criado-por:<usuário logado>" à lista de tags — usada em
        toda criação de Bug/Test Case no Azure DevOps.

        ATENÇÃO: st.session_state só existe na thread principal do Streamlit.
        Quem cria itens dentro de um ThreadPoolExecutor precisa calcular a
        tag ANTES de abrir as threads (ou passar `username` pronto) — senão
        a tag sai como "criado-por:desconhecido". Existe pra dar
        rastreabilidade de quem fez o quê mesmo quando todas as chamadas
        usam o mesmo PAT compartilhado: o Azure DevOps grava toda mudança
        no campo Tags como uma revisão normal na aba History do próprio
        item — inclusive se alguém remover essa tag depois, a remoção
        também fica lá, com quem tirou e quando.

        tags_existentes: string já no formato "tag1; tag2" (ou None) —
        outras tags que a pessoa já tenha escolhido manualmente, se houver.
        """
        if username is None:
            username = self._identidade_azure() or "desconhecido"
        username_tag = username.strip().replace(";", "").replace(" ", "-").lower()
        tag_autor = f"criado-por:{username_tag}"
        if tags_existentes:
            partes = [t.strip() for t in tags_existentes.split(";") if t.strip()]
            if tag_autor not in partes:
                partes.append(tag_autor)
            return "; ".join(partes)
        return tag_autor

    # Arquivo simples (não é banco de dados) só pra lembrar quem já
    # dispensou o aviso de "PAT virou compartilhado" — dura enquanto o
    # container do Streamlit Cloud não reiniciar. Não é o tipo de dado que
    # precisa sobreviver pra sempre (pior caso, a pessoa vê o aviso de novo
    # depois de um restart raro), então não criei tabela nova só pra isso.
    _PAT_NOTICE_FILE = Path(tempfile.gettempdir()) / "qa_testgen_pat_notice_dismissed.json"
    # Versão do aviso de novidades: mude este valor quando houver algo novo
    # pra anunciar — quem dispensou uma versão anterior volta a ver o modal
    # uma vez. O arquivo guarda {"dispensado": {usuario: versao}}; o formato
    # antigo ({"usuarios": [...]}) conta como "dispensou só a versão do PAT".
    _NOTICE_VERSION = "2026-09-18-fila-testrun"

    @classmethod
    def _pat_notice_ja_dispensado(cls, username: str) -> bool:
        try:
            dados = json.loads(cls._PAT_NOTICE_FILE.read_text(encoding="utf-8"))
            return dados.get("dispensado", {}).get(username) == cls._NOTICE_VERSION
        except Exception:
            return False

    @classmethod
    def _marcar_pat_notice_dispensado(cls, username: str):
        try:
            dados = {}
            if cls._PAT_NOTICE_FILE.exists():
                dados = json.loads(cls._PAT_NOTICE_FILE.read_text(encoding="utf-8"))
            dispensado = dados.get("dispensado") or {}
            dispensado[username] = cls._NOTICE_VERSION
            cls._PAT_NOTICE_FILE.write_text(json.dumps({"dispensado": dispensado}), encoding="utf-8")
        except Exception:
            pass  # não crítico — pior caso, a pessoa vê o aviso de novo

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

    def _render_project_name_field(self, default_value: str, field_key: str, label: str = "Nome do Projeto *") -> str:
        """
        Campo obrigatório de nome de projeto, pré-preenchido com um
        valor padrão (ex.: nome do projeto no Azure DevOps) — com
        destaque visual discreto (borda colorida na lateral, via CSS
        compartilhado), sinalizando "isso foi puxado automaticamente,
        confira se é o que você quer" sem precisar de texto explicativo.

        field_key: chave única por fluxo (cada um usa a sua, sem misturar)
        Retorna o valor atual do campo.
        """
        if f"{field_key}_input" not in st.session_state:
            st.session_state[f"{field_key}_input"] = self.state.get(field_key) or default_value or ""

        with st.container(key=f"project_name_highlight_{field_key}"):
            valor = st.text_input(
                label,
                key=f"{field_key}_input",
                disabled=self.state.get('is_processing'),
            )
        self.state.set(field_key, valor)
        return valor

    def _render_document_storage_section(self, fluxo_origem: str, nome_projeto: str, arquivos: list):
        """
        Seção "Armazenar esta documentação" — reaproveitada em qualquer
        fluxo que gere CSV/PDF (Passo 6, Relatório de Testes, Manual de
        Testes). Só aparece pro dono do app (feature admin-only). Salva
        todos os arquivos de uma vez com o mesmo grupo_id no banco Turso,
        pra ficarem organizados juntos (ex.: o CSV e o PDF do mesmo Passo 6).

        arquivos: [{"tipo": "csv"|"pdf", "nome_arquivo": str, "conteudo": bytes}, ...]
        """
        current_username = st.session_state.get(SESSION_USER_KEY, "")
        if current_username != self.config.owner_username or not arquivos:
            return

        st.divider()
        with st.expander("🗄️ Armazenar esta documentação (admin)"):
            st.caption(
                f"Guarda {len(arquivos)} arquivo(s) deste fluxo no banco de documentos — "
                "ficam organizados juntos (mesmo grupo) e disponíveis depois em "
                "'🗄️ Documentos Armazenados', na barra lateral."
            )
            campo_key = f"store_nome_doc_{fluxo_origem}_{hashlib.md5((nome_projeto or '').encode()).hexdigest()[:8]}"
            if campo_key not in st.session_state:
                st.session_state[campo_key] = nome_projeto or ""
            nome_documento = st.text_input(
                "Nome do Documento *",
                key=campo_key,
                disabled=self.state.get('is_processing'),
                help="Obrigatório — é esse nome que aparece na lista de 'Documentos Armazenados', "
                     "então vale diferenciar de outros grupos já salvos (ex.: incluindo a data ou "
                     "uma versão), em vez de deixar sempre o nome do projeto puro.",
            )
            btn_key = f"btn_store_{fluxo_origem}_{hashlib.md5((nome_projeto or '').encode()).hexdigest()[:8]}"
            with st.container(key="azure_blue_btn_store_docs"):
                if st.button(
                    "💾 Armazenar esta documentação",
                    key=btn_key,
                    disabled=self.state.get('is_processing') or not nome_documento.strip(),
                    width="stretch",
                ):
                    try:
                        prefixo = re.sub(r'[^\w\-]+', '_', nome_documento.strip())[:60]
                        arquivos_renomeados = [
                            {**arq, "nome_arquivo": f"{prefixo}__{arq['nome_arquivo']}"}
                            for arq in arquivos
                        ]
                        store = DocumentStore(self.config.turso_database_url, self.config.turso_auth_token)
                        with st.spinner("Salvando no banco de documentos..."):
                            store.ensure_schema()
                            store.salvar_grupo(fluxo_origem, nome_documento.strip(), arquivos_renomeados, criado_por=current_username)
                        st.success("✅ Documentação armazenada com sucesso.")
                        self._log(
                            "Armazenar Documentação", fluxo_origem,
                            f"'{nome_documento.strip()}' — {len(arquivos)} arquivo(s)",
                        )
                    except DocumentStoreError as error:
                        st.error(f"❌ {error}")
                    except Exception as error:
                        st.error(f"❌ Não foi possível armazenar: {error}")
            if not nome_documento.strip():
                st.caption("Preencha o Nome do Documento para habilitar o botão de salvar.")

    def _flash_error(self, message: str) -> None:
        """
        Guarda uma mensagem de erro pra mostrar DEPOIS do próximo rerun.
        NUNCA use st.error() direto bem antes de um st.rerun() na mesma
        passada — o rerun descarta a mensagem antes da pessoa conseguir
        ver (ela pisca por uma fração de segundo, ou nem isso). Use isso
        no lugar, e a mensagem aparece corretamente na resposta seguinte.
        """
        self.state.set('_flash_message', {'kind': 'error', 'text': message})

    def _flash_warning(self, message: str) -> None:
        """Mesma ideia de _flash_error, mas pra st.warning()."""
        self.state.set('_flash_message', {'kind': 'warning', 'text': message})

    def _flash_success(self, message: str) -> None:
        """Mesma ideia de _flash_error, mas pra st.success()."""
        self.state.set('_flash_message', {'kind': 'success', 'text': message})

    def _render_flash_message(self) -> None:
        """
        Mostra (uma única vez) a mensagem guardada por _flash_error/
        _flash_warning, se houver — chamado no topo do render principal,
        depois de qualquer st.rerun() já ter acontecido.

        Rola a tela até a mensagem quando ela aparece: como ela é sempre
        renderizada aqui em cima, e quem disparou o aviso (ex.: "Buscar
        Casos de Teste vinculados") geralmente clicou um botão bem mais
        abaixo na página, sem isso a mensagem nasce fora da área visível
        e passa despercebida — a pessoa precisa rolar manualmente pra
        cima só pra descobrir que apareceu algum aviso.
        """
        flash = self.state.get('_flash_message')
        if flash:
            self.state.set('_flash_message', None)
            st.markdown('<div id="flash-message-anchor"></div>', unsafe_allow_html=True)
            if flash['kind'] == 'error':
                st.error(f"❌ {flash['text']}")
            elif flash['kind'] == 'warning':
                st.warning(flash['text'])
            elif flash['kind'] == 'success':
                st.success(f"✅ {flash['text']}")
            st.markdown(
                """
                <svg onload="
                    var alvo = window.parent.document.getElementById('flash-message-anchor');
                    if (alvo) { alvo.scrollIntoView({behavior: 'smooth', block: 'start'}); }
                " style="display:none;"></svg>
                """,
                unsafe_allow_html=True
            )

    def _render_aviso_ia(self) -> None:
        """
        Aviso FIXO, no topo da área principal, dizendo o que causou a falha da
        geração por IA — em português e em uma frase.

        Antes, a única pista era a label do st.status lá no fim da página
        ("o lote anterior falhou") e o erro cru em inglês dentro dele; quem
        está usando o app via "Aguardando 52s" e não tinha como saber que o
        motivo era a cota por minuto do provedor gratuito. Fica na tela até a
        pessoa dispensar ou começar outra geração (_iniciar_geracao_em_lotes
        limpa), porque a geração continua rodando e ela precisa saber o que
        está acontecendo enquanto espera.
        """
        aviso = self.state.get('ia_causa_visivel')
        if not aviso:
            return
        st.markdown('<div id="aviso-ia-anchor"></div>', unsafe_allow_html=True)
        titulo = aviso.get('causa') or "**A chamada à IA falhou.**"
        onde = f" — em: {aviso['onde']}" if aviso.get('onde') else ""
        extra = f" ({aviso['extra']})" if aviso.get('extra') else ""
        st.error(f"⚠️ {titulo}{onde}{extra}")
        col_detalhe, col_ok = st.columns([4, 1])
        with col_detalhe:
            with st.expander("🔍 Detalhe técnico (o que o provedor respondeu)"):
                st.code(aviso.get('detalhe') or "(sem detalhe)", language="text")
        with col_ok:
            if st.button("Dispensar", key="btn_dispensar_aviso_ia", width="stretch"):
                self.state.set('ia_causa_visivel', None)
                st.rerun()

    def _render_area_help(self, area: str):
        """
        Expander padrão "ℹ️ O que é e como usar esta área", logo abaixo do
        título/caption de cada tela. Um texto por área em AREA_HELP — mesma
        estrutura em todas (o que é, quando usar, passo a passo, onde clicar).
        """
        texto = AREA_HELP.get(area)
        if not texto:
            return
        with st.expander("ℹ️ O que é e como usar esta área"):
            st.markdown(texto)

    def _email_do_usuario_logado(self) -> str:
        """
        E-mail cadastrado no app pro usuário logado (via Controle de Acesso),
        usado pra pré-selecionar a pessoa certa no Azure DevOps. Cache por
        sessão; string vazia se não der pra descobrir.
        """
        if self.state.get('_email_usuario_logado') is None:
            email = ""
            try:
                username = st.session_state.get(SESSION_USER_KEY, "")
                for u in AccessControlClient(self.config).list_users():
                    if u.get("username") == username:
                        email = (u.get("email") or "").strip().lower()
                        break
            except Exception:
                email = ""
            self.state.set('_email_usuario_logado', email)
        return self.state.get('_email_usuario_logado') or ""

    def _render_step7_assigned_to(self, ado_client):
        """
        "Assigned To" dos Test Cases criados no Passo 7. Sem isso, o Azure
        DevOps atribui ao criador — que no modo de PAT compartilhado é
        sempre o dono do PAT, não quem está usando o app. Pré-seleciona a
        pessoa cujo e-mail no Azure bate com o e-mail cadastrado no app.
        """
        st.markdown("##### 👤 Atribuir os Test Cases a")
        st.caption(
            "Quem fica como responsável (Assigned To) em cada Test Case criado. Por padrão, você — "
            "se o seu e-mail cadastrado no app existir no projeto do Azure DevOps."
        )
        membros = self._wi_render_pessoas(ado_client, "passo7")
        if not membros:
            st.caption("Busque as pessoas acima pra escolher. Sem escolha, o Azure DevOps atribui ao criador (dono do PAT).")
            self.state.set('ado_test_case_assigned_to', None)
            return
        opcoes = ["— Não atribuir (o Azure DevOps decide)"] + [f"{m['display_name']} ({m['unique_name']})" for m in membros]
        email = self._email_do_usuario_logado()
        default_idx = 0
        for i, m in enumerate(membros, start=1):
            if email and (m.get("unique_name") or "").strip().lower() == email:
                default_idx = i
                break
        escolha = st.selectbox("Responsável pelos Test Cases", opcoes, index=default_idx, key="ado_tc_assigned_to_select",
                               disabled=self.state.get('is_processing'))
        idx = opcoes.index(escolha)
        self.state.set('ado_test_case_assigned_to', membros[idx - 1]["unique_name"] if idx > 0 else None)
        if idx == 0:
            st.warning("⚠️ Sem responsável escolhido, os Test Cases vão nascer atribuídos ao dono do PAT compartilhado.")
        elif default_idx == 0 and email:
            st.caption(f"ℹ️ Seu e-mail no app ({email}) não foi encontrado entre as pessoas do projeto — confira o cadastro.")

    def _navigate_or_confirm(self, pending_state_updates: dict):
        """
        Aplica as mudanças de estado em `pending_state_updates` (ex.: trocar
        de página/sidebar) — a não ser que estejamos na página de Relatório
        de Testes com um PDF já gerado, caso em que primeiro pede
        confirmação (evita perder o relatório sem querer ao clicar em
        qualquer outro botão/menu).

        Também reseta as telas de confirmação de Criar Bug (livre e a
        partir de Caso de Teste) toda vez que a pessoa navega pra
        qualquer lugar — sem isso, sair no meio de uma confirmação e
        voltar depois pra "Criar Bug" deixava a pessoa presa revendo a
        tela de confirmação antiga.
        """
        if self.state.get('show_execution_report_page') and self.state.get('report_pdf_bytes'):
            self.state.set('_pending_navigation_after_report', pending_state_updates)
            self.state.set('show_leave_report_modal', True)
            st.rerun()
        elif (
            self.state.get('show_api_tests_page')
            and self._api_tem_relatorio_nao_baixado()
            and pending_state_updates.get('show_api_tests_page') is False
        ):
            self.state.set('_pending_navigation_after_api', pending_state_updates)
            self.state.set('show_leave_api_modal', True)
            st.rerun()
        else:
            # A página de Testes de API não está listada nos dicionários de
            # navegação das outras áreas — desliga aqui, de forma central.
            self.state.set('show_api_tests_page', False)
            for key, value in pending_state_updates.items():
                self.state.set(key, value)
            self.state.set('show_bug_confirm_modal', False)
            st.rerun()

    # Todas as áreas do app que dependem de permissão, na ordem em que
    # aparecem na barra lateral: (chave de estado da página, permissão,
    # rótulo). Fonte única usada tanto pra decidir o que mostrar na
    # sidebar quanto pra saber pra onde levar quem NÃO tem o assistente
    # de QA — sem isso, quem só tem "Criar Bug" logava e caía no fluxo de
    # documentação (Passos 1–6), que não deveria nem existir pra ela.
    _AREAS_COM_PERMISSAO = [
        ('show_manual_page', 'manual_testes', '📘 Manual de Testes (UAT)'),
        ('show_document_store_page', 'documentos_armazenados', '🗄️ Documentos Armazenados'),
        ('show_mindmap_page', 'mapa_mental', '🧠 Mapa Mental'),
        ('show_bug_page', 'criar_bug', '🐛 Criar Bug'),
        ('show_work_item_page', 'criar_work_item', '🧱 Criar Work Item'),
        ('show_wiql_generation_page', 'azure_devops', '🔎 Criar Query com IA'),
        ('show_execution_report_page', 'execution_report', '📊 Relatório de Testes'),
        ('show_api_tests_page', 'testes_api', '🔌 Testes de API'),
    ]

    def _areas_liberadas(self) -> list:
        """Áreas (fora do assistente de QA) que o usuário atual pode acessar."""
        return [
            (chave, perm, rotulo)
            for chave, perm, rotulo in self._AREAS_COM_PERMISSAO
            if self._get_permission_cached(perm)
        ]

    def _pagina_ativa(self) -> str:
        """Chave da página de área aberta agora, ou '' se está no assistente."""
        for chave, _perm, _rotulo in self._AREAS_COM_PERMISSAO:
            if self.state.get(chave):
                return chave
        return ''

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

                /* Destaque discreto pra campos pré-preenchidos automaticamente
                   (ex.: Nome do Projeto puxado do Azure DevOps) — sinaliza
                   visualmente "confira/ajuste isso" sem precisar de texto
                   explicativo, e sem destoar do resto da UI. */
                div[class*="st-key-project_name_highlight_"] {
                    border-left: 3px solid #F15A24;
                    padding-left: 10px;
                    margin-bottom: 4px;
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

            # "Nova Análise" e "Início" só fazem sentido pra quem tem o
            # assistente de QA — as duas navegam pra dentro do fluxo de
            # Passos 1–6. Sem elas (e sem processamento rodando), a seção
            # "Controles" ficaria só um título com nada embaixo.
            tem_assistente = self._get_permission_cached("assistente_qa")
            processando = self.state.get('is_processing')

            if tem_assistente or processando:
                st.divider()
                st.warning("⚠️ Controles")
            if processando:
                st.info("Processamento em andamento. Aguarde a conclusão ou solicite a interrupção.")
                if st.button("⏹️ Interromper Processamento", width="stretch", type="primary", key="btn_interrupt_sidebar"):
                    self.state.set('show_interrupt_modal', True)
                    st.rerun()

            if tem_assistente and st.button("🔄 Nova Análise", width="stretch", type="primary", key="btn_new_sidebar"):
                self.state.set('show_new_analysis_modal', True)
                st.rerun()

            if tem_assistente and st.button("🏠 Início", width="stretch", disabled=self.state.get('is_processing'), key="btn_home_sidebar"):
                # Diferente de "Nova Análise": só navega pro Passo 1, sem
                # apagar nada — tudo que já foi preenchido continua lá, e
                # dá pra voltar a qualquer passo já feito normalmente.
                if self.state.get('show_execution_report_page') and self.state.get('report_pdf_bytes'):
                    self.state.set('_pending_navigation_after_report', {'show_execution_report_page': False, 'step': 1})
                    self.state.set('show_leave_report_modal', True)
                    st.rerun()
                elif self.state.get('show_api_tests_page') and self._api_tem_relatorio_nao_baixado():
                    self.state.set('_pending_navigation_after_api', {'show_api_tests_page': False, 'step': 1})
                    self.state.set('show_leave_api_modal', True)
                    st.rerun()
                elif self._has_editing_in_progress():
                    confirm_navigate_away_modal(1)
                else:
                    clear_widget_states()
                    self._set_step(1)
                    st.rerun()

            st.divider()
            if st.button("ℹ️ Sobre o app", width="stretch", key="btn_about_sidebar", disabled=self.state.get('is_processing')):
                self._navigate_or_confirm({
                    'show_about_page': True, 'show_admin_page': False,
                    'show_execution_report_page': False,
                    'show_wiql_generation_page': False, 'show_manual_page': False,
                    'show_document_store_page': False, 'show_mindmap_page': False, 'show_bug_page': False, 'show_work_item_page': False,
                })

            current_username = st.session_state.get(SESSION_USER_KEY, "")
            if self._get_permission_cached("manual_testes"):
                if st.button("📘 Manual de Testes (UAT)", width="stretch", key="btn_manual_sidebar", disabled=self.state.get('is_processing')):
                    self._navigate_or_confirm({
                        'show_manual_page': True, 'show_about_page': False,
                        'show_admin_page': False, 'show_execution_report_page': False,
                        'show_wiql_generation_page': False, 'show_document_store_page': False,
                        'show_mindmap_page': False, 'show_bug_page': False, 'show_work_item_page': False,
                    })
            if self._get_permission_cached("documentos_armazenados"):
                if st.button("🗄️ Documentos Armazenados", width="stretch", key="btn_document_store_sidebar", disabled=self.state.get('is_processing')):
                    self._navigate_or_confirm({
                        'show_document_store_page': True, 'show_about_page': False,
                        'show_admin_page': False, 'show_execution_report_page': False,
                        'show_wiql_generation_page': False, 'show_manual_page': False,
                        'show_mindmap_page': False, 'show_bug_page': False, 'show_work_item_page': False,
                    })
            if self._get_permission_cached("mapa_mental"):
                if st.button("🧠 Mapa Mental", width="stretch", key="btn_mindmap_sidebar", disabled=self.state.get('is_processing')):
                    self._navigate_or_confirm({
                        'show_mindmap_page': True, 'show_bug_page': False, 'show_work_item_page': False, 'show_about_page': False,
                        'show_admin_page': False, 'show_execution_report_page': False,
                        'show_wiql_generation_page': False, 'show_manual_page': False,
                        'show_document_store_page': False,
                    })
            if self._get_permission_cached("criar_bug"):
                if st.button("🐛 Criar Bug", width="stretch", key="btn_bug_sidebar", disabled=self.state.get('is_processing')):
                    self._navigate_or_confirm({
                        'show_bug_page': True, 'show_work_item_page': False,
                        'show_mindmap_page': False, 'show_about_page': False,
                        'show_admin_page': False, 'show_execution_report_page': False,
                        'show_wiql_generation_page': False, 'show_manual_page': False,
                        'show_document_store_page': False,
                    })
            if self._get_permission_cached("criar_work_item"):
                if st.button("🧱 Criar Work Item", width="stretch", key="btn_work_item_sidebar", disabled=self.state.get('is_processing')):
                    self._navigate_or_confirm({
                        'show_work_item_page': True, 'show_bug_page': False,
                        'show_mindmap_page': False, 'show_about_page': False,
                        'show_admin_page': False, 'show_execution_report_page': False,
                        'show_wiql_generation_page': False, 'show_manual_page': False,
                        'show_document_store_page': False,
                    })
            if self._get_permission_cached("azure_devops"):
                if st.button("🔎 Criar Query com IA", width="stretch", key="btn_wiql_sidebar", disabled=self.state.get('is_processing')):
                    self._navigate_or_confirm({
                        'show_wiql_generation_page': True, 'show_about_page': False,
                        'show_admin_page': False, 'show_execution_report_page': False,
                        'show_manual_page': False, 'show_document_store_page': False,
                        'show_mindmap_page': False, 'show_bug_page': False, 'show_work_item_page': False,
                    })
            if self._get_permission_cached("execution_report"):
                if st.button("📊 Relatório de Testes", width="stretch", key="btn_report_sidebar", disabled=self.state.get('is_processing')):
                    # Já estar na própria página de Relatório não conta como
                    # "sair" dela — não precisa do guarda aqui. Sair dos
                    # Testes de API com relatório não baixado, sim.
                    if self.state.get('show_api_tests_page') and self._api_tem_relatorio_nao_baixado():
                        self.state.set('_pending_navigation_after_api', {'show_api_tests_page': False, 'show_execution_report_page': True})
                        self.state.set('show_leave_api_modal', True)
                        st.rerun()
                    self.state.set('show_execution_report_page', True)
                    self.state.set('show_about_page', False)
                    self.state.set('show_admin_page', False)
                    self.state.set('show_wiql_generation_page', False)
                    self.state.set('show_manual_page', False)
                    self.state.set('show_document_store_page', False)
                    self.state.set('show_mindmap_page', False)
                    self.state.set('show_api_tests_page', False)
                    st.rerun()
            if self._get_permission_cached("testes_api"):
                if st.button("🔌 Testes de API", width="stretch", key="btn_api_tests_sidebar", disabled=self.state.get('is_processing')):
                    self._navigate_or_confirm({
                        'show_api_tests_page': True, 'show_about_page': False,
                        'show_admin_page': False, 'show_execution_report_page': False,
                        'show_wiql_generation_page': False, 'show_manual_page': False,
                        'show_document_store_page': False, 'show_mindmap_page': False,
                        'show_bug_page': False, 'show_work_item_page': False,
                    })
            if is_approver(self.config, current_username):
                # "Administração" agora fica visível pra qualquer aprovador,
                # não só pro dono — a página em si mostra Solicitações
                # Pendentes pra todo mundo, mas só o dono vê o cadastro de
                # aprovadores/permissões (isso é decidido dentro da própria
                # página, não aqui).
                if st.button("🛡️ Administração", width="stretch", key="btn_admin_sidebar", disabled=self.state.get('is_processing')):
                    self._navigate_or_confirm({
                        'show_admin_page': True, 'show_about_page': False,
                        'show_execution_report_page': False,
                        'show_wiql_generation_page': False, 'show_manual_page': False,
                        'show_document_store_page': False, 'show_mindmap_page': False, 'show_bug_page': False, 'show_work_item_page': False,
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
        if st.button(f"{marker} {label}", key=f"{active_key}_{index}", width="stretch", disabled=disabled):
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
            'fetch_wi_report': 'Buscando Work Items do Board',
            'suggest_report_narrative_wi': 'Consultando a IA para sugerir os textos',
            'generate_execution_report_wi': 'Gerando o Relatório de Testes',
            'fetch_wi_mindmap': 'Buscando Work Items do Board',
            'fetch_wi_manual': 'Buscando Work Items do Board',
            'generate_manual': 'Escrevendo o Manual com IA',
            'build_manual_pdf': 'Montando o PDF do Manual',
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
            'confirm_bug_de_caso': 'Criando o Bug no Azure DevOps',
            'confirm_bug_livre': 'Criando o Bug no Azure DevOps',
            'wi_confirm_criar': 'Criando o Work Item no Azure DevOps',
            'wi_lote_confirm': 'Criando os Work Items filhos no Azure DevOps',
            'wi_fetch_membros_unico': 'Buscando pessoas nos Times do projeto',
            'wi_fetch_membros_lote': 'Buscando pessoas nos Times do projeto',
            'wi_fetch_pais_unico': 'Buscando Work Items do Board',
            'wi_fetch_pais_lote': 'Buscando Work Items do Board',
            'wi_fetch_colunas_unico': 'Buscando Colunas dos Boards',
            'wi_fetch_tags_unico': 'Buscando Tags do projeto',
            'wi_fetch_tags_lote': 'Buscando Tags do projeto',
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
            if st.button("⏹️ Cancelar Processamento", key="qa_processing_cancel_btn", width="stretch", type="primary"):
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
                        if st.button(label, key=f"nav_step_{i}", width="stretch", disabled=is_processing):
                            if self._has_editing_in_progress():
                                confirm_navigate_away_modal(i)
                            else:
                                clear_widget_states()
                                self._set_step(i)
                                st.rerun()
                    else:
                        st.button(label, key=f"nav_step_{i}", width="stretch", disabled=True)
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
        self._render_area_help("assistente")
        if self.state.get('processing_interrupted'):
            st.info("⚠️ Processamento interrompido. Você pode continuar editando esta etapa.")

        opcoes_origem = ["📄 Enviar Documento(s)", "🎯 Gerar a partir de Work Items existentes no Azure DevOps"]
        if self._get_permission_cached("azure_query"):
            opcoes_origem.append("🔎 Gerar a partir de uma Query do Azure DevOps")

        origem = st.radio(
            "Como você quer fornecer a especificação?",
            options=opcoes_origem,
            index=0,
            key="step1_origem_radio",
            disabled=self.state.get('is_processing'),
            help="A segunda opção usa a Descrição e os Critérios de Aceite de Work Items já existentes como especificação — sem precisar enviar documento nenhum. A terceira parte de uma query já salva no Azure DevOps.",
        )
        st.divider()

        if origem.startswith("🔎"):
            self._step1_from_query()
            return

        if origem.startswith("🎯"):
            self._step1_from_work_items()
            return

        col1, col2 = st.columns(2)
        with col1:
            if not DOCUMENT_UPLOAD_ENABLED:
                st.warning(DOCUMENT_UPLOAD_DISABLED_MSG)
            uploaded_new = st.file_uploader(
                "Documento(s) de Requisitos (Máx 20MB cada) *",
                type=["pdf", "txt", "docx"],
                key='step1_uploaded_file',
                disabled=(not DOCUMENT_UPLOAD_ENABLED) or self.state.get('is_processing'),
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
                placeholder="Ex: Sistema de Login",
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
            tipo_documento = st.multiselect(
                "Tipo de Documento *",
                options=["Visão", "Requisitos Funcionais", "Especificações Funcionais", "Outros"],
                key="tipo_documento_input",
                disabled=self.state.get('is_processing'),
                placeholder="Selecione um ou mais...",
                help=(
                    "Pode escolher mais de um se o conjunto de documentos misturar níveis de "
                    "detalhe (ex.: um Documento de Visão + uma Especificação Funcional juntos). "
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
            conn = self._setup_azure_devops_connection(show_area_path_picker=False)
            if conn is None:
                return  # conexão com o Azure DevOps ainda incompleta — não mostra o resto do Passo 1 até terminar (ou desmarcar a opção)
            ado_client, ado_org, ado_project, _default_area_path = conn

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
                area_paths_step1 = st.multiselect(
                    "Area Path(s)",
                    options=area_path_options,
                    disabled=self.state.get('is_processing'),
                    key="step1_area_paths_select",
                    help="Selecione uma ou mais — a busca de Work Items considera todas juntas.",
                )
            area_path = area_paths_step1[0] if area_paths_step1 else ado_project

            with col_btn:
                with st.container(key="azure_blue_btn_fetch_wi_step1"):
                    st.button(
                        "🔄 Buscar Work Items do Board",
                        disabled=self.state.get('is_processing'),
                        key="btn_fetch_wi_step1",
                        on_click=self.trigger_action,
                        args=("fetch_wi_step1",),
                        width="stretch",
                    )
            if self.state.get('current_action') == 'fetch_wi_step1' and not self.state.get('show_interrupt_modal'):
                try:
                    paths_to_search = area_paths_step1 or [ado_project]
                    with st.spinner(f"Buscando Work Items em {len(paths_to_search)} Area Path(s)..."):
                        items_by_id = {}
                        for ap in paths_to_search:
                            for item in ado_client.fetch_work_items_by_area_path(ap):
                                items_by_id[item["id"]] = item
                        items = list(items_by_id.values())
                    self.state.set('step1_board_items', items)
                    if not items:
                        self._flash_warning("Nenhum Work Item encontrado" + (" nessas Area Paths." if area_paths_step1 else " neste projeto."))
                except Exception as error:
                    self._flash_error(f"Não foi possível buscar Work Items: {error}")
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
                    f"pra que o pré-vínculo funcione."
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

        with st.container(key="azure_blue_btn_analyze_docs"):
            st.button(
                "🔍 Executar Análise de Cobertura (IA)",
                width="stretch",
                on_click=self._iniciar_geracao_em_lotes,
                args=("analyze_docs", ("analise", "analise_imagens")),
                disabled=self.state.get('is_processing'),
            )

        if self.state.get('current_action') == 'analyze_docs' and not self.state.get('show_interrupt_modal'):
            # A preparação (extrair texto + imagens, registrar no log) roda UMA
            # vez e fica em '_analise_prep': as esperas de retentativa (imagens
            # ou análise) disparam reruns, e sem esse cache tudo isso — inclusive
            # o registro no log — se repetiria a cada rerun.
            prep = self.state.get('_analise_prep')
            if prep is None:
                with st.spinner("Extraindo texto dos documentos..."):
                    text = DocumentProcessor.extract_plain_text_multi(uploaded, self.state.get('step1_doc_work_item_map'))
                if not text:
                    st.error("Não foi possível extrair texto.")
                    self.clear_action()
                    st.rerun()
                    return
                doc_wi_map = self.state.get('step1_doc_work_item_map') or {}
                log_detail = f"Projeto '{project}' — {len(uploaded)} documento(s): {', '.join(f.name for f in uploaded)}"
                if doc_wi_map:
                    log_detail += f" ({len(doc_wi_map)} vinculado(s) a Work Items)"
                self._log("Analisar Documento(s)", "Passo 1", log_detail)

                # Extrai imagens relevantes do corpo dos documentos (ignora
                # cabeçalho/rodapé, ícones pequenos e logos repetidos) pra
                # interpretar cada uma via IA, inserindo a descrição de volta
                # no texto, na posição em que a imagem apareceu — assim a
                # IA de análise/geração "vê" o conteúdo visual também.
                img_result = DocumentProcessor.extract_images_with_context(uploaded)
                prep = {"text": text, "project": project, "images": img_result["images"], "warnings": img_result["warnings"]}
                self.state.set('_analise_prep', prep)

            for warn in prep["warnings"]:
                st.caption(f"ℹ️ {warn}")
            text = prep["text"]
            if prep["images"] and prep.get("descricoes") is None:
                # Uma imagem por execução, com a regra de retentativa: sem
                # espera entre imagens que deram certo; se uma falhar, espera
                # a janela do rate limit e tenta a MESMA de novo (até 3x) antes
                # de pulá-la. O resultado vai pro cache ('descricoes'): a
                # espera de retentativa da ANÁLISE, logo abaixo, também gera
                # reruns, e sem isso as imagens seriam interpretadas de novo.
                def interpretar_imagem(img):
                    try:
                        descricao = self.client.interpret_image(
                            img["bytes"], img["mime"], img["context"], prep["project"],
                            source_file=img["source_file"], location=img["location"],
                        )
                        return [f"\n[IMAGEM — {img['source_file']}, {img['location']}]: {descricao}\n"], None
                    except Exception as error:
                        return [], f"{img['source_file']} ({img['location']}): {self._erro_lote_amigavel(error)}"

                with st.status(f"Interpretando {len(prep['images'])} imagem(ns) do documento...", expanded=True) as status_img:
                    resultado_img = self._processar_um_lote_por_execucao(
                        "analise_imagens", lambda: prep["images"], interpretar_imagem, status_img,
                        nome_item="imagem", espera_entre_lotes=0,
                    )
                    if resultado_img is None:
                        return  # rerun() já disparado (próxima imagem ou espera pós-erro)
                    descricoes, erros_img = resultado_img
                    status_img.update(label=f"{len(descricoes)} imagem(ns) interpretada(s)"
                                      + (f", {len(erros_img)} pulada(s)" if erros_img else "") + ".", state="complete")
                prep["descricoes"], prep["erros_img"] = descricoes, erros_img
                self.state.set('_analise_prep', prep)
            for _, erro in prep.get("erros_img") or []:
                st.caption(f"⚠️ Não foi possível interpretar uma imagem, pulada: {erro}")
            if prep.get("descricoes"):
                text += "\n\n===== DESCRIÇÕES DE IMAGENS DO DOCUMENTO (geradas por IA) =====\n" + "".join(prep["descricoes"])

            self._run_analysis(text, prep["project"])

    _TAMANHO_LOTE_GERACAO = 8

    _TAMANHO_LOTE_MATRIZ_CHARS = 12000
    _MAX_PARAGRAFOS_POR_LOTE_MATRIZ = 15

    @classmethod
    def _dividir_texto_em_lotes(cls, texto: str) -> list:
        """
        Divide um texto longo em pedaços menores, respeitando quebras de
        parágrafo (nunca corta no meio de uma frase) — usado pra gerar a
        Matriz de Cobertura em lotes quando o documento é grande demais
        pra uma chamada de IA só.

        Dois gatilhos, o que vier primeiro: tamanho em caracteres OU
        quantidade de parágrafos. Só o tamanho em caracteres não é bom
        proxy suficiente pro tempo de geração — um documento CURTO mas
        denso (muitos requisitos curtos, um por parágrafo) pode pedir
        uma Matriz de 20+ linhas sem o texto em si ser longo; nesse
        caso, o limite de parágrafos pega o que o de caracteres sozinho
        deixaria passar.

        Documento pequeno/médio nos dois critérios: retorna uma lista
        com um único item — nenhuma mudança de comportamento.
        """
        tamanho_maximo = cls._TAMANHO_LOTE_MATRIZ_CHARS
        max_paragrafos = cls._MAX_PARAGRAFOS_POR_LOTE_MATRIZ
        texto = texto or ''
        paragrafos = [p for p in texto.split("\n\n") if p.strip()]

        if len(texto) <= tamanho_maximo and len(paragrafos) <= max_paragrafos:
            return [texto]

        lotes = []
        lote_atual = []
        tamanho_atual = 0
        for paragrafo in paragrafos:
            tamanho_paragrafo = len(paragrafo) + 2
            estouraria = (
                lote_atual
                and (tamanho_atual + tamanho_paragrafo > tamanho_maximo or len(lote_atual) >= max_paragrafos)
            )
            if estouraria:
                lotes.append("\n\n".join(lote_atual))
                lote_atual = []
                tamanho_atual = 0
            lote_atual.append(paragrafo)
            tamanho_atual += tamanho_paragrafo
        if lote_atual:
            lotes.append("\n\n".join(lote_atual))
        return lotes

    _TAMANHO_LOTE_PLANOS = 10

    # Teto de tokens por minuto do provedor mais apertado da cascata (Groq
    # gratuito: 8.000 TPM). Usamos 7.000 pra sobrar folga pro prompt fixo do
    # workflow e pra resposta, que também contam no mesmo limite.
    _TOKENS_POR_MINUTO = 7000

    @staticmethod
    def _matriz_do_lote(matriz: list, lote_casos: list) -> list:
        """
        Só as linhas da Matriz que os Casos DESTE lote referenciam
        (`requisitos_relacionados`).

        A Matriz inteira ia junto em TODO lote: com 48 linhas isso são ~2.900
        tokens repetidos a cada chamada, contra um teto de 8.000 por minuto —
        o que estourava a cota mesmo com o lote de Casos pequeno. O prompt dos
        Planos usa a Matriz só como contexto dos Casos que está organizando,
        então as linhas dos outros lotes não fazem falta.

        Sem correspondência nenhuma (Casos sem `requisitos_relacionados`),
        devolve a Matriz inteira — melhor mandar demais do que mandar vazio.
        """
        ids = {str(r).strip() for caso in (lote_casos or []) for r in (caso.get('requisitos_relacionados') or [])}
        if not ids:
            return matriz or []
        do_lote = [linha for linha in (matriz or []) if str(linha.get('id', '')).strip() in ids]
        return do_lote or (matriz or [])

    @classmethod
    def _espera_por_cota(cls, matriz: list, lote_exemplo: list) -> float:
        """
        Segundos entre lotes pra não estourar o teto de tokens POR MINUTO.

        Estima o tamanho de uma chamada (1 token ≈ 4 caracteres) e devolve a
        fração do minuto que ela ocupa da cota. Nunca menos que a espera curta
        padrão, nunca mais que 30s (acima disso é melhor a pessoa ver o erro e
        decidir do que o app ficar parado sozinho).
        """
        payload = json.dumps({"m": cls._matriz_do_lote(matriz, lote_exemplo), "c": lote_exemplo}, ensure_ascii=False)
        tokens = max(len(payload) // 4, 1)
        segundos = 60.0 * tokens / cls._TOKENS_POR_MINUTO
        return max(IaRetryMixin._ESPERA_ENTRE_LOTES_SEGUNDOS, min(segundos, 30.0))

    @staticmethod
    def _normalizar_titulo_caso(titulo: str) -> str:
        """
        Normaliza um título de Caso de Teste pra comparação TOLERANTE a
        pequenas diferenças de formatação — remove acentos, baixa a caixa,
        colapsa espaços. Usado só pra RECONCILIAR o título que a IA dos
        Planos devolve com o título real do Caso (ver
        _reconciliar_titulos_planos) — o restante do código continua
        comparando string exata contra o título original/canônico.
        """
        t = unicodedata.normalize('NFKD', titulo or '')
        t = ''.join(c for c in t if not unicodedata.combining(c))
        t = re.sub(r'\s+', ' ', t).strip().lower()
        return t

    def _reconciliar_titulos_planos(self, planos: list, test_cases: list) -> tuple:
        """
        Corrige, EM CADA Suíte de CADA Plano, o título de cada 'caso'
        referenciado pra bater com o título REAL de algum Caso de Teste
        (comparação normalizada — sem acento, minúsculo, espaços
        colapsados), substituindo pelo título CANÔNICO verdadeiro.

        Por quê: os Planos de Teste são gerados numa chamada de IA
        SEPARADA da que gerou os Casos (self.client.trigger_plans, que
        passa por até 5 provedores de IA em cascata de fallback) — o
        vínculo entre "qual título de Caso pertence a qual Suíte" depende
        inteiramente da IA reproduzir o título EXATO, caractere por
        caractere. Qualquer diferença mínima (acento, espaço extra,
        maiúscula, reticências) faz o vínculo falhar — em TODO o resto do
        código essa comparação é sempre string EXATA (`titulo in
        titulos_necessarios`, `case_ids[titulo]`), sem normalização
        nenhuma. Sem essa reconciliação logo na origem, o sintoma é o
        Test Plan subir criado no Azure DevOps com Suítes vazias, sem
        nenhum erro ANTES do push de verdade — só depois, e só se TODAS
        as Suítes falharem ao mesmo tempo (ver aviso em
        _push_static_suites_azure_devops).

        Retorna (planos_corrigidos, titulos_orfaos) — títulos_orfaos é a
        lista de títulos referenciados que não bateram com NENHUM Caso
        real, nem normalizados (provavelmente a IA inventou/alucinou um
        título) — removidos das Suítes e reportados separadamente, pra
        não ficarem escondidos atrás de um "Test Plan vazio" silencioso.

        CUIDADO com colisão de chave normalizada: se DOIS Casos de Teste
        DIFERENTES normalizarem pra mesma chave (ex.: um só difere do
        outro por acento/espaço/maiúscula — plausível, já que os Casos
        também são gerados em lotes de IA), usar essa chave pra "corrigir"
        um título vinculava silenciosamente o título de Suíte ao Caso
        ERRADO, mesmo quando esse título já batia EXATO com o Caso
        certo. Por isso: (1) título com correspondência EXATA nunca passa
        pela normalização — sempre vence antes de qualquer "correção";
        (2) uma chave normalizada que colide entre Casos DIFERENTES fica
        marcada como ambígua e NUNCA é usada pra corrigir nada — vira
        órfão (mais seguro reportar como "não bateu" do que arriscar
        vincular ao Caso errado).
        """
        titulos_reais_exatos = {tc.get('titulo', '') for tc in test_cases if tc.get('titulo')}

        titulo_real_por_chave = {}
        chaves_ambiguas = set()
        for tc in test_cases:
            titulo_real = tc.get('titulo', '')
            chave = self._normalizar_titulo_caso(titulo_real)
            if not chave:
                continue
            existente = titulo_real_por_chave.get(chave)
            if existente is not None and existente != titulo_real:
                chaves_ambiguas.add(chave)
            else:
                titulo_real_por_chave[chave] = titulo_real

        titulos_orfaos = set()
        contador_suite_sem_nome = 0
        for plano in planos:
            for suite in plano.get('suites', []):
                casos_corrigidos = []
                for titulo in suite.get('casos', []):
                    if titulo in titulos_reais_exatos:
                        casos_corrigidos.append(titulo)
                        continue
                    chave = self._normalizar_titulo_caso(titulo)
                    titulo_real = None if chave in chaves_ambiguas else titulo_real_por_chave.get(chave)
                    if titulo_real:
                        casos_corrigidos.append(titulo_real)
                    else:
                        titulos_orfaos.add(titulo)
                suite['casos'] = casos_corrigidos
                # Suíte sem nome (raro, mas a IA já devolveu isso): o push
                # pro Azure DevOps recusa criar uma Suíte sem nome — sem
                # essa correção, o modal de confirmação prometia "criar
                # essa Suíte" (só checa se tem Caso, não se tem nome), mas
                # na hora H ela era pulada silenciosamente e os Casos
                # ficavam órfãos, sem Suíte nenhuma, no Azure DevOps.
                if not (suite.get('nome') or '').strip() and casos_corrigidos:
                    contador_suite_sem_nome += 1
                    suite['nome'] = f"Suíte sem nome ({contador_suite_sem_nome})"
        return planos, sorted(titulos_orfaos)

    def _gerar_planos_em_lotes(self, doc_text: str, matriz: list, test_cases: list, answers: dict,
                                 project: str, status):
        """
        Gera Planos de Teste em lotes menores de Casos de Teste —
        processando 1 lote por execução do script (ver
        _processar_um_lote_por_execucao pro motivo).

        NÃO manda o texto do documento original nessa chamada — conferi
        o prompt real do workflow (Doc_QA_Plans_HA) e a lógica de
        organização (identificar escopos, agrupar em Suítes) analisa os
        CASOS DE TESTE e a MATRIZ, não o documento bruto. Mandar o
        documento inteiro em TODO lote inflava o tamanho da requisição
        sem necessidade — foi exatamente isso que estourou o limite de
        tokens por minuto do Groq (8000 TPM) num teste real, mesmo com
        poucos Casos no lote, porque o documento sozinho já ocupava a
        maior parte do espaço.

        Retorna (planos_combinados, erros) só quando TODOS os lotes
        terminarem.
        """
        tam = self._TAMANHO_LOTE_PLANOS

        def montar_lotes():
            return [test_cases[i:i + tam] for i in range(0, len(test_cases), tam)]

        def processar_um_lote(lote_casos):
            try:
                resp = self.client.trigger_plans("", self._matriz_do_lote(matriz, lote_casos), lote_casos, answers, project)
                return resp.get('planos_de_teste') or [], None
            except Exception as error:
                return None, self._erro_lote_amigavel(error)

        # O limite do Groq gratuito é por MINUTO (8.000 tokens somando TODAS as
        # chamadas): com 5 lotes disparados em ~30s, o problema não é o tamanho
        # de um lote, é a SOMA deles na mesma janela. Espaça os lotes pelo peso
        # real do que vai ser enviado, em vez dos 5s fixos.
        espera = self._espera_por_cota(matriz, test_cases[:tam])
        resultado = self._processar_um_lote_por_execucao("geracao_planos", montar_lotes, processar_um_lote, status,
                                                         espera_entre_lotes=espera)
        if resultado is None:
            return None
        planos_combinados, erros = resultado

        nomes_vistos = {}
        for plano in planos_combinados:
            nome = plano.get('nome', '(sem nome)')
            nomes_vistos[nome] = nomes_vistos.get(nome, 0) + 1
            if nomes_vistos[nome] > 1:
                plano['nome'] = f"{nome} ({nomes_vistos[nome]})"

        planos_combinados, titulos_orfaos = self._reconciliar_titulos_planos(planos_combinados, test_cases)

        return planos_combinados, erros, titulos_orfaos

    def _gerar_matriz_em_lotes(self, doc_text: str, answers: dict, project: str, tipo_documento: str, status):
        """
        Gera a Matriz de Cobertura em lotes menores do TEXTO do
        documento — processando 1 lote por execução do script (ver
        _processar_um_lote_por_execucao pro motivo). Renumera tudo no
        final (MC-001, MC-002, ...) — cada chamada de IA recomeça a
        numeração sozinha, então sem isso haveria ID repetido entre
        lotes.

        Retorna (matriz_combinada, erros) só quando TODOS os lotes
        terminarem — erros é uma lista de (número_do_lote, mensagem)
        pros que falharam.
        """
        def montar_lotes():
            return self._dividir_texto_em_lotes(doc_text)

        def processar_um_lote(lote_texto):
            try:
                resp = self.client.trigger_matrix(lote_texto, answers, project, tipo_documento)
                return resp.get('matriz') or [], None
            except Exception as error:
                return None, self._erro_lote_amigavel(error)

        resultado = self._processar_um_lote_por_execucao("geracao_matriz", montar_lotes, processar_um_lote, status)
        if resultado is None:
            return None
        matriz_combinada, erros = resultado
        for idx, row in enumerate(matriz_combinada, start=1):
            row['id'] = f"MC-{idx:03d}"
        return matriz_combinada, erros

    @staticmethod
    def _normalizar_mc_id_geracao(valor) -> str:
        """
        Extrai só a parte "MC-XXX" de um ID, ignorando qualquer sufixo de
        ambiente que venha depois (ex.: "MC-001 HML" -> "MC-001") — mesma
        lógica usada em pdf_report.py pra Rastreabilidade, aplicada aqui
        pra checar cobertura logo após gerar os Casos, não só na hora do
        PDF (assim já avisa a pessoa antes mesmo dela gerar o documento).
        """
        m = re.match(r'^\s*(MC-\d+)', str(valor or ''), re.IGNORECASE)
        return m.group(1).upper() if m else str(valor or '').strip().upper()

    def _invalidar_estado_dependente_de_casos(self):
        """
        Limpa tudo que foi derivado de uma versão ANTERIOR de `test_cases` —
        chamado sempre que os Casos de Teste são (re)gerados no Passo 3.

        Por quê: repetir "Gerar Casos de Teste" (ex.: depois de um lote
        falhar/travar) troca a lista de Casos, mas Planos de Teste
        (`test_plans`), vínculos com Work Items (`ado_wi_case_links`),
        cache de IDs já criados no Azure (`ado_test_case_ids`) e a análise
        de duplicados (`ado_duplicate_case_titles`/`ado_duplicate_analysis`)
        continuavam com os TÍTULOS da tentativa antiga. Na hora do push pro
        Azure DevOps, esses títulos não batem mais com os Casos atuais —
        os Test Cases certos entram em `case_ids`, mas a Suite/vínculo é
        montado a partir da lista velha, então nenhum Caso entra de
        verdade nela: o Test Plan sobe criado, porém vazio, sem nenhum
        erro que explique o motivo. Cada chave abaixo volta ao default
        (`SessionState.DEFAULTS`) no próximo rerun, já que ela é
        reconstruída do zero a cada execução do script.
        """
        for key in (
            'test_plans', 'csv_cases', 'csv_plans',
            'pdf_report_bytes', 'pdf_report_fingerprint',
            'ado_wi_case_links', 'ado_test_case_ids', 'ado_case_links',
            'ado_duplicate_case_titles', 'ado_duplicate_analysis',
            'ado_excluded_case_titles', 'ado_suggest_message',
            'ado_wi_prelinked_marker', 'ado_wi_prelink_diag',
            'ado_full_push_log', 'ado_static_push_log',
        ):
            self.state.delete(key)

    def _gerar_casos_em_lotes(self, doc_text: str, matriz_completa: list, answers: dict,
                                project: str, tipo_documento: str, status):
        """
        Gera Casos de Teste em lotes menores da Matriz de Cobertura —
        processando 1 lote por execução do script (ver
        _processar_um_lote_por_execucao pro motivo). Um lote que falhar
        não derruba os outros — só fica de fora do resultado final,
        reportado separadamente.

        Retorna (casos_combinados, erros) só quando TODOS os lotes
        terminarem — erros é uma lista de (índice_do_lote, mensagem)
        pros que falharam.
        """
        tam = self._TAMANHO_LOTE_GERACAO

        def montar_lotes():
            return [matriz_completa[i:i + tam] for i in range(0, len(matriz_completa), tam)]

        def processar_um_lote(lote):
            try:
                resp = self.client.trigger_generation(doc_text, lote, answers, project, tipo_documento)
                return resp.get('casos_de_teste') or [], None
            except Exception as error:
                return None, self._erro_lote_amigavel(error)

        return self._processar_um_lote_por_execucao("geracao_casos", montar_lotes, processar_um_lote, status)

    def _run_analysis(self, text: str, project: str):
        """
        Roda a análise de IA (mesma do Passo 1) e navega pro Passo 2 se der
        certo. Reutilizado tanto pelo Passo 1 (documento enviado) quanto
        pela geração a partir de Work Items do Azure DevOps.

        Com a regra de retentativa (_chamar_ia_com_retentativas): quem chama
        precisa poder ser re-executado a cada rerun da espera sem refazer
        trabalho caro — por isso os chamadores guardam a preparação em
        '_analise_prep' e este método a limpa ao terminar.
        """
        with st.status("Aguarde enquanto a análise é processada… Isso pode levar alguns minutos...", expanded=True) as status:
            resultado = self._chamar_ia_com_retentativas(
                "analise", lambda: {"text": text, "project": project},
                lambda p: self.client.trigger_analysis(p["text"], p["project"]), status,
            )
            if resultado is None:
                return  # rerun() já disparado (espera pós-erro)
            payload, resp, erro = resultado
            if erro:
                status.update(label="Falha na análise.", state="error")
                self.state.delete('_analise_prep')
                self._err(erro)
                self.clear_action()
                st.rerun()
                return
            status.update(label="Análise concluída.", state="complete")
        self.state.delete('_analise_prep')
        self.state.set('doc_text', payload["text"])
        self.state.set('project_name', payload["project"])
        self.state.set('questions', resp.get('duvidas') or [])
        self._set_step(2, allow_during_processing=True)
        self.clear_action()
        st.rerun()

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
            if st.button("← Voltar", width="stretch", disabled=self.state.get('is_processing')):
                self._set_step(1)
                st.rerun()
        with c2:
            st.button(
                "📊 Gerar Matriz de Cobertura",
                width="stretch",
                type="primary",
                on_click=self._iniciar_geracao_em_lotes,
                args=("generate_matrix", "geracao_matriz"),
                disabled=self.state.get('is_processing'),
            )

        if self.state.get('current_action') == 'generate_matrix' and not self.state.get('show_interrupt_modal'):
            with st.status("Estruturando Matriz de Cobertura...", expanded=True) as status:
                resultado_matriz = self._gerar_matriz_em_lotes(
                    self.state.get('doc_text'),
                    self.state.get('step_2_answers', answers),
                    self.state.get('project_name'),
                    ", ".join(self.state.get('tipo_documento') or []),
                    status,
                )
                if resultado_matriz is None:
                    return  # rerun() já disparado dentro do lote — essa linha nunca roda de verdade
                matriz, erros = resultado_matriz
                if not matriz:
                    status.update(label="Falha ao gerar a Matriz.", state="error", expanded=True)
                    if erros:
                        self._flash_error(f"Não foi possível gerar a Matriz — todos os lotes falharam: {erros[0][1]}")
                    else:
                        self._flash_error("Matriz vazia.")
                    self.clear_action()
                    st.rerun()
                else:
                    if erros:
                        status.update(label=f"Concluído com {len(erros)} lote(s) com falha.", state="complete")
                        self._flash_warning(
                            f"{len(matriz)} linha(s) da Matriz gerada(s), mas {len(erros)} lote(s) do "
                            "documento falharam — a Matriz pode estar incompleta. Revise antes de prosseguir."
                        )
                    else:
                        status.update(label=f"Matriz gerada — {len(matriz)} linha(s).", state="complete")
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
                                if st.button("💾 Salvar Alterações", key=f"save_m_{i}", type="primary", width="stretch"):
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
                                if st.button("✖ Cancelar", key=f"cancel_m_{i}", width="stretch"):
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
                            if st.button("✏️ Editar", key=f"btn_edit_m_{i}", width="stretch", disabled=self.state.get('is_processing')):
                                self.state.set(f"edit_m_{i}", True)
                                self.state.set('active_matriz_row', i)
                                st.rerun()
                        with cd:
                            if st.button("🗑️ Excluir", key=f"btn_del_m_{i}", type="primary", width="stretch", disabled=self.state.get('is_processing')):
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
                        if st.button("💾 Salvar Novo Cenário", key="save_newm", type="primary", width="stretch"):
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
                        if st.button("✖ Cancelar", key="cancel_newm", width="stretch"):
                            confirm_discard_new_modal('adding_matriz_row')
        else:
            if st.button("➕ Adicionar Novo Cenário à Matriz", width="stretch", disabled=editing_any or self.state.get('is_processing')):
                self.state.set('active_matriz_row', None)
                self.state.set('adding_matriz_row', True)
                st.rerun()

        st.divider()
        c1, c2 = st.columns([1, 3])
        with c1:
            if st.button("← Voltar", width="stretch", disabled=self.state.get('is_processing')):
                self._set_step(2)
                st.rerun()
        with c2:
            if editing_any or self.state.get('adding_matriz_row'):
                st.warning("⚠️ Salve ou cancele a edição/criação em aberto para prosseguir.")
            else:
                with st.container(key="azure_blue_btn_generate_cases"):
                    st.button(
                        "🚀 Gerar Casos de Teste",
                        width="stretch",
                        on_click=self._iniciar_geracao_em_lotes,
                        args=("generate_cases", "geracao_casos"),
                        disabled=self.state.get('is_processing'),
                    )

        if self.state.get('current_action') == 'generate_cases' and not self.state.get('show_interrupt_modal'):
            matriz_completa = self.state.get('matriz') or []
            with st.status("Gerando Casos de Teste...", expanded=True) as status:
                resultado_casos = self._gerar_casos_em_lotes(
                    self.state.get('doc_text'),
                    matriz_completa,
                    self.state.get('user_answers'),
                    self.state.get('project_name'),
                    ", ".join(self.state.get('tipo_documento') or []),
                    status,
                )
                if resultado_casos is None:
                    return
                casos, erros = resultado_casos

                # Checagem de cobertura: mesmo quando um lote "dá certo" (sem
                # exceção nenhuma), a IA pode devolver menos Casos do que os
                # itens da Matriz pedidos naquele lote — um "sucesso"
                # incompleto que não aparece em `erros`. Compara a Matriz
                # inteira contra o que os Casos realmente cobrem, ignorando
                # sufixo de ambiente (mesma normalização do PDF), pra pegar
                # isso também.
                ids_matriz = {self._normalizar_mc_id_geracao(row.get('id', '')) for row in matriz_completa}
                ids_cobertos = {
                    self._normalizar_mc_id_geracao(mc_id)
                    for caso in casos
                    for mc_id in (caso.get('requisitos_relacionados') or [])
                }
                ids_sem_cobertura = sorted(ids_matriz - ids_cobertos - {''})

                if not casos and erros:
                    status.update(label="Falha ao gerar Casos de Teste.", state="error", expanded=True)
                    self._flash_error("Não foi possível gerar nenhum Caso de Teste — todos os lotes falharam.")
                    self.clear_action()
                    st.rerun()
                elif erros or ids_sem_cobertura:
                    status.update(label="Concluído, mas com pendência(s) — veja o aviso.", state="complete")
                    partes_aviso = [f"{len(casos)} Caso(s) gerado(s)."]
                    if erros:
                        numeros_lotes_falhos = ", ".join(str(n) for n, _ in erros)
                        partes_aviso.append(f"{len(erros)} lote(s) de geração falharam (lote(s) {numeros_lotes_falhos}).")
                    if ids_sem_cobertura:
                        partes_aviso.append(
                            f"{len(ids_sem_cobertura)} item(ns) da Matriz ficaram sem nenhum Caso, mesmo sem "
                            f"erro reportado ({', '.join(ids_sem_cobertura)})."
                        )
                    partes_aviso.append("Revise a lista abaixo, gere manualmente o que faltar, ou volte e tente de novo.")
                    self._flash_warning(" ".join(partes_aviso))
                    self._invalidar_estado_dependente_de_casos()
                    self.state.set('test_cases', casos)
                    self._set_step(4, allow_during_processing=True)
                    self.clear_action()
                    st.rerun()
                else:
                    status.update(label=f"{len(casos)} Caso(s) de Teste gerado(s).", state="complete")
                    self._invalidar_estado_dependente_de_casos()
                    self.state.set('test_cases', casos)
                    self._set_step(4, allow_during_processing=True)
                    self.clear_action()
                    st.rerun()

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
                                if st.button("💾 Salvar Caso de Teste", key=f"save_tc_{idx}", type="primary", width="stretch"):
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
                                if st.button("✖ Cancelar", key=f"cancel_tc_{idx}", width="stretch"):
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
                            if st.button("✏️ Editar", key=f"btn_edit_tc_{idx}", width="stretch", disabled=self.state.get('is_processing')):
                                self.state.set(f"edit_tc_{idx}", True)
                                self.state.set('active_test_case_row', idx)
                                st.rerun()
                        with cd:
                            if st.button("🗑️ Excluir", key=f"btn_del_tc_{idx}", type="primary", width="stretch", disabled=self.state.get('is_processing')):
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
                        if st.button("💾 Salvar Novo Caso de Teste", key="save_newtc", type="primary", width="stretch"):
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
                        if st.button("✖ Cancelar", key="cancel_newtc", width="stretch"):
                            confirm_discard_new_modal('adding_test_case')
        else:
            if st.button("➕ Adicionar Novo Caso de Teste", width="stretch", disabled=editing_any or self.state.get('is_processing')):
                self.state.set('active_test_case_row', None)
                self.state.set('adding_test_case', True)
                st.rerun()

        st.divider()
        c1, c2 = st.columns([1, 3])
        with c1:
            if st.button("← Voltar", width="stretch", disabled=self.state.get('is_processing')):
                self._set_step(3)
                st.rerun()
        with c2:
            if editing_any or self.state.get('adding_test_case'):
                st.warning("⚠️ Salve ou cancele a edição/criação em aberto para prosseguir.")
            else:
                st.button(
                    "📁 Gerar Planos de Teste",
                    width="stretch",
                    type="primary",
                    on_click=self._iniciar_geracao_em_lotes,
                    args=("generate_plans", "geracao_planos"),
                    disabled=self.state.get('is_processing'),
                )

        if self.state.get('current_action') == 'generate_plans' and not self.state.get('show_interrupt_modal'):
            with st.status("Gerando Planos de Teste...", expanded=True) as status:
                resultado_planos = self._gerar_planos_em_lotes(
                    self.state.get('doc_text'),
                    self.state.get('matriz'),
                    self.state.get('test_cases'),
                    self.state.get('user_answers'),
                    self.state.get('project_name'),
                    status,
                )
                if resultado_planos is None:
                    return
                plans, erros, titulos_orfaos = resultado_planos
                if not plans:
                    status.update(label="Falha ao gerar Planos de Teste.", state="error", expanded=True)
                    if erros:
                        self._flash_error(f"Não foi possível gerar Planos — todos os lotes falharam: {erros[0][1]}")
                    else:
                        self._flash_error("Nenhum Plano de Teste retornado. Valide a chave JSON de saída no n8n.")
                    self.clear_action()
                    st.rerun()
                else:
                    avisos = []
                    if erros:
                        avisos.append(
                            f"{len(erros)} lote(s) de Casos falharam — pode faltar algum Caso sem Plano."
                        )
                    if titulos_orfaos:
                        preview = ", ".join(titulos_orfaos[:5]) + ("..." if len(titulos_orfaos) > 5 else "")
                        avisos.append(
                            f"{len(titulos_orfaos)} título(s) que os Planos referenciaram não correspondem a "
                            f"nenhum Caso de Teste real e foram removidos das Suítes ({preview}) — a IA pode "
                            "ter reescrito o título ao gerar os Planos. Revise as Suítes no Passo 5 antes de "
                            "integrar com o Azure DevOps."
                        )
                    if avisos:
                        status.update(label=f"Concluído com {len(erros)} lote(s) com falha.", state="complete")
                        self._flash_warning(f"{len(plans)} Plano(s) gerado(s), mas " + " ".join(avisos))
                    else:
                        status.update(label=f"{len(plans)} Plano(s) de Teste gerado(s).", state="complete")
                    self.state.set('test_plans', plans)
                    self._set_step(5, allow_during_processing=True)
                    self.clear_action()
                    st.rerun()

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
                                if st.button("💾 Salvar Plano", key=f"save_p_{i}", type="primary", width="stretch"):
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
                                if st.button("✖ Cancelar", key=f"cancel_p_{i}", width="stretch"):
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
                            if st.button("✏️ Editar", key=f"btn_edit_p_{i}", width="stretch"):
                                self.state.set(f"edit_p_{i}", True)
                                self.state.set('active_test_plan_row', i)
                                st.rerun()
                        with cd:
                            if st.button("🗑️ Excluir", key=f"btn_del_p_{i}", type="primary", width="stretch"):
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
                        if st.button("💾 Salvar Novo Plano", key="save_newp", type="primary", width="stretch"):
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
                        if st.button("✖ Cancelar", key="cancel_newp", width="stretch"):
                            confirm_discard_new_modal('adding_test_plan')
        else:
            if st.button("➕ Adicionar Novo Plano de Teste", width="stretch", disabled=editing_any or self.state.get('is_processing')):
                self.state.set('active_test_plan_row', None)
                self.state.set('adding_test_plan', True)
                st.rerun()

        st.divider()
        c1, c2 = st.columns([1, 3])
        with c1:
            if st.button("← Voltar", width="stretch", disabled=self.state.get('is_processing')):
                self._set_step(4)
                st.rerun()
        with c2:
            if editing_any or self.state.get('adding_test_plan'):
                st.warning("⚠️ Salve ou cancele a edição/criação em aberto para prosseguir.")
            else:
                st.button(
                    "📥 Consolidar e Construir Artefatos",
                    width="stretch",
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

        st.markdown("#### 📄 Exportações CSV – Azure DevOps")
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
                width="stretch",
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
                width="stretch",
                type="primary",
            )

        st.divider()
        author_name = st.text_input(
            "Nome de quem está gerando este relatório",
            value=self.state.get('author_name', ''),
            key="author_name_input",
            help="Aparece no rodapé do PDF. Pode vir preenchido automaticamente a partir do Passo 7 — confira se é o seu nome mesmo antes de gerar.",
        )
        self.state.set('author_name', author_name)

        st.divider()
        col_pdf, col_azure = st.columns(2)
        with col_pdf:
            st.markdown("#### 📑 Documentação Técnica – PDF Report")
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
                width="stretch",
                type="primary",
            )
        with col_azure:
            st.markdown("#### 🔗 Azure DevOps")
            st.caption("Envie os artefatos gerados direto para o seu projeto no Azure DevOps.")
            with st.container(key="azure_blue_btn_goto_step7"):
                if st.button("🔗 Ir para Integração com Azure DevOps →", width="stretch", disabled=self.state.get('is_processing'), key="btn_goto_step7"):
                    self._set_step(7, allow_during_processing=True)
                    st.rerun()

        self._render_document_storage_section(
            "Documentação QA (Passo 6)", project,
            [
                {"tipo": "csv", "nome_arquivo": f"QA_Cases_{safe_name}.csv", "conteudo": csv_cases},
                {"tipo": "csv", "nome_arquivo": f"QA_Plans_{safe_name}.csv", "conteudo": csv_plans},
                {"tipo": "pdf", "nome_arquivo": f"QA_Report_{safe_name}.pdf", "conteudo": pdf_bytes},
            ],
        )

        st.divider()
        c1, c2 = st.columns(2)
        with c1:
            if st.button("← Voltar", width="stretch", disabled=self.state.get('is_processing'), key="btn_back_step6"):
                self._set_step(5)
                st.rerun()
        with c2:
            if st.button("🔄 Nova Análise", width="stretch", type="primary", disabled=self.state.get('is_processing'), key="btn_new_step6"):
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

        # Reconcilia de novo, contra os Casos ATUAIS, toda vez que essa tela
        # é aberta — não só na hora da geração original. Sem isso, editar ou
        # excluir um Caso no Passo 4 DEPOIS de já ter gerado os Planos no
        # Passo 5 deixava as Suítes referenciando um título que não existe
        # mais, silenciosamente (nenhum aviso em lugar nenhum até esse
        # exato momento) — reproduzindo o mesmo "Test Plan sobe vazio" por
        # um caminho diferente (edição manual, não drift da IA). Chamar de
        # novo aqui é seguro/idempotente: em dado já reconciliado, não
        # muda nada.
        test_plans, titulos_orfaos_reedicao = self._reconciliar_titulos_planos(test_plans, test_cases)
        self.state.set('test_plans', test_plans)
        if titulos_orfaos_reedicao:
            preview = ", ".join(titulos_orfaos_reedicao[:5]) + ("..." if len(titulos_orfaos_reedicao) > 5 else "")
            st.warning(
                f"⚠️ {len(titulos_orfaos_reedicao)} título(s) referenciado(s) pelos Planos não "
                f"correspondem mais a nenhum Caso de Teste atual ({preview}) — provavelmente um Caso foi "
                "editado ou excluído no Passo 4 depois dos Planos já terem sido gerados. Esses títulos "
                "foram removidos das Suítes automaticamente. Revise o Passo 5 antes de integrar."
            )

        st.markdown("#### 📋 Test Plan (destino no Azure DevOps)")
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
                "🔍 Buscar Test Plans existentes no Projeto",
                disabled=self.state.get('is_processing'),
                key="btn_fetch_static_plans",
                on_click=self.trigger_action,
                args=("fetch_static_plans",),
            )
        if self.state.get('current_action') == 'fetch_static_plans' and not self.state.get('show_interrupt_modal'):
            try:
                # Test Plans pertencem ao PROJETO, não à Area Path (mesmo
                # que uma Area Path tenha sido escolhida em outra parte do
                # fluxo, pra achar Work Items no board) — busca sem filtro.
                with st.spinner("Buscando Test Plans do projeto..."):
                    existing = ado_client.list_test_plans()
                self.state.set('ado_static_existing_plans', existing)
                self.state.set('ado_static_existing_plans_path', fallback_area_path)
            except AzureDevOpsError as error:
                self._flash_error(f"Não foi possível buscar Test Plans existentes: {error}")
            except Exception as error:
                self._flash_error(f"Erro inesperado: {error}")
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
                type="primary", width="stretch",
                disabled=self.state.get('is_processing') or not plan_name.strip(),
                key="btn_confirm_static_push",
            ):
                # NÃO lê 'ado_excluded_case_titles' nem 'ado_duplicate_case_titles'
                # aqui — as duas são chaves de session_state COMPARTILHADAS com o
                # modo "Vincular a Work Items", que tem sua PRÓPRIA UI pra
                # preenchê-las (exclusão manual, e detecção de duplicata via IA
                # contra Work Items existentes — conceito que não existe nesse
                # modo "Sem Work Items"). O modo "com Work Items" inclusive seta
                # um valor padrão em 'ado_excluded_case_titles' sozinho, só de
                # renderizar a tela dele (exclui todo Caso ainda sem Work Item
                # vinculado) — se a pessoa só passar o olho por aquele modo
                # dentro da MESMA sessão, esse valor "vaza" pra cá e fazia (bug
                # real, confirmado consultando a API do Azure DevOps direto: Test
                # Plan criado, ZERO Casos/Suítes de verdade) este modo achar que
                # tudo já existia/estava excluído, pulando a criação inteira
                # silenciosamente — sem disparar nenhuma das travas de segurança,
                # já que 'titulos_necessarios' continuava não-vazio.
                existing_case_ids = self.state.get('ado_test_case_ids') or {}
                titulos_necessarios = set()
                suites_display = []
                for plan in test_plans:
                    for suite in plan.get('suites', []):
                        casos_titulos = list(suite.get('casos', []))
                        titulos_necessarios.update(casos_titulos)
                        if casos_titulos:
                            suites_display.append((suite.get('nome', ''), casos_titulos))
                cases_to_create_titles = [
                    tc.get('titulo') for tc in test_cases
                    if tc.get('titulo') in titulos_necessarios
                    and tc.get('titulo') not in existing_case_ids
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
            st.markdown("##### 📋 Resultado da integração")
            for line in log:
                st.write(line)
            self._api_render_registrar_test_run(ado_client)

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
        # 'ado_excluded_case_titles'/'ado_duplicate_case_titles' PROPOSITALMENTE
        # não são lidas aqui — ver comentário em _render_step7_static_suite_mode
        # (o botão "Confirmar e Integrar") sobre por que consultá-las nesse modo
        # causava Test Plan vazio.
        case_ids = dict(self.state.get('ado_test_case_ids') or {})
        titled = AzureCsvFormatter._titled(test_cases, self.state.get('ambiente_testes', ''))
        log = []

        # Trava de segurança: sem Casos de Teste e/ou sem Planos gerados,
        # não há nada de verdade pra criar/vincular — antes disso o código
        # criava o Test Plan igual, resultando num Test Plan vazio no Azure
        # DevOps sem nenhum erro explicando o motivo.
        if not test_cases or not test_plans:
            log.append(
                "❌ Não há Casos de Teste e/ou Planos de Teste gerados — nada foi enviado ao Azure DevOps. "
                "Volte às etapas de geração antes de integrar."
            )
            self.state.set('ado_static_push_log', log)
            self._flash_error("Nenhum Caso/Plano de Teste gerado — a integração foi cancelada antes de criar o Test Plan.")
            self.clear_action()
            st.rerun()
            return

        # Segunda trava: `test_plans` pode não estar vazio mas ainda assim
        # não ter NENHUMA Suíte com Caso dentro (ex.: um Plano "casca vazia"
        # devolvido pela IA quando o provedor falhou/ficou sem cota no Passo
        # 5-6, mas mesmo assim retornou um plano com nome e 0 suítes). A
        # checagem "not test_plans" acima não pega isso — a lista tem 1
        # item, só que inútil. Sem essa trava aqui, o Test Plan é criado no
        # Azure DevOps igual, vazio, sem nenhum aviso.
        titulos_necessarios = set()
        for plan in test_plans:
            for suite in plan.get('suites', []):
                titulos_necessarios.update(suite.get('casos', []))
        if not titulos_necessarios:
            log.append(
                "❌ Os Planos de Teste gerados não têm nenhuma Suíte com Caso associado — nada foi "
                "enviado ao Azure DevOps. Volte ao Passo 5/6 e gere os Planos de Teste novamente."
            )
            self.state.set('ado_static_push_log', log)
            self._flash_error(
                "Os Planos de Teste gerados estão vazios (sem Suítes/Casos) — a integração foi cancelada "
                "antes de criar o Test Plan. Gere os Planos de Teste de novo antes de tentar integrar."
            )
            self.clear_action()
            st.rerun()
            return

        # 1) Test Plan: cria novo ou reaproveita existente.
        if existing_plan_id:
            plan_id = existing_plan_id
            self.state.set('ado_last_plan_id', plan_id)
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
                self.state.set('ado_last_plan_id', plan_id)
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
        # Azure DevOps (os já criados nessa mesma sessão são pulados —
        # `case_ids` vem de 'ado_test_case_ids', cache legítimo desta MESMA
        # função pra não recriar em caso de retry). `titulos_necessarios`
        # já foi calculado antes de criar o Test Plan (trava de segurança
        # acima).
        cases_to_create = [
            tc for tc in test_cases
            if tc.get('titulo') in titulos_necessarios
            and tc.get('titulo') not in case_ids
        ]
        if cases_to_create:
            total = len(cases_to_create)
            progress = st.progress(0, text=f"Criando Test Cases no Azure DevOps... (0/{total})")
            done = 0

            # Calculados AQUI (thread principal): dentro do executor não
            # existe st.session_state, e a tag sairia "criado-por:desconhecido".
            tag_autor = self._tag_criado_por()
            atribuir_a = self.state.get('ado_test_case_assigned_to') or None

            def _create_case(tc):
                titulo = tc.get('titulo')
                titulo_prefixado = titled.get(titulo, titulo)
                result = ado_client.create_test_case(
                    titulo_prefixado, tc.get('pre_condicoes', ''), tc.get('passos', []), area_path, initial_state,
                    tags=tag_autor, assigned_to=atribuir_a,
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
                casos_titulos = list(suite.get('casos', []))
                case_id_list = [case_ids[t] for t in casos_titulos if t in case_ids]
                if nome_suite and case_id_list:
                    suite_tasks.append((nome_suite, case_id_list))

        # suite_tasks_ok guarda só as Suítes que REALMENTE tiveram sucesso
        # na API (não suite_tasks, que é só o planejamento de ANTES de
        # tentar) — sem isso, uma falha de API (outage, permissão,
        # throttling) em create_test_suite/add_cases_to_suite não derrubava
        # nenhuma exceção pro chamador, mas também não tirava a Suíte da
        # contagem final: total_casos_vinculados continuava batendo com
        # total_casos_esperados mesmo com a Suíte de verdade vazia no Azure
        # DevOps, e nenhum aviso disparava — só uma linha de log solta,
        # fácil de não notar no meio de várias linhas "✅".
        suite_tasks_ok = []
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
                    suite_tasks_ok.append((nome_suite, case_id_list))
                except AzureDevOpsError as error:
                    log.append(f"❌ Falha ao processar a Suíte '{nome_suite}': {error}")
                except Exception as error:
                    log.append(f"❌ Erro inesperado na Suíte '{nome_suite}': {error}")
                progress2.progress(idx / total_suites, text=f"Criando/atualizando Suítes no Azure DevOps... ({idx}/{total_suites})")

        total_casos_esperados = sum(
            len(suite.get('casos', []))
            for plan in test_plans for suite in plan.get('suites', [])
        )
        total_casos_vinculados = sum(len(case_id_list) for _, case_id_list in suite_tasks_ok)

        if total_casos_esperados and not total_casos_vinculados:
            self._flash_error(
                f"O Test Plan foi criado, mas NENHUM dos {total_casos_esperados} Caso(s) de Teste "
                "esperado(s) foi vinculado a nenhuma Suíte — o Test Plan ficou vazio no Azure DevOps. "
                "Confira o log abaixo: se aparecer '❌ Falha ao criar um Caso de Teste', a criação em "
                "si falhou; se os Casos aparecem como criados mas mesmo assim nenhuma Suíte foi "
                "processada, os títulos gerados pelos Planos não bateram com os títulos dos Casos."
            )
        elif total_casos_esperados and total_casos_vinculados < total_casos_esperados:
            self._flash_error(
                f"Só {total_casos_vinculados} de {total_casos_esperados} Caso(s) de Teste esperado(s) "
                "foram vinculados a alguma Suíte no Azure DevOps — confira o log abaixo pra ver quais "
                "faltaram e por quê."
            )

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
        st.markdown("#### 📋 1. Escolha o Test Plan anterior")
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
                self._flash_error(f"Não foi possível buscar Test Plans: {error}")
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
                    width="stretch",
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
                # 'rotulo' é o identificador usado em TODA a tela (opções do
                # multiselect, payload pra IA, resolução final do vínculo) —
                # não 'titulo' puro. Título de Test Case NÃO é único no Azure
                # DevOps (comum repetir algo genérico tipo "Validar campo
                # obrigatório" em Suítes diferentes do mesmo Test Plan); usar
                # só o título como chave colapsava Casos diferentes com o
                # mesmo texto num único ID (o último da lista "vencia"),
                # podendo vincular ao Azure DevOps um Caso diferente do que a
                # pessoa escolheu — sem erro, aviso ou log. Só acrescenta
                # "(Caso #ID)" quando o título realmente se repete, pra não
                # poluir a tela à toa quando não há ambiguidade nenhuma.
                contagem_titulo = {}
                for cid, titulo in seen.items():
                    contagem_titulo[titulo] = contagem_titulo.get(titulo, 0) + 1
                old_cases = [
                    {
                        "id": cid,
                        "titulo": titulo,
                        "rotulo": titulo if contagem_titulo[titulo] <= 1 else f"{titulo} (Caso #{cid})",
                    }
                    for cid, titulo in seen.items()
                ]
                self.state.set('ado_recon_old_plan_id', old_plan_id)
                self.state.set('ado_recon_old_cases', old_cases)
                if not old_cases:
                    self._flash_warning("Nenhum Caso de Teste encontrado nesse Test Plan.")
            except Exception as error:
                self._flash_error(f"Não foi possível buscar os Casos de Teste: {error}")
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
        st.markdown("#### 🎯 2. Busque os Work Items novos")
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
                self._flash_error(f"Não foi possível buscar Work Items: {error}")
            self.clear_action()
            st.rerun()

        board_items = self.state.get('ado_recon_board_items') or []
        if not board_items:
            return

        st.divider()
        st.markdown("#### 🤖 3. Sugestão automática com IA")
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
                on_click=self._iniciar_geracao_em_lotes,
                args=("suggest_recon_links", "vinculos_recon"),
            )
        if self.state.get('current_action') == 'suggest_recon_links' and not self.state.get('show_interrupt_modal'):
            with st.status("Consultando a IA para sugerir os vínculos...", expanded=True) as status:
                resultado = self._chamar_ia_com_retentativas(
                    "vinculos_recon",
                    lambda: {"items": selected_items, "cases": [{"titulo": c["rotulo"]} for c in old_cases],
                             "project": self.state.get('project_name')},
                    lambda p: self.client.trigger_matching(p["items"], p["cases"], p["project"]), status,
                )
                if resultado is None:
                    return  # rerun() já disparado (espera pós-erro)
                _payload, result, erro = resultado
                status.update(label="Falha ao consultar a IA." if erro else "Sugestão recebida.",
                              state="error" if erro else "complete")
            if erro:
                self._flash_error(f"Não foi possível obter a sugestão da IA: {erro}")
            else:
                try:
                    links = {}
                    for vinculo in result.get("vinculos", []):
                        wid = str(vinculo.get("work_item_id"))
                        links[wid] = vinculo.get("casos", [])
                    self.state.set('ado_recon_wi_case_links', links)
                    for item in selected_items:
                        widget_key = f"ado_recon_multiselect_{item['id']}"
                        st.session_state[widget_key] = [c for c in links.get(str(item['id']), []) if c in [oc['rotulo'] for oc in old_cases]]
                except Exception as error:
                    self._flash_error(f"Não foi possível obter a sugestão da IA: {error}")
            self.clear_action()
            st.rerun()

        st.divider()
        st.markdown("#### ✏️ 4. Revisar e confirmar")
        st.caption("Cada Caso só pode ser vinculado a UM Work Item — se já estiver escolhido em outro, some das opções aqui.")
        links = dict(self.state.get('ado_recon_wi_case_links') or {})
        ordered_wids = [str(item['id']) for item in selected_items]
        links = self._dedupe_case_assignments(links, ordered_wids)
        case_titles = [c["rotulo"] for c in old_cases]
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
                "🔗 Confirmar e Vincular no Azure DevOps", type="primary", width="stretch",
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
            st.markdown("##### 📋 Resultado da reconciliação")
            for line in log:
                st.write(line)

    def _push_reconciliation(self, ado_client, old_plan_id: int, old_cases: list, links: dict):
        """
        Cria (se preciso) a Requirement Suite de cada Work Item e vincula os
        Casos já existentes a ele. Resolve o vínculo por 'rotulo' (título +
        ID quando o título se repete), não por título puro — título de Test
        Case não é único no Azure DevOps; usar só o título colapsaria dois
        Casos diferentes com o mesmo texto num único ID (o último da lista
        "venceria"), arriscando vincular o Caso errado sem nenhum aviso.
        """
        case_id_by_rotulo = {c["rotulo"]: c["id"] for c in old_cases}
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
                    for rotulo in titulos:
                        case_id = case_id_by_rotulo.get(rotulo)
                        if not case_id:
                            continue
                        try:
                            ado_client.link_test_case_to_work_item(case_id, work_item_id)
                            log.append(f"　　→ '{rotulo}' vinculado ao Work Item {work_item_id}.")
                        except AzureDevOpsError as error:
                            log.append(f"　　❌ Falha ao vincular '{rotulo}': {error}")
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
                "← Voltar", width="stretch",
                disabled=self.state.get('is_processing'), key=f"btn_back_step7_{key_suffix}",
            ):
                self._set_step(back_step)
                st.rerun()
        with c2:
            if st.button(
                "🔄 Nova Análise", width="stretch", type="primary",
                disabled=self.state.get('is_processing'), key=f"btn_new_step7_{key_suffix}",
            ):
                self.state.set('show_new_analysis_modal', True)
                st.rerun()

    @staticmethod
    def _filtrar_por_coluna_e_tag(itens: list, area_path_selecionada: bool, key_prefix: str) -> list:
        """
        Filtro opcional por Coluna do Board e/ou Tag — só aparece quando
        uma Area Path específica foi escolhida (não faz sentido filtrar
        coluna/tag "do projeto inteiro", que pode ter vários boards/times
        misturados). Deriva as opções DIRETO dos itens já buscados (não é
        uma chamada nova à API) — evita a complexidade de descobrir qual
        Team é dono de qual Area Path só pra listar colunas.

        `itens`: lista de dicts com 'board_column' e 'tags' (já vem assim
        de fetch_work_items_by_area_path / get_work_items_basic_fields).
        Retorna a lista filtrada (ou a lista original, sem filtro nenhum
        selecionado ou sem Area Path específica escolhida).
        """
        if not area_path_selecionada or not itens:
            return itens

        colunas_disponiveis = sorted({it.get('board_column', '') for it in itens if it.get('board_column')})
        tags_disponiveis = sorted({t for it in itens for t in (it.get('tags') or [])})

        if not colunas_disponiveis and not tags_disponiveis:
            return itens

        st.caption("Filtro opcional — restringe a lista abaixo por Coluna do Board e/ou Tag:")
        col_f1, col_f2 = st.columns(2)
        colunas_escolhidas = []
        tags_escolhidas = []
        with col_f1:
            if colunas_disponiveis:
                colunas_escolhidas = st.multiselect(
                    "📋 Coluna do Board", options=colunas_disponiveis,
                    key=f"{key_prefix}_filtro_coluna",
                    help="Deriva das colunas que os itens encontrados realmente têm — não é uma lista fixa do projeto.",
                )
        with col_f2:
            if tags_disponiveis:
                tags_escolhidas = st.multiselect(
                    "🏷️ Tag", options=tags_disponiveis,
                    key=f"{key_prefix}_filtro_tag",
                    help="Deriva das tags que os itens encontrados realmente têm.",
                )

        if not colunas_escolhidas and not tags_escolhidas:
            return itens

        filtrados = []
        for it in itens:
            if colunas_escolhidas and it.get('board_column', '') not in colunas_escolhidas:
                continue
            if tags_escolhidas and not (set(it.get('tags') or []) & set(tags_escolhidas)):
                continue
            filtrados.append(it)
        return filtrados

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
        st.markdown("##### 🔧 Configuração do Azure DevOps")
        st.caption(
            "Organização, Projeto e Area Path vêm direto do Azure DevOps — nada aqui é digitado livremente."
        )

        st.markdown("##### 🔑 Personal Access Token (PAT)")

        pat_compartilhado = ""
        try:
            pat_compartilhado = st.secrets.get("AZURE_DEVOPS_PAT", "")
        except Exception:
            pat_compartilhado = ""

        if pat_compartilhado:
            user_pat = pat_compartilhado
            self.state.set('ado_user_pat', user_pat)
            st.caption(
                "PAT compartilhado, configurado pelo administrador — você não precisa "
                "informar nada aqui. Toda ação continua registrada em seu nome no "
                "histórico interno do app (visível só para administradores); só o "
                "Azure DevOps em si (campos como \"Created by\") vai mostrar a conta "
                "dona do token compartilhado, não a sua."
            )
        else:
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
                self._log("PAT Validado", "Azure DevOps", "PAT validado com sucesso")

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
                        width="stretch",
                    )

        if self.state.get('current_action') == 'fetch_projects' and not self.state.get('show_interrupt_modal'):
            probe_proj = AzureDevOpsClient(ado_org, "", user_pat)
            try:
                with st.spinner(f"Buscando projetos em '{ado_org}'..."):
                    projects = probe_proj.list_projects()
                self.state.set('ado_available_projects', projects)
                self.state.set('ado_projects_org', ado_org)
            except AzureDevOpsError as error:
                self._flash_error(f"Não foi possível listar projetos de '{ado_org}': {error}")
                self.state.set('ado_available_projects', [])
            except Exception as error:
                self._flash_error(f"Erro inesperado ao listar projetos: {error}")
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
                self._flash_error(f"Erro inesperado ao listar Area Paths: {error}")
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
                    width="stretch",
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
                    self._flash_warning("Nenhum Work Item encontrado" + (" nessas Area Paths (além de Test Cases)." if area_paths else " nesse projeto (além de Test Cases)."))
            except AzureDevOpsError as error:
                self._flash_error(f"{error}")
            except Exception as error:
                self._flash_error(f"Erro inesperado: {error}")
            self.clear_action()
            st.rerun()

        board_items_fetched = self.state.get('ado_board_items') or []
        if not board_items_fetched:
            st.info("Busque os Work Items do Board (botão acima) antes de escolher o Modo de Envio.")
            st.divider()
            self._render_step7_back_and_new("waiting_wi")
            return

        st.divider()
        self._render_step7_assigned_to(ado_client)

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
        tipo_doc = self.state.get('tipo_documento') or []
        default_modo_idx = 1 if "Visão" in tipo_doc else 0
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
            st.markdown("#### 🤖 Sugestão automática de vínculos com IA")
            st.caption(
                "Envia os Work Items selecionados abaixo e os Casos de Teste gerados pro n8n, "
                "que devolve uma sugestão de quais casos se relacionam a quais Work Items. Você "
                "pode ajustar tudo manualmente depois, antes de confirmar."
            )

            wi_labels = {
                f"{item['id']} - {item['title']} ({item['type']}, {item['state']})": item
                for item in self._filtrar_por_coluna_e_tag(board_items, bool(area_paths), "ado_wi_matching")
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
                    on_click=self._iniciar_geracao_em_lotes,
                    args=("suggest_ado_links", "vinculos_ia"),
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
            duplicate_analysis = self.state.get('ado_duplicate_analysis') or {}
            if duplicate_analysis:
                confirmados = [t for t, info in duplicate_analysis.items() if info.get("mesmo_contexto")]
                falsos_positivos = [t for t, info in duplicate_analysis.items() if not info.get("mesmo_contexto")]
                with st.expander(f"🔁 {len(duplicate_analysis)} possível(is) duplicidade(s) — análise de contexto pela IA", expanded=True):
                    st.caption(
                        "Esses Casos novos pareceram parecidos (por título) com Casos que já existem "
                        "no Work Item correspondente. A IA analisou o **conteúdo real** (pré-condições "
                        "e passos) de cada par pra confirmar se testam mesmo a mesma coisa. **Nada é "
                        "excluído automaticamente no Azure DevOps** — a análise é só uma recomendação; "
                        "casos confirmados como duplicados não aparecem pré-selecionados na revisão "
                        "abaixo, mas você pode incluí-los manualmente se discordar da IA."
                    )
                    for t in confirmados:
                        info = duplicate_analysis[t]
                        rec = info.get("recomendacao")
                        if rec == "novo_melhor":
                            badge = "🆕 A IA acha o Caso **novo** mais completo"
                            sugestao = (
                                f"Considere revisar e **excluir manualmente** o Caso antigo (ID "
                                f"{info['existing_id']}) no Azure DevOps depois, se decidir usar o novo."
                            )
                        elif rec == "existente_melhor":
                            badge = "📌 A IA acha o Caso **já existente** mais completo"
                            sugestao = "Provavelmente não vale a pena subir o novo."
                        else:
                            badge = "⚖️ A IA considera os dois equivalentes em qualidade"
                            sugestao = "Decisão mais neutra — os dois parecem cobrir o mesmo tanto."
                        st.markdown(f"**{t}**")
                        st.caption(
                            f"{badge}. Comparado com o Caso já existente (ID {info['existing_id']} "
                            f"- \"{info['existing_titulo']}\"). {sugestao}"
                        )
                        if info.get("motivo"):
                            st.caption(f"💬 *{info['motivo']}*")
                        st.divider()
                    if falsos_positivos:
                        st.success(
                            f"✅ {len(falsos_positivos)} caso(s) pareciam duplicados pelo título, mas a "
                            "IA confirmou que testam coisas diferentes de verdade — continuam disponíveis "
                            "normalmente na revisão abaixo:"
                        )
                        for t in falsos_positivos:
                            info = duplicate_analysis[t]
                            st.caption(f"• **{t}** — {info.get('motivo', 'contexto diferente do Caso existente.')}")
                        st.divider()

            st.divider()
            st.markdown("#### ✏️ Revisar e confirmar vínculos")
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
            st.markdown("#### 🚫 Excluir Casos de Teste do Envio")
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
            st.markdown("#### 📋 Test Plan")

            with st.container(key="azure_blue_btn_fetch_existing_plans"):
                st.button(
                    "🔍 Buscar Test Plans existentes no Projeto",
                    disabled=self.state.get('is_processing'),
                    key="btn_fetch_existing_plans",
                    on_click=self.trigger_action,
                    args=("fetch_existing_plans",),
                )
            if self.state.get('current_action') == 'fetch_existing_plans' and not self.state.get('show_interrupt_modal'):
                try:
                    # Test Plans pertencem ao PROJETO, não à Area Path (mesmo
                    # que uma ou mais Area Paths tenham sido escolhidas acima,
                    # pra achar Work Items no board) — busca sem filtro.
                    with st.spinner("Buscando Test Plans do projeto..."):
                        existing = ado_client.list_test_plans()
                    self.state.set('ado_existing_plans_in_path', existing)
                    self.state.set('ado_existing_plans_fetched', True)
                except AzureDevOpsError as error:
                    self._flash_error(f"Não foi possível buscar Test Plans existentes: {error}")
                except Exception as error:
                    self._flash_error(f"Erro inesperado: {error}")
                self.clear_action()
                st.rerun()

            existing_plans = self.state.get('ado_existing_plans_in_path') or [] if self.state.get('ado_existing_plans_fetched') else []

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
                        width="stretch",
                        disabled=self.state.get('is_processing'),
                        key="btn_open_ado_full_confirm",
                    ):
                        if not plan_name.strip():
                            self.state.set('ado_plan_name_error', "❌ Informe um nome para o Test Plan antes de continuar.")
                            st.rerun()
                        else:
                            existing_case_ids = self.state.get('ado_test_case_ids') or {}
                            duplicate_titles_now = set(self.state.get('ado_duplicate_case_titles') or [])
                            # A tela promete explicitamente que dá pra incluir
                            # manualmente um Caso marcado como duplicata pela IA
                            # "se discordar" (vinculando ele a um Work Item no
                            # multiselect acima) — mas até aqui isso era ignorado:
                            # duplicate_titles_now excluía o título de qualquer
                            # jeito, mesmo já escolhido manualmente. Um título
                            # presente em items_with_cases É a pessoa discordando
                            # da IA na prática — conta como override explícito.
                            titulos_vinculados_manualmente = {
                                t for casos in items_with_cases.values() for t in casos
                            }
                            cases_to_create_titles = [
                                tc.get('titulo') for tc in test_cases
                                if tc.get('titulo') not in excluded_titles
                                and tc.get('titulo') not in existing_case_ids
                                and (
                                    tc.get('titulo') not in duplicate_titles_now
                                    or tc.get('titulo') in titulos_vinculados_manualmente
                                )
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
                st.markdown("##### 📋 Resultado da integração")
                for line in log:
                    st.write(line)
                self._api_render_registrar_test_run(ado_client)

        st.divider()
        self._render_step7_back_and_new("main")

    def _execution_report_page(self):
        st.subheader("📊 Relatório de Testes (execução)")
        st.caption(
            "Documenta o que já foi EXECUTADO no Azure DevOps — não depende de terminar o "
            "assistente de geração, só busca dados que já existem lá."
        )
        self._render_area_help("relatorio")

        c_back, c_new = st.columns(2)
        with c_back:
            if st.button("← Voltar", key="btn_report_back_top", width="stretch"):
                self._navigate_or_confirm({'show_execution_report_page': False})
        with c_new:
            if st.button("🔄 Novo Relatório", key="btn_new_report_top", width="stretch", disabled=not self.state.get('report_pdf_bytes')):
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
        st.markdown("##### 🧭 Como você quer montar este relatório?")
        modo_relatorio = st.radio(
            "Fonte dos dados",
            options=[
                "📋 Por Test Plan(s) (Suítes e Casos de dentro dele)",
                "🎯 Por Work Items (ex.: User Stories, de qualquer coluna do board)",
            ],
            index=0,
            key="report_source_mode_radio",
            disabled=self.state.get('is_processing'),
            help=(
                "Por Test Plan: precisa que os Casos de Teste já tenham vínculo 'Tests' com um "
                "Work Item pra aparecerem com status/Matriz. Por Work Items: você escolhe o "
                "Work Item direto (de qualquer coluna), e o relatório usa os Casos já vinculados a ele."
            ),
        )
        st.divider()

        if modo_relatorio.startswith("🎯"):
            self._render_execution_report_by_work_items(ado_client, ado_project, area_paths)
        else:
            self._render_execution_report_section(ado_client, ado_project, area_paths)

        st.divider()
        if st.button("← Voltar", key="btn_report_back_bottom"):
            self._navigate_or_confirm({'show_execution_report_page': False})

    def _step1_from_work_items(self):
        st.caption(
            "Escolhe Work Items existentes no Azure DevOps pra usar como especificação, no lugar "
            "de enviar um documento — a Descrição e os Critérios de Aceite de cada um viram o "
            "texto de entrada, e o resto do processo segue igual (Dúvidas → Matriz → Casos → Planos). "
            "Disponível pra qualquer pessoa logada — usa a mesma conexão com o Azure DevOps do Passo 7."
        )

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
                    width="stretch",
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
                self._flash_error(f"{error}")
                self.state.set('wigen_board_items', [])
            except Exception as error:
                self._flash_error(f"Erro inesperado: {error}")
                self.state.set('wigen_board_items', [])
            self.clear_action()
            st.rerun()

        board_items = self.state.get('wigen_board_items') or []
        if not board_items:
            return

        board_items = self._filtrar_por_coluna_e_tag(board_items, bool(area_paths), "wigen")

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

        selected_wis_full = [wi_labels[label] for label in selected_labels]
        self._render_wi_spec_confirmation(
            ado_client, ado_project, selected_wis_full, selected_ids,
            confirm_action_key="confirm_wigen",
            log_flow_label="Gerar a partir de Work Items",
        )

    def _render_wi_spec_confirmation(self, ado_client, ado_project: str, selected_wis_full: list, selected_ids: list,
                                       confirm_action_key: str, log_flow_label: str):
        """
        Parte compartilhada entre 'Gerar a partir de Work Items' (varre o
        board por Area Path) e 'Gerar a partir de Query' (roda uma query
        já salva no Azure DevOps) — depois que a lista de Work Items já
        foi escolhida, não importa por qual caminho, o resto do fluxo é
        idêntico: Nome do Test Plan, Ambiente, documentos complementares
        opcionais, Tipo de Documento (se houver complementar), confirmar.

        confirm_action_key: precisa ser diferente entre quem chama, senão
        os dois fluxos disputariam o mesmo 'current_action' se ambos
        ficassem montados ao mesmo tempo (não deveria acontecer, já que
        só um modo fica visível por vez, mas evita acoplamento frágil).
        log_flow_label: nome do fluxo pro log de auditoria (ex.: 'Gerar a
        partir de Work Items' vs. 'Gerar a partir de Query').
        """
        col_name, col_amb = st.columns(2)
        with col_name:
            project_name = self._render_project_name_field(ado_project, "project_name", "Nome do Test Plan *")
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

        st.divider()
        st.markdown("##### 📎 Documento(s) complementar(es) (opcional)")
        st.caption(
            "Além dos Work Items escolhidos acima, você pode anexar documento(s) pra "
            "complementar a especificação — o texto de todos é combinado numa única análise, "
            "junto com a Descrição e os Critérios de Aceite dos Work Items."
        )
        if not DOCUMENT_UPLOAD_ENABLED:
            st.warning(DOCUMENT_UPLOAD_DISABLED_MSG)
        uploaded_complementares = st.file_uploader(
            "Documento(s) complementar(es) (PDF, DOCX ou TXT)",
            type=["pdf", "docx", "txt"],
            accept_multiple_files=True,
            key="wigen_uploaded_files_input",
            disabled=(not DOCUMENT_UPLOAD_ENABLED) or self.state.get('is_processing'),
        )
        if uploaded_complementares:
            self.state.set('wigen_uploaded_files', uploaded_complementares)
        uploaded_complementares = self.state.get('wigen_uploaded_files') or []

        MAX_FILE_MB_WIGEN = 20
        oversized_wigen = [f.name for f in uploaded_complementares if f.size > MAX_FILE_MB_WIGEN * 1024 * 1024]
        if oversized_wigen:
            st.error(f"❌ Arquivo(s) excedem o limite de {MAX_FILE_MB_WIGEN}MB cada: {', '.join(oversized_wigen)}")
            self.state.set('wigen_uploaded_files', [])
            uploaded_complementares = []

        if uploaded_complementares:
            tipo_documento = st.multiselect(
                "Tipo de Documento *",
                options=["Visão", "Requisitos Funcionais", "Especificações Funcionais", "Outros"],
                placeholder="Selecione um ou mais...",
                key="wigen_tipo_documento_input",
                disabled=self.state.get('is_processing'),
                help=(
                    "Pode escolher mais de um se os Work Items/documentos complementares "
                    "misturarem níveis de detalhe. Calibra o nível de detalhe que a IA assume ao "
                    "gerar Matriz/Casos a partir da Descrição/Critérios de Aceite dos Work Items "
                    "(e dos documentos complementares) — mesma lógica usada quando a especificação "
                    "vem de um documento enviado. Work Items com descrição pouco detalhada (ex.: só "
                    "um título e um parágrafo) tendem a se comportar como 'Visão'; Work Items com "
                    "Critérios de Aceite bem escritos tendem a 'Especificações Funcionais'."
                ),
            )
            if tipo_documento:
                self.state.set('tipo_documento', tipo_documento)
        else:
            tipo_documento = []
            self.state.set('tipo_documento', [])

        # Tipo de Documento só é obrigatório se realmente subiu algum
        # documento complementar — sem documento nenhum (só Work Items),
        # não faz sentido exigir essa calibração.
        tipo_documento_obrigatorio = bool(uploaded_complementares)
        falta_tipo_documento = tipo_documento_obrigatorio and not tipo_documento

        with st.container(key=f"azure_blue_btn_{confirm_action_key}"):
            st.button(
                "✅ Confirmar e Gerar Especificação",
                type="primary",
                width="stretch",
                disabled=self.state.get('is_processing') or not project_name.strip() or not ambiente or falta_tipo_documento,
                key=f"btn_{confirm_action_key}",
                on_click=self._iniciar_geracao_em_lotes,
                args=(confirm_action_key, "analise"),
            )
        if not ambiente or falta_tipo_documento:
            msg = "Selecione o Ambiente dos Testes"
            msg += " e o Tipo de Documento" if falta_tipo_documento else ""
            st.caption(f"{msg} para habilitar a confirmação.")

        if self.state.get('current_action') == confirm_action_key and not self.state.get('show_interrupt_modal'):
            prep = self.state.get('_analise_prep')
            if prep is not None:
                # Rerun de uma espera de retentativa da análise: não busca os
                # Work Items de novo nem registra no log outra vez.
                self._run_analysis(prep["text"], prep["project"])
                return
            try:
                with st.spinner(f"Buscando detalhes completos de {len(selected_ids)} Work Item(s)..."):
                    details = ado_client.get_work_items_full_details(selected_ids)
                if not details:
                    st.error("❌ Não foi possível buscar os detalhes dos Work Items selecionados.")
                    self.clear_action()
                    st.rerun()
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

                    log_detail = f"Projeto '{project_name.strip()}' — {len(details)} Work Item(s): {', '.join(str(wi['id']) for wi in details)}"
                    if uploaded_complementares:
                        with st.spinner(f"Extraindo texto de {len(uploaded_complementares)} documento(s) complementar(es)..."):
                            texto_docs = DocumentProcessor.extract_plain_text_multi(uploaded_complementares)
                        if texto_docs:
                            text = text + "\n\n" + texto_docs
                            log_detail += f" + {len(uploaded_complementares)} documento(s) complementar(es)"

                    self._log(log_flow_label, "Passo 1", log_detail)
                    self.state.set('_analise_prep', {"text": text, "project": project_name.strip()})
                    self._run_analysis(text, project_name.strip())
            except Exception as error:
                self._flash_error(f"Erro ao buscar detalhes dos Work Items: {error}")
                self.clear_action()
                st.rerun()

    def _step1_from_query(self):
        """
        Modo 'Gerar a partir de Query' — a pessoa escolhe uma query JÁ
        SALVA no Azure DevOps (My Queries ou Shared Queries), o app roda
        e traz os Work Items que ela retorna, com a mesma tela de
        inclusão/exclusão do modo 'Gerar a partir de Work Items' — dali
        em diante, o fluxo é idêntico (reaproveita
        _render_wi_spec_confirmation).
        """
        st.caption(
            "Escolhe uma query já salva no Azure DevOps (sua ou compartilhada) — os Work Items "
            "que ela retorna viram a lista pra escolher o que entra na especificação, igual ao "
            "modo 'Gerar a partir de Work Items'."
        )

        conn = self._setup_azure_devops_connection(show_area_path_picker=False)
        if conn is None:
            return
        ado_client, ado_org, ado_project, _default_area_path = conn

        # Se o atalho "Usar pra Gerar Testes" (dentro de Criar Query com IA)
        # já rodou a query e populou isso, pula direto pra escolha de
        # Work Items — sem precisar escolher uma query salva de novo.
        veio_do_atalho = bool(self.state.get('query_wigen_board_items')) and not self.state.get('query_wigen_available_queries')
        if veio_do_atalho:
            st.info("✨ Usando o resultado da query que você acabou de criar em '🔎 Criar Query com IA'.")
            if st.button("🔄 Escolher uma query salva em vez disso", key="btn_wiql_atalho_reset"):
                self.state.set('query_wigen_board_items', [])
                st.rerun()
        else:
            with st.container(key="azure_blue_btn_fetch_queries"):
                st.button(
                    "🔄 Buscar Queries Salvas",
                    disabled=self.state.get('is_processing'),
                    key="btn_fetch_saved_queries",
                    on_click=self.trigger_action,
                    args=("fetch_saved_queries",),
                    width="stretch",
                )

            if self.state.get('current_action') == 'fetch_saved_queries' and not self.state.get('show_interrupt_modal'):
                try:
                    with st.spinner("Buscando suas queries salvas no Azure DevOps..."):
                        queries = ado_client.list_saved_queries()
                    self.state.set('query_wigen_available_queries', queries)
                    if 'query_wigen_select' in st.session_state:
                        del st.session_state['query_wigen_select']
                    if not queries:
                        self._flash_warning("Nenhuma query salva encontrada nesse projeto (nem em 'My Queries', nem em 'Shared Queries').")
                except AzureDevOpsError as error:
                    self._flash_error(f"{error}")
                    self.state.set('query_wigen_available_queries', [])
                except Exception as error:
                    self._flash_error(f"Erro inesperado: {error}")
                    self.state.set('query_wigen_available_queries', [])
                self.clear_action()
                st.rerun()

            queries = self.state.get('query_wigen_available_queries') or []
            if not queries:
                st.caption("Busque as queries salvas acima pra continuar.")
                return

        if not veio_do_atalho:
            query_labels = {q["path"]: q for q in queries}
            st.divider()
            selected_query_label = st.selectbox(
                "📋 Query salva",
                options=list(query_labels.keys()),
                key="query_wigen_select",
                disabled=self.state.get('is_processing'),
                index=None,
                placeholder="Escolha uma query...",
            )

            with st.container(key="azure_blue_btn_run_query"):
                st.button(
                    "▶️ Rodar Query",
                    disabled=self.state.get('is_processing') or not selected_query_label,
                    key="btn_run_saved_query",
                    on_click=self.trigger_action,
                    args=("run_saved_query",),
                    width="stretch",
                )

            if self.state.get('current_action') == 'run_saved_query' and not self.state.get('show_interrupt_modal'):
                try:
                    query_obj = query_labels[selected_query_label]
                    with st.spinner(f"Rodando a query '{query_obj['name']}'..."):
                        resultado = ado_client.run_wiql_query(query_obj['wiql'])
                        ids_to_show = [item['id'] for item in resultado['items']]
                        details = ado_client.get_work_items_basic_fields(ids_to_show) if ids_to_show else []
                    self.state.set('query_wigen_board_items', details)
                    self.state.set('query_wigen_selected_ids', [])
                    if 'query_wigen_multiselect' in st.session_state:
                        del st.session_state['query_wigen_multiselect']
                    if not details:
                        self._flash_warning(f"A query '{query_obj['name']}' não retornou nenhum Work Item.")
                except AzureDevOpsError as error:
                    self._flash_error(f"Erro ao rodar a query: {error}")
                    self.state.set('query_wigen_board_items', [])
                except Exception as error:
                    self._flash_error(f"Erro inesperado: {error}")
                    self.state.set('query_wigen_board_items', [])
                self.clear_action()
                st.rerun()

        board_items = self.state.get('query_wigen_board_items') or []
        if not board_items:
            return

        wi_labels = {
            f"{item['id']} - {item['title']} ({item['type']}, {item['state']})": item
            for item in board_items
        }
        selected_ids = self.state.get('query_wigen_selected_ids') or []
        label_by_id = {item['id']: label for label, item in wi_labels.items()}
        current_labels = [label_by_id[wid] for wid in selected_ids if wid in label_by_id]

        selected_labels = st.multiselect(
            "🎯 Work Items para usar como especificação",
            options=list(wi_labels.keys()),
            default=current_labels,
            disabled=self.state.get('is_processing'),
            key="query_wigen_multiselect",
            help="Selecione quantos quiser — clique em vários seguidos, sem precisar segurar Ctrl/Shift.",
        )
        selected_ids = [wi_labels[label]['id'] for label in selected_labels]
        self.state.set('query_wigen_selected_ids', selected_ids)

        if not selected_labels:
            st.caption("Nenhum Work Item selecionado ainda — escolha acima.")
            return

        selected_wis_full = [wi_labels[label] for label in selected_labels]
        self._render_wi_spec_confirmation(
            ado_client, ado_project, selected_wis_full, selected_ids,
            confirm_action_key="confirm_query_wigen",
            log_flow_label="Gerar a partir de Query",
        )

    def _mind_map_page(self):
        st.subheader("🧠 Mapa Mental")
        if st.button("← Voltar", key="btn_mindmap_back"):
            self.state.set('show_mindmap_page', False)
            st.rerun()

        if not self._get_permission_cached("mapa_mental"):
            st.error("❌ Você não tem permissão pra acessar esta área.")
            return

        st.caption(
            "Visualiza a hierarquia como um mapa mental — a partir da sessão atual, de um "
            "grupo de documentos já armazenado, ou de Work Items escolhidos direto no Azure DevOps."
        )
        self._render_area_help("mapa")

        origem = st.radio(
            "Origem dos dados",
            options=["📋 Sessão atual", "🗄️ Grupo armazenado", "🎯 Work Items escolhidos"],
            index=0,
            key="mindmap_origem_radio",
            horizontal=True,
        )

        raiz_nome = "Projeto"
        hierarquia = {}  # {plano_nome: {suite_nome: [casos]}}

        if origem.startswith("📋"):
            test_plans = self.state.get('test_plans') or []
            if not test_plans:
                st.info("Nenhum Plano de Teste na sessão atual — gere Planos (Passo 5) primeiro, ou escolha 'Grupo armazenado'.")
                return
            raiz_nome = self.state.get('project_name') or "Projeto"
            for plano in test_plans:
                nome_plano = plano.get('nome', '(sem nome)')
                hierarquia[nome_plano] = {}
                for suite in plano.get('suites', []):
                    nome_suite = suite.get('nome', '(sem nome)')
                    hierarquia[nome_plano][nome_suite] = list(suite.get('casos', []))
        elif origem.startswith("🗄️"):
            store = DocumentStore(self.config.turso_database_url, self.config.turso_auth_token)
            try:
                with st.spinner("Carregando grupos armazenados..."):
                    store.ensure_schema()
                    grupos = store.listar_grupos()
            except DocumentStoreError as error:
                st.error(f"❌ {error}")
                return
            except Exception as error:
                st.error(f"❌ Não foi possível carregar os documentos: {error}")
                return

            grupos_com_planos = [
                g for g in grupos
                if any("plan" in a['nome_arquivo'].lower() and a['tipo'] == 'csv' for a in g['arquivos'])
            ]
            if not grupos_com_planos:
                st.info(
                    "Nenhum grupo armazenado tem um CSV de Planos (só o Passo 6 gera esse "
                    "arquivo). Armazene uma Documentação QA primeiro, ou use 'Sessão atual'."
                )
                return

            labels = {
                f"{g['nome_projeto'] or '(sem nome)'} — {g['fluxo_origem']} — {g['criado_em'][:10]}": g
                for g in grupos_com_planos
            }
            escolha = st.selectbox("Grupo armazenado", options=list(labels.keys()), key="mindmap_grupo_select")
            grupo = labels[escolha]
            raiz_nome = grupo['nome_projeto'] or "Projeto"

            arq_planos = next(a for a in grupo['arquivos'] if "plan" in a['nome_arquivo'].lower() and a['tipo'] == 'csv')
            try:
                with st.spinner("Lendo o CSV de Planos armazenado..."):
                    conteudo = store.buscar_conteudo(arq_planos['id'])
                hierarquia = self._parse_plans_csv(conteudo)
            except Exception as error:
                st.error(f"❌ Não foi possível ler o CSV armazenado: {error}")
                return

        else:  # 🎯 Work Items escolhidos
            conn = self._setup_azure_devops_connection(show_area_path_picker=False)
            if conn is None:
                return
            ado_client, ado_org, ado_project, _default_area_path = conn
            raiz_nome = ado_project

            st.markdown("##### 📁 Area Path(s) (opcional)")
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
                    "Area Path(s)", options=area_path_options,
                    key="mindmap_area_paths_select",
                    help="Deixe vazio pra considerar o projeto inteiro.",
                )
            with col_btn:
                with st.container(key="azure_blue_btn_fetch_wi_mindmap"):
                    st.button(
                        "🔄 Buscar Work Items",
                        key="btn_fetch_wi_mindmap",
                        on_click=self.trigger_action,
                        args=("fetch_wi_mindmap",),
                        width="stretch",
                    )
            if self.state.get('current_action') == 'fetch_wi_mindmap' and not self.state.get('show_interrupt_modal'):
                try:
                    paths_to_search = area_paths or [ado_project]
                    with st.spinner(f"Buscando Work Items em {len(paths_to_search)} Area Path(s)..."):
                        items_by_id = {}
                        for ap in paths_to_search:
                            for item in ado_client.fetch_work_items_by_area_path(ap, excluded_states=set()):
                                items_by_id[item["id"]] = item
                        self.state.set('mindmap_board_items', list(items_by_id.values()))
                    self.state.set('mindmap_wi_hierarquia', {})
                    if not items_by_id:
                        self._flash_warning("Nenhum Work Item encontrado.")
                except Exception as error:
                    self._flash_error(f"Não foi possível buscar Work Items: {error}")
                self.clear_action()
                st.rerun()

            board_items = self.state.get('mindmap_board_items') or []
            if not board_items:
                st.caption("Busque os Work Items acima pra continuar.")
                return

            board_items = self._filtrar_por_coluna_e_tag(board_items, bool(area_paths), "mindmap")

            wi_labels = {f"{i['id']} - {i['title']} ({i['type']}, {i['state']})": i for i in board_items}
            selected_labels = st.multiselect(
                "🎯 Work Items a incluir no mapa mental",
                options=list(wi_labels.keys()),
                key="mindmap_wi_select",
                help="O mapa usa os Casos de Teste já vinculados a cada Work Item selecionado (relação 'Tests').",
            )
            selected_wis = [wi_labels[l] for l in selected_labels]

            if selected_wis:
                raiz_nome_sugerida = self._render_project_name_field(
                    ado_project, "mindmap_wi_nome_raiz", "Nome do Projeto/Iniciativa *"
                )
            else:
                raiz_nome_sugerida = ""

            with st.container(key="azure_blue_btn_gerar_mindmap_wi"):
                st.button(
                    "🧠 Gerar Mapa Mental",
                    type="primary",
                    width="stretch",
                    disabled=self.state.get('is_processing') or not selected_wis or not raiz_nome_sugerida.strip(),
                    key="btn_gerar_mindmap_wi",
                    on_click=self.trigger_action,
                    args=("gerar_mindmap_wi",),
                )
            if not selected_wis:
                st.caption("Selecione ao menos um Work Item acima pra habilitar o botão.")
            elif not raiz_nome_sugerida.strip():
                st.caption("Preencha o Nome do Projeto/Iniciativa acima pra habilitar o botão.")

            if self.state.get('current_action') == 'gerar_mindmap_wi' and not self.state.get('show_interrupt_modal'):
                hierarquia_wi = {}
                with st.spinner(f"Buscando Casos de Teste vinculados a {len(selected_wis)} Work Item(s)..."):
                    for wi in selected_wis:
                        try:
                            casos = ado_client.get_test_cases_for_work_item(wi['id'])
                        except Exception:
                            casos = []
                        nome_plano = f"{wi['id']} - {wi['title']}"
                        hierarquia_wi[nome_plano] = {"Casos vinculados": [c['titulo'] for c in casos]}
                self.state.set('mindmap_wi_hierarquia', hierarquia_wi)
                self.state.set('mindmap_wi_raiz_nome', raiz_nome_sugerida.strip())
                self.clear_action()
                st.rerun()

            hierarquia = self.state.get('mindmap_wi_hierarquia') or {}
            raiz_nome = self.state.get('mindmap_wi_raiz_nome') or raiz_nome
            if not hierarquia:
                st.caption("Clique em '🧠 Gerar Mapa Mental' acima pra montar o mapa a partir dos Work Items escolhidos.")
                return

        if not hierarquia:
            st.warning("Não encontrei nenhum Plano/Suíte/Caso pra montar o mapa mental.")
            return

        total_planos = len(hierarquia)
        total_suites = sum(len(s) for s in hierarquia.values())
        total_casos = sum(len(c) for s in hierarquia.values() for c in s.values())
        st.caption(f"{total_planos} Plano(s), {total_suites} Suíte(s), {total_casos} Caso(s) de Teste.")

        # Altura dinâmica: com tudo recolhido por padrão, só a raiz +
        # Planos aparecem de cara — dá uma folga extra pra quando a
        # pessoa for expandindo ramos.
        altura_estimada = max(500, 90 + total_planos * 62)
        componente_html = self._d3_mind_map_html(raiz_nome, hierarquia)
        components.html(componente_html, height=altura_estimada, scrolling=True)

        pdf_bytes_mapa = self._gerar_pdf_mapa_mental(raiz_nome, hierarquia)
        st.download_button(
            "📄 Baixar Mapa Mental (PDF)",
            data=pdf_bytes_mapa,
            file_name="mapa_mental.pdf",
            mime="application/pdf",
            help="Igual ao botão '⬇️ Baixar Mapa Mental' dentro do mapa (sempre com tudo expandido), só que em PDF.",
        )

        with st.expander("📋 Ver lista completa (texto)"):
            for plano, suites in hierarquia.items():
                st.markdown(f"**{plano}**")
                for suite, casos in suites.items():
                    st.write(f"　• {suite} ({len(casos)} caso(s))")
                    for caso in casos:
                        st.caption(f"　　　- {caso}")

    @staticmethod
    def _area_path_pertence_ao_team(area_path_escolhida: str, valores_time: list) -> bool:
        """
        Confere se a Area Path escolhida está dentro do escopo configurado
        do Team (correspondência exata, ou descendente de um valor com
        "incluir sub-áreas" marcado) — é isso que determina se um Work
        Item aparece no board desse Team, não o campo de coluna sozinho.
        """
        for v in valores_time:
            if area_path_escolhida == v["value"]:
                return True
            if v["include_children"] and area_path_escolhida.startswith(v["value"] + "\\"):
                return True
        return False

    def _render_bug_metadata_picker(self, ado_client, key_prefix: str, area_path_escolhida: str) -> dict:
        """
        Seletor compartilhado de Coluna do Board, Tags e Atribuir a —
        usado pelos 2 modos de Criar Bug. Só considera Teams cujo
        escopo de Area Path REALMENTE inclui a Area Path escolhida
        acima — sem isso, o Bug pode nascer com o campo de coluna
        preenchido certinho e mesmo assim nunca aparecer em nenhum
        board, porque o Team dono daquele board não "enxerga" essa
        Area Path (era exatamente isso que estava acontecendo).

        As chaves dos widgets de escolha (Coluna/Tags/Atribuir a) levam
        "_{versao}" no final — mesma lógica do Título/Descrição em
        _bug_from_test_case_flow/_bug_free_form_flow: apagar do
        session_state nem sempre reseta visualmente um selectbox já
        renderizado (mesmo comportamento conhecido do Streamlit, só que
        pra widget de seleção em vez de campo de texto). Trocar a
        versão força o widget a nascer de novo, do zero, de verdade.

        Retorna {'team_id', 'coluna', 'tags', 'atribuir_a'} — campos
        ficam None/vazios até tudo que é necessário estar escolhido.
        """
        versao = self.state.get(f'bug_{key_prefix}_form_versao') or 0
        resultado = {"team_id": None, "coluna": None, "tags": [], "atribuir_a": None}

        with st.container(key=f"azure_blue_btn_fetch_colunas_{key_prefix}"):
            st.button(
                "🔄 Buscar Colunas do Board",
                disabled=self.state.get('is_processing'),
                key=f"btn_fetch_colunas_{key_prefix}",
                on_click=self.trigger_action,
                args=(f"fetch_colunas_{key_prefix}",),
                width="stretch",
            )
        if self.state.get('current_action') == f'fetch_colunas_{key_prefix}' and not self.state.get('show_interrupt_modal'):
            try:
                with st.spinner("Buscando Colunas dos Boards que realmente incluem essa Area Path..."):
                    teams = ado_client.list_teams()
                    colunas_por_nome = {}
                    algum_board_encontrado = False
                    for team in teams:
                        try:
                            valores_time = ado_client.get_team_area_paths(team["id"])
                        except Exception:
                            continue
                        if not self._area_path_pertence_ao_team(area_path_escolhida, valores_time):
                            continue
                        try:
                            boards = ado_client.list_boards_for_team(team["id"])
                        except Exception:
                            continue
                        for board in boards:
                            try:
                                colunas = ado_client.list_board_columns(team["id"], board["id"])
                            except Exception:
                                continue
                            if colunas:
                                algum_board_encontrado = True
                                for c in colunas:
                                    colunas_por_nome.setdefault(c["name"], {
                                        "name": c["name"], "team_id": team["id"], "board_id": board["id"],
                                        "state_bug": (c.get("state_mappings") or {}).get("Bug"),
                                    })
                self.state.set(f'bug_colunas_combinadas_{key_prefix}', list(colunas_por_nome.values()))
                if not algum_board_encontrado:
                    self._flash_error(
                        f"Nenhum Team com Board configurado inclui a Area Path '{area_path_escolhida}' no "
                        "escopo dele. Verifica no Azure DevOps (Configurações do Team → Área) se algum "
                        "Team inclui essa Area Path especificamente."
                    )
            except Exception as error:
                self._flash_error(f"Não foi possível buscar Boards/Colunas: {error}")
                self.state.set(f'bug_colunas_combinadas_{key_prefix}', [])
            self.clear_action()
            st.rerun()

        colunas = self.state.get(f'bug_colunas_combinadas_{key_prefix}')
        if colunas is None:
            st.caption("Busque as Colunas do Board acima pra continuar.")
            return resultado
        if not colunas:
            st.caption("Nenhuma Coluna disponível — veja o aviso acima.")
            return resultado

        nomes_colunas = [c["name"] for c in colunas]
        coluna_escolhida_nome = st.selectbox(
            "Coluna do Board", options=nomes_colunas, index=0, key=f"bug_coluna_select_{key_prefix}_{versao}",
            disabled=self.state.get('is_processing'),
            help="Em qual coluna do Kanban o card do Bug já nasce.",
        )
        resultado["coluna"] = coluna_escolhida_nome
        coluna_info = next((c for c in colunas if c["name"] == coluna_escolhida_nome), None)
        if coluna_info:
            resultado["team_id"] = coluna_info["team_id"]
            resultado["coluna_board_id"] = coluna_info["board_id"]
            resultado["coluna_state"] = coluna_info.get("state_bug")
            if not resultado["coluna_state"]:
                st.caption(
                    "⚠️ Essa coluna não tem um State de Bug mapeado — o Bug pode nascer na coluna "
                    "padrão do State inicial, em vez dessa aqui."
                )

        # --- Tags ---
        with st.expander("🏷️ Tags (opcional)", key=f"bug_exp_tags_{key_prefix}", on_change="rerun"):
            with st.container(key=f"azure_blue_btn_fetch_tags_{key_prefix}"):
                st.button(
                    "🔄 Buscar Tags existentes no Projeto",
                    disabled=self.state.get('is_processing'),
                    key=f"btn_fetch_tags_{key_prefix}",
                    on_click=self.trigger_action,
                    args=(f"fetch_tags_{key_prefix}",),
                    width="stretch",
                )
            if self.state.get('current_action') == f'fetch_tags_{key_prefix}' and not self.state.get('show_interrupt_modal'):
                try:
                    with st.spinner("Buscando Tags..."):
                        tags_existentes = ado_client.list_project_tags()
                    self.state.set(f'bug_tags_existentes_{key_prefix}', tags_existentes)
                except Exception as error:
                    self._flash_error(f"Não foi possível buscar Tags: {error}")
                self.clear_action()
                st.rerun()

            tags_existentes = self.state.get(f'bug_tags_existentes_{key_prefix}')
            if tags_existentes is None:
                st.caption("Busque as Tags acima se quiser adicionar alguma.")
            elif not tags_existentes:
                st.caption("Não há Tags cadastradas nesse projeto.")
            else:
                resultado["tags"] = st.multiselect(
                    "Tags", options=tags_existentes, key=f"bug_tags_select_{key_prefix}_{versao}",
                    disabled=self.state.get('is_processing'),
                )

        # --- Atribuir a ---
        with st.expander("👤 Atribuir a (opcional)", key=f"bug_exp_membros_{key_prefix}", on_change="rerun"):
            with st.container(key=f"azure_blue_btn_fetch_membros_{key_prefix}"):
                st.button(
                    "🔄 Buscar Pessoas pra Atribuir",
                    disabled=self.state.get('is_processing'),
                    key=f"btn_fetch_membros_{key_prefix}",
                    on_click=self.trigger_action,
                    args=(f"fetch_membros_{key_prefix}",),
                    width="stretch",
                    help=(
                        "Busca em todos os Times do projeto — não fica limitado ao Time "
                        "da Area Path escolhida, já que o Azure DevOps permite atribuir "
                        "qualquer Work Item a qualquer pessoa do projeto. Pode levar "
                        "alguns segundos em projetos com muitos Times."
                    ),
                )
            if self.state.get('current_action') == f'fetch_membros_{key_prefix}' and not self.state.get('show_interrupt_modal'):
                try:
                    with st.spinner("Buscando pessoas em todos os Times do projeto..."):
                        # Atribuição no Azure DevOps NÃO é restrita por Team/board
                        # — qualquer pessoa com acesso ao projeto pode ser
                        # "Assigned To" em qualquer Work Item, independente de
                        # qual Team "dono" a Area Path escolhida tem. Por isso
                        # aqui a busca é em TODOS os Teams do projeto (união dos
                        # membros), não só nos que batem com a Area Path — ao
                        # contrário da lógica de Coluna do Board acima, que
                        # precisa mesmo ser restrita por escopo (um Work Item só
                        # aparece num board se o Team dono "enxergar" a Area
                        # Path). Escopar por Area Path aqui também deixava a
                        # lista vazia/só-o-dono-do-PAT sempre que a Area Path
                        # escolhida era a raiz do projeto (só o Team padrão,
                        # criado automaticamente e raramente com mais gente
                        # além de quem criou o projeto, bate na raiz).
                        equipes = ado_client.list_teams()
                        membros_por_chave = {}
                        for equipe in equipes:
                            try:
                                membros_equipe = ado_client.list_team_members(equipe["id"])
                            except Exception:
                                continue
                            for m in membros_equipe:
                                chave = m.get("unique_name") or m.get("display_name")
                                if chave:
                                    membros_por_chave.setdefault(chave, m)
                        membros = sorted(membros_por_chave.values(), key=lambda m: m["display_name"].lower())
                    self.state.set(f'bug_membros_{key_prefix}', membros)
                    if not membros:
                        self._flash_warning(
                            "Nenhuma pessoa encontrada em nenhum Team desse projeto — confira no Azure "
                            "DevOps (Configurações do Team → Membros) se algum Team realmente tem gente "
                            "cadastrada."
                        )
                except Exception as error:
                    self._flash_error(f"Não foi possível buscar Membros: {error}")
                self.clear_action()
                st.rerun()

            membros = self.state.get(f'bug_membros_{key_prefix}')
            if membros:
                opcoes_membro = ["(Ninguém)"] + [f"{m['display_name']} ({m['unique_name']})" for m in membros]
                escolha_membro = st.selectbox(
                    "Atribuir a", options=opcoes_membro, index=0, key=f"bug_membro_select_{key_prefix}_{versao}",
                    disabled=self.state.get('is_processing'),
                )
                if escolha_membro != "(Ninguém)":
                    idx = opcoes_membro.index(escolha_membro) - 1
                    resultado["atribuir_a"] = membros[idx]["unique_name"]
            elif membros is not None:
                st.caption("Nenhuma pessoa disponível.")
            else:
                st.caption("Busque as Pessoas acima se quiser atribuir o Bug a alguém.")

        return resultado

    def _render_repro_steps_editor(self, key_prefix: str) -> list:
        """
        Lista dinâmica de Passos de Reprodução — adicionar/remover, com
        numeração automática (a pessoa não digita "1.", "2."...) e mínimo
        de 1 passo, mesmo padrão já usado pros Steps de Caso de Teste.
        Retorna a lista de textos (str), na ordem.
        """
        steps_key = f'{key_prefix}_repro_steps_list'
        if self.state.get(steps_key) is None:
            self.state.set(steps_key, [{"uid": str(uuid.uuid4()), "texto": ""}])

        steps_list = self.state.get(steps_key)
        resultado = []
        for index, passo in enumerate(steps_list):
            uid = passo['uid']
            col_texto, col_del = st.columns([9, 1])
            with col_texto:
                texto = st.text_area(
                    f"Passo {index + 1} *", value=passo.get('texto', ''),
                    key=f"{key_prefix}_repro_texto_{uid}", height=70,
                    disabled=self.state.get('is_processing'),
                )
            with col_del:
                st.markdown("<div style='margin-top:1.8rem'></div>", unsafe_allow_html=True)
                if st.button("🗑️", key=f"{key_prefix}_repro_del_{uid}", disabled=len(steps_list) <= 1 or self.state.get('is_processing')):
                    novos = [p for p in steps_list if p['uid'] != uid]
                    self.state.set(steps_key, novos)
                    st.rerun()
            resultado.append({"uid": uid, "texto": texto})

        if len(steps_list) <= 1:
            st.caption("ℹ️ É necessário manter ao menos 1 passo.")
        self.state.set(steps_key, resultado)

        if st.button("➕ Adicionar Passo", key=f"{key_prefix}_repro_add", disabled=self.state.get('is_processing')):
            atual = self.state.get(steps_key)
            atual.append({"uid": str(uuid.uuid4()), "texto": ""})
            self.state.set(steps_key, atual)
            st.rerun()

        return [p['texto'] for p in resultado]

    def _render_bug_extra_fields(self, key_prefix: str) -> dict:
        """
        Campos extras OPCIONAIS na criação de Bug — System Info,
        Acceptance Criteria, Discussion, e evidência em imagem —
        compartilhados pelos 2 modos (Livre / A partir de Caso de Teste).

        As imagens sobem como anexo de verdade no Work Item (aparecem na
        aba "Attachments") E ficam embutidas como <img> dentro do campo
        "System Info" — escolhido em vez de "Discussion" porque é um
        campo comum do Bug (aparece na aba principal, junto com o resto
        dos dados, sem precisar abrir a aba de comentários) e existe
        desde a criação. "Discussion" no Azure DevOps não é um campo
        comum — é sempre baseado em comentários (API separada); o texto
        digitado aqui vira o PRIMEIRO comentário do Bug, postado logo
        depois dele ser criado.

        Mesma chave versionada "_{key_prefix}_{versao}" dos outros campos
        do formulário — garante que "Criar outro Bug" reseta esses campos
        também (ver docstring de _limpar_estado_bug).

        Retorna {'system_info', 'acceptance_criteria', 'discussion',
        'imagens'} — 'imagens' já como [(nome_arquivo, bytes), ...], lido
        na hora (o objeto do widget de upload não sobrevive a reruns de
        forma confiável pra guardar só a referência).
        """
        versao = self.state.get(f'bug_{key_prefix}_form_versao') or 0
        with st.expander("➕ Detalhes adicionais (opcional)", expanded=True):
            system_info = st.text_area(
                "System Info", key=f"bug_system_info_{key_prefix}_{versao}",
                placeholder="Navegador, sistema operacional, ambiente (HML/PROD), versão, etc.",
                disabled=self.state.get('is_processing'),
            )
            acceptance_criteria = st.text_area(
                "Acceptance Criteria", key=f"bug_acceptance_criteria_{key_prefix}_{versao}",
                placeholder="Condições que precisam ser atendidas pra considerar o Bug corrigido.",
                disabled=self.state.get('is_processing'),
            )
            discussion = st.text_area(
                "Discussion", key=f"bug_discussion_{key_prefix}_{versao}",
                placeholder="Vira o primeiro comentário do Bug, na aba Discussion, criado após ele.",
                disabled=self.state.get('is_processing'),
            )
            uploaded = st.file_uploader(
                "Evidências (imagens)", type=["png", "jpg", "jpeg"], accept_multiple_files=True,
                key=f"bug_evidencias_{key_prefix}_{versao}",
                disabled=self.state.get('is_processing'),
                help="Sobem como anexo do Bug e ficam também embutidas no campo System Info.",
            )
        imagens = [(f.name, f.getvalue()) for f in (uploaded or [])]
        return {
            "system_info": system_info.strip(),
            "acceptance_criteria": acceptance_criteria.strip(),
            "discussion": discussion.strip(),
            "imagens": imagens,
        }

    # ------------------------------------------------------------------ #
    # Criar Work Item (livre, qualquer tipo) — o formulário é montado a
    # partir dos metadados do processo do projeto, não hardcoded, porque
    # cada organização/processo tem tipos e campos próprios (ex.: "Spike",
    # "Improvement", "Custom.Bloqueado").
    # ------------------------------------------------------------------ #
    _WI_CAMPOS_DESTAQUE = [
        # Ordem em que os campos "conhecidos" aparecem, quando o tipo os
        # tiver. O resto vai pro expander de "outros campos".
        "Microsoft.VSTS.Common.AcceptanceCriteria",
        "Microsoft.VSTS.TCM.ReproSteps",
        "Microsoft.VSTS.TCM.SystemInfo",
        "Microsoft.VSTS.Common.Priority",
        "Microsoft.VSTS.Common.Severity",
        "Microsoft.VSTS.Common.ValueArea",
        "Microsoft.VSTS.Common.Risk",
        "Microsoft.VSTS.Scheduling.StoryPoints",
        "Microsoft.VSTS.Scheduling.Effort",
        "Microsoft.VSTS.Scheduling.OriginalEstimate",
        "Microsoft.VSTS.Scheduling.RemainingWork",
        "Microsoft.VSTS.Common.BusinessValue",
    ]

    def _render_campo_wi(self, campo: dict, tipo_dado: str, key: str, membros: list = None,
                          eh_identidade: bool = False):
        """
        Renderiza UM campo de Work Item escolhendo o widget pelo tipo de
        dado que a própria API do Azure DevOps informa, e devolve o valor
        pronto pra mandar no PATCH (ou None quando vazio).

        Regras:
        - campo com lista fechada (allowed_values) -> selectbox; quando não
          é obrigatório, entra um "(vazio)" na frente pra poder não mandar
        - html -> text_area, convertido pra HTML (quebra de linha -> <br>),
          igual ao que os fluxos de Bug já fazem
        - identidade (ex.: Assigned To) -> selectbox de pessoas quando a
          lista já foi buscada; se não, campo de texto pra digitar o e-mail
        - dateTime -> date_input, serializado em ISO (o que a API espera)
        """
        label = campo["name"] + (" *" if campo["always_required"] else "")
        help_text = campo.get("help_text") or None
        desabilitado = self.state.get('is_processing')

        if campo.get("allowed_values"):
            opcoes = list(campo["allowed_values"])
            if not campo["always_required"]:
                opcoes = ["(vazio)"] + opcoes
            escolha = st.selectbox(label, options=opcoes, index=0, key=key,
                                    disabled=desabilitado, help=help_text)
            if escolha == "(vazio)":
                return None
            # Campos numéricos com lista fechada chegam como texto ("1",
            # "2"...) — a API recusa string onde espera número.
            if tipo_dado == "integer":
                try:
                    return int(escolha)
                except (TypeError, ValueError):
                    return escolha
            if tipo_dado == "double":
                try:
                    return float(escolha)
                except (TypeError, ValueError):
                    return escolha
            return escolha

        if tipo_dado in ("html", "plainText"):
            texto = st.text_area(label, key=key, height=110, disabled=desabilitado, help=help_text)
            if not texto.strip():
                return None
            if tipo_dado == "html":
                return html.escape(texto).replace("\n", "<br>")
            return texto.strip()

        if tipo_dado == "boolean":
            if campo["always_required"]:
                return st.checkbox(label, key=key, disabled=desabilitado, help=help_text)
            # Opcional usa 3 estados em vez de checkbox: com checkbox não dá
            # pra distinguir "não mexi nisso" de "marquei como Não", e aí
            # todo campo booleano do tipo iria como False, sobrescrevendo
            # silenciosamente o padrão que o processo definir.
            escolha = st.selectbox(label, options=["(vazio)", "Sim", "Não"], index=0,
                                    key=key, disabled=desabilitado, help=help_text)
            if escolha == "(vazio)":
                return None
            return escolha == "Sim"

        if tipo_dado in ("integer", "double"):
            # value=None deixa o campo vazio de verdade, em vez de começar em
            # 0. Sem isso, não havia como diferenciar "não preenchi" de
            # "quero 0" — e o 0 acabava descartado por ser falsy, travando
            # campo obrigatório que a pessoa via preenchido com 0 na tela.
            valor = st.number_input(
                label, value=None, step=1 if tipo_dado == "integer" else 0.5,
                key=key, disabled=desabilitado, help=help_text,
            )
            if valor is None:
                return None
            return int(valor) if tipo_dado == "integer" else float(valor)

        if tipo_dado == "dateTime":
            data = st.date_input(label, value=None, key=key, disabled=desabilitado, help=help_text)
            return data.isoformat() if data else None

        if eh_identidade and membros:
            opcoes = ["(Ninguém)"] + [f"{m['display_name']} ({m['unique_name']})" for m in membros]
            escolha = st.selectbox(label, options=opcoes, index=0, key=key,
                                    disabled=desabilitado, help=help_text)
            if escolha == "(Ninguém)":
                return None
            return membros[opcoes.index(escolha) - 1]["unique_name"]

        texto = st.text_input(
            label, key=key, disabled=desabilitado, help=help_text,
            placeholder="e-mail da pessoa" if eh_identidade else None,
        )
        return texto.strip() or None

    def _wi_carregar_metadados(self, ado_client, ado_project: str):
        """
        Garante que os tipos de Work Item e o catálogo de campos do
        projeto estão em memória (1 chamada cada, por projeto). Devolve
        (tipos, catalogo_campos) ou (None, None) se ainda não deu pra
        carregar — nesse caso a mensagem já foi mostrada.
        """
        if self.state.get('wi_metadados_project') != ado_project:
            self.state.set('wi_tipos', None)
            self.state.set('wi_catalogo_campos', None)
            self.state.set('wi_campos_por_tipo', {})
            # Tudo que foi buscado do projeto ANTERIOR tem que sair da tela
            # junto: senão, depois de trocar de projeto, a pessoa ainda veria
            # (e poderia usar) tags, pessoas, colunas e Work Items do projeto
            # antigo — dando pra atribuir alguém que não é do projeto, ou
            # pendurar um filho num pai de outro projeto.
            for sufixo in ("unico", "lote"):
                self.state.set(f'wi_tags_existentes_{sufixo}', None)
                self.state.set(f'wi_membros_{sufixo}', None)
                self.state.set(f'wi_colunas_{sufixo}', None)
                self.state.set(f'wi_pais_{sufixo}', None)
            self.state.set('wi_snapshot', None)
            self.state.set('wi_lote_snapshot', None)
            self.state.set('wi_lote_pai_detalhe_id', None)

        if self.state.get('wi_tipos') is None:
            try:
                with st.spinner("Carregando tipos de Work Item e campos do projeto..."):
                    tipos = ado_client.list_work_item_types()
                    catalogo = ado_client.list_project_fields()
                self.state.set('wi_tipos', tipos)
                self.state.set('wi_catalogo_campos', catalogo)
                self.state.set('wi_metadados_project', ado_project)
            except Exception as error:
                st.error(f"❌ Não foi possível carregar os tipos de Work Item: {error}")
                return None, None

        return self.state.get('wi_tipos'), self.state.get('wi_catalogo_campos')

    def _wi_campos_do_tipo(self, ado_client, tipo_nome: str):
        """Campos de um tipo, com cache por tipo (evita rebuscar a cada rerun)."""
        cache = self.state.get('wi_campos_por_tipo') or {}
        if tipo_nome not in cache:
            try:
                with st.spinner(f"Carregando campos do tipo '{tipo_nome}'..."):
                    cache[tipo_nome] = ado_client.get_work_item_type_fields(tipo_nome)
                self.state.set('wi_campos_por_tipo', cache)
            except Exception as error:
                st.error(f"❌ Não foi possível carregar os campos de '{tipo_nome}': {error}")
                return None
        return cache.get(tipo_nome)

    def _wi_render_tags(self, ado_client, key_prefix: str) -> str:
        """
        Tags: escolhe entre as que já existem no projeto E/OU digita novas.
        Tag nova não precisa ser cadastrada antes — o Azure DevOps cria
        sozinho quando o Work Item nasce com ela em System.Tags.
        Retorna a string no formato que a API espera ("tag1; tag2") ou "".
        """
        with st.expander("🏷️ Tags (opcional)", key=f"wi_exp_tags_{key_prefix}", on_change="rerun"):
            st.button(
                "🔄 Buscar Tags existentes no Projeto",
                disabled=self.state.get('is_processing'),
                key=f"btn_wi_fetch_tags_{key_prefix}",
                on_click=self.trigger_action,
                args=(f"wi_fetch_tags_{key_prefix}",),
                width="stretch",
            )
            if self.state.get('current_action') == f'wi_fetch_tags_{key_prefix}' and not self.state.get('show_interrupt_modal'):
                try:
                    with st.spinner("Buscando Tags..."):
                        self.state.set(f'wi_tags_existentes_{key_prefix}', ado_client.list_project_tags())
                except Exception as error:
                    self._flash_error(f"Não foi possível buscar Tags: {error}")
                self.clear_action()
                st.rerun()

            existentes = self.state.get(f'wi_tags_existentes_{key_prefix}')
            escolhidas = []
            if existentes:
                escolhidas = st.multiselect(
                    "Tags existentes", options=existentes,
                    key=f"wi_tags_select_{key_prefix}",
                    disabled=self.state.get('is_processing'),
                )
            elif existentes is not None:
                st.caption("Não há Tags cadastradas nesse projeto — use o campo abaixo pra criar.")
            else:
                st.caption("Busque as Tags acima se quiser reaproveitar alguma já existente.")

            novas = st.text_input(
                "Criar tag(s) nova(s)", key=f"wi_tags_novas_{key_prefix}",
                placeholder="ex.: regressao; sprint-42",
                disabled=self.state.get('is_processing'),
                help="Separe por ponto e vírgula. Tags novas são criadas automaticamente no Azure DevOps junto com o Work Item.",
            )
            novas_lista = [t.strip() for t in (novas or "").split(";") if t.strip()]

            todas = list(dict.fromkeys(list(escolhidas) + novas_lista))
            if todas:
                st.caption(f"Tags que vão no Work Item: {', '.join(todas)}")
            return "; ".join(todas)

    def _wi_render_pessoas(self, ado_client, key_prefix: str):
        """
        Lista de pessoas pra atribuir — mesma lógica do Criar Bug: varre
        TODOS os Times do projeto, porque atribuição no Azure DevOps não é
        restrita por Time/board. Retorna a lista (ou None se não buscou).
        """
        st.button(
            "🔄 Buscar Pessoas pra Atribuir",
            disabled=self.state.get('is_processing'),
            key=f"btn_wi_fetch_membros_{key_prefix}",
            on_click=self.trigger_action,
            args=(f"wi_fetch_membros_{key_prefix}",),
            width="stretch",
            help="Busca em todos os Times do projeto. Pode levar alguns segundos em projetos com muitos Times.",
        )
        if self.state.get('current_action') == f'wi_fetch_membros_{key_prefix}' and not self.state.get('show_interrupt_modal'):
            try:
                with st.spinner("Buscando pessoas em todos os Times do projeto..."):
                    membros_por_chave = {}
                    for equipe in ado_client.list_teams():
                        try:
                            for m in ado_client.list_team_members(equipe["id"]):
                                chave = m.get("unique_name") or m.get("display_name")
                                if chave:
                                    membros_por_chave.setdefault(chave, m)
                        except Exception:
                            continue
                    membros = sorted(membros_por_chave.values(), key=lambda m: m["display_name"].lower())
                self.state.set(f'wi_membros_{key_prefix}', membros)
                if not membros:
                    self._flash_warning("Nenhuma pessoa encontrada nos Times desse projeto.")
            except Exception as error:
                self._flash_error(f"Não foi possível buscar Pessoas: {error}")
            self.clear_action()
            st.rerun()
        return self.state.get(f'wi_membros_{key_prefix}')

    def _wi_render_coluna(self, ado_client, key_prefix: str, area_path: str, tipo_nome: str):
        """
        Coluna do board (opcional). Ao contrário de "Atribuir a", aqui o
        escopo de Area Path importa de verdade: um Work Item só aparece no
        board de um Team se o escopo daquele Team incluir a Area Path dele.
        Também pega o State que a coluna representa PRO TIPO escolhido
        (stateMappings é por tipo de Work Item), porque a posição real no
        board vem principalmente do State.

        Retorna {'coluna', 'campo_coluna', 'state'} — tudo None se não
        escolhido/não aplicável.
        """
        vazio = {"coluna": None, "campo_coluna": None, "state": None}
        with st.expander("📋 Coluna do Board (opcional)", key=f"wi_exp_coluna_{key_prefix}", on_change="rerun"):
            if not area_path:
                st.caption("Escolha uma Area Path acima pra poder posicionar o item numa coluna de board.")
                return vazio

            st.button(
                "🔄 Buscar Colunas do Board",
                disabled=self.state.get('is_processing'),
                key=f"btn_wi_fetch_colunas_{key_prefix}",
                on_click=self.trigger_action,
                args=(f"wi_fetch_colunas_{key_prefix}",),
                width="stretch",
            )
            if self.state.get('current_action') == f'wi_fetch_colunas_{key_prefix}' and not self.state.get('show_interrupt_modal'):
                try:
                    with st.spinner("Buscando Colunas dos Boards que incluem essa Area Path..."):
                        colunas_por_nome = {}
                        for team in ado_client.list_teams():
                            try:
                                valores = ado_client.get_team_area_paths(team["id"])
                            except Exception:
                                continue
                            if not self._area_path_pertence_ao_team(area_path, valores):
                                continue
                            try:
                                boards = ado_client.list_boards_for_team(team["id"])
                            except Exception:
                                continue
                            for board in boards:
                                try:
                                    colunas = ado_client.list_board_columns(team["id"], board["id"])
                                except Exception:
                                    continue
                                for c in colunas:
                                    mapeamentos = c.get("state_mappings") or {}
                                    # Só oferece a coluna se ela realmente
                                    # existe pro tipo escolhido — board de
                                    # Bug não serve pra Epic, por exemplo.
                                    if tipo_nome not in mapeamentos:
                                        continue
                                    colunas_por_nome.setdefault(c["name"], {
                                        "name": c["name"], "team_id": team["id"],
                                        "board_id": board["id"], "state": mapeamentos.get(tipo_nome),
                                    })
                    self.state.set(f'wi_colunas_{key_prefix}', list(colunas_por_nome.values()))
                    if not colunas_por_nome:
                        self._flash_warning(
                            f"Nenhum board que inclui '{area_path}' tem coluna pro tipo '{tipo_nome}'. "
                            "O item vai nascer na coluna padrão do estado inicial."
                        )
                except Exception as error:
                    self._flash_error(f"Não foi possível buscar Colunas: {error}")
                self.clear_action()
                st.rerun()

            colunas = self.state.get(f'wi_colunas_{key_prefix}')
            if not colunas:
                if colunas is not None:
                    st.caption("Nenhuma coluna disponível — veja o aviso acima.")
                else:
                    st.caption("Busque as Colunas acima se quiser escolher em qual o item nasce.")
                return vazio

            nomes = ["(padrão do estado inicial)"] + [c["name"] for c in colunas]
            escolha = st.selectbox("Coluna do Board", options=nomes, index=0,
                                    key=f"wi_coluna_select_{key_prefix}",
                                    disabled=self.state.get('is_processing'))
            if escolha == nomes[0]:
                return vazio

            info = next((c for c in colunas if c["name"] == escolha), None)
            if not info:
                return vazio
            try:
                campo_coluna = ado_client.get_board_column_field_name(info["team_id"], info["board_id"])
            except Exception as error:
                st.caption(f"⚠️ Não foi possível descobrir o campo de coluna desse board: {error}")
                return vazio
            if not info.get("state"):
                st.caption("⚠️ Essa coluna não tem um State mapeado pra esse tipo — pode nascer na coluna padrão.")
            return {"coluna": info["name"], "campo_coluna": campo_coluna, "state": info.get("state")}

    def _render_sem_assistente(self):
        """
        Tela para quem NÃO tem a permissão do assistente de QA (Passos
        1–6). Se a pessoa tem alguma outra área liberada, leva direto pra
        ela (logar já cai no lugar certo, sem passar por um fluxo que ela
        não pode usar). Se tem mais de uma, mostra os atalhos. Se não tem
        nenhuma, explica em vez de deixar uma tela vazia/quebrada.
        """
        areas = self._areas_liberadas()

        # Leva direto pra área quando ela é a única — mas só na primeira
        # vez na sessão. Se redirecionasse sempre, o "← Voltar" de dentro
        # da área voltaria pra cá e seria reenviado pra lá na hora,
        # parecendo um botão quebrado.
        if len(areas) == 1 and not self.state.get('_area_unica_ja_aberta'):
            chave, _perm, _rotulo = areas[0]
            self.state.set('_area_unica_ja_aberta', True)
            self.state.set(chave, True)
            st.rerun()

        if not areas:
            st.info(
                "👋 Seu acesso ao app está ativo, mas nenhuma funcionalidade foi liberada "
                "para o seu usuário ainda."
            )
            st.caption(
                "Um administrador precisa liberar ao menos uma funcionalidade no cadastro do "
                "seu usuário (Administração → Usuários → Permissões). Enquanto isso, você "
                "ainda pode consultar o \"Sobre o app\" na barra lateral."
            )
            return

        st.subheader("👋 Bem-vindo")
        st.caption("Escolha por onde começar — essas são as áreas liberadas para o seu usuário.")
        for chave, _perm, rotulo in areas:
            if st.button(rotulo, width="stretch", key=f"btn_home_area_{chave}"):
                self.state.set(chave, True)
                st.rerun()

    def _work_item_creation_page(self):
        self._processing_banner()
        st.subheader("🧱 Criar Work Item")
        if st.button("← Voltar", key="btn_wi_back"):
            self.state.set('show_work_item_page', False)
            st.rerun()

        if not self._get_permission_cached("criar_work_item"):
            st.error("❌ Você não tem permissão pra acessar esta área.")
            return

        st.caption(
            "Cria Work Items de qualquer tipo direto no Azure DevOps — os campos do formulário "
            "vêm do processo do próprio projeto, então tipos e campos customizados da sua "
            "organização aparecem aqui automaticamente."
        )
        self._render_area_help("work_item")

        conn = self._setup_azure_devops_connection(show_area_path_picker=False)
        if conn is None:
            return
        ado_client, _ado_org, ado_project, _default_area_path = conn

        tipos, catalogo = self._wi_carregar_metadados(ado_client, ado_project)
        if not tipos:
            return

        st.divider()
        n_fila = len(self.state.get('wi_fila') or [])
        # Os rótulos precisam ser fixos: se mudassem (ex.: "— 2 na fila"), o Streamlit trataria o rádio como
        # um widget novo e voltaria pra primeira opção a cada mudança na fila — inclusive logo após o envio.
        modo = st.radio(
            "O que você quer criar?",
            options=["📝 Um Work Item", "🧩 Vários filhos de um Work Item (quebrar em Tasks)",
                     "📋 Fila / planilha (vários Work Items)"],
            index=0,
            key="wi_modo_radio",
            horizontal=True,
            disabled=self.state.get('is_processing'),
            help=(
                "O segundo modo é pro fluxo de quebrar uma User Story em Tasks: escolhe a Story, "
                "lista as Tasks, e cria todas de uma vez já vinculadas como filhas dela. "
                "O terceiro monta uma fila de vários Work Items (de qualquer tipo) — pelo formulário "
                "ou por planilha — e envia tudo de uma vez."
            ),
        )

        if n_fila and not modo.startswith("📋"):
            st.caption(f"🧺 {n_fila} item(ns) esperando na fila — envie pelo modo **Fila / planilha**.")

        if modo.startswith("🧩"):
            self._wi_modo_filhos_em_lote(ado_client, ado_project, tipos, catalogo)
        elif modo.startswith("📋"):
            self._wi_modo_fila(ado_client, ado_project, tipos, catalogo)
        else:
            self._wi_modo_unico(ado_client, ado_project, tipos, catalogo)

    def _wi_selecionar_area_e_iteration(self, ado_client, ado_project: str, key_prefix: str):
        """Area Path e Iteration Path (os dois opcionais). Retorna (area_path, iteration_path)."""
        col_a, col_i = st.columns(2)
        with col_a:
            if self.state.get('ado_available_area_paths') and self.state.get('ado_area_paths_project') == ado_project:
                area_options = self.state.get('ado_available_area_paths') or []
            else:
                try:
                    with st.spinner("Buscando Area Paths..."):
                        area_options = ado_client.list_area_paths()
                    self.state.set('ado_available_area_paths', area_options)
                    self.state.set('ado_area_paths_project', ado_project)
                except Exception as error:
                    st.error(f"❌ Não foi possível buscar Area Paths: {error}")
                    area_options = []
            area_path = st.selectbox(
                "Area Path (opcional)", options=area_options, index=None,
                placeholder="Padrão do projeto...", key=f"wi_area_select_{key_prefix}",
                disabled=self.state.get('is_processing'),
            )
        with col_i:
            if self.state.get('wi_iterations_project') != ado_project:
                self.state.set('wi_iterations', None)
            if self.state.get('wi_iterations') is None:
                try:
                    with st.spinner("Buscando Iterations..."):
                        self.state.set('wi_iterations', ado_client.list_iteration_paths())
                    self.state.set('wi_iterations_project', ado_project)
                except Exception:
                    self.state.set('wi_iterations', [])
            iteration_path = st.selectbox(
                "Iteration / Sprint (opcional)", options=self.state.get('wi_iterations') or [], index=None,
                placeholder="Padrão do projeto...", key=f"wi_iter_select_{key_prefix}",
                disabled=self.state.get('is_processing'),
            )
        return area_path, iteration_path

    def _wi_modo_unico(self, ado_client, ado_project: str, tipos: list, catalogo: dict):
        """Modo 'um Work Item': formulário completo, montado conforme o tipo."""
        nomes_tipos = [t["name"] for t in tipos]
        tipo_nome = st.selectbox(
            "Tipo de Work Item *", options=nomes_tipos, index=None,
            placeholder="Escolha o tipo...", key="wi_tipo_select",
            disabled=self.state.get('is_processing'),
        )
        if not tipo_nome:
            st.caption("Escolha o tipo pra montar o formulário.")
            return

        campos = self._wi_campos_do_tipo(ado_client, tipo_nome)
        if campos is None:
            return

        por_ref = {c["reference_name"]: c for c in campos}
        valores = {}

        st.divider()
        st.markdown("##### 📌 Principais")
        campo_titulo = por_ref.get("System.Title")
        titulo = st.text_input(
            "Título *", key="wi_titulo", disabled=self.state.get('is_processing'),
            help=(campo_titulo or {}).get("help_text") or None,
        )
        if titulo.strip():
            valores["System.Title"] = titulo.strip()

        if "System.Description" in por_ref:
            desc_campo = por_ref["System.Description"]
            label = "Descrição *" if desc_campo["always_required"] else "Descrição"
            descricao = st.text_area(label, key="wi_descricao", height=140,
                                      disabled=self.state.get('is_processing'),
                                      placeholder="Descreva o item...")
            if descricao.strip():
                tipo_desc = (catalogo.get("System.Description") or {}).get("type", "html")
                valores["System.Description"] = (
                    html.escape(descricao).replace("\n", "<br>") if tipo_desc == "html" else descricao.strip()
                )

        # Obrigatórios do tipo que não têm widget dedicado (ex.: Value Area,
        # campos customizados como Custom.Bloqueado) — precisam aparecer em
        # destaque, senão a criação falha sem a pessoa entender por quê.
        obrigatorios_extra = [
            c for c in campos
            if c["always_required"]
            and c["reference_name"] not in AzureDevOpsClient.CAMPOS_COM_WIDGET_PROPRIO
            # Somente-leitura marcado como obrigatório existe em processo
            # customizado: mostrar viraria um campo que a pessoa preenche e
            # a API recusa depois.
            and not (catalogo.get(c["reference_name"]) or {}).get("read_only")
        ]
        if obrigatorios_extra:
            st.caption("Campos obrigatórios desse tipo de Work Item:")
            for c in obrigatorios_extra:
                meta = catalogo.get(c["reference_name"]) or {}
                valor = self._render_campo_wi(
                    c, meta.get("type", "string"), f"wi_obrig_{c['reference_name']}",
                    eh_identidade=bool(meta.get("is_identity")),
                )
                if valor is not None:
                    valores[c["reference_name"]] = valor

        st.divider()
        st.markdown("##### 🗂️ Organização")
        area_path, iteration_path = self._wi_selecionar_area_e_iteration(ado_client, ado_project, "unico")
        if area_path:
            valores["System.AreaPath"] = area_path
        if iteration_path:
            valores["System.IterationPath"] = iteration_path

        coluna_info = self._wi_render_coluna(ado_client, "unico", area_path, tipo_nome)
        if coluna_info.get("coluna") and coluna_info.get("campo_coluna"):
            valores[coluna_info["campo_coluna"]] = coluna_info["coluna"]

        tags = self._wi_render_tags(ado_client, "unico")
        if tags:
            valores["System.Tags"] = self._tag_criado_por(tags)
        else:
            valores["System.Tags"] = self._tag_criado_por()

        st.divider()
        st.markdown("##### 👤 Responsável")
        membros = self._wi_render_pessoas(ado_client, "unico")
        if membros:
            opcoes = ["(Ninguém)"] + [f"{m['display_name']} ({m['unique_name']})" for m in membros]
            escolha = st.selectbox("Atribuir a", options=opcoes, index=0, key="wi_assigned_select",
                                    disabled=self.state.get('is_processing'))
            if escolha != "(Ninguém)":
                valores["System.AssignedTo"] = membros[opcoes.index(escolha) - 1]["unique_name"]
        elif membros is None:
            st.caption("Busque as Pessoas acima se quiser já atribuir o item a alguém.")

        st.divider()
        st.markdown("##### 🔗 Vínculo com um pai (opcional)")
        parent_id = self._wi_selecionar_pai(ado_client, ado_project, tipos, "unico")

        st.divider()
        st.markdown("##### 📎 Evidências em imagem (opcional)")
        # A versão na key faz o uploader nascer vazio depois de "Criar outro"
        # — sem isso, as imagens do item anterior continuariam selecionadas e
        # iriam anexadas de novo no próximo, em silêncio. Mesmo padrão dos
        # widgets versionados do Criar Bug.
        versao_wi = self.state.get('wi_form_versao') or 0
        uploaded = st.file_uploader(
            "Imagens", type=["png", "jpg", "jpeg"], accept_multiple_files=True,
            key=f"wi_evidencias_unico_{versao_wi}",
            disabled=self.state.get('is_processing'),
            help=(
                "Igual ao Criar Bug: sobem como anexo formal do Work Item (aba Attachments) "
                "e ficam também embutidas no fim da Descrição, pra aparecerem direto no item."
            ),
        )
        imagens = [(f.name, f.getvalue()) for f in (uploaded or [])]
        if imagens:
            st.caption(f"{len(imagens)} imagem(ns) selecionada(s).")

        # Demais campos do tipo, pra quem precisar de algo específico.
        ja_tratados = set(AzureDevOpsClient.CAMPOS_COM_WIDGET_PROPRIO) | {
            c["reference_name"] for c in obrigatorios_extra
        }
        restantes = [c for c in campos if c["reference_name"] not in ja_tratados]
        destaque = [r for r in self._WI_CAMPOS_DESTAQUE if r in {c["reference_name"] for c in restantes}]
        ordenados = (
            [next(c for c in restantes if c["reference_name"] == ref) for ref in destaque]
            + sorted(
                (c for c in restantes if c["reference_name"] not in destaque),
                key=lambda c: c["name"].lower(),
            )
        )
        if ordenados:
            st.divider()
            with st.expander(f"➕ Outros campos de '{tipo_nome}' ({len(ordenados)} disponíveis)"):
                st.caption(
                    "Todos os campos editáveis desse tipo no processo do seu projeto. "
                    "Deixe em branco o que não quiser preencher."
                )
                for c in ordenados:
                    meta = catalogo.get(c["reference_name"]) or {}
                    if meta.get("read_only") or meta.get("type") in ("history", "treePath"):
                        continue
                    valor = self._render_campo_wi(
                        c, meta.get("type", "string"), f"wi_extra_{c['reference_name']}",
                        membros, eh_identidade=bool(meta.get("is_identity")),
                    )
                    if valor is not None and valor != "":
                        valores[c["reference_name"]] = valor

        st.divider()
        pode_criar = bool(titulo.strip()) and all(
            c["reference_name"] in valores for c in obrigatorios_extra
            if (catalogo.get(c["reference_name"]) or {}).get("type") != "boolean"
        )
        snapshot_atual = {
            "tipo": tipo_nome, "campos": valores, "parent_id": parent_id,
            "state": coluna_info.get("state"), "coluna": coluna_info.get("coluna"),
            "imagens": imagens,
            # Só embute <img> na Descrição se o tipo realmente tem esse
            # campo editável — senão o anexo formal ainda vale, mas a
            # Descrição não é tocada.
            "tem_descricao": "System.Description" in por_ref,
        }
        col_criar, col_fila = st.columns([2, 1])
        with col_criar:
            if st.button("🧱 Criar Work Item", type="primary", width="stretch",
                          disabled=self.state.get('is_processing') or not pode_criar,
                          key="btn_wi_criar"):
                self.state.set('wi_snapshot', snapshot_atual)
                st.rerun()
        with col_fila:
            # Alternativa ao "criar agora": guarda na fila pra enviar vários de
            # uma vez (modo "Fila / planilha"), sem tocar no Azure DevOps ainda.
            if st.button("➕ Adicionar à fila", width="stretch",
                         disabled=self.state.get('is_processing') or not pode_criar, key="btn_wi_add_fila",
                         help="Guarda este Work Item numa fila pra enviar vários de uma vez (modo \"Fila / planilha\")."):
                self._wi_fila_adicionar(dict(snapshot_atual, ref="", parent_ref=None,
                                             titulo=valores.get("System.Title", ""), origem="formulário"))
                self._flash_success(
                    f"'{valores.get('System.Title', '')}' adicionado à fila "
                    f"({len(self.state.get('wi_fila') or [])} item(ns)). Preencha o próximo ou vá ao modo "
                    "\"Fila / planilha\" pra enviar."
                )
                st.rerun()
        if not pode_criar:
            faltando = []
            if not titulo.strip():
                faltando.append("Título")
            for c in obrigatorios_extra:
                if c["reference_name"] not in valores and (catalogo.get(c["reference_name"]) or {}).get("type") != "boolean":
                    faltando.append(c["name"])
            if faltando:
                st.caption(f"Preencha: {', '.join(faltando)}.")

        self._wi_confirmar_e_criar(ado_client)

    def _wi_selecionar_pai(self, ado_client, ado_project: str, tipos: list, key_prefix: str):
        """
        Escolha opcional de um Work Item pai. Retorna o id (int) ou None.
        Tem dois caminhos: buscar por Area Path (lista pra escolher) ou
        digitar o ID direto, pra quem já sabe qual é.
        """
        usar_pai = st.checkbox(
            "Vincular este item como filho de outro Work Item",
            key=f"wi_usar_pai_{key_prefix}",
            disabled=self.state.get('is_processing'),
            help="Cria o vínculo Parent/Child — é o que faz o item aparecer na lista de filhos do pai, no Azure DevOps.",
        )
        if not usar_pai:
            return None

        como = st.radio(
            "Como escolher o pai?",
            options=["🔎 Buscar no board", "#️⃣ Digitar o ID"],
            index=0, horizontal=True, key=f"wi_como_pai_{key_prefix}",
            disabled=self.state.get('is_processing'),
        )
        if como.startswith("#"):
            id_txt = st.text_input("ID do Work Item pai", key=f"wi_pai_id_{key_prefix}",
                                    disabled=self.state.get('is_processing'))
            if id_txt.strip().isdigit():
                return int(id_txt.strip())
            if id_txt.strip():
                st.caption("⚠️ Informe só o número do ID.")
            return None

        pais = self._wi_buscar_work_items(ado_client, ado_project, tipos, key_prefix)
        if not pais:
            return None
        rotulos = {f"{p['id']} - {p['title']} ({p['type']}, {p['state']})": p for p in pais}
        escolha = st.selectbox("Work Item pai", options=list(rotulos.keys()), index=None,
                                placeholder="Escolha o pai...", key=f"wi_pai_select_{key_prefix}",
                                disabled=self.state.get('is_processing'))
        return rotulos[escolha]["id"] if escolha else None

    def _wi_buscar_work_items(self, ado_client, ado_project: str, tipos: list, key_prefix: str):
        """
        Busca Work Items de um Area Path pra servir de pai. Mostra filtro
        de tipo (padrão: User Story, que é o caso de uso do time). Retorna
        a lista já filtrada, ou [] se ainda não buscou.
        """
        if self.state.get('ado_available_area_paths') and self.state.get('ado_area_paths_project') == ado_project:
            area_options = self.state.get('ado_available_area_paths') or []
        else:
            try:
                area_options = ado_client.list_area_paths()
                self.state.set('ado_available_area_paths', area_options)
                self.state.set('ado_area_paths_project', ado_project)
            except Exception as error:
                st.error(f"❌ Não foi possível buscar Area Paths: {error}")
                return []

        col_ap, col_tp = st.columns(2)
        with col_ap:
            area_busca = st.selectbox(
                "Buscar em qual Area Path?", options=area_options, index=0 if area_options else None,
                key=f"wi_pai_area_{key_prefix}", disabled=self.state.get('is_processing'),
            )
        with col_tp:
            nomes_tipos = [t["name"] for t in tipos]
            idx_padrao = nomes_tipos.index("User Story") if "User Story" in nomes_tipos else 0
            tipos_filtro = st.multiselect(
                "Filtrar por tipo", options=nomes_tipos, default=[nomes_tipos[idx_padrao]] if nomes_tipos else [],
                key=f"wi_pai_tipos_{key_prefix}", disabled=self.state.get('is_processing'),
            )

        st.button(
            "🔄 Buscar Work Items",
            disabled=self.state.get('is_processing') or not area_busca,
            key=f"btn_wi_fetch_pais_{key_prefix}",
            on_click=self.trigger_action,
            args=(f"wi_fetch_pais_{key_prefix}",),
            width="stretch",
        )
        if self.state.get('current_action') == f'wi_fetch_pais_{key_prefix}' and not self.state.get('show_interrupt_modal'):
            try:
                with st.spinner("Buscando Work Items..."):
                    # excluded_states=set() porque aqui a pessoa pode
                    # querer pendurar filhos em item de qualquer estado —
                    # diferente dos fluxos de geração de teste, que
                    # ignoram "Finalizado"/"Backlog" de propósito.
                    itens = ado_client.fetch_work_items_by_area_path(area_busca, excluded_states=set())
                self.state.set(f'wi_pais_{key_prefix}', itens)
                if not itens:
                    self._flash_warning("Nenhum Work Item encontrado nessa Area Path.")
            except Exception as error:
                self._flash_error(f"Não foi possível buscar Work Items: {error}")
                self.state.set(f'wi_pais_{key_prefix}', [])
            self.clear_action()
            st.rerun()

        itens = self.state.get(f'wi_pais_{key_prefix}')
        if itens is None:
            st.caption("Busque os Work Items acima pra escolher o pai.")
            return []
        if tipos_filtro:
            itens = [i for i in itens if i["type"] in tipos_filtro]
        if not itens:
            st.caption("Nenhum Work Item desse tipo nessa Area Path — troque o filtro ou a Area Path.")
        return itens

    def _wi_modo_filhos_em_lote(self, ado_client, ado_project: str, tipos: list, catalogo: dict):
        """
        Modo 'quebrar em Tasks': escolhe um Work Item pai (ex.: User Story),
        lista vários filhos (título + descrição opcional), e cria todos de
        uma vez já vinculados como filhos dele.
        """
        st.markdown("##### 1️⃣ Escolha o Work Item pai")
        pais = self._wi_buscar_work_items(ado_client, ado_project, tipos, "lote")
        if not pais:
            return
        rotulos = {f"{p['id']} - {p['title']} ({p['type']}, {p['state']})": p for p in pais}
        escolha_pai = st.selectbox("Work Item pai *", options=list(rotulos.keys()), index=None,
                                    placeholder="Escolha o pai...", key="wi_lote_pai_select",
                                    disabled=self.state.get('is_processing'))
        if not escolha_pai:
            return
        pai = rotulos[escolha_pai]

        # Mostra a descrição do pai — é dela que a pessoa tira as tasks.
        if self.state.get('wi_lote_pai_detalhe_id') != pai["id"]:
            try:
                with st.spinner("Carregando descrição do pai..."):
                    detalhes = ado_client.get_work_items_full_details([pai["id"]])
                self.state.set('wi_lote_pai_detalhe', detalhes[0] if detalhes else None)
                self.state.set('wi_lote_pai_detalhe_id', pai["id"])
            except Exception:
                self.state.set('wi_lote_pai_detalhe', None)
                self.state.set('wi_lote_pai_detalhe_id', pai["id"])
        detalhe = self.state.get('wi_lote_pai_detalhe')
        if detalhe and (detalhe.get("description") or detalhe.get("acceptance_criteria")):
            with st.expander(f"📄 Contexto de '{pai['title']}'", expanded=True):
                if detalhe.get("description"):
                    st.markdown("**Descrição:**")
                    st.write(detalhe["description"])
                if detalhe.get("acceptance_criteria"):
                    st.markdown("**Critérios de Aceite:**")
                    st.write(detalhe["acceptance_criteria"])

        st.divider()
        st.markdown("##### 2️⃣ Configure os filhos")
        nomes_tipos = [t["name"] for t in tipos]
        idx_task = nomes_tipos.index("Task") if "Task" in nomes_tipos else 0
        tipo_filho = st.selectbox("Tipo dos filhos *", options=nomes_tipos, index=idx_task,
                                   key="wi_lote_tipo_filho", disabled=self.state.get('is_processing'))

        area_path, iteration_path = self._wi_selecionar_area_e_iteration(ado_client, ado_project, "lote")
        if not area_path:
            st.caption(f"ℹ️ Sem Area Path escolhida, os filhos herdam a do pai: `{pai.get('area_path') or ado_project}`")

        membros = None
        with st.expander("👤 Atribuir todos a alguém (opcional)", key="wi_exp_membros_lote", on_change="rerun"):
            membros = self._wi_render_pessoas(ado_client, "lote")
        atribuir_a = None
        if membros:
            opcoes = ["(Ninguém)"] + [f"{m['display_name']} ({m['unique_name']})" for m in membros]
            escolha = st.selectbox("Atribuir todos a", options=opcoes, index=0,
                                    key="wi_lote_assigned", disabled=self.state.get('is_processing'))
            if escolha != "(Ninguém)":
                atribuir_a = membros[opcoes.index(escolha) - 1]["unique_name"]

        tags = self._wi_render_tags(ado_client, "lote")

        st.divider()
        st.markdown(f"##### 3️⃣ Liste os(as) {tipo_filho}(s)")
        filhos = self._wi_render_lista_filhos()

        preenchidos = [f for f in filhos if f["titulo"].strip()]
        st.divider()
        if st.button(
            f"🧩 Criar {len(preenchidos)} {tipo_filho}(s) dentro de '{pai['title']}'",
            type="primary", width="stretch",
            disabled=self.state.get('is_processing') or not preenchidos,
            key="btn_wi_lote_criar",
        ):
            self.state.set('wi_lote_snapshot', {
                "pai": pai, "tipo_filho": tipo_filho,
                "area_path": area_path or pai.get("area_path"),
                "iteration_path": iteration_path,
                "atribuir_a": atribuir_a, "tags": tags,
                "filhos": [{"titulo": f["titulo"].strip(), "descricao": f["descricao"].strip()} for f in preenchidos],
            })
            st.rerun()
        if not preenchidos:
            st.caption("Preencha ao menos um título de filho.")

        self._wi_confirmar_e_criar_lote(ado_client, catalogo)

    def _wi_render_lista_filhos(self) -> list:
        """
        Lista dinâmica de filhos (título + descrição) — mesmo padrão dos
        Passos de Reprodução do Criar Bug: mínimo 1, adicionar/remover por
        UUID (chave estável mesmo removendo item do meio).
        """
        chave = 'wi_lote_filhos'
        if self.state.get(chave) is None:
            self.state.set(chave, [{"uid": str(uuid.uuid4()), "titulo": "", "descricao": ""}])

        lista = self.state.get(chave)
        resultado = []
        for i, item in enumerate(lista):
            uid = item["uid"]
            col_t, col_d, col_x = st.columns([5, 5, 1])
            with col_t:
                titulo = st.text_input(f"Título {i + 1} *", value=item.get("titulo", ""),
                                        key=f"wi_filho_titulo_{uid}",
                                        disabled=self.state.get('is_processing'))
            with col_d:
                descricao = st.text_input("Descrição (opcional)", value=item.get("descricao", ""),
                                           key=f"wi_filho_desc_{uid}",
                                           disabled=self.state.get('is_processing'))
            with col_x:
                st.markdown("<div style='margin-top:1.8rem'></div>", unsafe_allow_html=True)
                if st.button("🗑️", key=f"wi_filho_del_{uid}",
                              disabled=len(lista) <= 1 or self.state.get('is_processing')):
                    self.state.set(chave, [p for p in lista if p["uid"] != uid])
                    st.rerun()
            resultado.append({"uid": uid, "titulo": titulo, "descricao": descricao})

        self.state.set(chave, resultado)
        if st.button("➕ Adicionar outro", key="wi_filho_add", disabled=self.state.get('is_processing')):
            atual = self.state.get(chave)
            atual.append({"uid": str(uuid.uuid4()), "titulo": "", "descricao": ""})
            self.state.set(chave, atual)
            st.rerun()
        return resultado

    def _wi_confirmar_e_criar(self, ado_client):
        """Confirmação + criação de UM Work Item (modo único)."""
        snapshot = self.state.get('wi_snapshot')
        resultado_anterior = self.state.get('wi_ultimo_criado')
        if resultado_anterior:
            st.success(f"🎉 {resultado_anterior['tipo']} criado: **{resultado_anterior['titulo']}** (ID {resultado_anterior['id']})")
            for linha in resultado_anterior.get("log", []):
                st.markdown(linha)
            if st.button("🧱 Criar outro", key="btn_wi_criar_outro", width="stretch"):
                self.state.set('wi_ultimo_criado', None)
                self.state.set('wi_snapshot', None)
                # Renova a key do uploader pra ele nascer vazio (ver comentário
                # no formulário).
                self.state.set('wi_form_versao', (self.state.get('wi_form_versao') or 0) + 1)
                st.rerun()
            return

        if not snapshot:
            return

        if self.state.get('current_action') == 'wi_confirm_criar' and not self.state.get('show_interrupt_modal'):
            try:
                campos = dict(snapshot["campos"])
                avisos = []

                # Mesma ordem do Criar Bug: as imagens sobem ANTES do item
                # existir (upload_attachment não depende de Work Item), pra já
                # dar pra embutir a URL de cada uma como <img> na Descrição na
                # mesma chamada que cria o item. O vínculo formal como anexo
                # (aba Attachments) vem depois, com o id em mãos — best-effort:
                # falha ali não desfaz o item, vira aviso.
                anexos_urls = []
                for nome, conteudo in (snapshot.get("imagens") or []):
                    try:
                        anexo = ado_client.upload_attachment(conteudo, nome)
                        if anexo.get("url"):
                            anexos_urls.append(anexo["url"])
                    except Exception as error:
                        avisos.append(f"⚠️ Falha ao enviar a imagem '{nome}': {error}")

                if anexos_urls and snapshot.get("tem_descricao"):
                    imgs_html = "".join(
                        f'<div><img src="{u}" style="max-width:100%" /></div>' for u in anexos_urls
                    )
                    descricao_atual = campos.get("System.Description") or ""
                    campos["System.Description"] = (
                        f"{descricao_atual}<br><br>{imgs_html}" if descricao_atual else imgs_html
                    )

                resultado = ado_client.create_work_item(
                    snapshot["tipo"], campos,
                    parent_id=snapshot.get("parent_id"), state=snapshot.get("state"),
                )

                for url in anexos_urls:
                    try:
                        ado_client.attach_file_to_work_item(
                            resultado["id"], url, comment="Evidência anexada via QA TestGen",
                        )
                    except Exception as error:
                        avisos.append(f"⚠️ Item criado, mas falha ao vincular uma imagem como anexo formal: {error}")

                log = []
                if anexos_urls:
                    onde = "anexada(s) e embutida(s) na Descrição" if snapshot.get("tem_descricao") else "anexada(s)"
                    log.append(f"↳ {len(anexos_urls)} imagem(ns) {onde}")
                log.extend(avisos)
                if snapshot.get("parent_id"):
                    log.append(f"↳ Vinculado como filho do Work Item {snapshot['parent_id']} (Parent/Child)")
                if snapshot.get("coluna"):
                    log.append(f"↳ Nasceu na coluna '{snapshot['coluna']}'")
                if resultado.get("state_warning"):
                    log.append(f"⚠️ {resultado['state_warning']}")
                if resultado.get("url"):
                    log.append(f"\n🔗 Abrir no Azure DevOps: {resultado['url']}")
                self._log("Criar Work Item", "Criar Work Item",
                          f"{snapshot['tipo']} #{resultado['id']} '{snapshot['campos'].get('System.Title', '')}'")
                self.state.set('wi_ultimo_criado', {
                    "id": resultado["id"], "tipo": snapshot["tipo"],
                    "titulo": snapshot["campos"].get("System.Title", ""), "log": log,
                })
                self.state.set('wi_snapshot', None)
            except Exception as error:
                self._flash_error(f"Não foi possível criar o Work Item: {error}")
            self.clear_action()
            st.rerun()

        n_imgs = len(snapshot.get("imagens") or [])
        st.warning(
            f"Vai criar um **{snapshot['tipo']}** chamado **{snapshot['campos'].get('System.Title', '')}** "
            f"com {len(snapshot['campos'])} campo(s) preenchido(s)"
            + (f", como filho do Work Item {snapshot['parent_id']}" if snapshot.get("parent_id") else "")
            + (f", com {n_imgs} imagem(ns) anexada(s)" if n_imgs else "")
            + ". Isso cria um item real no Azure DevOps e **não pode ser desfeito pelo app**. Confirma?"
        )
        with st.expander("🔍 Ver exatamente o que vai ser enviado"):
            for ref, valor in snapshot["campos"].items():
                st.markdown(f"- `{ref}` = {valor}")
            if n_imgs:
                st.markdown("**Imagens:** " + ", ".join(nome for nome, _ in snapshot["imagens"]))
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ Sim, criar", type="primary", width="stretch", key="btn_wi_confirm_sim"):
                self.trigger_action('wi_confirm_criar')
                st.rerun()
        with c2:
            if st.button("✖ Cancelar", width="stretch", key="btn_wi_confirm_nao"):
                self.state.set('wi_snapshot', None)
                st.rerun()

    def _wi_confirmar_e_criar_lote(self, ado_client, catalogo: dict):
        """Confirmação + criação em lote dos filhos de um pai."""
        snapshot = self.state.get('wi_lote_snapshot')
        resultado_anterior = self.state.get('wi_lote_ultimo')
        if resultado_anterior:
            st.success(resultado_anterior["resumo"])
            for linha in resultado_anterior.get("log", []):
                st.markdown(linha)
            if st.button("🧩 Criar outro lote", key="btn_wi_lote_outro", width="stretch"):
                self.state.set('wi_lote_ultimo', None)
                self.state.set('wi_lote_snapshot', None)
                self.state.set('wi_lote_filhos', None)
                st.rerun()
            return

        if not snapshot:
            return

        if self.state.get('current_action') == 'wi_lote_confirm' and not self.state.get('show_interrupt_modal'):
            log = []
            criados = 0
            tipo_desc = (catalogo.get("System.Description") or {}).get("type", "html")
            try:
                for filho in snapshot["filhos"]:
                    campos = {"System.Title": filho["titulo"]}
                    if filho.get("descricao"):
                        campos["System.Description"] = (
                            html.escape(filho["descricao"]).replace("\n", "<br>")
                            if tipo_desc == "html" else filho["descricao"]
                        )
                    if snapshot.get("area_path"):
                        campos["System.AreaPath"] = snapshot["area_path"]
                    if snapshot.get("iteration_path"):
                        campos["System.IterationPath"] = snapshot["iteration_path"]
                    if snapshot.get("atribuir_a"):
                        campos["System.AssignedTo"] = snapshot["atribuir_a"]
                    campos["System.Tags"] = self._tag_criado_por(snapshot.get("tags") or None)
                    try:
                        res = ado_client.create_work_item(
                            snapshot["tipo_filho"], campos, parent_id=snapshot["pai"]["id"],
                        )
                        criados += 1
                        link = f" — [abrir]({res['url']})" if res.get("url") else ""
                        log.append(f"✅ {snapshot['tipo_filho']} {res['id']}: **{filho['titulo']}**{link}")
                        if res.get("state_warning"):
                            log.append(f"  ⚠️ {res['state_warning']}")
                    except Exception as error:
                        log.append(f"❌ Falhou '{filho['titulo']}': {error}")
                resumo = (
                    f"🎉 {criados} de {len(snapshot['filhos'])} {snapshot['tipo_filho']}(s) "
                    f"criado(s) dentro de '{snapshot['pai']['title']}' (Work Item {snapshot['pai']['id']})"
                )
                self._log("Criar Work Item (lote)", "Criar Work Item",
                          f"{criados} {snapshot['tipo_filho']}(s) sob o Work Item {snapshot['pai']['id']}")
                self.state.set('wi_lote_ultimo', {"resumo": resumo, "log": log})
                self.state.set('wi_lote_snapshot', None)
            except Exception as error:
                self._flash_error(f"Não foi possível criar os filhos: {error}")
            self.clear_action()
            st.rerun()

        st.warning(
            f"Vai criar **{len(snapshot['filhos'])} {snapshot['tipo_filho']}(s)** como filhos de "
            f"**{snapshot['pai']['title']}** (Work Item {snapshot['pai']['id']}). "
            "Isso cria itens reais no Azure DevOps e **não pode ser desfeito pelo app**. Confirma?"
        )
        with st.expander("🔍 Ver a lista", expanded=True):
            for f in snapshot["filhos"]:
                st.markdown(f"- **{f['titulo']}**" + (f" — {f['descricao']}" if f.get("descricao") else ""))
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ Sim, criar todos", type="primary", width="stretch", key="btn_wi_lote_sim"):
                self.trigger_action('wi_lote_confirm')
                st.rerun()
        with c2:
            if st.button("✖ Cancelar", width="stretch", key="btn_wi_lote_nao"):
                self.state.set('wi_lote_snapshot', None)
                st.rerun()

    def _bug_creation_page(self):
        self._processing_banner()
        st.markdown('<div id="bug-form-top-anchor"></div>', unsafe_allow_html=True)
        st.subheader("🐛 Criar Bug")
        if st.button("← Voltar", key="btn_bug_back"):
            self.state.set('show_bug_page', False)
            self.state.set('show_bug_confirm_modal', False)
            st.rerun()

        if not self._get_permission_cached("criar_bug"):
            st.error("❌ Você não tem permissão pra acessar esta área.")
            return

        st.caption(
            "Cria um Bug diretamente no Azure DevOps — livremente, ou a partir de um Work Item "
            "com Casos de Teste relacionados. Nesse segundo modo, dá pra vincular a um Caso de "
            "Teste específico (título e Passos de Reprodução já vêm preenchidos, e o Bug fica "
            "vinculado de volta a ele) ou abrir direto no Work Item principal, sem escolher um "
            "Caso específico."
        )
        self._render_area_help("bug")

        conn = self._setup_azure_devops_connection(show_area_path_picker=False)
        if conn is None:
            return
        ado_client, ado_org, ado_project, _default_area_path = conn

        st.divider()
        modo = st.radio(
            "Como criar o Bug?",
            options=["📝 Livre", "🔗 A partir de um Work Item"],
            index=0,
            key="bug_modo_radio",
            horizontal=True,
            disabled=self.state.get('is_processing'),
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

        board_escolhido = st.selectbox(
            "Board (Area Path) *",
            options=area_path_options,
            index=None,
            placeholder="Escolha o board...",
            key="bug_board_select",
            disabled=self.state.get('is_processing'),
            help="Obrigatório nos dois modos — define em qual Area Path o Bug vai ser criado.",
        )
        if not board_escolhido:
            st.caption("Escolha um board pra continuar.")
            return

        if modo.startswith("🔗"):
            self._bug_from_test_case_flow(ado_client, board_escolhido)
        else:
            self._bug_free_form_flow(ado_client, board_escolhido)

    def _bug_from_test_case_flow(self, ado_client, board_escolhido: str):
        # Chamado aqui em cima (e não só no fim da função) porque agora
        # _limpar_estado_bug apaga (na prática, aposenta via versão — ver
        # abaixo) bug_wi_select/bug_caso_select depois de criar o Bug —
        # se essa chamada continuasse só no fim, o "return" antecipado de
        # "nenhum Work Item escolhido" (mais abaixo) nunca deixaria a
        # mensagem de sucesso aparecer.
        self._render_bug_confirmation_screen(ado_client, "de_caso", board_escolhido)

        # Work Item/Caso de Teste também levam "_{versao}" na chave — não
        # bastava apagar do session_state (del): um selectbox já renderizado
        # às vezes não reseta visualmente mesmo com a chave removida (mesmo
        # comportamento conhecido do Streamlit que já valia pra Título via
        # bug_titulo_de_caso_{versao}, só que também acontecendo aqui).
        versao = self.state.get('bug_de_caso_form_versao') or 0

        st.divider()
        st.markdown("##### 🔎 Escolha o Work Item de origem")

        with st.container(key="azure_blue_btn_fetch_wi_bug"):
            st.button(
                "🔄 Buscar Work Items desse Board",
                disabled=self.state.get('is_processing'),
                key="btn_fetch_wi_bug",
                on_click=self.trigger_action,
                args=("fetch_wi_bug",),
                width="stretch",
            )
        if self.state.get('current_action') == 'fetch_wi_bug' and not self.state.get('show_interrupt_modal'):
            try:
                with st.spinner("Buscando Work Items..."):
                    items = ado_client.fetch_work_items_by_area_path(board_escolhido, excluded_states=set())
                self.state.set('bug_board_items', items)
                self.state.set('bug_test_cases', [])
                if f'bug_wi_select_{versao}' in st.session_state:
                    del st.session_state[f'bug_wi_select_{versao}']
                if not items:
                    self._flash_warning("Nenhum Work Item encontrado nesse board.")
            except Exception as error:
                self._flash_error(f"Não foi possível buscar Work Items: {error}")
                self.state.set('bug_board_items', [])
            self.clear_action()
            st.rerun()

        board_items = self.state.get('bug_board_items') or []
        if not board_items:
            st.caption("Busque os Work Items acima pra continuar.")
            return

        board_items = self._filtrar_por_coluna_e_tag(board_items, True, "bug_wi")
        wi_labels = {f"{i['id']} - {i['title']} ({i['type']}, {i['state']})": i for i in board_items}
        escolha_wi = st.selectbox(
            "Work Item de origem", options=list(wi_labels.keys()), index=None,
            placeholder="Escolha um Work Item...", key=f"bug_wi_select_{versao}",
            disabled=self.state.get('is_processing'),
        )
        if not escolha_wi:
            return
        work_item_escolhido = wi_labels[escolha_wi]

        vinculo_modo = st.radio(
            "Vincular o Bug a...",
            options=["🎯 Um Caso de Teste específico", "📌 Direto no Work Item (sem Caso específico)"],
            index=0,
            key=f"bug_vinculo_modo_{versao}",
            horizontal=True,
            disabled=self.state.get('is_processing'),
            help=(
                "Escolha a segunda opção quando o problema não é de um Caso de Teste "
                "específico, mas sim do Work Item principal como um todo (ex.: algo que "
                "afeta vários Casos relacionados, ou que você percebeu sem estar "
                "executando um Caso específico)."
            ),
        )

        caso_escolhido = None
        if vinculo_modo.startswith("🎯"):
            with st.container(key="azure_blue_btn_fetch_tc_bug"):
                st.button(
                    "🔄 Buscar Casos de Teste vinculados a esse Work Item",
                    disabled=self.state.get('is_processing'),
                    key="btn_fetch_tc_bug",
                    on_click=self.trigger_action,
                    args=("fetch_tc_bug",),
                    width="stretch",
                )
            if self.state.get('current_action') == 'fetch_tc_bug' and not self.state.get('show_interrupt_modal'):
                try:
                    with st.spinner("Buscando Casos de Teste vinculados..."):
                        casos = ado_client.get_existing_test_cases_full(work_item_escolhido['id'])
                    self.state.set('bug_test_cases', casos)
                    if f'bug_caso_select_{versao}' in st.session_state:
                        del st.session_state[f'bug_caso_select_{versao}']
                    if not casos:
                        self._flash_warning("Esse Work Item não tem nenhum Caso de Teste vinculado no Azure DevOps.")
                except Exception as error:
                    self._flash_error(f"Não foi possível buscar Casos de Teste: {error}")
                    self.state.set('bug_test_cases', [])
                self.clear_action()
                st.rerun()

            casos = self.state.get('bug_test_cases') or []
            if not casos:
                st.caption("Busque os Casos de Teste vinculados acima pra continuar.")
                return

            caso_labels = {f"{c['id']} - {c['titulo']}": c for c in casos}
            escolha_caso = st.selectbox(
                "Caso de Teste de origem", options=list(caso_labels.keys()), index=None,
                placeholder="Escolha um Caso de Teste...", key=f"bug_caso_select_{versao}",
                disabled=self.state.get('is_processing'),
            )
            if not escolha_caso:
                return
            caso_escolhido = caso_labels[escolha_caso]

        # Chave usada só pra saber SE precisa reprefill os Passos de
        # Reprodução (muda de Caso, ou alterna entre Caso/Work Item) — sem
        # isso, qualquer rerun do Streamlit (ex.: digitar em outro campo)
        # reapagaria o que a pessoa já tivesse editado manualmente.
        chave_prefill_atual = caso_escolhido['id'] if caso_escolhido else '__sem_caso__'
        if self.state.get('bug_de_caso_repro_prefilled_from') != chave_prefill_atual:
            passos_iniciais = self._montar_passos_repro_de_caso(caso_escolhido) if caso_escolhido else [""]
            self.state.set('bug_de_caso_repro_steps_list', [
                {"uid": str(uuid.uuid4()), "texto": t} for t in passos_iniciais
            ])
            self.state.set('bug_de_caso_repro_prefilled_from', chave_prefill_atual)

        st.divider()
        st.markdown("##### 🐞 Detalhes do Bug")
        if caso_escolhido:
            st.caption("Título e Passos de Reprodução já vêm preenchidos a partir do Caso de Teste — ajuste se precisar.")
            titulo_padrao = f"Bug: {caso_escolhido['titulo']}"
        else:
            st.caption("Bug será aberto direto no Work Item principal, sem vincular a um Caso de Teste específico.")
            titulo_padrao = f"Bug: {work_item_escolhido['title']}"
        titulo = st.text_input(
            "Título *", value=titulo_padrao, key=f"bug_titulo_de_caso_{versao}",
            disabled=self.state.get('is_processing'),
        )
        descricao = st.text_area(
            "Descrição do bug *", key=f"bug_descricao_de_caso_{versao}", height=120,
            placeholder="Descreva o que aconteceu de errado...",
            disabled=self.state.get('is_processing'),
        )
        st.markdown("**Passos de Reprodução ***")
        passos = self._render_repro_steps_editor("bug_de_caso")
        extras = self._render_bug_extra_fields("de_caso")

        col_p, col_s = st.columns(2)
        with col_p:
            prioridade = st.selectbox("Prioridade", options=[1, 2, 3, 4], index=1, key=f"bug_prioridade_de_caso_{versao}",
                                       disabled=self.state.get('is_processing'))
        with col_s:
            severidade = st.selectbox(
                "Severidade", options=["1 - Critical", "2 - High", "3 - Medium", "4 - Low"],
                index=2, key=f"bug_severidade_de_caso_{versao}", disabled=self.state.get('is_processing'),
            )

        st.divider()
        metadata = self._render_bug_metadata_picker(ado_client, "de_caso", board_escolhido)

        passos_preenchidos = [p.strip() for p in passos if p.strip()]
        pode_confirmar = bool(titulo.strip()) and bool(descricao.strip()) and bool(passos_preenchidos) and bool(metadata['coluna'])

        st.divider()
        with st.container(key="azure_blue_btn_ir_confirmar_de_caso"):
            if st.button(
                "🐛 Criar Bug", type="primary", width="stretch",
                disabled=self.state.get('is_processing') or not pode_confirmar,
                key="btn_ir_confirmar_de_caso",
            ):
                self.state.set('bug_de_caso_snapshot', {
                    "titulo": titulo.strip(), "descricao": descricao.strip(),
                    "passos": passos_preenchidos, "prioridade": prioridade, "severidade": severidade,
                    "board": board_escolhido, "coluna": metadata['coluna'],
                    "coluna_team_id": metadata['team_id'], "coluna_board_id": metadata.get('coluna_board_id'),
                    "coluna_state": metadata.get('coluna_state'),
                    "tags": metadata['tags'], "atribuir_a": metadata['atribuir_a'],
                    "system_info": extras['system_info'], "acceptance_criteria": extras['acceptance_criteria'],
                    "discussion": extras['discussion'], "imagens": extras['imagens'],
                    "vinculo": {
                        **({"caso_id": caso_escolhido['id'], "caso_titulo": caso_escolhido['titulo']} if caso_escolhido else {}),
                        "wi_id": work_item_escolhido['id'], "wi_titulo": work_item_escolhido['title'],
                    },
                })
                self.state.set('bug_confirm_key_prefix', 'de_caso')
                self.state.set('show_bug_confirm_modal', True)
                st.rerun()
        if not pode_confirmar:
            faltando = []
            if not titulo.strip():
                faltando.append("Título")
            if not descricao.strip():
                faltando.append("Descrição")
            if not passos_preenchidos:
                faltando.append("pelo menos 1 Passo de Reprodução preenchido")
            if not metadata['coluna']:
                faltando.append("Coluna do Board (busque acima)")
            st.caption(f"Preencha: {', '.join(faltando)}.")

        self._render_bug_confirm_inline('de_caso')

    def _bug_free_form_flow(self, ado_client, board_escolhido: str):
        st.divider()
        st.markdown("##### 🐞 Detalhes do Bug")
        versao = self.state.get('bug_livre_form_versao') or 0
        titulo = st.text_input("Título *", key=f"bug_titulo_livre_{versao}", disabled=self.state.get('is_processing'))
        descricao = st.text_area(
            f"Descrição *", key=f"bug_descricao_livre_{versao}", height=120,
            placeholder="Descreva o que aconteceu de errado...",
            disabled=self.state.get('is_processing'),
        )
        st.markdown("**Passos de Reprodução ***")
        passos = self._render_repro_steps_editor("bug_livre")
        extras = self._render_bug_extra_fields("livre")

        col_p, col_s = st.columns(2)
        with col_p:
            prioridade = st.selectbox("Prioridade", options=[1, 2, 3, 4], index=1, key=f"bug_prioridade_livre_{versao}",
                                       disabled=self.state.get('is_processing'))
        with col_s:
            severidade = st.selectbox(
                "Severidade", options=["1 - Critical", "2 - High", "3 - Medium", "4 - Low"],
                index=2, key=f"bug_severidade_livre_{versao}", disabled=self.state.get('is_processing'),
            )

        st.divider()
        metadata = self._render_bug_metadata_picker(ado_client, "livre", board_escolhido)

        passos_preenchidos = [p.strip() for p in passos if p.strip()]
        pode_confirmar = bool(titulo.strip()) and bool(descricao.strip()) and bool(passos_preenchidos) and bool(metadata['coluna'])

        st.divider()
        with st.container(key="azure_blue_btn_ir_confirmar_livre"):
            if st.button(
                "🐛 Criar Bug", type="primary", width="stretch",
                disabled=self.state.get('is_processing') or not pode_confirmar,
                key="btn_ir_confirmar_livre",
            ):
                self.state.set('bug_livre_snapshot', {
                    "titulo": titulo.strip(), "descricao": descricao.strip(),
                    "passos": passos_preenchidos, "prioridade": prioridade, "severidade": severidade,
                    "board": board_escolhido, "coluna": metadata['coluna'],
                    "coluna_team_id": metadata['team_id'], "coluna_board_id": metadata.get('coluna_board_id'),
                    "coluna_state": metadata.get('coluna_state'),
                    "tags": metadata['tags'], "atribuir_a": metadata['atribuir_a'],
                    "system_info": extras['system_info'], "acceptance_criteria": extras['acceptance_criteria'],
                    "discussion": extras['discussion'], "imagens": extras['imagens'],
                })
                self.state.set('bug_confirm_key_prefix', 'livre')
                self.state.set('show_bug_confirm_modal', True)
                st.rerun()
        if not pode_confirmar:
            faltando = []
            if not titulo.strip():
                faltando.append("Título")
            if not descricao.strip():
                faltando.append("Descrição")
            if not passos_preenchidos:
                faltando.append("pelo menos 1 Passo de Reprodução preenchido")
            if not metadata['coluna']:
                faltando.append("Coluna do Board (busque acima)")
            st.caption(f"Preencha: {', '.join(faltando)}.")

        self._render_bug_confirm_inline('livre')
        self._render_bug_confirmation_screen(ado_client, "livre", board_escolhido)

    def _criar_bug_via_azure(self, ado_client, dados: dict):
        """
        Faz a chamada de verdade ao Azure DevOps pra criar o Bug (e os 2
        vínculos, se vier de um Caso de Teste). Chamada de dentro de
        _render_bug_confirmation_screen quando current_action ==
        confirm_bug_{key_prefix}, igual a todo outro processamento do
        app — o que dá de graça o overlay global "Processamento em
        andamento" (_processing_banner), incluindo bloqueio de clique
        na tela inteira e o botão real de Cancelar.

        Evidências em imagem (dados['imagens']) sobem ANTES do Bug
        existir (upload_attachment não depende de nenhum Work Item) —
        assim já dá pra embutir a URL de cada uma como <img> dentro do
        campo System Info na MESMA chamada que cria o Bug. Só depois,
        com o Bug já criado, cada anexo é formalmente vinculado a ele
        (attach_file_to_work_item) — best-effort, uma falha aqui não
        desfaz o Bug já criado, só vira aviso.

        Discussion não é um campo comum do Work Item (ver docstring de
        add_work_item_comment) — só dá pra postar DEPOIS do Bug existir,
        como o primeiro comentário dele.

        Retorna (resultado, avisos) — avisos é uma lista de strings pra
        problemas não-fatais que não impediram o Bug de ser criado (ex.:
        uma evidência específica falhou ao subir, ou o comentário de
        Discussion não pôde ser postado).
        """
        passos_html = [html.escape(p).replace("\n", "<br>") for p in dados['passos']]
        repro_texto = "<br>".join(f"{i}. {p}" for i, p in enumerate(passos_html, start=1))
        campo_coluna = None
        if dados.get('coluna_team_id') and dados.get('coluna_board_id'):
            campo_coluna = ado_client.get_board_column_field_name(
                dados['coluna_team_id'], dados['coluna_board_id']
            )

        avisos = []
        anexos_urls = []
        for nome, conteudo in (dados.get('imagens') or []):
            try:
                anexo = ado_client.upload_attachment(conteudo, nome)
                if anexo.get('url'):
                    anexos_urls.append(anexo['url'])
            except Exception as error:
                avisos.append(f"⚠️ Falha ao enviar a evidência '{nome}': {error}")

        system_info_texto = dados.get('system_info', '')
        system_info_html = html.escape(system_info_texto).replace("\n", "<br>") if system_info_texto else ""
        if anexos_urls:
            imgs_html = "".join(f'<div><img src="{u}" style="max-width:100%" /></div>' for u in anexos_urls)
            system_info_html = f"{system_info_html}<br><br>{imgs_html}" if system_info_html else imgs_html

        acceptance_criteria_texto = dados.get('acceptance_criteria', '')
        acceptance_criteria_html = (
            html.escape(acceptance_criteria_texto).replace("\n", "<br>") if acceptance_criteria_texto else ""
        )

        resultado = ado_client.create_bug(
            dados['titulo'], dados['board'], dados['descricao'], repro_texto,
            dados['prioridade'], dados['severidade'], dados['atribuir_a'],
            self._tag_criado_por("; ".join(dados['tags']) if dados['tags'] else None),
            dados['coluna'], campo_coluna, dados.get('coluna_state'),
            system_info_html, acceptance_criteria_html,
        )

        for url in anexos_urls:
            try:
                ado_client.attach_file_to_work_item(resultado['id'], url, comment="Evidência anexada via QA TestGen")
            except Exception as error:
                avisos.append(f"⚠️ Bug criado, mas falha ao vincular uma evidência como anexo formal: {error}")

        discussion_texto = dados.get('discussion', '')
        if discussion_texto:
            try:
                ado_client.add_work_item_comment(
                    resultado['id'], html.escape(discussion_texto).replace("\n", "<br>")
                )
            except Exception as error:
                avisos.append(f"⚠️ Bug criado, mas falha ao postar o comentário de Discussion: {error}")

        vinculo = dados.get('vinculo')
        if vinculo:
            if vinculo.get('caso_id'):
                ado_client.link_bug_to_test_case(resultado['id'], vinculo['caso_id'])
            if vinculo.get('wi_id'):
                ado_client.link_bug_to_related_work_item(resultado['id'], vinculo['wi_id'])
        return resultado, avisos

    def _iniciar_novo_bug(self, key_prefix: str):
        self.state.set(f'bug_ultimo_criado_{key_prefix}', None)
        self._limpar_estado_bug(key_prefix, limpar_metadados=True)
        self.state.set('scroll_to_top_pending', True)

    def _render_bug_confirmation_screen(self, ado_client, key_prefix: str, board_escolhido: str):
        """
        Processa a criação do Bug (chamado pelos 2 modos) — a confirmação
        de criação em si é renderizada inline por _render_bug_confirm_inline,
        logo abaixo do botão "Criar Bug" em cada modo (não aqui, que roda
        cedo demais na página pra isso fazer sentido visualmente). Esse
        método cuida do que roda cedo: o processamento em si (current_action
        == confirm_bug_{key_prefix} — mesmo padrão de todo outro
        processamento do app, pra herdar o _processing_banner), o
        resultado, e a confirmação de "Criar outro Bug".
        """
        if self.state.get(f'_bug_confirmado_{key_prefix}'):
            # Render intermediário e rápido — ver docstring de
            # _render_bug_confirm_inline pra entender por que isso não é
            # feito direto no clique do botão "Sim, Criar Bug".
            self.state.set(f'_bug_confirmado_{key_prefix}', False)
            self.state.set('current_action', f'confirm_bug_{key_prefix}')
            self.state.set('is_processing', True)
            st.rerun()

        if self.state.get('current_action') == f'confirm_bug_{key_prefix}' and not self.state.get('show_interrupt_modal'):
            dados = self.state.get(f'bug_{key_prefix}_snapshot')
            vinculo = dados.get('vinculo') if dados else None
            try:
                if not dados:
                    raise ValueError("Os dados do Bug foram perdidos antes de confirmar — tenta preencher de novo.")
                resultado, avisos = self._criar_bug_via_azure(ado_client, dados)
                detalhe_log = f"Bug #{resultado['id']} '{dados['titulo']}' — coluna '{dados['coluna']}'"
                log = [f"✅ Bug criado: **{dados['titulo']}** (ID {resultado['id']})"]
                if dados.get('imagens'):
                    log.append(f"↳ {len(dados['imagens'])} evidência(s) em imagem anexada(s) e embutida(s) no System Info")
                if dados.get('discussion'):
                    log.append("↳ Comentário postado na aba Discussion")
                for aviso in avisos:
                    log.append(aviso)
                if vinculo:
                    if vinculo.get('caso_id'):
                        log.append(f"↳ Vinculado ao Caso de Teste '{vinculo['caso_titulo']}' (Caso {vinculo['caso_id']}) — Tested By")
                    log.append(f"↳ Vinculado ao Work Item '{vinculo['wi_titulo']}' (Work Item {vinculo['wi_id']}) — Related")
                    if vinculo.get('caso_id'):
                        detalhe_log += f" — a partir do Caso {vinculo['caso_id']} (Work Item {vinculo['wi_id']})"
                    else:
                        detalhe_log += f" — direto no Work Item {vinculo['wi_id']} (sem Caso específico)"
                else:
                    detalhe_log += " — modo livre"
                if resultado.get('url'):
                    log.append(f"\n🔗 Confira o Bug no Azure DevOps: {resultado['url']}")
                self._log("Criar Bug", "Criar Bug", detalhe_log)
                self.state.set(f'bug_ultimo_criado_{key_prefix}', {'resultado': resultado, 'log': log})
                self.state.set('scroll_to_top_pending', True)
                self._limpar_estado_bug(key_prefix)
            except Exception as error:
                self._flash_error(f"Erro ao criar o Bug: {error}")
            self.clear_action()
            st.rerun()

        ultimo = self.state.get(f'bug_ultimo_criado_{key_prefix}')
        if ultimo:
            st.markdown('<div id="bug-sucesso-anchor"></div>', unsafe_allow_html=True)
            st.markdown("##### 📋 Resultado da integração")
            for line in ultimo['log']:
                st.write(line)
            if self.state.get(f'show_new_bug_modal_{key_prefix}'):
                with st.container(border=True):
                    st.markdown("##### ⚠️ Criar Outro Bug")
                    st.markdown(
                        "Isso vai limpar os campos já preenchidos pro próximo Bug — Work Item/Caso "
                        "de Teste selecionado (se houver), título, passos, prioridade, etc., se você "
                        "já tiver começado a preencher algo. Essas informações serão **perdidas "
                        "permanentemente**. Tem certeza que deseja criar outro Bug?"
                    )
                    cc1, cc2 = st.columns(2)
                    with cc1:
                        if st.button("🔄 Sim, Criar Outro", width="stretch", type="primary",
                                      key=f"confirm_new_bug_yes_{key_prefix}"):
                            self._iniciar_novo_bug(key_prefix)
                            self.state.set(f'show_new_bug_modal_{key_prefix}', False)
                            st.rerun()
                    with cc2:
                        if st.button("Cancelar", width="stretch", key=f"confirm_new_bug_no_{key_prefix}"):
                            self.state.set(f'show_new_bug_modal_{key_prefix}', False)
                            st.rerun()
            else:
                if st.button("➕ Criar outro Bug", key=f"btn_bug_outro_{key_prefix}", width="stretch"):
                    self.state.set(f'show_new_bug_modal_{key_prefix}', True)
                    st.rerun()

    def _render_bug_confirm_inline(self, key_prefix: str):
        """
        Confirmação de criação do Bug, renderizada como conteúdo normal
        da página — NÃO um st.dialog. Chamada logo abaixo do botão
        "Criar Bug" em cada modo (_bug_from_test_case_flow /
        _bug_free_form_flow), não em _render_bug_confirmation_screen (que
        roda no topo da página, longe do botão — faria a confirmação
        nascer fora da área visível).

        Por que não é mais um st.dialog: em testes reais, o modal ficava
        sobreposto à tela mesmo depois de "fechado" (show_bug_confirm_modal
        = False + st.rerun()), inclusive por cima do overlay de
        processamento — persistiu mesmo depois de isolar o fechamento num
        render rápido, separado do processamento em si. Conteúdo normal
        (não dialog) não tem essa ambiguidade: o Streamlit garante que ele
        para de existir assim que a condição vira False, sem depender de
        quando (ou se) o navegador decide desmontar um modal.

        "Sim, Criar Bug" NÃO liga current_action/is_processing direto —
        só marca _bug_confirmado_{key_prefix} e dá um rerun rápido, sem
        trabalho lento nele. É em _render_bug_confirmation_screen, no
        PRÓXIMO render (já sem esse bloco na tela), que essa marca liga
        current_action/is_processing de verdade — daí sim o trabalho roda,
        com o overlay global "Processamento em andamento".
        """
        if not (self.state.get('show_bug_confirm_modal') and self.state.get('bug_confirm_key_prefix') == key_prefix):
            return
        dados = self.state.get(f'bug_{key_prefix}_snapshot')
        if not dados:
            self.state.set('show_bug_confirm_modal', False)
            return

        with st.container(border=True):
            st.markdown("##### 🐛 Confirmar Criação de Bug")
            st.markdown(f"**Título:** {dados['titulo']}")
            st.markdown(f"**Board (Area Path):** {dados['board']}")
            st.markdown(f"**Coluna do Board:** {dados['coluna']}")
            st.markdown(f"**Prioridade:** {dados['prioridade']}  |  **Severidade:** {dados['severidade']}")
            if dados.get('tags'):
                st.markdown(f"**Tags:** {', '.join(dados['tags'])}")
            if dados.get('atribuir_a'):
                st.markdown(f"**Atribuído a:** {dados['atribuir_a']}")

            with st.expander("📋 Descrição e Passos de Reprodução", expanded=True):
                st.markdown(f"**Descrição:**\n\n{dados['descricao']}")
                st.markdown("**Passos de Reprodução:**")
                for i, p in enumerate(dados['passos'], start=1):
                    st.write(f"{i}. {p}")

            if dados.get('system_info') or dados.get('acceptance_criteria') or dados.get('discussion') or dados.get('imagens'):
                with st.expander("➕ Detalhes adicionais"):
                    if dados.get('system_info'):
                        st.markdown(f"**System Info:**\n\n{dados['system_info']}")
                    if dados.get('imagens'):
                        st.markdown(f"**Evidências:** {len(dados['imagens'])} imagem(ns) — entram anexadas e embutidas no System Info")
                    if dados.get('acceptance_criteria'):
                        st.markdown(f"**Acceptance Criteria:**\n\n{dados['acceptance_criteria']}")
                    if dados.get('discussion'):
                        st.markdown(f"**Discussion:**\n\n{dados['discussion']}")

            vinculo = dados.get('vinculo')
            if vinculo and vinculo.get('caso_id'):
                st.info(
                    f"Esse Bug será vinculado automaticamente ao Caso de Teste **{vinculo['caso_titulo']}** "
                    f"(Tested By) e ao Work Item **{vinculo['wi_titulo']}** (Related)."
                )
            elif vinculo:
                st.info(
                    f"Esse Bug será vinculado automaticamente ao Work Item **{vinculo['wi_titulo']}** "
                    "(Related), sem um Caso de Teste específico."
                )

            st.warning(
                "Essa ação cria um item real no seu projeto do Azure DevOps e **não pode ser desfeita "
                "automaticamente** — se algo sair errado, a exclusão precisa ser feita manualmente lá. "
                "Tem certeza que deseja prosseguir?"
            )

            c1, c2 = st.columns(2)
            with c1:
                if st.button("🐛 Sim, Criar Bug", width="stretch", type="primary",
                              key=f"confirm_bug_yes_{key_prefix}"):
                    self.state.set('show_bug_confirm_modal', False)
                    self.state.set(f'_bug_confirmado_{key_prefix}', True)
                    st.rerun()
            with c2:
                if st.button("❌ Cancelar", width="stretch", key=f"confirm_bug_no_{key_prefix}"):
                    self.state.set('show_bug_confirm_modal', False)
                    st.rerun()

    def _limpar_estado_bug(self, key_prefix: str, limpar_metadados: bool = False):
        """
        Limpa o estado da tela de Criar Bug — nunca mexe em PAT/Org/
        Projeto/Area Path (isso é gerenciado por
        _setup_azure_devops_connection, sem prefixo "bug_", então
        continua intacto).

        As ESCOLHAS do formulário (Work Item/Caso de Teste de origem,
        Coluna, Tags, Atribuir a, Prioridade, Severidade) são sempre
        resetadas aqui, mesmo na chamada automática pós-sucesso — senão o
        próximo Bug nascia herdando a seleção do Bug anterior: com o
        mesmo Caso de Teste ainda selecionado, Título e Passos de
        Reprodução voltavam a vir pré-preenchidos com os mesmos dados
        de novo, dando a impressão de que nada tinha sido limpo.
        limpar_metadados=True vai além disso e também descarta as
        LISTAS já buscadas do Azure DevOps (Work Items, Casos de Teste,
        Colunas, Tags, Membros) — usado em "Criar outro Bug", pra
        começar do zero de verdade; a limpeza pós-sucesso comum não
        mexe nessas listas, só nas escolhas, pra não obrigar buscar
        tudo de novo à toa se o próximo Bug for do mesmo board.

        Incrementa "bug_{prefixo}_form_versao" — TODOS os widgets de
        escolha do formulário (Título, Descrição, Work Item, Caso de
        Teste, Coluna, Tags, Atribuir a, Prioridade, Severidade) levam
        essa versão como parte da própria chave (não uma chave fixa),
        porque só apagar do session_state nem sempre reseta visualmente
        um widget já renderizado antes — testei ao vivo e confirmei isso
        também pra selectbox (Work Item ficava mostrando a escolha
        anterior mesmo com a chave apagada de session_state), não só
        pra campo de texto como se pensava antes. Trocar a versão força
        o widget a nascer de novo, do zero, de verdade — mesma
        estratégia já usada nos Passos de Reprodução, que usam um UUID
        novo a cada reinício. Por isso o `del st.session_state[...]`
        abaixo é só faxina (evita acumular chaves de versões antigas
        órfãs) — quem garante o reset visual é a versão ter mudado.
        """
        self.state.set(f'bug_{key_prefix}_snapshot', None)
        self.state.set('show_bug_confirm_modal', False)
        self.state.set(f'bug_{key_prefix}_form_versao', (self.state.get(f'bug_{key_prefix}_form_versao') or 0) + 1)
        if key_prefix == "de_caso":
            self.state.set('bug_de_caso_repro_prefilled_from', None)
            campos_limpar = ['bug_de_caso_repro_steps_list']
        else:
            campos_limpar = ['bug_livre_repro_steps_list']

        # Faxina das chaves versionadas de versões anteriores (Work Item,
        # Caso de Teste, Coluna, Tags, Atribuir a, Prioridade, Severidade)
        # — sempre, independente de limpar_metadados. O reset visual em si
        # já aconteceu ao incrementar form_versao acima; isso aqui só evita
        # que session_state acumule lixo de reinícios anteriores.
        for k in list(st.session_state.keys()):
            if k.startswith((f'bug_wi_select_', f'bug_caso_select_', f'bug_vinculo_modo_', f'bug_coluna_select_{key_prefix}',
                              f'bug_tags_select_{key_prefix}', f'bug_membro_select_{key_prefix}',
                              f'bug_prioridade_{key_prefix}', f'bug_severidade_{key_prefix}',
                              f'bug_system_info_{key_prefix}', f'bug_acceptance_criteria_{key_prefix}',
                              f'bug_discussion_{key_prefix}', f'bug_evidencias_{key_prefix}')):
                campos_limpar.append(k)

        if limpar_metadados:
            if key_prefix == "de_caso":
                for k in ('bug_board_items', 'bug_test_cases'):
                    self.state.set(k, [])
            for k in list(st.session_state.keys()):
                if k.startswith((f'bug_colunas_combinadas_{key_prefix}', f'bug_tags_existentes_{key_prefix}',
                                  f'bug_membros_{key_prefix}')):
                    campos_limpar.append(k)
        for k in campos_limpar:
            if k in st.session_state:
                del st.session_state[k]

    @staticmethod
    def _montar_passos_repro_de_caso(caso: dict) -> list:
        """
        Converte os passos (Ação -> Resultado Esperado) de um Caso de
        Teste já existente numa LISTA de textos, um por passo — usado
        pra pré-preencher a lista dinâmica de Passos de Reprodução do
        Bug (cada passo do Caso vira 1 item editável da lista).
        """
        passos_texto = []
        for passo in (caso.get('passos') or []):
            texto = passo.get('acao', '') or ''
            if passo.get('resultado_esperado'):
                texto = f"{texto}\nEsperado: {passo['resultado_esperado']}" if texto else f"Esperado: {passo['resultado_esperado']}"
            if texto:
                passos_texto.append(texto)
        return passos_texto or [""]

    @staticmethod
    def _parse_plans_csv(conteudo: bytes) -> dict:
        """
        Lê de volta o CSV gerado por AzureCsvFormatter.plans_suites_cases
        (colunas: CASES_HEADER + Suite + Plan) e reconstrói a hierarquia
        {plano: {suite: [casos]}}. Linhas de Step (sem "Test Case" na
        coluna de tipo) são ignoradas — só interessa o nível de Caso aqui.
        """
        import csv
        import io
        text = conteudo.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            return {}
        hierarquia = {}
        for row in rows[1:]:
            if len(row) < 4 or row[1] != "Test Case":
                continue
            titulo = row[2]
            suite_nome = row[-2] or "(sem suíte)"
            plano_nome = row[-1] or "(sem plano)"
            hierarquia.setdefault(plano_nome, {}).setdefault(suite_nome, []).append(titulo)
        return hierarquia

    _MAPA_LARGURA_NO = 220
    _MAPA_ALTURA_NO = 46
    _MAPA_ESPACO_VERTICAL = 18
    _MAPA_ESPACO_HORIZONTAL = 280
    _MAPA_MARGEM = 30

    @staticmethod
    def _montar_arvore_mapa(raiz_nome: str, hierarquia: dict) -> dict:
        """
        Monta a árvore raiz -> Planos/Work Items -> [Suítes] -> Casos como
        um dict aninhado simples ({"nome","count","filhos": [...]}) — usado
        tanto pra gerar o PDF (ReportLab) quanto poderia ser reaproveitado
        por outras exportações futuras. Mesma lógica de agrupamento do
        equivalente em JS (_d3_mind_map_html): suíte única vira filho
        direto do Plano, mais de uma suíte cria um nível extra.
        """
        def montar_no(nome, suites=None, casos=None):
            if casos is not None:
                return {"nome": nome, "count": None, "filhos": []}
            total_casos = sum(len(c) for c in suites.values())
            n_suites = len(suites)
            if n_suites <= 1:
                casos_unicos = next(iter(suites.values())) if suites else []
                return {"nome": nome, "count": total_casos, "filhos": [montar_no(c, casos=True) for c in casos_unicos]}
            return {
                "nome": nome, "count": total_casos,
                "filhos": [
                    {"nome": ns, "count": len(cs), "filhos": [montar_no(c, casos=True) for c in cs]}
                    for ns, cs in suites.items()
                ],
            }
        return {"nome": raiz_nome, "count": None, "filhos": [montar_no(np, s) for np, s in hierarquia.items()]}

    @classmethod
    def _calcular_layout_mapa(cls, no: dict, profundidade: int, y_cursor: list):
        """
        Preenche no["x"]/no["y"] em cada nó da árvore (mutando in-place),
        sempre com tudo expandido — layout simples: folhas recebem Y
        sequencial (post-order), nós internos centralizam sobre os filhos.
        y_cursor é uma lista de 1 elemento usada como contador mutável.
        """
        no["x"] = profundidade * cls._MAPA_ESPACO_HORIZONTAL
        if not no["filhos"]:
            no["y"] = y_cursor[0]
            y_cursor[0] += cls._MAPA_ALTURA_NO + cls._MAPA_ESPACO_VERTICAL
        else:
            for filho in no["filhos"]:
                cls._calcular_layout_mapa(filho, profundidade + 1, y_cursor)
            no["y"] = sum(f["y"] for f in no["filhos"]) / len(no["filhos"])

    @classmethod
    def _coletar_bounds_mapa(cls, no: dict, bounds: list):
        bounds[0] = min(bounds[0], no["x"])
        bounds[1] = max(bounds[1], no["x"] + cls._MAPA_LARGURA_NO)
        bounds[2] = min(bounds[2], no["y"] - cls._MAPA_ALTURA_NO / 2)
        bounds[3] = max(bounds[3], no["y"] + cls._MAPA_ALTURA_NO / 2)
        for filho in no["filhos"]:
            cls._coletar_bounds_mapa(filho, bounds)

    @staticmethod
    def _quebrar_texto_mapa(c, texto: str, largura_max: float, max_linhas: int, font_name: str, font_size: float) -> list:
        """Mesma lógica de quebra de linha usada no mapa interativo (JS), só que medindo com c.stringWidth do ReportLab."""
        palavras = texto.split()
        linhas = []
        linha_atual = []
        for palavra in palavras:
            tentativa = " ".join(linha_atual + [palavra])
            if c.stringWidth(tentativa, font_name, font_size) > largura_max and linha_atual:
                linhas.append(" ".join(linha_atual))
                linha_atual = [palavra]
                if len(linhas) >= max_linhas:
                    break
            else:
                linha_atual.append(palavra)
        if len(linhas) < max_linhas:
            linhas.append(" ".join(linha_atual))
        linhas = linhas[:max_linhas]
        palavras_usadas = sum(len(l.split()) for l in linhas)
        if palavras_usadas < len(palavras) and linhas:
            ultima = linhas[-1]
            while c.stringWidth(ultima + "…", font_name, font_size) > largura_max and len(ultima) > 1:
                ultima = ultima[:-1]
            linhas[-1] = ultima + "…"
        return linhas

    @classmethod
    def _desenhar_no_mapa(cls, c, no: dict, y_offset: float, profundidade: int):
        from reportlab.lib import colors
        largura_no, altura_no = cls._MAPA_LARGURA_NO, cls._MAPA_ALTURA_NO
        x = no["x"] + cls._MAPA_MARGEM
        y = y_offset - no["y"]
        is_raiz = profundidade == 0

        c.setFillColor(colors.HexColor('#F15A24') if is_raiz else colors.HexColor('#2D2D2D'))
        c.roundRect(x, y - altura_no / 2, largura_no, altura_no, 6, fill=1, stroke=0)

        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 9)
        linhas = cls._quebrar_texto_mapa(c, no["nome"], largura_no - (24 + 30), 2, "Helvetica-Bold", 9)
        line_height = 11
        y_texto_inicial = y + (line_height * (len(linhas) - 1)) / 2
        for i, linha in enumerate(linhas):
            c.drawString(x + 10, y_texto_inicial - i * line_height + 3, linha)

        if no["count"] is not None:
            c.setFillColor(colors.HexColor('#CFCFCF'))
            c.setFont("Helvetica", 8)
            c.drawRightString(x + largura_no - 8, y + altura_no / 2 - 14, f"({no['count']})")

        for filho in no["filhos"]:
            fx = filho["x"] + cls._MAPA_MARGEM
            fy = y_offset - filho["y"]
            mx = (x + largura_no + fx) / 2
            c.setStrokeColor(colors.HexColor('#BBBBBB'))
            c.setLineWidth(1.2)
            p = c.beginPath()
            p.moveTo(x + largura_no, y)
            p.curveTo(mx, y, mx, fy, fx, fy)
            c.drawPath(p, stroke=1, fill=0)
            cls._desenhar_no_mapa(c, filho, y_offset, profundidade + 1)

    @classmethod
    def _gerar_pdf_mapa_mental(cls, raiz_nome: str, hierarquia: dict) -> bytes:
        """
        Gera o mapa mental como PDF — sempre com TUDO expandido (mesmo
        espírito do download em .svg), numa única página do tamanho exato
        da árvore inteira (sem paginação — dividir um mapa mental entre
        páginas cortaria as conexões entre os ramos, ficaria ilegível).
        """
        import io
        from reportlab.lib import colors
        from reportlab.pdfgen import canvas as reportlab_canvas

        arvore = cls._montar_arvore_mapa(raiz_nome, hierarquia)
        y_cursor = [0]
        cls._calcular_layout_mapa(arvore, 0, y_cursor)

        bounds = [float("inf"), float("-inf"), float("inf"), float("-inf")]
        cls._coletar_bounds_mapa(arvore, bounds)
        min_x, max_x, min_y, max_y = bounds

        largura_pagina = (max_x - min_x) + cls._MAPA_MARGEM * 2
        altura_pagina = (max_y - min_y) + cls._MAPA_MARGEM * 2

        buffer = io.BytesIO()
        c = reportlab_canvas.Canvas(buffer, pagesize=(largura_pagina, altura_pagina))
        c.setFillColor(colors.HexColor('#FDFCF8'))
        c.rect(0, 0, largura_pagina, altura_pagina, fill=1, stroke=0)

        y_offset = altura_pagina - cls._MAPA_MARGEM + min_y - cls._MAPA_ALTURA_NO / 2
        cls._desenhar_no_mapa(c, arvore, y_offset, 0)

        c.showPage()
        c.save()
        return buffer.getvalue()

    @staticmethod
    def _d3_mind_map_html(raiz_nome: str, hierarquia: dict) -> str:
        """
        Mapa mental horizontal, expandível ramo a ramo — raiz -> Planos/
        Work Items -> Suítes (se houver mais de uma) -> Casos. Usa D3.js
        (via CDN) pra layout em árvore com conectores curvos e clique
        pra expandir/recolher, no estilo do NotebookLM.

        Cresce da esquerda pra direita; tudo começa recolhido (só a raiz
        aberta) e cada ramo expande independente. Título de cada nó quebra
        em até 2 linhas (e trunca com "…" só se ainda não couber) — some
        com tooltip nativo do navegador mostrando o texto completo, como
        reforço extra. Zoom com Ctrl+scroll (ou pinça de trackpad, que o
        navegador reporta como wheel+ctrlKey) e arrastar pra mover; botão
        de resetar o zoom/posição.

        Testado com screenshot real de navegador (Playwright): título
        longo quebrando em 2 linhas sem estourar a caixa, tooltip com
        texto completo presente, Ctrl+scroll aplicando zoom corretamente
        (e scroll comum SEM Ctrl não fazendo nada), e o botão de reset
        restaurando a visão original.
        """
        def montar_no(nome, suites=None, casos=None):
            if casos is not None:
                return {"name": nome}
            total_casos = sum(len(c) for c in suites.values())
            n_suites = len(suites)
            if n_suites <= 1:
                casos_unicos = next(iter(suites.values())) if suites else []
                return {
                    "name": nome, "count": total_casos,
                    "children": [montar_no(c, casos=True) for c in casos_unicos],
                }
            return {
                "name": nome, "count": total_casos,
                "children": [
                    {
                        "name": nome_suite, "count": len(casos_suite),
                        "children": [montar_no(c, casos=True) for c in casos_suite],
                    }
                    for nome_suite, casos_suite in suites.items()
                ],
            }

        dados = {
            "name": raiz_nome,
            "children": [montar_no(nome_plano, suites) for nome_plano, suites in hierarquia.items()],
        }
        dados_json = json.dumps(dados, ensure_ascii=False)

        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  body {{ margin: 0; font-family: sans-serif; background: #fdfcf8; overflow: hidden; }}
  .node rect {{ fill: #2D2D2D; stroke: none; rx: 6; ry: 6; }}
  .node.raiz rect {{ fill: #F15A24; }}
  .node text {{ fill: white; font-size: 12px; font-weight: 600; }}
  .node .contagem {{ fill: #cfcfcf; font-size: 11px; font-weight: 400; }}
  .link {{ fill: none; stroke: #bbb; stroke-width: 1.5px; }}
  .toggle {{ cursor: pointer; }}
  .toggle circle {{ fill: #555; }}
  .toggle text {{ fill: white; font-size: 11px; text-anchor: middle; dominant-baseline: middle; }}
  svg {{ cursor: grab; }}
  #btn-reset-zoom {{
    position: fixed; top: 8px; right: 8px; z-index: 10; padding: 6px 12px;
    border-radius: 6px; border: 1px solid #ccc; background: white; cursor: pointer; font-size: 12px;
  }}
  #btn-baixar {{
    position: fixed; top: 8px; right: 130px; z-index: 10; padding: 6px 12px;
    border-radius: 6px; border: 1px solid #ccc; background: white; cursor: pointer; font-size: 12px;
  }}
</style></head>
<body>
<button id="btn-baixar">⬇️ Baixar Mapa Mental</button>
<button id="btn-reset-zoom">🔍 Resetar zoom</button>
<div id="tree-container"></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"></script>
<script>
const dadosMapa = {dados_json};
const largura_no = 280, altura_no = 52, espaco_vertical = 60, espaco_horizontal = 340;

function quebrarTexto(textSelection, larguraMax, maxLinhas) {{
  textSelection.each(function () {{
    const text = d3.select(this);
    const textoOriginal = text.text();
    const palavras = textoOriginal.split(/\\s+/);
    text.text(null);

    const tspanTeste = text.append("tspan");
    let linhas = [];
    let linhaAtual = [];

    for (const palavra of palavras) {{
      const tentativa = [...linhaAtual, palavra].join(" ");
      tspanTeste.text(tentativa);
      if (tspanTeste.node().getComputedTextLength() > larguraMax && linhaAtual.length > 0) {{
        linhas.push(linhaAtual.join(" "));
        linhaAtual = [palavra];
        if (linhas.length >= maxLinhas) break;
      }} else {{
        linhaAtual = [...linhaAtual, palavra];
      }}
    }}
    if (linhas.length < maxLinhas) {{
      linhas.push(linhaAtual.join(" "));
    }}
    linhas = linhas.slice(0, maxLinhas);

    const palavrasUsadas = linhas.join(" ").split(/\\s+/).length;
    if (palavrasUsadas < palavras.length) {{
      let ultimaLinha = linhas[linhas.length - 1];
      tspanTeste.text(ultimaLinha + "…");
      while (tspanTeste.node().getComputedTextLength() > larguraMax && ultimaLinha.length > 1) {{
        ultimaLinha = ultimaLinha.slice(0, -1);
        tspanTeste.text(ultimaLinha + "…");
      }}
      linhas[linhas.length - 1] = ultimaLinha + "…";
    }}
    tspanTeste.remove();

    const lineHeight = 1.15;
    linhas.forEach((linha, i) => {{
      text.append("tspan")
        .attr("x", 12).attr("y", 0)
        .attr("dy", (i * lineHeight - (linhas.length - 1) * lineHeight / 2 + 0.35) + "em")
        .text(linha);
    }});
  }});
}}

function bezier(d) {{
  const sx = d.source.y + largura_no, sy = d.source.x;
  const tx = d.target.y, ty = d.target.x;
  const mx = (sx + tx) / 2;
  return `M${{sx}},${{sy}} C${{mx}},${{sy}} ${{mx}},${{ty}} ${{tx}},${{ty}}`;
}}

function construir(dados) {{
  const root = d3.hierarchy(dados);
  root.x0 = 0;
  root.y0 = 0;
  root.descendants().forEach((d, i) => {{
    d.id = i;
    d._children = d.children;
    if (d.depth > 0) d.children = null;
  }});

  const svg = d3.select("#tree-container").append("svg")
    .attr("width", "100vw").attr("height", "100vh");

  const gZoom = svg.append("g");    // recebe o transform de zoom/pan
  const gTree = gZoom.append("g");  // recebe o transform de posicionamento da árvore

  const zoom = d3.zoom()
    .filter((event) => {{
      if (event.type === "wheel") return event.ctrlKey;  // só zoom com Ctrl+scroll (pinça de trackpad chega como wheel+ctrlKey no navegador)
      return !event.button;  // arrastar (pan) sempre liberado
    }})
    .scaleExtent([0.3, 2.5])
    .on("zoom", (event) => {{ gZoom.attr("transform", event.transform); }});

  svg.call(zoom);
  document.getElementById("btn-reset-zoom").addEventListener("click", () => {{
    svg.transition().duration(300).call(zoom.transform, d3.zoomIdentity);
  }});

  const treeLayout = d3.tree().nodeSize([altura_no + espaco_vertical, espaco_horizontal]);

  const alturaViewport = document.querySelector("#tree-container").getBoundingClientRect().height || 600;

  function atualizar(source) {{
    const duracao = 300;
    treeLayout(root);
    const nos = root.descendants();
    const links = root.links();

    // Recentraliza verticalmente TODA VEZ (não só na primeira
    // renderização) — sem isso, expandir um ramo cujos filhos se
    // espalham bastante pra cima/baixo do pai deixava parte deles fora
    // da área visível, sem rolar automaticamente pra mostrar.
    let minX = Infinity, maxX = -Infinity;
    nos.forEach(d => {{ minX = Math.min(minX, d.x); maxX = Math.max(maxX, d.x); }});
    const centroAtual = (minX + maxX) / 2;
    gTree.transition().duration(duracao).attr("transform", `translate(40, ${{alturaViewport / 2 - centroAtual}})`);

    const node = gTree.selectAll("g.node").data(nos, d => d.id);
    const nodeEnter = node.enter().append("g")
      .attr("class", d => "node" + (d.depth === 0 ? " raiz" : ""))
      .attr("transform", d => `translate(${{source.y0 || 0}},${{source.x0 || 0}})`);

    nodeEnter.append("rect").attr("width", largura_no).attr("height", altura_no).attr("y", -altura_no / 2);
    nodeEnter.append("title").text(d => d.data.name);  // tooltip nativo com o texto completo

    const texto = nodeEnter.append("text").attr("x", 12).text(d => d.data.name);
    texto.call(quebrarTexto, largura_no - (24 + 34), 2);

    nodeEnter.filter(d => d.data.count !== undefined).append("text")
      .attr("class", "contagem").attr("x", largura_no - 34).attr("y", -8).attr("dy", "0.35em")
      .text(d => `(${{d.data.count}})`);

    const toggle = nodeEnter.filter(d => d._children).append("g")
      .attr("class", "toggle").attr("transform", `translate(${{largura_no + 10}}, 0)`)
      .on("click", (event, d) => {{ d.children = d.children ? null : d._children; atualizar(d); }});
    toggle.append("circle").attr("r", 10);
    toggle.append("text").text(d => d.children ? "−" : "+");

    const nodeUpdate = nodeEnter.merge(node);
    nodeUpdate.transition().duration(duracao).attr("transform", d => `translate(${{d.y}},${{d.x}})`);
    nodeUpdate.select(".toggle text").text(d => d.children ? "−" : "+");

    node.exit().transition().duration(duracao)
      .attr("transform", d => `translate(${{source.y}},${{source.x}})`).remove();

    const link = gTree.selectAll("path.link").data(links, d => d.target.id);
    const linkEnter = link.enter().insert("path", "g").attr("class", "link")
      .attr("d", d => bezier({{source: {{x: source.x0 || 0, y: source.y0 || 0}}, target: {{x: source.x0 || 0, y: source.y0 || 0}}}}));
    linkEnter.merge(link).transition().duration(duracao).attr("d", bezier);
    link.exit().transition().duration(duracao)
      .attr("d", d => bezier({{source: {{x: source.x, y: source.y}}, target: {{x: source.x, y: source.y}}}})).remove();

    nos.forEach(d => {{ d.x0 = d.x; d.y0 = d.y; }});
  }}

  atualizar(root);
}}

construir(dadosMapa);

// -------------------------------------------------------------------- //
// Download — reconstrói do ZERO a partir de dadosMapa (não do estado
// atual da tela), com TUDO expandido, sempre. Usa um SVG temporário,
// fora da árvore interativa, pra nunca disturbar o que o usuário está
// vendo na tela no momento do clique.
// -------------------------------------------------------------------- //
function baixarMapaCompleto() {{
  const rootExport = d3.hierarchy(dadosMapa); // TUDO expandido por padrão, sem a lógica de recolher
  const treeLayoutExport = d3.tree().nodeSize([altura_no + espaco_vertical, espaco_horizontal]);
  treeLayoutExport(rootExport);

  const nos = rootExport.descendants();
  const links = rootExport.links();

  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  nos.forEach(d => {{
    minX = Math.min(minX, d.x); maxX = Math.max(maxX, d.x);
    minY = Math.min(minY, d.y); maxY = Math.max(maxY, d.y + largura_no + 40);
  }});
  const margem = 30;
  const largura_total = (maxY - minY) + margem * 2;
  const altura_total = (maxX - minX) + margem * 2;

  const svgTemp = d3.select("body").append("svg")
    .attr("width", largura_total).attr("height", altura_total)
    .attr("xmlns", "http://www.w3.org/2000/svg")
    .style("position", "fixed").style("left", "-99999px").style("top", "0");

  svgTemp.append("style").text(`
    .node rect {{ fill: #2D2D2D; }}
    .node.raiz rect {{ fill: #F15A24; }}
    .node text {{ fill: white; font-size: 12px; font-weight: 600; font-family: sans-serif; }}
    .node .contagem {{ fill: #cfcfcf; font-size: 11px; font-weight: 400; }}
    .link {{ fill: none; stroke: #bbb; stroke-width: 1.5px; }}
  `);
  svgTemp.append("rect").attr("width", largura_total).attr("height", altura_total).attr("fill", "#fdfcf8");

  const g = svgTemp.append("g").attr("transform", `translate(${{margem - minY}}, ${{margem - minX}})`);

  g.selectAll("path.link").data(links).enter().insert("path", "g").attr("class", "link").attr("d", bezier);

  const nodeG = g.selectAll("g.node").data(nos).enter().append("g")
    .attr("class", d => "node" + (d.depth === 0 ? " raiz" : ""))
    .attr("transform", d => `translate(${{d.y}},${{d.x}})`);

  nodeG.append("rect").attr("width", largura_no).attr("height", altura_no).attr("y", -altura_no / 2).attr("rx", 6).attr("ry", 6);
  const textoExport = nodeG.append("text").attr("x", 12).text(d => d.data.name);
  textoExport.call(quebrarTexto, largura_no - (24 + 34), 2);
  nodeG.filter(d => d.data.count !== undefined).append("text")
    .attr("class", "contagem").attr("x", largura_no - 34).attr("y", -8).attr("dy", "0.35em")
    .text(d => `(${{d.data.count}})`);

  // Remove o estilo de posicionamento temporário antes de serializar —
  // senão ele vaza pro arquivo baixado e quebra a exibição em qualquer
  // lugar que abrir esse .svg depois.
  svgTemp.attr("style", null);
  const svgNode = svgTemp.node();
  const serializer = new XMLSerializer();
  let svgString = serializer.serializeToString(svgNode);
  svgString = '<?xml version="1.0" standalone="no"?>\\r\\n' + svgString;

  const blob = new Blob([svgString], {{ type: "image/svg+xml;charset=utf-8" }});
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "mapa_mental_completo.svg";
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);

  svgTemp.remove();
}}

document.getElementById("btn-baixar").addEventListener("click", baixarMapaCompleto);
</script>
</body></html>"""

    def _document_store_page(self):
        st.subheader("🗄️ Documentos Armazenados")
        if st.button("← Voltar", key="btn_document_store_back"):
            self.state.set('show_document_store_page', False)
            st.rerun()

        if not self._get_permission_cached("documentos_armazenados"):
            st.error("❌ Você não tem permissão pra acessar esta área.")
            return

        st.caption(
            "Documentos (CSV/PDF) que você escolheu guardar ao longo do tempo, organizados por "
            "fluxo de origem — mais recentes primeiro. Guardados num banco separado (Turso), "
            "fora do app em si."
        )
        self._render_area_help("documentos")

        store = DocumentStore(self.config.turso_database_url, self.config.turso_auth_token)
        try:
            with st.spinner("Carregando documentos armazenados..."):
                store.ensure_schema()
                grupos = store.listar_grupos()
        except DocumentStoreError as error:
            st.error(f"❌ {error}")
            return
        except Exception as error:
            st.error(f"❌ Não foi possível carregar os documentos: {error}")
            return

        if not grupos:
            st.info(
                "Nenhum documento armazenado ainda. Use o botão '💾 Armazenar esta "
                "documentação' que aparece ao final de cada fluxo (Passo 6, Relatório de "
                "Testes, Manual de Testes)."
            )
            return

        fluxos_disponiveis = sorted({g['fluxo_origem'] for g in grupos})
        filtro = st.multiselect(
            "Filtrar por fluxo de origem", options=fluxos_disponiveis,
            key="document_store_filter",
        )
        grupos_filtrados = [g for g in grupos if not filtro or g['fluxo_origem'] in filtro]
        st.caption(f"{len(grupos_filtrados)} grupo(s) de documento(s).")

        for grupo in grupos_filtrados:
            criado_em_fmt = grupo['criado_em']
            try:
                dt = datetime.fromisoformat(criado_em_fmt.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                criado_em_fmt = dt.astimezone(TZ_BR).strftime("%d/%m/%Y %H:%M")
            except Exception:
                pass
            total_kb = sum(a['tamanho_bytes'] for a in grupo['arquivos']) / 1024
            titulo_grupo = f"📁 {grupo['nome_projeto'] or '(sem nome)'} — {grupo['fluxo_origem']} — {criado_em_fmt} ({total_kb:.0f} KB)"
            with st.expander(titulo_grupo, key=f"docstore_grupo_{grupo['grupo_id']}", on_change="rerun"):
                st.caption(f"Armazenado por: {grupo['criado_por'] or 'desconhecido'}")
                for arq in grupo['arquivos']:
                    col_info, col_btn = st.columns([3, 1])
                    with col_info:
                        icone = {"csv": "📄", "md": "📝", "zip": "🗜️"}.get(arq['tipo'], "📑")
                        st.write(f"{icone} {arq['nome_arquivo']} ({arq['tamanho_bytes'] / 1024:.0f} KB)")
                    with col_btn:
                        conteudo_pronto = self.state.get(f"document_store_content_{arq['id']}")
                        if conteudo_pronto:
                            mime = {"csv": "text/csv", "md": "text/markdown", "zip": "application/zip"}.get(arq['tipo'], "application/pdf")
                            st.download_button(
                                "💾 Salvar", data=conteudo_pronto, file_name=arq['nome_arquivo'],
                                mime=mime, key=f"dlbtn_{arq['id']}", width="stretch",
                            )
                        else:
                            if st.button("⬇️ Buscar", key=f"btn_prep_{arq['id']}", width="stretch"):
                                try:
                                    with st.spinner("Buscando arquivo..."):
                                        conteudo = store.buscar_conteudo(arq['id'])
                                    self.state.set(f"document_store_content_{arq['id']}", conteudo)
                                    st.rerun()
                                except Exception as error:
                                    st.error(f"❌ {error}")
                st.divider()
                current_username_doc_store = st.session_state.get(SESSION_USER_KEY, "")
                if current_username_doc_store == self.config.owner_username:
                    delete_flag_key = f"confirm_delete_group_{grupo['grupo_id']}"
                    if not self.state.get(delete_flag_key):
                        if st.button("🗑️ Excluir este grupo", key=f"btn_delete_group_{grupo['grupo_id']}"):
                            self.state.set(delete_flag_key, True)
                            st.rerun()
                    else:
                        st.warning("Tem certeza? Isso apaga os arquivos deste grupo permanentemente do banco.")
                        c1, c2 = st.columns(2)
                        with c1:
                            if st.button("✅ Sim, excluir", key=f"btn_confirm_delete_{grupo['grupo_id']}", type="primary", width="stretch"):
                                try:
                                    store.excluir_grupo(grupo['grupo_id'])
                                    st.success("Excluído.")
                                    self._log(
                                        "Excluir Documentação Armazenada", grupo['fluxo_origem'],
                                        grupo['nome_projeto'] or '',
                                    )
                                except Exception as error:
                                    st.error(f"❌ {error}")
                                self.state.set(delete_flag_key, False)
                                st.rerun()
                        with c2:
                            if st.button("✖ Cancelar", key=f"btn_cancel_delete_{grupo['grupo_id']}", width="stretch"):
                                self.state.set(delete_flag_key, False)
                                st.rerun()

    def _manual_limpar(self):
        """
        Zera tudo do manual anterior — estado do app E as chaves de widget do
        Streamlit. Sem limpar as chaves de widget, os campos de título,
        introdução e de cada passo continuam com o texto antigo mesmo depois
        de gerar outro manual (elas só são inicializadas quando NÃO existem).
        O uploader não aceita ser esvaziado por código: ganha uma key nova
        (`manual_form_versao`) pra nascer vazio.
        """
        for chave in ('manual_generated', 'manual_passo_images', 'manual_pdf_bytes',
                      'manual_collected_images', 'manual_uploaded_files', 'manual_nome',
                      'manual_board_items'):
            self.state.set(chave, None)
        prefixos = ('manual_passo_',)
        exatas = {'manual_titulo_input', 'manual_introducao_input', 'manual_nome_input',
                  'manual_wi_select', 'manual_query_select', 'manual_area_paths_select'}
        for chave in [k for k in list(st.session_state.keys())
                      if k in exatas or any(k.startswith(p) for p in prefixos)]:
            st.session_state.pop(chave, None)
        self.state.set('manual_form_versao', (self.state.get('manual_form_versao') or 0) + 1)

    def _manual_generation_page(self):
        st.subheader("📘 Manual de Testes (UAT)")
        col_voltar, col_novo = st.columns([1, 1])
        with col_voltar:
            if st.button("← Voltar", key="btn_manual_back", width="stretch"):
                self.state.set('show_manual_page', False)
                st.rerun()
        with col_novo:
            tem_algo = bool(self.state.get('manual_generated') or self.state.get('manual_uploaded_files')
                            or self.state.get('manual_collected_images'))
            if st.button("🆕 Novo manual", key="btn_manual_novo", width="stretch",
                         disabled=(not tem_algo) or self.state.get('is_processing'),
                         help="Limpa o manual atual (textos, imagens e PDF) pra começar outro do zero."):
                self._manual_limpar()
                self._flash_success("Tela limpa — pode montar um manual novo.")
                st.rerun()

        if not self._get_permission_cached("manual_testes"):
            st.error("❌ Você não tem permissão pra acessar esta área.")
            return

        st.caption(
            "Gera um manual de reprodução passo a passo, escrito em linguagem simples pra quem "
            "não é de TI (times de Produto/Marketing em UAT) — evita relatos de \"bug\" que na "
            "real são passos executados fora de ordem ou mal interpretados."
        )
        self._render_area_help("manual")
        st.info(
            "📷 O app **não tira print de tela ao vivo** — ele só reaproveita imagens que **você "
            "já tiver**, anexadas em documentos ou já existentes nos Work Items do Azure DevOps."
        )

        origem = st.radio(
            "Origem do conteúdo",
            options=["📄 Documentos", "🎯 Work Items do Azure DevOps", "🔀 Mesclado (Documentos + Work Items)"],
            index=0,
            key="manual_origem_radio",
            disabled=self.state.get('is_processing'),
            horizontal=True,
        )
        usa_documentos = origem.startswith("📄") or origem.startswith("🔀")
        usa_work_items = origem.startswith("🎯") or origem.startswith("🔀")

        texto_documentos = ""
        texto_work_items = ""
        selected_wis = []
        # Reseta a coleta de imagens a cada renderização — remonta a partir
        # das fontes ativas agora (documentos/Work Items podem ter mudado).
        imagens_coletadas = []

        st.divider()
        if usa_documentos:
            st.markdown("##### 📄 Documentos")
            if not DOCUMENT_UPLOAD_ENABLED:
                st.warning(DOCUMENT_UPLOAD_DISABLED_MSG)
            uploaded_manual = st.file_uploader(
                "Documento(s) (PDF, DOCX, TXT ou CSV — pode anexar mais de um, de formatos diferentes)",
                type=["pdf", "docx", "txt", "csv"],
                accept_multiple_files=True,
                key=f"manual_uploaded_files_input_{self.state.get('manual_form_versao') or 0}",
                disabled=(not DOCUMENT_UPLOAD_ENABLED) or self.state.get('is_processing'),
            )
            if uploaded_manual:
                self.state.set('manual_uploaded_files', uploaded_manual)
            uploaded_manual = self.state.get('manual_uploaded_files') or []

            if uploaded_manual:
                with st.spinner("Extraindo texto e imagens dos documentos..."):
                    texto_documentos = DocumentProcessor.extract_plain_text_multi(uploaded_manual)
                    img_result = DocumentProcessor.extract_images_with_context(uploaded_manual, max_images=0)
                for warn in img_result.get("warnings", []):
                    st.caption(f"ℹ️ {warn}")
                for idx, img in enumerate(img_result.get("images", [])):
                    fname = f"{img['source_file']}_{img['location']}_{idx}.jpg".replace(" ", "_")
                    imagens_coletadas.append({"filename": fname, "bytes": img["bytes"], "origem": img["source_file"], "context": img.get("context", "")})
                if imagens_coletadas:
                    st.caption(f"✅ {len(imagens_coletadas)} imagem(ns) encontrada(s) nos documentos.")

        if usa_work_items:
            st.markdown("##### 🎯 Work Items do Azure DevOps")
            conn = self._setup_azure_devops_connection(show_area_path_picker=False)
            if conn is None:
                return
            ado_client, ado_org, ado_project, _default_area_path = conn

            forma_busca = st.radio(
                "Como buscar os Work Items?",
                options=["📁 Board (Area Path)", "🔎 Query salva no Azure DevOps"],
                index=0,
                key="manual_wi_forma_busca",
                horizontal=True,
                disabled=self.state.get('is_processing'),
            )

            if forma_busca.startswith("🔎"):
                with st.container(key="azure_blue_btn_fetch_manual_queries"):
                    st.button(
                        "🔄 Buscar Queries Salvas",
                        disabled=self.state.get('is_processing'),
                        key="btn_fetch_manual_queries",
                        on_click=self.trigger_action,
                        args=("fetch_manual_queries",),
                        width="stretch",
                    )
                if self.state.get('current_action') == 'fetch_manual_queries' and not self.state.get('show_interrupt_modal'):
                    try:
                        with st.spinner("Buscando suas queries salvas no Azure DevOps..."):
                            queries_manual = ado_client.list_saved_queries()
                        self.state.set('manual_query_available', queries_manual)
                        if not queries_manual:
                            self._flash_warning("Nenhuma query salva encontrada nesse projeto.")
                    except Exception as error:
                        self._flash_error(f"Erro ao buscar queries: {error}")
                        self.state.set('manual_query_available', [])
                    self.clear_action()
                    st.rerun()

                queries_manual = self.state.get('manual_query_available') or []
                if not queries_manual:
                    st.caption("Busque as queries salvas acima pra continuar.")
                    return

                query_labels_manual = {q["path"]: q for q in queries_manual}
                escolha_query_manual = st.selectbox(
                    "📋 Query salva", options=list(query_labels_manual.keys()),
                    key="manual_query_select", index=None, placeholder="Escolha uma query...",
                )
                with st.container(key="azure_blue_btn_run_manual_query"):
                    st.button(
                        "▶️ Rodar Query",
                        disabled=self.state.get('is_processing') or not escolha_query_manual,
                        key="btn_run_manual_query",
                        on_click=self.trigger_action,
                        args=("run_manual_query",),
                        width="stretch",
                    )
                if self.state.get('current_action') == 'run_manual_query' and not self.state.get('show_interrupt_modal'):
                    try:
                        query_obj = query_labels_manual[escolha_query_manual]
                        with st.spinner(f"Rodando a query '{query_obj['name']}'..."):
                            resultado = ado_client.run_wiql_query(query_obj['wiql'])
                            ids_to_show = [item['id'] for item in resultado['items']]
                            details = ado_client.get_work_items_basic_fields(ids_to_show) if ids_to_show else []
                        self.state.set('manual_board_items', details)
                        if 'manual_wi_select' in st.session_state:
                            del st.session_state['manual_wi_select']
                        if not details:
                            self._flash_warning(f"A query '{query_obj['name']}' não retornou nenhum Work Item.")
                    except Exception as error:
                        self._flash_error(f"Erro ao rodar a query: {error}")
                        self.state.set('manual_board_items', [])
                    self.clear_action()
                    st.rerun()
                area_paths_manual = []  # query não usa Area Path — filtro de Coluna/Tag não se aplica aqui
            else:
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
                    area_paths_manual = st.multiselect(
                        "Area Path(s)",
                        options=area_path_options,
                        disabled=self.state.get('is_processing'),
                        key="manual_area_paths_select",
                    )
                with col_btn:
                    with st.container(key="azure_blue_btn_fetch_wi_manual"):
                        st.button(
                            "🔄 Buscar Work Items do Board",
                            disabled=self.state.get('is_processing'),
                            key="btn_fetch_wi_manual",
                            on_click=self.trigger_action,
                            args=("fetch_wi_manual",),
                            width="stretch",
                        )
                if self.state.get('current_action') == 'fetch_wi_manual' and not self.state.get('show_interrupt_modal'):
                    try:
                        paths_to_search = area_paths_manual or [ado_project]
                        with st.spinner(f"Buscando Work Items em {len(paths_to_search)} Area Path(s)..."):
                            items_by_id = {}
                            for ap in paths_to_search:
                                for item in ado_client.fetch_work_items_by_area_path(ap, excluded_states=set()):
                                    items_by_id[item["id"]] = item
                        self.state.set('manual_board_items', list(items_by_id.values()))
                    except Exception as error:
                        self._flash_error(f"Não foi possível buscar Work Items: {error}")
                    self.clear_action()
                    st.rerun()

            board_items = self.state.get('manual_board_items') or []
            if board_items:
                board_items = self._filtrar_por_coluna_e_tag(board_items, bool(area_paths_manual), "manual")
                wi_labels = {f"{i['id']} - {i['title']} ({i['type']}, {i['state']})": i for i in board_items}
                selected_labels = st.multiselect(
                    "Work Items a incluir no manual",
                    options=list(wi_labels.keys()),
                    key="manual_wi_select",
                    disabled=self.state.get('is_processing'),
                )
                selected_wis = [wi_labels[l] for l in selected_labels]
                if selected_wis:
                    with st.spinner(f"Buscando detalhes e anexos de {len(selected_wis)} Work Item(s)..."):
                        details = ado_client.get_work_items_full_details([wi['id'] for wi in selected_wis])
                        text_parts = []
                        for wi in details:
                            part = f"===== WORK ITEM {wi['id']} - {wi['title']} ({wi['type']}) =====\n"
                            if wi.get('description'):
                                part += f"Descrição:\n{wi['description']}\n"
                            if wi.get('acceptance_criteria'):
                                part += f"\nCritérios de Aceite:\n{wi['acceptance_criteria']}\n"
                            part += f"===== FIM DO WORK ITEM {wi['id']} ====="
                            text_parts.append(part)
                        texto_work_items = "\n\n".join(text_parts)

                        # As imagens quase nunca estão no Work Item escolhido: elas
                        # ficam DENTRO dos Casos de Teste vinculados (anexo de passo
                        # ou <img> colada no texto). Buscar só anexo do próprio Work
                        # Item devolvia zero imagem num card que tem dezenas delas.
                        # Os passos desses Casos também entram no conteúdo de origem:
                        # é o que faz o manual descrever o fluxo REAL em vez de um
                        # roteiro genérico.
                        partes_casos, falhas_imagens = [], []
                        for wi in selected_wis:
                            try:
                                imgs, casos_vinculados = ado_client.get_work_item_images(wi['id'])
                                imagens_coletadas.extend(imgs)
                                for caso in casos_vinculados:
                                    linhas = [f"===== CASO DE TESTE {caso['id']} - {caso['titulo']} (vinculado ao Work Item {wi['id']}) ====="]
                                    if caso.get('pre_condicoes'):
                                        linhas.append(f"Pré-condições: {caso['pre_condicoes']}")
                                    for passo in caso.get('passos') or []:
                                        linhas.append(f"Passo {passo['numero']}: {passo['acao']}\n  Resultado esperado: {passo['resultado_esperado']}")
                                    linhas.append(f"===== FIM DO CASO DE TESTE {caso['id']} =====")
                                    partes_casos.append("\n".join(linhas))
                            except Exception as error:
                                falhas_imagens.append(f"Work Item {wi['id']}: {error}")
                        if partes_casos:
                            texto_work_items += "\n\n" + "\n\n".join(partes_casos)
                    if imagens_coletadas:
                        de_casos = sum(1 for i in imagens_coletadas if str(i.get('origem', '')).lower().startswith(('test case', 'caso de teste')))
                        detalhe = f" — {de_casos} vinda(s) dos Casos de Teste vinculados" if de_casos else ""
                        st.caption(f"✅ {len(imagens_coletadas)} imagem(ns) encontrada(s) no total (documentos + Work Items){detalhe}.")
                    else:
                        st.caption("ℹ️ Nenhuma imagem encontrada nos Work Items escolhidos nem nos Casos de Teste vinculados a eles.")
                    if falhas_imagens:
                        st.warning("Não consegui ler as imagens de: " + "; ".join(falhas_imagens))

        self.state.set('manual_collected_images', imagens_coletadas)

        conteudo_origem = (texto_documentos + "\n\n" + texto_work_items).strip()
        if not conteudo_origem:
            st.caption("Adicione documento(s) e/ou selecione Work Items pra continuar.")
            return

        st.divider()
        if usa_work_items:
            nome_manual = self._render_project_name_field(ado_project, "manual_nome", "Nome do Manual *")
        else:
            nome_manual = self._render_project_name_field("", "manual_nome", "Nome do Manual *")

        with st.container(key="azure_blue_btn_generate_manual"):
            st.button(
                "🤖 Gerar Manual com IA",
                type="primary",
                width="stretch",
                disabled=self.state.get('is_processing') or not nome_manual.strip(),
                key="btn_generate_manual",
                on_click=self._iniciar_geracao_em_lotes,
                args=("generate_manual", "manual_ia"),
            )
        if self.state.get('current_action') == 'generate_manual' and not self.state.get('show_interrupt_modal'):
            with st.status("Escrevendo o manual e sugerindo as imagens de cada passo (isso pode levar um minuto)...", expanded=True) as status:
                resultado = self._chamar_ia_com_retentativas(
                    "manual_ia",
                    lambda: {"conteudo": conteudo_origem, "nome": nome_manual.strip(),
                             "imagens": [{"filename": img["filename"], "context": img.get("context", "")} for img in imagens_coletadas]},
                    lambda p: self.client.trigger_manual_generation(p["conteudo"], p["nome"], p["imagens"]), status,
                )
                if resultado is None:
                    return  # rerun() já disparado (espera pós-erro)
                _payload, resp, erro = resultado
                status.update(label="Falha ao gerar o manual." if erro else "Manual gerado.",
                              state="error" if erro else "complete")
            try:
                if erro:
                    raise RuntimeError(erro)
                self.state.set('manual_generated', resp)
                # A IA já sugere quais imagens combinam com cada passo (via
                # "imagens_sugeridas") — pré-popula a partir disso, mas só
                # com nomes que realmente existem no pool coletado (nunca
                # confia cegamente no que a IA "lembrou" do nome do arquivo).
                nomes_validos = {img["filename"] for img in imagens_coletadas}
                passo_images_sugeridas = {}
                for passo in resp.get('passos', []):
                    sugeridas = [f for f in (passo.get('imagens_sugeridas') or []) if f in nomes_validos]
                    if sugeridas:
                        passo_images_sugeridas[str(passo.get('numero'))] = sugeridas
                self.state.set('manual_passo_images', passo_images_sugeridas)
                self.state.set('manual_pdf_bytes', None)
                total_sugeridas = sum(len(v) for v in passo_images_sugeridas.values())
                if total_sugeridas:
                    st.toast(f"✅ A IA já sugeriu {total_sugeridas} imagem(ns) distribuída(s) pelos passos — revise abaixo.")
            except Exception as error:
                self._flash_error(f"Não foi possível gerar o manual: {error}")
            self.clear_action()
            st.rerun()

        self._render_manual_review()

    def _render_manual_review(self):
        generated = self.state.get('manual_generated')
        if not generated:
            return

        st.divider()
        st.markdown("#### ✏️ Revisar o Manual")
        st.caption("Edite os textos livremente e escolha quais imagens ilustram cada passo antes de gerar o PDF.")

        if "manual_titulo_input" not in st.session_state:
            st.session_state["manual_titulo_input"] = generated.get('titulo_manual', '')
        titulo_manual = st.text_input("Título do Manual", key="manual_titulo_input")

        if "manual_introducao_input" not in st.session_state:
            st.session_state["manual_introducao_input"] = generated.get('introducao', '')
        introducao = st.text_area("Introdução", key="manual_introducao_input", height=80)

        imagens = self.state.get('manual_collected_images') or []
        img_by_filename = {img["filename"]: img for img in imagens}
        passo_images = dict(self.state.get('manual_passo_images') or {})

        passos_editados = []
        for passo in generated.get('passos', []):
            numero = passo.get('numero')
            with st.expander(f"Passo {numero} — {passo.get('titulo', '')}", expanded=True):
                key_prefix = f"manual_passo_{numero}"
                if f"{key_prefix}_titulo" not in st.session_state:
                    st.session_state[f"{key_prefix}_titulo"] = passo.get('titulo', '')
                p_titulo = st.text_input("Título do passo", key=f"{key_prefix}_titulo")

                if f"{key_prefix}_descricao" not in st.session_state:
                    st.session_state[f"{key_prefix}_descricao"] = passo.get('descricao', '')
                p_descricao = st.text_area("Descrição", key=f"{key_prefix}_descricao", height=100)

                if f"{key_prefix}_aviso" not in st.session_state:
                    st.session_state[f"{key_prefix}_aviso"] = passo.get('aviso', '') or ''
                p_aviso = st.text_input("Aviso (opcional)", key=f"{key_prefix}_aviso")

                if imagens:
                    default_imgs = passo_images.get(str(numero), [])
                    chosen = st.multiselect(
                        "Imagens deste passo",
                        options=list(img_by_filename.keys()),
                        default=[f for f in default_imgs if f in img_by_filename],
                        key=f"{key_prefix}_imagens",
                        disabled=self.state.get('is_processing'),
                    )
                    passo_images[str(numero)] = chosen
                    for fname in chosen:
                        st.image(img_by_filename[fname]["bytes"], caption=fname, width=200)
                else:
                    st.caption("Nenhuma imagem disponível pra atribuir (nenhuma foi encontrada nos documentos/Work Items).")

                passos_editados.append({
                    "numero": numero, "titulo": p_titulo, "descricao": p_descricao, "aviso": p_aviso,
                })

        self.state.set('manual_passo_images', passo_images)

        st.divider()
        with st.container(key="azure_blue_btn_build_manual_pdf"):
            st.button(
                "📄 Gerar PDF do Manual",
                type="primary",
                width="stretch",
                disabled=self.state.get('is_processing'),
                key="btn_build_manual_pdf",
                on_click=self.trigger_action,
                args=("build_manual_pdf",),
            )

        if self.state.get('current_action') == 'build_manual_pdf' and not self.state.get('show_interrupt_modal'):
            try:
                with st.spinner("Montando o PDF do manual..."):
                    pdf_bytes = ManualPdfGenerator.generate(
                        titulo=titulo_manual,
                        introducao=introducao,
                        passos=passos_editados,
                        passo_images=passo_images,
                        img_by_filename={k: v["bytes"] for k, v in img_by_filename.items()},
                        author_name=self.state.get('author_name', ''),
                    )
                self.state.set('manual_pdf_bytes', pdf_bytes)
                self._log("Gerar Manual de Testes (UAT)", "Manual de Testes", f"'{titulo_manual}' — {len(passos_editados)} passo(s)")
            except Exception as error:
                self._flash_error(f"Não foi possível gerar o PDF: {error}")
            self.clear_action()
            st.rerun()

        pdf_bytes = self.state.get('manual_pdf_bytes')
        if pdf_bytes:
            safe_name = (titulo_manual or 'manual').replace(' ', '_')
            st.download_button(
                "⬇️ Baixar Manual (PDF)",
                data=pdf_bytes,
                file_name=f"Manual_{safe_name}.pdf",
                mime="application/pdf",
                width="stretch",
                type="primary",
                key="download_manual_pdf",
            )

            self._render_document_storage_section(
                "Manual de Testes (UAT)", titulo_manual or (generated.get("titulo_manual") or "Manual"),
                [{"tipo": "pdf", "nome_arquivo": f"Manual_{safe_name}.pdf", "conteudo": pdf_bytes}],
            )

    def _wiql_generation_page(self):
        st.subheader("🔎 Criar Query no Azure DevOps com IA")
        st.caption(
            "Descreve em português o que você quer consultar — a IA traduz pra WIQL (a "
            "linguagem de query do Azure DevOps). Antes de criar qualquer coisa de verdade, "
            "você vê um preview de quantos itens a query traria, pra confirmar que é isso mesmo."
        )
        self._render_area_help("wiql")

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
                width="stretch",
                disabled=self.state.get('is_processing') or not descricao.strip(),
                key="btn_generate_wiql",
                on_click=self._iniciar_geracao_em_lotes,
                args=("generate_wiql", "wiql_ia"),
            )

        if self.state.get('current_action') == 'generate_wiql' and not self.state.get('show_interrupt_modal'):
            with st.status("Traduzindo sua descrição em uma query WIQL...", expanded=True) as status:
                resultado = self._chamar_ia_com_retentativas(
                    "wiql_ia",
                    lambda: {"descricao": descricao.strip(), "projeto": self.state.get('project_name') or ado_project},
                    lambda p: self.client.trigger_wiql_generation(p["descricao"], p["projeto"]), status,
                )
                if resultado is None:
                    return  # rerun() já disparado (espera pós-erro)
                _payload, resp, erro = resultado
                status.update(label="Falha ao gerar a query." if erro else "Query gerada.",
                              state="error" if erro else "complete")
            try:
                if erro:
                    raise RuntimeError(erro)
                self.state.set('wiql_generated', resp)
                self.state.set('wiql_preview_result', None)
                st.session_state['wiql_titulo_input'] = resp.get('titulo_sugerido', '')
                st.session_state['wiql_text_input'] = resp.get('wiql', '')
            except Exception as error:
                self._flash_error(f"Não foi possível gerar a query: {error}")
            self.clear_action()
            st.rerun()

        generated = self.state.get('wiql_generated')
        if not generated:
            return

        st.divider()
        st.markdown("##### 📝 Revise antes de criar")
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
                width="stretch",
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
                self._flash_error(f"Erro na query: {error}")
                self.state.set('wiql_preview_result', None)
            except Exception as error:
                self._flash_error(f"Erro inesperado: {error}")
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
                st.dataframe(rows, width="stretch", hide_index=True)
                if preview['count'] > len(details):
                    st.caption(f"Mostrando os primeiros {len(details)} de {preview['count']} itens.")

            with st.container(key="azure_blue_btn_confirm_wiql"):
                st.button(
                    "✅ Confirmar e Criar Query no Azure DevOps",
                    type="primary",
                    width="stretch",
                    disabled=self.state.get('is_processing') or not titulo.strip(),
                    key="btn_confirm_wiql",
                    on_click=self.trigger_action,
                    args=("confirm_wiql",),
                )

            st.caption("Ou pule a etapa de salvar a query e use o resultado dela direto:")
            col_atalho1, col_atalho2 = st.columns(2)
            with col_atalho1:
                if self._get_permission_cached("azure_query"):
                    st.button(
                        "🎯 Usar pra Gerar Testes",
                        width="stretch",
                        disabled=self.state.get('is_processing'),
                        key="btn_wiql_para_testes",
                        on_click=self.trigger_action,
                        args=("wiql_atalho_testes",),
                        help="Roda essa query de novo (sem limite de 50) e já leva pro Passo 1, no modo Query, com os Work Items prontos pra escolher.",
                    )
                else:
                    st.caption("🎯 Gerar Testes: precisa da permissão 'azure_query'.")
            with col_atalho2:
                if self._get_permission_cached("manual_testes"):
                    st.button(
                        "📘 Usar pra Criar Manual",
                        width="stretch",
                        disabled=self.state.get('is_processing'),
                        key="btn_wiql_para_manual",
                        on_click=self.trigger_action,
                        args=("wiql_atalho_manual",),
                        help="Roda essa query de novo (sem limite de 50) e já leva pro Manual de Testes, com os Work Items prontos pra escolher.",
                    )
                else:
                    st.caption("📘 Criar Manual: precisa da permissão 'manual_testes'.")

            if self.state.get('current_action') in ('wiql_atalho_testes', 'wiql_atalho_manual') and not self.state.get('show_interrupt_modal'):
                destino = self.state.get('current_action')
                try:
                    with st.spinner("Rodando a query e buscando todos os Work Items..."):
                        resultado_completo = ado_client.run_wiql_query(wiql_text.strip())
                        ids_completos = [item['id'] for item in resultado_completo['items']]
                        detalhes_completos = ado_client.get_work_items_basic_fields(ids_completos) if ids_completos else []
                    if destino == 'wiql_atalho_testes':
                        self.state.set('query_wigen_board_items', detalhes_completos)
                        self.state.set('query_wigen_selected_ids', [])
                        self.state.set('query_wigen_available_queries', self.state.get('query_wigen_available_queries') or [])
                        if 'query_wigen_multiselect' in st.session_state:
                            del st.session_state['query_wigen_multiselect']
                        st.session_state['step1_origem_radio'] = "🔎 Gerar a partir de uma Query do Azure DevOps"
                        self.state.set('show_wiql_generation_page', False)
                        self._set_step(1)
                    else:
                        self.state.set('manual_board_items', detalhes_completos)
                        if 'manual_wi_select' in st.session_state:
                            del st.session_state['manual_wi_select']
                        st.session_state['manual_origem_radio'] = "🎯 Work Items do Azure DevOps"
                        self.state.set('show_wiql_generation_page', False)
                        self.state.set('show_manual_page', True)
                    if not detalhes_completos:
                        self._flash_warning("Essa query não retornou nenhum Work Item.")
                    self.clear_action()
                    st.rerun()
                except AzureDevOpsError as error:
                    self._flash_error(f"Erro ao rodar a query: {error}")
                    self.clear_action()
                    st.rerun()
                except Exception as error:
                    self._flash_error(f"Erro inesperado: {error}")
                    self.clear_action()
                    st.rerun()

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
                st.rerun()
        else:
            st.caption("Testa a query acima antes de poder confirmar a criação.")

    def _render_execution_report_by_work_items(self, ado_client, ado_project: str, area_paths: list):
        """
        Modo alternativo de montar o Relatório de Testes: em vez de partir
        de um Test Plan (e depender de que os Casos já tenham vínculo
        'Tests' pra aparecer status/Matriz), a pessoa escolhe os Work Items
        DIRETO — de qualquer coluna do board — e o relatório usa os Casos
        de Teste que já estiverem vinculados a cada um deles.
        """
        st.caption(
            "Escolha os Work Items (ex.: User Stories) que você quer reportar — de qualquer "
            "coluna do board, qualquer status. O relatório usa os Casos de Teste já vinculados "
            "a cada um (relação 'Tests' no Azure DevOps) e o status vem da coluna atual do "
            "próprio Work Item selecionado."
        )

        with st.container(key="azure_blue_btn_fetch_wi_report"):
            st.button(
                "🔄 Buscar Work Items",
                disabled=self.state.get('is_processing'),
                key="btn_fetch_wi_report",
                on_click=self.trigger_action,
                args=("fetch_wi_report",),
            )
        if self.state.get('current_action') == 'fetch_wi_report' and not self.state.get('show_interrupt_modal'):
            try:
                paths_to_search = area_paths or [ado_project]
                with st.spinner(f"Buscando Work Items em {len(paths_to_search)} Area Path(s)..."):
                    items_by_id = {}
                    for ap in paths_to_search:
                        for item in ado_client.fetch_work_items_by_area_path(ap, excluded_states={"Backlog"}):
                            items_by_id[item["id"]] = item
                    items = list(items_by_id.values())
                self.state.set('report_wi_board_items', items)
                if not items:
                    self._flash_warning("Nenhum Work Item encontrado" + (" nessas Area Paths." if area_paths else " neste projeto."))
            except AzureDevOpsError as error:
                self._flash_error(str(error))
            except Exception as error:
                self._flash_error(f"Erro inesperado: {error}")
            self.clear_action()
            st.rerun()

        board_items = self.state.get('report_wi_board_items') or []
        if not board_items:
            st.caption("Busque os Work Items acima pra continuar.")
            return

        all_types = sorted({item['type'] for item in board_items})
        default_types = [t for t in all_types if t.strip().lower() in ("user story", "história de usuário", "historia de usuario")]
        col_tipo, col_wi = st.columns(2)
        with col_tipo:
            tipos_filtro = st.multiselect(
                "Filtrar por tipo (opcional)",
                options=all_types,
                default=default_types,
                key="report_wi_type_filter",
                disabled=self.state.get('is_processing'),
                help="Deixe vazio pra ver todos os tipos.",
            )
        filtered_items = [item for item in board_items if not tipos_filtro or item['type'] in tipos_filtro]
        filtered_items = self._filtrar_por_coluna_e_tag(filtered_items, bool(area_paths), "report_wi")
        wi_labels = {f"{i['id']} - {i['title']} ({i['type']}, {i['state']})": i for i in filtered_items}
        with col_wi:
            selected_labels = st.multiselect(
                "🎯 Work Items a reportar",
                options=list(wi_labels.keys()),
                key="report_wi_select",
                disabled=self.state.get('is_processing'),
                help="Podem estar em qualquer coluna — o status de cada um vem da coluna em que estiver agora.",
            )
        selected_wis = [wi_labels[label] for label in selected_labels]

        if not selected_wis:
            st.caption("Selecione ao menos um Work Item acima.")
            return

        default_nome_relatorio = ", ".join(area_paths) if area_paths else ado_project
        nome_relatorio = self._render_project_name_field(default_nome_relatorio, "report_wi_project_name", "Nome do Projeto *")

        with st.container(key="azure_blue_btn_suggest_narrative_wi"):
            st.button(
                "🤖 Sugerir Contexto/Escopo/Conclusão com IA",
                disabled=self.state.get('is_processing'),
                key="btn_suggest_report_narrative_wi",
                on_click=self._iniciar_geracao_em_lotes,
                args=("suggest_report_narrative_wi", "narrativa_wi"),
                help="A IA analisa os Work Items selecionados e sugere os textos abaixo.",
            )
        if self.state.get('current_action') == 'suggest_report_narrative_wi' and not self.state.get('show_interrupt_modal'):
            self._suggest_report_narrative_from_work_items(ado_client, selected_wis)

        contexto, ambiente, escopo_proposito, conclusao, proximos_passos, status_manual = self._render_report_narrative_fields()

        with st.container(key="azure_blue_btn_generate_report_wi"):
            st.button(
                "📊 Buscar Resultados e Gerar Relatório",
                type="primary",
                width="stretch",
                disabled=self.state.get('is_processing') or not nome_relatorio.strip() or not contexto or not escopo_proposito or not conclusao or not status_manual,
                key="btn_generate_execution_report_wi",
                on_click=self.trigger_action,
                args=("generate_execution_report_wi",),
            )
        if not nome_relatorio.strip() or not (contexto and escopo_proposito and conclusao and status_manual):
            st.caption("Preencha o Nome do Projeto, Contexto, Escopo e Propósito, Conclusão, e escolha o Status para habilitar a geração.")

        if self.state.get('current_action') == 'generate_execution_report_wi' and not self.state.get('show_interrupt_modal'):
            self._generate_execution_report_from_work_items(ado_client, selected_wis, contexto, ambiente, escopo_proposito, conclusao, proximos_passos, ado_project, area_paths, status_manual, nome_relatorio.strip())

        report_bytes = self.state.get('report_pdf_bytes')
        if report_bytes:
            safe_name = (self.state.get('project_name') or 'projeto').replace(' ', '_')
            st.download_button(
                "⬇️ Baixar Relatório de Testes (PDF)",
                data=report_bytes,
                file_name=f"Relatorio_Testes_{safe_name}.pdf",
                mime="application/pdf",
                width="stretch",
                type="primary",
                key="download_report_wi",
            )
            for warn in self.state.get('report_warnings') or []:
                st.caption(f"ℹ️ {warn}")

            self._render_document_storage_section(
                "Relatório de Testes (por Work Items)", self.state.get('project_name') or 'projeto',
                [{"tipo": "pdf", "nome_arquivo": f"Relatorio_Testes_{safe_name}.pdf", "conteudo": report_bytes}],
            )

    def _suggest_report_narrative_from_work_items(self, ado_client, work_items: list):
        EXCLUDE_TYPES = {"test plan", "test suite", "test case"}

        def montar_payload():
            # Roda UMA vez (fica no estado durante as esperas de retentativa).
            wi_names = ", ".join(wi['title'] for wi in work_items)
            if True:
                total_casos = 0
                for wi in work_items:
                    try:
                        total_casos += len(ado_client.get_test_cases_for_work_item(wi['id']))
                    except Exception:
                        pass
                resumo_resultados = (
                    f"{len(work_items)} Work Item(s) selecionado(s) diretamente pra este relatório, "
                    f"totalizando {total_casos} Caso(s) de Teste vinculado(s). Work Items: {wi_names}."
                )

                details = ado_client.get_work_items_full_details([wi['id'] for wi in work_items])
                relevantes = [d for d in details if (d.get('type') or '').strip().lower() not in EXCLUDE_TYPES]
                partes = []
                for d in relevantes:
                    desc = (d.get('description') or '').strip()
                    if desc:
                        partes.append(f"[{d.get('type')}] {d.get('title')}: {desc[:500]}")
                descricoes_texto = "\n\n".join(partes)
            return dict(
                nome_projeto=self.state.get('project_name') or wi_names,
                nome_plano=wi_names,
                resumo_resultados=resumo_resultados,
                matriz=self.state.get('matriz') or [],
                descricoes_work_items=descricoes_texto,
            )

        with st.status("Analisando os Work Items selecionados para sugerir os textos...", expanded=True) as status:
            resultado = self._chamar_ia_com_retentativas(
                "narrativa_wi", montar_payload,
                lambda p: self.client.trigger_execution_report_narrative(**p), status,
            )
            if resultado is None:
                return  # rerun() já disparado (espera pós-erro)
            _payload, resp, erro = resultado
            status.update(label="Falha ao gerar a sugestão." if erro else "Sugestão recebida.",
                          state="error" if erro else "complete")
        if erro:
            self._flash_error(f"Não foi possível gerar a sugestão da IA: {erro}")
        else:
            self._aplicar_narrativa_sugerida(resp)

        self.clear_action()
        st.rerun()

    def _aplicar_narrativa_sugerida(self, resp: dict):
        """Leva os textos sugeridos pela IA pro estado e pros campos da tela."""
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

    def _generate_execution_report_from_work_items(self, ado_client, work_items: list, contexto: str, ambiente: str,
                                                      escopo_proposito: str, conclusao: str, proximos_passos: str,
                                                      ado_project: str = "", area_paths: list = None, status_manual: str = "",
                                                      nome_relatorio: str = ""):
        area_paths = area_paths or []
        warnings = []
        evidencias_por_caso = {}
        casos = []

        # Nome já confirmado pela pessoa na tela (com sugestão da IA
        # baseada nos Work Items disponível) — não recorre mais a um
        # nome genérico do Azure DevOps como valor de fallback silencioso.
        report_project_name = nome_relatorio or "Projeto"

        try:
            wi_ids_with_cases = set()
            with st.spinner(f"Buscando Casos de Teste vinculados a {len(work_items)} Work Item(s)..."):
                for wi in work_items:
                    try:
                        cases = ado_client.get_test_cases_for_work_item(wi['id'])
                    except Exception as error:
                        warnings.append(f"Falha ao buscar Casos do Work Item {wi['id']} ({wi['title']}): {error}")
                        continue
                    if not cases:
                        warnings.append(f"Work Item {wi['id']} ({wi['title']}) não tem nenhum Caso de Teste vinculado (relação 'Tests').")
                        continue
                    wi_ids_with_cases.add(wi['id'])
                    for case in cases:
                        titulo = case.get('titulo') or f"Caso #{case.get('id')}"
                        casos.append({"titulo": titulo, "outcome": "Desconhecido", "suite_name": wi['title']})
                        case_id = case.get('id')
                        if case_id:
                            try:
                                imgs, img_warnings = ado_client.get_test_case_attachments(case_id)
                                if imgs:
                                    evidencias_por_caso[titulo] = imgs
                                warnings.extend(img_warnings)
                            except Exception as error:
                                warnings.append(f"Falha ao buscar imagens de '{titulo}': {error}")

            # Status vem direto da coluna do board de CADA Work Item
            # selecionado — não precisa procurar vínculo nenhum, porque a
            # seleção já partiu do próprio Work Item.
            wi_status_by_title = {}
            with st.spinner("Consultando status de QA (coluna do board) de cada Work Item..."):
                for wi in work_items:
                    try:
                        wi_status_by_title[wi['title']] = ado_client.get_work_item_qa_status(wi['id'])
                    except Exception as error:
                        wi_status_by_title[wi['title']] = "Desconhecido"
                        warnings.append(f"Falha ao checar status do Work Item {wi['id']}: {error}")
            for caso in casos:
                caso["outcome"] = wi_status_by_title.get(caso["suite_name"], "Desconhecido")

            status_geral = status_manual or "Pendente"

            # Matriz independente, direto a partir dos Work Items
            # selecionados (já temos os IDs — sem precisar descobrir vínculo).
            matriz_to_use = []
            if wi_ids_with_cases:
                try:
                    with st.spinner(f"Montando Matriz de Cobertura a partir de {len(wi_ids_with_cases)} Work Item(s)..."):
                        details = ado_client.get_work_items_full_details(list(wi_ids_with_cases))
                        EXCLUDE_TYPES_MTX = {"test plan", "test suite", "test case"}
                        relevantes = [d for d in details if (d.get('type') or '').strip().lower() not in EXCLUDE_TYPES_MTX]
                        CATEGORIA_POR_TIPO = {
                            "bug": "Correção de Defeito", "user story": "Fluxo Funcional",
                            "product backlog item": "Fluxo Funcional", "feature": "Fluxo Funcional",
                            "task": "Tarefa Técnica",
                        }
                        sigla = self._env_sigla()
                        for idx, wi_detail in enumerate(relevantes, start=1):
                            mc_id = f"MC-{idx:03d}" + (f" {sigla}" if sigla else "")
                            wi_type_label = wi_detail.get('type', '') or ''
                            categoria = CATEGORIA_POR_TIPO.get(wi_type_label.strip().lower(), wi_type_label or "—")
                            descricao = (wi_detail.get('description') or '').strip()
                            matriz_to_use.append({
                                "id": mc_id,
                                "funcionalidade": wi_detail.get('title', '') or '—',
                                "requisito": f"{wi_type_label} #{wi_detail.get('id', '')}".strip(),
                                "cenario": (descricao[:200] + "…") if len(descricao) > 200 else (descricao or "—"),
                                "categoria": categoria,
                                "prioridade": "—",
                                "criticidade": "—",
                            })
                    if matriz_to_use:
                        warnings.append(f"Matriz de Cobertura montada a partir dos {len(matriz_to_use)} Work Item(s) selecionado(s).")
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
            self._log(
                "Gerar Relatório de Testes (por Work Items)", "Relatório de Testes",
                f"{len(work_items)} Work Item(s) — status: {status_geral}",
            )
        except AzureDevOpsError as error:
            self._flash_error(f"{error}")
        except Exception as error:
            self._flash_error(f"Erro inesperado: {error}")
        self.clear_action()
        st.rerun()

    def _render_report_narrative_fields(self, default_ambiente_guess: bool = False):
        """
        Campos compartilhados entre os dois modos do Relatório de Testes
        (por Test Plan, ou por Work Items): Contexto, Ambiente, Escopo,
        Conclusão, Próximos Passos, Status manual. Retorna
        (contexto, ambiente, escopo_proposito, conclusao, proximos_passos, status_manual).
        """
        st.caption("Os campos abaixo já vêm com sugestão da IA (se você clicou no botão de sugestão acima) — revise e edite livremente antes de gerar o PDF.")

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
                ambiente_default = 0
                help_text = "Confirme antes de gerar."
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
            help="Você define o status final do relatório diretamente — não depende do cálculo automático pela coluna do board.",
        )
        return contexto, ambiente, escopo_proposito, conclusao, proximos_passos, status_manual

    def _render_execution_report_section(self, ado_client, ado_project: str = "", area_paths: list = None):
        area_paths = area_paths or []
        st.markdown("#### 📊 Relatório de Testes (execução)")
        st.caption(
            "Documenta o que foi EXECUTADO no Azure DevOps (diferente do PDF do Passo 6, que "
            "documenta o que foi planejado). Busca o Test Plan escolhido, os resultados de "
            "execução e as evidências (anexos) direto do Azure DevOps."
        )

        with st.container(key="azure_blue_btn_fetch_report_plans"):
            st.button(
                "🔍 Buscar Test Plans deste Projeto",
                disabled=self.state.get('is_processing'),
                key="btn_fetch_report_plans",
                on_click=self.trigger_action,
                args=("fetch_report_plans",),
            )
        if self.state.get('current_action') == 'fetch_report_plans' and not self.state.get('show_interrupt_modal'):
            try:
                # Test Plans pertencem ao PROJETO, não à Area Path (mesmo que
                # uma ou mais Area Paths tenham sido escolhidas acima, pra
                # nomear o relatório) — busca sem filtro.
                with st.spinner("Buscando Test Plans do projeto..."):
                    plans = ado_client.list_test_plans()
                self.state.set('report_available_plans', plans)
                if not plans:
                    st.warning("Nenhum Test Plan encontrado neste projeto.")
            except AzureDevOpsError as error:
                self._flash_error(f"{error}")
                self.state.set('report_available_plans', [])
            except Exception as error:
                self._flash_error(f"Erro inesperado: {error}")
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

        contexto, ambiente, escopo_proposito, conclusao, proximos_passos, status_manual = self._render_report_narrative_fields()

        with st.container(key="azure_blue_btn_generate_report"):
            st.button(
                "📊 Buscar Resultados e Gerar Relatório",
                type="primary",
                width="stretch",
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
                width="stretch",
                type="primary",
            )
            for warn in self.state.get('report_warnings') or []:
                st.caption(f"ℹ️ {warn}")

            self._render_document_storage_section(
                "Relatório de Testes (por Test Plan)", self.state.get('project_name') or 'projeto',
                [{"tipo": "pdf", "nome_arquivo": f"Relatorio_Testes_{safe_name}.pdf", "conteudo": report_bytes}],
            )

    def _suggest_report_narrative(self, ado_client, plans: list):
        EXCLUDE_TYPES = {"test plan", "test suite", "test case"}
        # Vocabulário real da coluna "Outcome" da aba Execute das Suites de
        # Teste no Azure DevOps — usado tal e qual, sem lumping genérico.
        OUTCOME_LABELS = {
            "Passed": "Aprovado", "Failed": "Reprovado", "Active": "Ativo (não iniciado)",
            "Paused": "Pausado", "Blocked": "Bloqueado", "NotApplicable": "Não Aplicável",
            "Not Run": "Não Executado",
        }
        def montar_payload():
            # Roda UMA vez (fica no estado durante as esperas de retentativa).
            plan_names = ", ".join(p['name'] for p in plans)
            if True:
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
            return dict(
                nome_projeto=self.state.get('project_name') or plan_names,
                nome_plano=plan_names,
                resumo_resultados=resumo_resultados,
                matriz=self.state.get('matriz') or [],
                descricoes_work_items=descricoes_texto,
            )

        with st.status("Analisando resultados dos Test Plans para sugerir os textos...", expanded=True) as status:
            resultado = self._chamar_ia_com_retentativas(
                "narrativa_planos", montar_payload,
                lambda p: self.client.trigger_execution_report_narrative(**p), status,
            )
            if resultado is None:
                return  # rerun() já disparado (espera pós-erro)
            _payload, resp, erro = resultado
            status.update(label="Falha ao gerar a sugestão." if erro else "Sugestão recebida.",
                          state="error" if erro else "complete")
        if erro:
            self._flash_error(f"Não foi possível gerar a sugestão da IA: {erro}")
        else:
            self._aplicar_narrativa_sugerida(resp)

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
            self._flash_error(f"{error}")
        except Exception as error:
            self._flash_error(f"Erro inesperado ao gerar o relatório: {error}")

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
        # a ele no Azure DevOps — com conteúdo completo (não só título),
        # pra poder comparar qualidade contra um Caso novo que pareça
        # duplicado, além de servir de contexto pra IA evitar sugerir algo
        # que já existe.
        # Cache em '_vinculos_ia_prep': as esperas de retentativa da IA
        # disparam reruns, e sem isso a busca no Azure se repetiria a cada um.
        existing_full_by_wid = self.state.get('_vinculos_ia_prep')
        if existing_full_by_wid is None:
            existing_full_by_wid = {}
            try:
                with st.spinner("Verificando Casos de Teste já existentes nos Work Items..."):
                    for item in board_items:
                        try:
                            existing_full_by_wid[item["id"]] = ado_client.get_existing_test_cases_full(item["id"])
                        except AzureDevOpsError:
                            existing_full_by_wid[item["id"]] = []
            except Exception:
                existing_full_by_wid = {item["id"]: [] for item in board_items}
            self.state.set('_vinculos_ia_prep', existing_full_by_wid)
        existing_by_wid = {
            wid: [c["titulo"] for c in casos] for wid, casos in existing_full_by_wid.items()
        }

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
                with st.status("Consultando a IA (n8n) para sugerir os vínculos...", expanded=True) as status:
                    resultado = self._chamar_ia_com_retentativas(
                        "vinculos_ia",
                        lambda: {"items": payload_items, "cases": payload_cases, "project": self.state.get('project_name')},
                        lambda p: self.client.trigger_matching(p["items"], p["cases"], p["project"]), status,
                    )
                    if resultado is None:
                        return  # rerun() já disparado (espera pós-erro)
                    _payload, result, erro = resultado
                    status.update(label="Falha ao consultar a IA." if erro else "Sugestão recebida.",
                                  state="error" if erro else "complete")
                if erro:
                    raise ValueError(erro)
            else:
                result = {"vinculos": []}
            self.state.delete('_vinculos_ia_prep')
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

            # Rede de segurança #2: identifica casos muito parecidos com um
            # Caso de Teste QUE JÁ EXISTE naquele Work Item no Azure DevOps.
            # Continuam pré-desmarcados por padrão (não criamos/vinculamos
            # sozinhos), mas agora com uma ANÁLISE DE CONTEXTO feita pela
            # IA, comparando o conteúdo real dos dois Casos — não só título
            # ou volume de texto. A similaridade de título aqui é só um
            # FILTRO barato pra decidir quais pares vale a pena mandar pra
            # IA analisar (evita mandar todo par de casos pra IA à toa).
            # Nada é excluído automaticamente no Azure DevOps — só uma
            # recomendação visível, com o motivo explicado pela IA.
            SIMILARITY_THRESHOLD = 0.80
            candidatos = {}  # id_par -> {wid_key, novo_titulo, existente}
            for wid_key, casos in links.items():
                existentes_full = existing_full_by_wid.get(int(wid_key), [])
                for c in casos:
                    c_norm = c.strip().lower()
                    melhor_match, melhor_ratio = None, 0.0
                    for e in existentes_full:
                        ratio = difflib.SequenceMatcher(None, c_norm, (e.get("titulo") or "").strip().lower()).ratio()
                        if ratio > melhor_ratio:
                            melhor_ratio, melhor_match = ratio, e
                    if melhor_ratio >= SIMILARITY_THRESHOLD and melhor_match:
                        id_par = str(uuid.uuid4())
                        tc_novo = next((tc for tc in test_cases if tc.get("titulo") == c), {})
                        candidatos[id_par] = {
                            "wid_key": wid_key, "novo_titulo": c,
                            "existing_id": melhor_match.get("id"),
                            "existing_titulo": melhor_match.get("titulo", ""),
                            "similaridade": round(melhor_ratio, 2),
                        }

            duplicate_case_titles = set()
            duplicate_analysis = {}
            if candidatos:
                pares_payload = []
                for id_par, info in candidatos.items():
                    tc_novo = next((tc for tc in test_cases if tc.get("titulo") == info["novo_titulo"]), {})
                    existente = next(
                        (e for e in existing_full_by_wid.get(int(info["wid_key"]), []) if e.get("id") == info["existing_id"]),
                        {}
                    )
                    pares_payload.append({
                        "id_par": id_par,
                        "novo": {
                            "titulo": tc_novo.get("titulo", ""),
                            "pre_condicoes": tc_novo.get("pre_condicoes", ""),
                            "passos": tc_novo.get("passos", []),
                        },
                        "existente": {
                            "titulo": existente.get("titulo", ""),
                            "pre_condicoes": existente.get("pre_condicoes", ""),
                            "passos": existente.get("passos", []),
                        },
                    })
                try:
                    with st.spinner(f"IA analisando o contexto de {len(pares_payload)} possível(is) duplicidade(s)..."):
                        comp_result = self.client.trigger_duplicate_comparison(pares_payload)
                    for comp in comp_result.get("comparacoes", []):
                        id_par = comp.get("id_par")
                        info = candidatos.get(id_par)
                        if not info:
                            continue
                        mesmo_contexto = bool(comp.get("mesmo_contexto"))
                        if mesmo_contexto:
                            duplicate_case_titles.add(info["novo_titulo"])
                        duplicate_analysis[info["novo_titulo"]] = {
                            "existing_id": info["existing_id"],
                            "existing_titulo": info["existing_titulo"],
                            "similaridade": info["similaridade"],
                            "mesmo_contexto": mesmo_contexto,
                            "recomendacao": comp.get("recomendacao", "equivalentes"),
                            "motivo": comp.get("motivo", ""),
                        }
                except Exception as error:
                    # Se a IA de comparação falhar, não bloqueia o fluxo —
                    # só não temos a análise de contexto desta vez; os
                    # candidatos ficam disponíveis normalmente, sem marcação.
                    st.warning(f"⚠️ Não foi possível comparar o contexto dos possíveis duplicados: {error}")

            final_links = {}
            for wid_key, casos in links.items():
                kept = [c for c in casos if c not in duplicate_case_titles]
                if kept:
                    final_links[wid_key] = kept
            links = final_links
            self.state.set('ado_duplicate_analysis', duplicate_analysis)

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
            self.state.delete('_vinculos_ia_prep')
            self.state.set('ado_suggest_message', ("error", f"❌ {error}"))
        except Exception as error:
            self.state.delete('_vinculos_ia_prep')
            self.state.set('ado_suggest_message', ("error", f"❌ Erro inesperado ao consultar sugestão da IA: {error}"))

    def _push_full_azure_devops(self, ado_client, area_path: str, plan_name: str, initial_state: str = None, existing_plan_id: int = None):
        test_cases = self.state.get('test_cases') or []
        project_name = self.state.get('project_name') or "QA TestGen"
        case_ids = dict(self.state.get('ado_test_case_ids') or {})
        case_links = dict(self.state.get('ado_case_links') or {})
        links = self.state.get('ado_wi_case_links') or {}
        excluded_titles = set(self.state.get('ado_excluded_case_titles') or [])
        log = []

        # Trava de segurança: sem nenhum Caso de Teste em `test_cases`, não
        # existe nada de verdade pra criar/vincular — sem essa checagem, o
        # código seguia em frente e criava o Test Plan (e possivelmente
        # Suites) igual, resultando num Test Plan vazio no Azure DevOps sem
        # nenhum erro que explicasse o motivo. Costuma acontecer quando os
        # Casos foram gerados de forma incompleta (ex.: um lote travou/falhou
        # na geração) e a pessoa avançou mesmo assim.
        if not test_cases:
            log.append(
                "❌ Não há nenhum Caso de Teste gerado (`test_cases` está vazio) — nada foi enviado ao "
                "Azure DevOps. Volte ao Passo 3/4 e gere os Casos de Teste antes de integrar."
            )
            self.state.set('ado_full_push_log', log)
            self._flash_error("Nenhum Caso de Teste gerado — a integração foi cancelada antes de criar o Test Plan.")
            self.clear_action()
            st.rerun()
            return

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
        # Override manual: a tela (aviso logo acima do multiselect de
        # vínculo) promete que dá pra incluir um Caso marcado como
        # duplicata pela IA "se discordar" — vinculando ele manualmente a
        # um Work Item. Um título presente em items_with_cases É a pessoa
        # discordando na prática; sem essa checagem, o Caso nunca ganhava
        # ID no Azure DevOps e o vínculo escolhido era descartado
        # silenciosamente (só aparecia, indiretamente, num aviso agregado
        # de "vínculos incompletos").
        titulos_vinculados_manualmente = {t for casos in items_with_cases.values() for t in casos}
        duplicate_titles_sem_override = duplicate_titles - titulos_vinculados_manualmente
        skipped_as_duplicate = [tc.get('titulo') for tc in test_cases if tc.get('titulo') in duplicate_titles_sem_override]
        if skipped_as_duplicate:
            log.append(
                f"🔁 {len(skipped_as_duplicate)} Caso(s) não foram criados por parecerem duplicados de "
                f"algo já existente no Azure DevOps: {', '.join(skipped_as_duplicate)}"
            )
        overrides_manuais = duplicate_titles & titulos_vinculados_manualmente
        if overrides_manuais:
            log.append(
                f"↪️ {len(overrides_manuais)} Caso(s) marcados como duplicados pela IA foram criados mesmo "
                f"assim, porque você os vinculou manualmente a um Work Item: {', '.join(overrides_manuais)}"
            )
        cases_to_create = [
            tc for tc in test_cases
            if tc.get('titulo') not in case_ids
            and tc.get('titulo') not in duplicate_titles_sem_override
            and tc.get('titulo') not in excluded_titles
        ]
        if cases_to_create:
            total = len(cases_to_create)
            done = 0
            progress = st.progress(0, text=f"Criando Test Cases no Azure DevOps... (0/{total})")

            # Calculados AQUI (thread principal): dentro do executor não
            # existe st.session_state, e a tag sairia "criado-por:desconhecido".
            tag_autor = self._tag_criado_por()
            atribuir_a = self.state.get('ado_test_case_assigned_to') or None

            def _create_case(tc):
                titulo = tc.get('titulo')
                titulo_prefixado = titled.get(titulo, titulo)
                result = ado_client.create_test_case(
                    titulo_prefixado, tc.get('pre_condicoes', ''), tc.get('passos', []), area_path, initial_state,
                    tags=tag_autor, assigned_to=atribuir_a,
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
            self.state.set('ado_last_plan_id', plan_id)
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
                self.state.set('ado_last_plan_id', plan_id)
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
        # suites_ok guarda os Work Items cuja Suite REALMENTE existe no
        # Test Plan (já existia, ou foi criada agora com sucesso) — sem
        # isso, uma falha de API aqui (outage, permissão, concorrência)
        # não impedia o vínculo "Tests" do passo 3b de "dar certo" (são
        # chamadas independentes), mas o Caso não aparece de verdade
        # dentro da estrutura do Test Plan pra esse Work Item (não tem
        # Suite pra ele "entrar"), e a checagem final abaixo não pegava
        # isso — mesmo problema, mesma causa raiz do modo "sem Work
        # Items" (_push_static_suites_azure_devops).
        suite_tasks = list(items_with_cases.items())  # [(work_item_id_str, [titulos]), ...]
        suites_ok = set()
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
                    suites_ok.add(work_item_id)
                else:
                    try:
                        suite_id = ado_client.create_requirement_based_suite(plan_id, root_suite_id, work_item_id)
                        log.append(f"✅ Suite criada para Work Item {work_item_id} (Suite ID {suite_id})")
                        suites_ok.add(work_item_id)
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

        # Mesma checagem de "sucesso incompleto e silencioso" do modo sem
        # Work Items (_push_static_suites_azure_devops): olha o que
        # REALMENTE ficou vinculado no fim, não só se alguma exceção foi
        # levantada — pega o caso de `items_with_cases` referenciar títulos
        # de uma tentativa de geração anterior (ex.: Casos regerados depois
        # de um lote falhar), que não existem mais em `test_cases`/`case_ids`
        # agora: cada vínculo é pulado silenciosamente (linha "não existe no
        # Azure DevOps, pulando vínculo"), a Suite chega a ser criada, mas
        # fica vazia — sem isso, a única pista ficava escondida no meio do
        # log.
        total_vinculos_esperados = sum(len(casos) for casos in items_with_cases.values())
        total_vinculos_ok = sum(
            1
            for wid_str, casos in items_with_cases.items()
            for titulo in casos
            if int(wid_str) in case_links.get(titulo, []) and int(wid_str) in suites_ok
        )
        if total_vinculos_esperados and not total_vinculos_ok:
            self._flash_error(
                f"O Test Plan foi criado, mas NENHUM dos {total_vinculos_esperados} vínculo(s) "
                "Caso↔Work Item esperado(s) foi efetivado — o Test Plan ficou vazio no Azure DevOps. "
                "Confira o log abaixo: se aparecer '⚠️ ... não existe no Azure DevOps', os títulos dos "
                "Casos vinculados não batem com os Casos atuais (comum depois de regerar os Casos de "
                "Teste) — volte ao Passo 7 e refaça o vínculo com a IA."
            )
        elif total_vinculos_esperados and total_vinculos_ok < total_vinculos_esperados:
            self._flash_error(
                f"Só {total_vinculos_ok} de {total_vinculos_esperados} vínculo(s) Caso↔Work Item "
                "esperado(s) foram efetivados no Azure DevOps — confira o log abaixo pra ver quais "
                "faltaram e por quê."
            )

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

        st.divider()
        st.markdown("#### 📖 Guias em PDF")
        username = st.session_state.get(SESSION_USER_KEY, "")
        is_owner = username == self.config.owner_username
        # __file__ = qa_testgen/ui/application.py -> .parent.parent = qa_testgen/ -> /assets
        assets_dir = Path(__file__).resolve().parent.parent / "assets"

        def _botao_download_guia(nome_arquivo: str, rotulo: str, key: str):
            caminho = assets_dir / nome_arquivo
            try:
                pdf_bytes = caminho.read_bytes()
                st.download_button(
                    rotulo, data=pdf_bytes, file_name=nome_arquivo,
                    mime="application/pdf", key=key,
                )
            except FileNotFoundError:
                st.caption(f"⚠️ {nome_arquivo} ainda não foi colocado em `qa_testgen/assets/`.")

        col_guia1, col_guia2 = st.columns(2)
        with col_guia1:
            _botao_download_guia("Guia_Usuario.pdf", "📘 Baixar Guia do Usuário", "btn_download_guia_usuario")
        with col_guia2:
            if is_owner:
                _botao_download_guia("Guia_Administrador.pdf", "🛡️ Baixar Guia do Administrador", "btn_download_guia_admin")

        st.markdown("#### 🧭 Arquitetura geral")
        if self.config.azure_devops_pat:
            pat_caption = (
                "usando um PAT compartilhado, configurado pelo administrador nos Secrets — "
                "quem fez cada ação fica registrado por uma tag automática "
                "(`criado-por:<usuário>`) em cada item, e no histórico interno do app"
            )
        else:
            pat_caption = (
                "usando o PAT pessoal que cada usuário informa na hora — nunca salvo em disco, "
                "só na memória da sessão"
            )
        st.caption(
            "O acesso passa por aprovação antes de entrar. Depois disso, o time de QA usa o "
            "app, que aciona o n8n (onde a IA gera o conteúdo, e onde o controle de acesso/logs "
            f"também vivem) e integra tudo direto no Azure DevOps, {pat_caption}."
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
        st.markdown(
            "**Passo 1 tem 3 formas de fornecer a especificação** (quem tem a permissão "
            "certa vê todas):\n"
            "- **📄 Enviar Documento(s)**: PDF, DOCX ou TXT\n"
            "- **🎯 Gerar a partir de Work Items**: varre o board por Area Path, escolhe quais entram — "
            "com uma Area Path específica escolhida, aparecem também filtros opcionais de Coluna do "
            "Board e/ou Tag\n"
            "- **🔎 Gerar a partir de uma Query**: parte de uma query já salva no Azure DevOps "
            "(sua ou compartilhada) — roda a query, e você escolhe quais Work Items do "
            "resultado entram, do mesmo jeito que no modo anterior (sem filtro de Coluna/Tag "
            "aqui — query é escopada por Projeto, não por Area Path)"
        )

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
        st.markdown(
            "- **📘 Manual de Testes**: origem por Documentos, Work Items (Board ou Query salva), ou "
            "Mesclado — nunca tira print ao vivo, só reaproveita imagem já existente\n"
            "- **🗄️ Documentos Armazenados**: qualquer pessoa com a permissão salva e visualiza; "
            "**excluir um grupo é exclusivo do dono do app**, mesmo pra quem tem a permissão\n"
            "- **🧠 Mapa Mental**: exporta em SVG ou PDF sempre com tudo expandido no arquivo, "
            "independente do que estiver aberto/fechado na tela\n"
            "- **🐛 Criar Bug**: cria um Bug no Azure DevOps livremente, ou a partir de um Work "
            "Item com Casos de Teste relacionados — vinculando a um Caso específico (título e "
            "Passos de Reprodução pré-preenchidos) ou direto no Work Item principal, sem "
            "depender de nenhum Caso. Campos extras opcionais: System Info, Acceptance "
            "Criteria, Discussion, e evidências em imagem (sobem como anexo do Bug e ficam "
            "também embutidas no System Info)\n"
            "- **🧱 Criar Work Item**: cria Work Items de qualquer tipo que o processo do "
            "projeto permita (User Story, Epic, Feature, Task, Spike, tipos customizados...), "
            "com o formulário montado a partir dos metadados do próprio projeto — campo "
            "customizado aparece sozinho. Aceita evidências em imagem (anexo + embutidas na "
            "Descrição), tem modo de criar **vários filhos de uma vez** sob um pai (quebrar "
            "uma User Story em Tasks), modo **Fila / planilha** (vários itens de qualquer tipo, "
            "pelo formulário ou por CSV/XLSX com modelo gerado do projeto, enviados de uma vez) "
            "e dá pra criar tag nova na hora\n"
            "- **🔌 Testes de API**: executa testes de API direto do app, em Python (sem Node/Newman). "
            "Os casos podem ser gerados por IA a partir do(s) Work Item(s) do Azure DevOps (ou de texto colado) — "
            "o botão Reconhecer a API descobre antes as rotas reais e o formato de erro, e segredos nunca vão "
            "pra IA —, vir de uma collection do Postman (v2.1, com "
            "environment opcional — os `pm.test` mais comuns viram asserções automaticamente), de uma "
            "definição salva pelo próprio módulo, "
            "ou são criados na tela. Variáveis `{{nome}}`, segredos só em sessão (mascarados em toda "
            "evidência), encadeamento de valores extraídos da resposta (token do login → rota "
            "protegida). Evidências: RELATORIO.md, RELATORIO.pdf no padrão QA TestGen e .zip com uma "
            "pasta por caso (request, response, resultado e prints), com opção de guardar em "
            "Documentos Armazenados. As chamadas saem do navegador de quem usa o app (padrão, contorna WAF) "
            "ou do servidor — configuração global do administrador. Ao final, \"Levar para o assistente\" "
            "transforma a bateria em Matriz, Casos e Plano pro Passo 7, e depois dá pra registrar a execução "
            "como Test Run oficial no Azure DevOps"
        )
        st.caption(
            "⚠️ \"🔎 Query com IA\" aqui é diferente do modo \"Gerar a partir de uma Query\" do "
            "Passo 1: essa daqui só descreve em português e **salva uma query nova dentro do "
            "Azure DevOps** (útil pra Dashboards/widgets de lá) — não gera nada de teste. O "
            "modo do Passo 1 faz o caminho inverso: parte de uma query **que você já tem** "
            "salva lá, pra gerar teste a partir do resultado dela. Depois de gerar uma query "
            "aqui, dois atalhos pulam a etapa de salvar: \"Usar pra Gerar Testes\" e \"Usar pra "
            "Criar Manual\", cada um só visível pra quem também tem a permissão do destino."
        )

        st.divider()

        st.markdown("#### 🔐 Controle de acesso e governança")
        st.markdown(
            "- **Login com aprovação**: só o dono do app entra direto — qualquer outra pessoa "
            "precisa ser aprovada por você ou por um aprovador cadastrado, a cada nova sessão\n"
            "- **Sessão via ID opaco na URL**: o link de sessão não revela usuário nem senha "
            "nenhuma — o dado real fica guardado no n8n, e pode ser revogado remotamente a "
            "qualquer momento (a sua própria sessão, ou a de outra pessoa) em Administração\n"
            "- **PAT do Azure DevOps**: por padrão, cada usuário informa o próprio (nunca salvo "
            "em disco, só na memória da sessão) — opcionalmente, o administrador pode configurar "
            "um PAT compartilhado nos Secrets do Streamlit, e nesse caso ninguém mais precisa "
            "digitar token nenhum. No modo compartilhado, cada Bug/Test Case criado recebe "
            "automaticamente a tag `criado-por:<usuário>` pra manter a rastreabilidade de quem "
            "fez o quê; e toda ação continua registrada com o usuário logado no histórico "
            "interno do app de qualquer forma\n"
            "- **Permissões granulares**: acesso à Integração com Azure DevOps, ao Relatório de "
            "Testes, ao modo \"Gerar a partir de uma Query\", ao Manual de Testes, aos "
            "Documentos Armazenados, ao Mapa Mental, ao Criar Bug, ao Criar Work Item e aos "
            "Testes de API são liberados individualmente — quem não tem permissão nem vê a opção\n"
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
            {node(156, "Login", "Aprovação + PAT (pessoal ou compartilhado)")}
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
            {node(453, 250, 185, "7. Azure DevOps", "PAT + merge")}
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
        <svg width="100%" viewBox="0 0 460 360" style="max-width:460px;display:block;margin:0 auto;">
            {node(20, 15, 200, "🔎 Criar Query com IA", "WIQL por descrição")}
            {node(240, 15, 200, "📘 Manual de Testes", "Reprodução em UAT")}
            {node(20, 90, 200, "🗄️ Documentos Armazenados", "Excluir é só do dono")}
            {node(240, 90, 200, "🧠 Mapa Mental", "Árvore navegável")}
            {node(20, 165, 200, "📊 Relatório de Testes", "Status real do board")}
            {node(240, 165, 200, "🐛 Criar Bug", "Com evidências em imagem")}
            {node(20, 240, 200, "🧱 Criar Work Item", "Qualquer tipo, campos dinâmicos")}
            {node(240, 240, 200, "🔌 Testes de API", "IA/Postman → execução → evidências")}
            {node(130, 315, 200, "🛡️ Administração", "Permissões e Logs")}
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
        self._render_flash_message()
        self._render_aviso_ia()

        username = st.session_state.get(SESSION_USER_KEY, "")
        is_owner = username == self.config.owner_username
        if (
            not is_owner
            and not self.state.get('_pat_notice_visto_nesta_sessao')
            and not self._pat_notice_ja_dispensado(username)
            and not self.state.get('show_interrupt_modal')
            and not self.state.get('show_new_analysis_modal')
        ):
            aviso_pat_compartilhado_modal(
                lambda: self._marcar_pat_notice_dispensado(username),
                tem_testes_api=self._get_permission_cached("testes_api"),
            )

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

        # Rolagem pro topo de uma vez só — usada tanto após confirmar um
        # Bug no modal quanto após confirmar "Criar outro Bug". Marcada
        # logo depois de cada uma dessas ações, disparada aqui na próxima
        # renderização, e imediatamente resetada pra não repetir em toda
        # renderização seguinte. Mira primeiro na âncora da mensagem de
        # sucesso (bug-sucesso-anchor) — existe só logo após criar um Bug.
        # Se não achar (ex.: depois de "Criar outro Bug", quando a
        # mensagem de sucesso já foi limpa), tenta bug-form-top-anchor
        # (topo fixo da página de Criar Bug). Só em último caso cai pro
        # topo cego da janela — porque o topo real da página (Config. do
        # Azure DevOps / PAT) fica ACIMA de onde o formulário aparece,
        # então rolar só até o topo não deixava os campos visíveis.
        if self.state.get('scroll_to_top_pending'):
            self.state.set('scroll_to_top_pending', False)
            st.markdown(
                """
                <svg onload="
                    var alvo = window.parent.document.getElementById('bug-sucesso-anchor')
                        || window.parent.document.getElementById('bug-form-top-anchor');
                    if (alvo) { alvo.scrollIntoView({behavior: 'smooth', block: 'start'}); }
                    else {
                        window.parent.scrollTo({top: 0, behavior: 'smooth'});
                        var m = window.parent.document.querySelector('.main');
                        if(m) m.scrollTo({top: 0, behavior: 'smooth'});
                    }
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

        if self.state.get('show_wiql_generation_page'):
            self._wiql_generation_page()
            return

        if self.state.get('show_manual_page'):
            self._manual_generation_page()
            return

        if self.state.get('show_document_store_page'):
            self._document_store_page()
            return

        if self.state.get('show_mindmap_page'):
            self._mind_map_page()
            return

        if self.state.get('show_bug_page'):
            self._bug_creation_page()
            return

        if self.state.get('show_work_item_page'):
            self._work_item_creation_page()
            return

        if self.state.get('show_api_tests_page'):
            self._api_tests_page()
            return

        # Daqui pra baixo é o assistente de QA (Passos 1–6). Quem não tem
        # essa permissão nunca deve cair aqui: vai direto pra área que
        # tem, ou vê uma tela explicando que não tem acesso a nada.
        if not self._get_permission_cached("assistente_qa"):
            self._render_sem_assistente()
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
