"""
Página "🔌 Testes de API" — Fase 1: definição (import Postman ou manual),
documentos de contexto, execução em Python e evidências (.md, .pdf, .zip,
Documentos Armazenados). Sem integração com o Azure DevOps ainda (Fase 2).

É um mixin: UserInterface herda daqui e chama `_api_tests_page()` no
`run()`. Todo estado fica em chaves `api_*` do SessionState.
"""
import json
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from qa_testgen.domain.models.api_test import (
    ASSERTION_TYPES, ASSERTION_LABELS, HTTP_METHODS, ApiTestCase,
)
from qa_testgen.infrastructure.api_discovery import montar_sondas, analisar as analisar_sondas
from qa_testgen.infrastructure.api_evidence import ApiEvidenceBuilder
from qa_testgen.infrastructure.api_to_assistant import converter_bateria, resultados_para_test_run
from qa_testgen.infrastructure.api_test_runner import ApiTestRunner
from qa_testgen.config import TZ_BR
from qa_testgen.infrastructure.document_processor import DocumentProcessor
from qa_testgen.infrastructure.document_store import (
    DocumentStore, DocumentStoreError, AppSettingsStore, CONFIG_API_TESTS_MODO_EXECUCAO,
)
from qa_testgen.infrastructure.pdf_report import PdfReportGenerator
from qa_testgen.infrastructure.postman_importer import PostmanImporter, PostmanImportError
from qa_testgen.ui.auth import SESSION_USER_KEY, log_action
from qa_testgen.ui.dialogs import confirm_new_api_run_modal, confirm_leave_api_tests_modal


API_TESTS_STATE_DEFAULTS = {
    'show_api_tests_page': False,
    'api_etapa': '1. Definição',
    'api_projeto': '',
    'api_ambiente': 'Homologação',
    'api_base_url': '',
    'api_timeout': 30,
    'api_variaveis': [],        # [{"nome","valor","secreto"}]
    'api_segredos': {},         # {nome: valor} — só em sessão, nunca em evidência
    'api_casos': [],            # [ApiTestCase.to_dict()]
    'api_contexto': '',
    'api_observacoes': '',
    'api_docs_nomes': [],
    'api_docs_texto': '',
    'api_resultados': None,     # [ApiCaseResult]
    'api_imagens': {},          # {case_id: [(nome, bytes)]}
    'api_md': None,
    'api_pdf': None,
    'api_zip': None,
    'api_ia_especificacao': '',
    'api_ia_ultimo_erro': None,
    'api_ia_fonte': '🎯 Work Item(s) do Azure DevOps',
    'api_wi_board_items': [],    # Work Items encontrados no board (pra escolher)
    'api_work_items': [],        # [{id, title, type}] escolhidos — também servem pra Fase 2 (vínculo)
    'api_ia_especificacao_wi': '',  # texto montado a partir dos Work Items escolhidos
    'api_ia_observacoes': '',
    'api_baixado': False,       # algum download/salvamento já foi feito nesta geração
    'api_test_run_pendente': None,
    'api_test_run_registrado': None,  # {titulo: {outcome, comentario}} + meta, pra registrar Test Run apos o Passo 7
    'api_modo_execucao': None,  # cache da configuração global (navegador|servidor)
    'api_browser_job': None,    # execução em andamento no navegador {run_id, casos, variaveis, timeout_s}
    'api_probe_job': None,      # reconhecimento da API em andamento (navegador)
    'api_reconhecimento': None, # último resultado do reconhecimento {observacoes, tabela, rotas_reais}
    'show_new_api_run_modal': False,
    'show_leave_api_modal': False,
}

_ETAPAS = ['1. Definição', '2. Execução', '3. Evidências']

# Componente que executa as requisições NO NAVEGADOR do usuário (HTML puro em
# ui/components/api_browser_runner). Motivo: WAFs como o CloudFront do HML
# bloqueiam IPs de provedores de nuvem (Streamlit Cloud, n8n na Oracle) mas
# aceitam o IP de quem usa o app — o mesmo do Postman.
_BROWSER_RUNNER = components.declare_component(
    "api_browser_runner", path=str(Path(__file__).resolve().parent / "components" / "api_browser_runner"),
)
MODOS_EXECUCAO = {"navegador": "Navegador do usuário (contorna bloqueio de WAF)", "servidor": "Servidor do app (chamada direta)"}
MODO_EXECUCAO_PADRAO = "navegador"
_AMBIENTES = ['Homologação', 'Produção']


class ApiTestsPageMixin:

    # ------------------------------------------------------------------ util
    def _api_reset(self):
        for chave, valor in API_TESTS_STATE_DEFAULTS.items():
            if chave == 'show_api_tests_page':
                continue
            self.state.set(chave, json.loads(json.dumps(valor)) if isinstance(valor, (dict, list)) else valor)
        for k in list(st.session_state.keys()):
            if isinstance(k, str) and k.startswith('apiw_'):
                del st.session_state[k]

    def _api_variaveis_resolvidas(self) -> dict:
        """Nome -> valor, juntando as normais com as secretas (só em memória)."""
        valores = {v['nome']: v.get('valor', '') for v in self.state.get('api_variaveis') or [] if v.get('nome')}
        valores.update({k: v for k, v in (self.state.get('api_segredos') or {}).items() if v})
        valores.setdefault('base_url', self.state.get('api_base_url') or '')
        if self.state.get('api_base_url'):
            valores['base_url'] = self.state.get('api_base_url')
        return valores

    def _api_lista_segredos(self) -> list:
        segredos = [v for v in (self.state.get('api_segredos') or {}).values() if v]
        resultados = self.state.get('api_resultados') or []
        # tokens extraídos em execução (ex.: auth_token) também são segredo
        for r in resultados:
            for k, v in (r.request_headers or {}).items():
                if k.lower() == 'authorization' and v:
                    segredos.append(v.split(' ', 1)[-1])
        return segredos

    def _api_modo_execucao(self) -> str:
        """
        Modo de execução dos Testes de API — configuração GLOBAL definida pelo
        dono em Administração → Configurações (vale pra todos). Lida uma vez
        por sessão; sem Turso configurado, cai no padrão.
        """
        if self.state.get('api_modo_execucao') is None:
            modo = MODO_EXECUCAO_PADRAO
            if getattr(self.config, 'turso_database_url', ''):
                try:
                    store = AppSettingsStore(self.config.turso_database_url, self.config.turso_auth_token)
                    store.ensure_schema()
                    modo = store.get(CONFIG_API_TESTS_MODO_EXECUCAO, MODO_EXECUCAO_PADRAO) or MODO_EXECUCAO_PADRAO
                except Exception:
                    modo = MODO_EXECUCAO_PADRAO
            self.state.set('api_modo_execucao', modo if modo in MODOS_EXECUCAO else MODO_EXECUCAO_PADRAO)
        return self.state.get('api_modo_execucao')

    def _api_marcar_baixado(self):
        self.state.set('api_baixado', True)

    def _api_invalidar_evidencias(self):
        self.state.set('api_baixado', False)
        self.state.set('api_md', None)
        self.state.set('api_pdf', None)
        self.state.set('api_zip', None)

    # ------------------------------------------------------------------ page
    def _api_tests_page(self):
        st.subheader("🔌 Testes de API")
        if st.button("← Voltar", key="btn_api_back"):
            # Mesmo guarda das outras telas: relatório gerado e não baixado
            # pede confirmação antes de sair.
            self._navigate_or_confirm({'show_api_tests_page': False})
        if not self._get_permission_cached("testes_api"):
            st.error("❌ Você não tem permissão pra acessar esta área.")
            return

        st.caption(
            "Executa testes de API (importados do Postman ou criados aqui) direto do app, "
            "sem Node/Newman, e gera evidências organizadas — request, response e asserções por caso, "
            "relatório em Markdown e PDF no padrão QA TestGen, e pacote .zip pra arquivar. "
            "Integração com o Azure DevOps (vincular a Test Cases e registrar resultado) vem na próxima fase."
        )
        self._api_render_ajuda()

        if self.state.get('show_new_api_run_modal'):
            confirm_new_api_run_modal(self._api_reset)
        if self.state.get('show_leave_api_modal'):
            confirm_leave_api_tests_modal(self._api_reset)

        col_e, col_n = st.columns([4, 1])
        with col_e:
            self._api_render_etapas()
        with col_n:
            if st.button("🔄 Nova execução", key="btn_api_reset", width="stretch",
                         disabled=not (self.state.get('api_casos') or self.state.get('api_resultados'))):
                self.state.set('show_new_api_run_modal', True)
                st.rerun()

        st.divider()
        etapa = self.state.get('api_etapa') or _ETAPAS[0]
        if etapa == _ETAPAS[0]:
            self._api_render_definicao()
        elif etapa == _ETAPAS[1]:
            self._api_render_execucao()
        else:
            self._api_render_evidencias()

    def _api_render_etapas(self):
        """
        Barra de etapas no mesmo visual da barra de progresso do assistente
        (Passos 1–7): a etapa atual em destaque, as outras como botões.
        """
        atual = self.state.get('api_etapa') or _ETAPAS[0]
        cols = st.columns(len(_ETAPAS))
        for col, (rotulo, etapa) in zip(cols, zip(["🧾 1. Definição", "▶️ 2. Execução", "📦 3. Evidências"], _ETAPAS)):
            with col:
                if etapa == atual:
                    st.markdown(
                        f"<div style='padding:.45rem .5rem;border-radius:4px;background:#d0e8ff;"
                        f"color:#0a4f8a;text-align:center;font-weight:700;border:1.5px solid #4A90D9'>"
                        f"{rotulo}</div>",
                        unsafe_allow_html=True,
                    )
                else:
                    pend = self._api_pendencias_para(etapa)
                    if st.button(rotulo, key=f"api_nav_{etapa[0]}", width="stretch", disabled=bool(pend),
                                 help=("Antes de ir para esta etapa: " + " · ".join(pend)) if pend else None):
                        self.state.set('api_etapa', etapa)
                        st.rerun()

    def _api_pendencias_para(self, etapa: str) -> list:
        """
        O que ainda falta pra poder entrar em `etapa` — em linguagem de
        usuário. Lista vazia = pode ir.
        """
        if etapa == _ETAPAS[1]:
            pend = list(self._api_validar_definicao())
            if self.state.get('api_browser_job'):
                pend.append("Há uma execução em andamento no navegador — aguarde terminar ou cancele.")
            return pend
        if etapa == _ETAPAS[2]:
            pend = []
            if not self.state.get('api_resultados'):
                pend.append("Execute os testes na etapa 2. Execução (as evidências são geradas a partir do resultado).")
            return pend
        return []

    def _api_tem_relatorio_nao_baixado(self) -> bool:
        return bool(self.state.get('api_md')) and not self.state.get('api_baixado')

    def _api_botao_proxima_etapa(self, destino: str, rotulo: str, key: str):
        st.divider()
        pend = self._api_pendencias_para(destino)
        if pend:
            st.warning("**Antes de seguir para " + destino + ", falta:**\n\n" + "\n".join(f"- {p}" for p in pend))
        if st.button(rotulo, key=key, type="primary", width="stretch", disabled=bool(pend)):
            self.state.set('api_etapa', destino)
            st.rerun()

    def _api_render_ajuda(self):
        with st.expander("ℹ️ O que é e como usar esta área"):
            st.markdown(
                "**O que é:** um executor de testes de API dentro do app. Você descreve as requisições "
                "(método, URL, headers, body) e o que a resposta precisa ter (status, campos, valores), o app "
                "chama a API de verdade, confere cada regra e monta a evidência — sem precisar do Postman "
                "aberto nem de Node/Newman.\n\n"
                "**As 3 etapas** (seletor logo abaixo):\n"
                "1. **Definição** — nome, ambiente, Base URL, de onde vêm os casos, variáveis e (opcional) contexto.\n"
                "2. **Execução** — roda os casos habilitados, na ordem, e mostra o resultado de cada asserção.\n"
                "3. **Evidências** — é aqui que ficam os **botões de download**: `RELATORIO.md`, `RELATORIO.pdf` "
                "(padrão QA TestGen), `.zip` com uma pasta por caso (request, response, resultado e seus prints) e "
                "a definição `.json` para repetir a bateria depois.\n\n"
                "**De onde vêm os casos** (etapa 1, \"Origem dos testes\"):\n"
                "- 🤖 **Gerar com IA** — escolha o(s) **Work Item(s) do Azure DevOps** (a Descrição e os Critérios de Aceite "
                "viram a especificação) ou cole o texto / anexe documentos em Contexto, "
                "informe a Base URL e clique em *Gerar casos com IA*: a bateria inteira (sucesso, validações, credenciais "
                "inválidas, regras de negócio, token) aparece pronta no editor. Senhas nunca vão pra IA — ela só declara "
                "as variáveis e você preenche.\n"
                "- 📮 **Collection do Postman** — no Postman: clique nos três pontos da collection → **Export** → "
                "formato *Collection v2.1* → salva um `*.postman_collection.json`. O **environment** é opcional: "
                "aba *Environments* → três pontos → **Export** → `*.postman_environment.json` (traz as variáveis). "
                "Os `pm.test` mais comuns são convertidos em asserções automaticamente; o que não der vira um aviso "
                "amarelo no caso, pra você completar na tela.\n"
                "- 🧩 **Definição salva** — o `.json` que a etapa 3 exporta. Serve pra repetir a mesma bateria "
                "amanhã sem depender do Postman.\n"
                "- ✍️ **Criar manualmente** — monta cada caso do zero na própria tela.\n\n"
                "**Variáveis:** qualquer `{{nome}}` em URL, headers ou body é substituído pelo valor da tabela. "
                "`{{base_url}}` é sempre o campo Base URL. Variáveis **secretas** (senhas, tokens) são pedidas em campo "
                "de senha, ficam só nesta sessão e saem **mascaradas** de toda evidência. Depois de preencher, clique em "
                "**💾 Salvar variáveis** (a tabela e as senhas só são gravadas com esse botão). Um caso pode **extrair** "
                "um valor da resposta pra uma variável (ex.: `data.token` → `auth_token`) e os casos seguintes usam "
                "`{{auth_token}}` — por isso a ordem importa.\n\n"
                "**🔎 Reconhecer a API** (dentro de Gerar com IA): antes de gerar, o app faz chamadas sem credencial "
                "nas rotas citadas na especificação e descobre a rota real (ex.: /api/v1/...), o formato dos erros "
                "(chave i18n, errors.<campo>) e quais rotas exigem token — e escreve isso nas Observações pra IA não "
                "inventar. Use sempre que a User Story não trouxer os endpoints.\n\n"
                "**Contexto (opcional):** texto livre e documentos de apoio que entram no relatório como seção "
                "\"Contexto\" — ex.: qual User Story está sendo testada, que credenciais/perfil foram usados, o que "
                "se espera. Não altera a execução; é só documentação.\n\n"
                "**De onde saem as chamadas:** conforme configuração do administrador — do **seu navegador** (padrão; "
                "mesmo IP do Postman, contorna bloqueios de WAF que barram servidores em nuvem) ou do servidor do app.\n\n"
                "**Levar para o Azure DevOps** (etapa 3, \"Levar para o assistente\"): cada caso vira um Caso de Teste, "
                "com Matriz e um Plano (uma suíte por endpoint); você cai no Passo 5 e usa o Passo 7 como sempre — "
                "vinculando a Work Items (o Work Item escolhido na geração já vem sugerido), sem Work Items ou "
                "reconciliando. Depois do Passo 7, o app oferece registrar a execução como **Test Run** oficial "
                "(Passed/Failed por caso + PDF anexado), visível na aba Execute do Test Plan.\n\n"
                "**Regras:** um caso sem asserção é reprovado (o mínimo é o status HTTP esperado); casos "
                "desabilitados não rodam e aparecem como \"Não Executado\"; nada é enviado ao Azure DevOps nesta versão."
            )

    # ------------------------------------------------------------ 1. Definição
    def _api_render_definicao(self):
        st.markdown("##### 🧾 Identificação")
        c1, c2, c3 = st.columns([3, 1.4, 1])
        with c1:
            self.state.set('api_projeto', st.text_input(
                "Nome do projeto / execução *", value=self.state.get('api_projeto') or '',
                placeholder="Ex.: Login API - Nova 360", key="apiw_projeto"))
        with c2:
            amb = st.radio("Ambiente *", _AMBIENTES, index=_AMBIENTES.index(self.state.get('api_ambiente') or 'Homologação'),
                           horizontal=True, key="apiw_ambiente")
            self.state.set('api_ambiente', amb)
        with c3:
            self.state.set('api_timeout', int(st.number_input("Timeout (s)", min_value=5, max_value=300,
                                                                value=int(self.state.get('api_timeout') or 30), key="apiw_timeout")))
        self.state.set('api_base_url', st.text_input(
            "Base URL * (vira a variável `{{base_url}}`)", value=self.state.get('api_base_url') or '',
            placeholder="https://api.exemplo.com.br", key="apiw_base_url").strip().rstrip('/'))

        st.divider()
        st.markdown("##### 📥 Origem dos testes")
        origem = st.radio("Como definir os casos?",
                          ["🤖 Gerar com IA (User Story / Work Item / documento)", "📮 Importar collection do Postman",
                           "🧩 Importar definição salva (.json deste módulo)", "✍️ Criar manualmente"],
                          horizontal=True, key="apiw_origem", label_visibility="collapsed")
        if origem.startswith("🤖"):
            self._api_render_geracao_ia()
        elif origem.startswith("📮"):
            cc, ce = st.columns(2)
            with cc:
                col_file = st.file_uploader("Collection (*.postman_collection.json) *", type=["json"], key="apiw_col_file")
            with ce:
                env_file = st.file_uploader("Environment (*.postman_environment.json) — opcional", type=["json"], key="apiw_env_file")
            casos_atuais = len(self.state.get('api_casos') or [])
            substituir = st.checkbox(
                "Começar do zero: apagar os casos já listados e ficar só com os desta collection",
                value=True, key="apiw_col_replace",
                help="Desmarque para ADICIONAR os casos desta collection ao fim da lista atual "
                     "(ex.: juntar Login + Cadastro numa mesma bateria).",
                disabled=casos_atuais == 0,
            )
            if casos_atuais:
                st.caption(f"Há {casos_atuais} caso(s) na lista agora.")
            if st.button("📥 Importar", key="azure_blue_btn_api_import", disabled=col_file is None, width="stretch"):
                self._api_importar_postman(col_file, env_file, substituir)
                st.rerun()
        elif origem.startswith("🧩"):
            def_file = st.file_uploader("Definição (.json exportado na etapa Evidências)", type=["json"], key="apiw_def_file")
            if st.button("📥 Carregar definição", key="azure_blue_btn_api_import_def", disabled=def_file is None, width="stretch"):
                self._api_importar_definicao(def_file)
                st.rerun()
        else:
            if st.button("➕ Adicionar caso", key="btn_api_add_case_top"):
                self._api_adicionar_caso()
                st.rerun()

        st.divider()
        self._api_render_variaveis()

        st.divider()
        st.markdown("##### 📄 Contexto (opcional)")
        docs = st.file_uploader("Documentos de apoio (PDF, DOCX, TXT ou CSV) — entram no relatório como referência",
                                type=["pdf", "docx", "txt", "csv"], accept_multiple_files=True, key="apiw_docs")
        if docs:
            nomes = [d.name for d in docs]
            if nomes != (self.state.get('api_docs_nomes') or []):
                with st.spinner("Extraindo texto dos documentos..."):
                    try:
                        texto = DocumentProcessor.extract_plain_text_multi(docs)
                    except Exception as error:
                        texto = ""
                        st.warning(f"Não foi possível extrair texto: {error}")
                self.state.set('api_docs_nomes', nomes)
                self.state.set('api_docs_texto', texto or "")
            st.caption("✅ " + ", ".join(self.state.get('api_docs_nomes') or []))
        self.state.set('api_contexto', st.text_area(
            "Contexto da execução (texto livre — vira a seção \"Contexto\" do relatório; não altera a execução)",
            value=self.state.get('api_contexto') or '', height=90,
            placeholder="Ex.: Testes da User Story de Login (POST /api/v1/auth/login) na prévia de HML, com credenciais de colaborador. Objetivo: validar 200/401/422 e o token.",
            help="Use para quem for ler o relatório entender o que estava sendo testado, por quê, com qual perfil/credencial e em que situação do projeto.",
            key="apiw_contexto"))

        st.divider()
        self._api_render_casos()
        self._api_botao_proxima_etapa(_ETAPAS[1], "➡️ Próxima etapa: 2. Execução", "btn_api_next_1")

    def _api_render_geracao_ia(self):
        """
        Gera a bateria a partir de uma especificação (texto colado e/ou
        documentos de contexto) + o mínimo obrigatório. A IA devolve casos
        no formato do módulo; segredos ficam pra pessoa preencher.
        """
        st.caption(
            "Cole a User Story / descrição do Work Item (ou anexe documentos em \"Contexto\", mais abaixo). "
            "Obrigatório: **Base URL** (acima) e a **especificação**. A IA monta os casos (sucesso, validações, "
            "credenciais inválidas, regras de negócio, uso do token) e você só revisa e executa."
        )
        fontes = ["🎯 Work Item(s) do Azure DevOps", "✍️ Texto colado"]
        fonte = st.radio("Fonte da especificação", fontes,
                         index=fontes.index(self.state.get('api_ia_fonte') or fontes[0]),
                         horizontal=True, key="apiw_ia_fonte")
        self.state.set('api_ia_fonte', fonte)
        if fonte.startswith("🎯"):
            self._api_render_fonte_work_items()
        else:
            self.state.set('api_ia_especificacao', st.text_area(
                "Especificação (User Story, critérios de aceite, descrição do endpoint) *",
                value=self.state.get('api_ia_especificacao') or '', height=220, key="apiw_ia_spec",
                placeholder="Ex.: User Story — Login. Endpoint POST /api/auth/login. Dados: email (obrigatório, formato válido), "
                            "password (obrigatório). Retorno 200: { data: { token, token_type, user } } ... 422 ... 401 ... 403 ...",
            ))
        self._api_render_reconhecimento(docs_txt_disponivel=bool(self.state.get('api_docs_texto')))
        self.state.set('api_ia_observacoes', st.text_area(
            "Observações / dicas pra IA (opcional)",
            value=self.state.get('api_ia_observacoes') or '', height=80, key="apiw_ia_obs",
            placeholder="Ex.: a rota real em HML é /api/v1/auth/login; existe usuário inativo de teste; não testar logout.",
        ))
        docs_txt = self.state.get('api_docs_texto') or ''
        if docs_txt:
            st.caption(f"📄 {len(self.state.get('api_docs_nomes') or [])} documento(s) de contexto também serão enviados à IA ({len(docs_txt)} caracteres).")
        substituir = st.checkbox("Começar do zero: apagar os casos já listados e ficar só com os gerados", value=True,
                                 key="apiw_ia_replace", disabled=not (self.state.get('api_casos') or []))
        espec_base = self._api_especificacao_efetiva()
        pronto = bool(espec_base.strip() or docs_txt) and \
            (self.state.get('api_base_url') or '').startswith(('http://', 'https://'))
        if not pronto:
            st.info("Preencha a Base URL e escolha o(s) Work Item(s) — ou cole a especificação / anexe documentos — pra habilitar a geração.")
        st.button("🤖 Gerar casos com IA", key="azure_blue_btn_api_gen_ia", width="stretch",
                  disabled=(not pronto) or self.state.get('is_processing'),
                  on_click=self.trigger_action, args=("api_generate_ai",))
        erro_ia = self.state.get('api_ia_ultimo_erro')
        if erro_ia:
            amigavel = "limite de uso dos provedores de IA (cota por minuto/dia)" if any(t in erro_ia.lower() for t in ("too many", "rate limit", "429", "quota")) else "resposta fora do formato esperado"
            st.error(
                f"❌ A última geração falhou — {amigavel}. Espere 1–2 minutos e clique de novo em **Gerar casos com IA** "
                "(a especificação e as observações continuam preenchidas)."
            )
            with st.expander("Detalhe técnico do erro (por provedor)"):
                st.code(erro_ia, language="text")
        if self.state.get('current_action') == 'api_generate_ai' and not self.state.get('show_interrupt_modal'):
            self.state.set('api_ia_ultimo_erro', None)
            # Limpa a ação antes da chamada (que pode levar 1 min): se a pessoa
            # clicar de novo e o Streamlit reiniciar o script, não dispara outra
            # chamada à IA por cima desta.
            self.clear_action()
            self.state.set('is_processing', True)
            try:
                especificacao = self._api_especificacao_efetiva().strip()
                if docs_txt:
                    # Limite curto de propósito: cada chamada à IA conta contra a
                    # cota por minuto dos provedores — documento inteiro derruba a geração.
                    especificacao += "\n\n=== DOCUMENTOS DE CONTEXTO (trecho) ===\n" + docs_txt[:6000]
                with st.spinner("A IA está montando a bateria de testes (isso pode levar até um minuto)..."):
                    resp = self.client.trigger_api_test_generation(
                        especificacao, self.state.get('api_base_url'), self.state.get('api_ambiente'),
                        self.state.get('api_ia_observacoes') or '', self.state.get('api_variaveis') or [],
                    )
                self._api_aplicar_geracao_ia(resp, substituir)
            except Exception as error:
                self.state.set('api_ia_ultimo_erro', str(error))
                self._flash_error(f"Não foi possível gerar os casos com IA: {error}")
            self.state.set('is_processing', False)
            st.rerun()

    def _api_render_reconhecimento(self, docs_txt_disponivel: bool = False):
        """
        "Reconhecer a API": sondagens sem credencial (corpo vazio) nas rotas
        citadas na especificação — ou nas convencionais — pelo mesmo caminho
        da bateria (navegador/servidor). O resultado vira texto nas
        Observações, pra IA não inventar rota nem formato de erro.
        """
        base_ok = (self.state.get('api_base_url') or '').startswith(('http://', 'https://'))
        espec = self._api_especificacao_efetiva()
        st.markdown("**🔎 Reconhecer a API** — descobre rotas reais, formato de erro e rotas protegidas, e preenche as Observações sozinho.")
        c1, c2 = st.columns([1, 2])
        with c1:
            if st.button("🔎 Reconhecer a API", key="azure_blue_btn_api_probe", width="stretch",
                         disabled=(not base_ok) or self.state.get('is_processing') or bool(self.state.get('api_probe_job'))):
                sondas = montar_sondas(espec, self.state.get('api_base_url'))
                if self._api_modo_execucao() == "navegador":
                    self.state.set('api_probe_job', {"run_id": str(uuid.uuid4()), "sondas": sondas,
                                                     "casos": [{k: s[k] for k in ("id", "nome", "metodo", "url", "headers", "body", "extrair")} for s in sondas],
                                                     "variaveis": {}, "timeout_s": 20})
                else:
                    self._api_concluir_reconhecimento(sondas, self._api_sondar_servidor(sondas))
                st.rerun()
        with c2:
            if not base_ok:
                st.caption("Informe a Base URL (acima) pra habilitar.")
            else:
                st.caption("Sem credencial e sem gravar nada: só chamadas com corpo vazio pra ver como a API responde.")

        job = self.state.get('api_probe_job')
        if job:
            st.info("⏳ Reconhecendo a API pelo seu navegador…")
            retorno = _BROWSER_RUNNER(**{k: job[k] for k in ("run_id", "casos", "variaveis", "timeout_s")},
                                      key=f"api_probe_runner_{job['run_id']}", default=None)
            if retorno and retorno.get("run_id") == job["run_id"]:
                self.state.set('api_probe_job', None)
                self._api_concluir_reconhecimento(job["sondas"], retorno.get("respostas") or [])
                st.rerun()
            elif st.button("✖ Cancelar", key="btn_api_probe_cancel"):
                self.state.set('api_probe_job', None)
                st.rerun()

        rec = self.state.get('api_reconhecimento')
        if rec:
            with st.expander(f"🔎 Resultado do reconhecimento ({len(rec['tabela'])} sondagem(ns))", expanded=False):
                st.dataframe(pd.DataFrame(rec['tabela']), width="stretch", hide_index=True)

    def _api_sondar_servidor(self, sondas: list) -> list:
        runner = ApiTestRunner({}, timeout=20)
        res = runner.executar([ApiTestCase(id=s['id'], nome=s['nome'], metodo=s['metodo'], url=s['url'],
                                           headers=s['headers'], body=s['body']) for s in sondas])
        return [{"status": r.status_code, "headers": r.response_headers, "body": r.response_body, "erro": r.erro} for r in res]

    def _api_concluir_reconhecimento(self, sondas: list, respostas: list):
        rec = analisar_sondas(sondas, respostas)
        self.state.set('api_reconhecimento', rec)
        atual = (self.state.get('api_ia_observacoes') or '').strip()
        # substitui um bloco anterior de reconhecimento, preserva o que a pessoa escreveu
        marcador = "[Reconhecimento automático da API]"
        if marcador in atual:
            atual = atual.split(marcador)[0].rstrip()
        novo = (atual + "\n\n" if atual else "") + marcador + "\n" + rec["observacoes"]
        self.state.set('api_ia_observacoes', novo)
        st.session_state.pop('apiw_ia_obs', None)   # o text_area passa a mostrar o texto novo
        self._flash_success(f"Reconhecimento concluído: {len(rec['rotas_reais'])} rota(s) confirmada(s). Observações preenchidas — revise e clique em Gerar casos com IA.")

    def _api_especificacao_efetiva(self) -> str:
        """Texto que vai pra IA: dos Work Items escolhidos ou colado, conforme a fonte."""
        if (self.state.get('api_ia_fonte') or '').startswith("🎯"):
            return self.state.get('api_ia_especificacao_wi') or ''
        return self.state.get('api_ia_especificacao') or ''

    def _api_render_fonte_work_items(self):
        """
        Escolha de Work Item(s) do Azure DevOps como especificação — mesmo
        fluxo do Passo 1/Manual (conexão, Area Path, busca no board). A
        Description + Critérios de Aceite viram o texto enviado à IA, e os
        itens escolhidos ficam guardados (servem também pro vínculo com o
        Azure DevOps na próxima fase).
        """
        conn = self._setup_azure_devops_connection(show_area_path_picker=False)
        if conn is None:
            return
        ado_client, _ado_org, ado_project, _default_ap = conn

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
            area_paths = st.multiselect("Area Path(s) (vazio = projeto inteiro)", options=area_path_options,
                                        disabled=self.state.get('is_processing'), key="apiw_wi_area_paths")
        with col_btn:
            st.button("🔄 Buscar Work Items do Board", disabled=self.state.get('is_processing'),
                      key="azure_blue_btn_api_fetch_wi", on_click=self.trigger_action, args=("api_fetch_wi",), width="stretch")
        if self.state.get('current_action') == 'api_fetch_wi' and not self.state.get('show_interrupt_modal'):
            try:
                paths = area_paths or [ado_project]
                with st.spinner(f"Buscando Work Items em {len(paths)} Area Path(s)..."):
                    por_id = {}
                    for ap in paths:
                        for item in ado_client.fetch_work_items_by_area_path(ap, excluded_states=set()):
                            por_id[item["id"]] = item
                self.state.set('api_wi_board_items', list(por_id.values()))
                if not por_id:
                    self._flash_warning("Nenhum Work Item encontrado nessas Area Paths.")
            except Exception as error:
                self._flash_error(f"Não foi possível buscar Work Items: {error}")
            self.clear_action()
            st.rerun()

        board_items = self.state.get('api_wi_board_items') or []
        if not board_items:
            st.caption("Busque os Work Items do board pra escolher qual(is) viram a especificação.")
            return
        rotulos = {f"{i['id']} - {i['title']} ({i['type']}, {i['state']})": i for i in board_items}
        escolhidos = st.multiselect("Work Item(s) que descrevem a API a testar *", options=list(rotulos.keys()),
                                    key="apiw_wi_select", disabled=self.state.get('is_processing'))
        selecionados = [rotulos[r] for r in escolhidos]
        ids = [wi['id'] for wi in selecionados]
        if ids != [wi['id'] for wi in (self.state.get('api_work_items') or [])]:
            texto = ""
            if selecionados:
                try:
                    with st.spinner(f"Lendo {len(selecionados)} Work Item(s)..."):
                        detalhes = ado_client.get_work_items_full_details(ids)
                    partes = []
                    for wi in detalhes:
                        parte = f"===== WORK ITEM {wi['id']} - {wi['title']} ({wi['type']}) =====\n"
                        if wi.get('description'):
                            parte += f"Descrição:\n{wi['description']}\n"
                        if wi.get('acceptance_criteria'):
                            parte += f"\nCritérios de Aceite:\n{wi['acceptance_criteria']}\n"
                        parte += f"===== FIM DO WORK ITEM {wi['id']} ====="
                        partes.append(parte)
                    texto = "\n\n".join(partes)
                except Exception as error:
                    self._flash_error(f"Não foi possível ler os Work Items: {error}")
            self.state.set('api_work_items', [{"id": wi['id'], "title": wi['title'], "type": wi.get('type', '')} for wi in selecionados])
            self.state.set('api_ia_especificacao_wi', texto)
            if selecionados and not self.state.get('api_projeto'):
                self.state.set('api_projeto', selecionados[0]['title'][:80])
                st.session_state.pop('apiw_projeto', None)
        if self.state.get('api_ia_especificacao_wi'):
            with st.expander(f"👁️ Ver a especificação lida ({len(self.state.get('api_ia_especificacao_wi'))} caracteres)"):
                st.text(self.state.get('api_ia_especificacao_wi')[:6000])

    def _api_aplicar_geracao_ia(self, resp: dict, substituir: bool):
        """Converte a resposta da IA em casos do módulo e mescla variáveis."""
        novos, invalidos = [], 0
        for c in resp.get('casos') or []:
            metodo = str(c.get('metodo', 'GET')).upper()
            if metodo not in HTTP_METHODS or not str(c.get('url') or '').strip():
                invalidos += 1
                continue
            # A IA às vezes devolve headers como objeto {"Accept": "..."} em vez
            # de lista de {chave, valor}, e body como objeto em vez de string.
            headers = {}
            raw_headers = c.get('headers') or []
            if isinstance(raw_headers, dict):
                headers = {str(k).strip(): str(v or '') for k, v in raw_headers.items() if str(k).strip()}
            else:
                for h in raw_headers:
                    if isinstance(h, dict):
                        chave = h.get('chave') or h.get('key') or h.get('nome') or h.get('name')
                        if chave:
                            headers[str(chave).strip()] = str(h.get('valor', h.get('value', '')) or '')
            body = c.get('body')
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False)
            body = str(body or '')
            assercoes = [
                {"tipo": str(a.get('tipo')), "alvo": str(a.get('alvo') or ''), "valor": str(a.get('valor') if a.get('valor') is not None else ''), "descricao": str(a.get('descricao') or '')}
                for a in (c.get('assercoes') or []) if isinstance(a, dict) and str(a.get('tipo', '')).strip().lower() in ASSERTION_TYPES
            ]
            for a in assercoes:
                a['tipo'] = a['tipo'].strip().lower()
            if not any(a['tipo'] == 'status' for a in assercoes):
                assercoes.insert(0, {"tipo": "status", "alvo": "", "valor": "200", "descricao": "Status esperado (revise)"})
            caso = ApiTestCase(
                id=str(uuid.uuid4()), nome=str(c.get('nome') or f"Caso {len(novos) + 1}"), metodo=metodo,
                url=str(c.get('url')).strip(), headers=headers, body=body,
                descricao=str(c.get('descricao') or ''),
            ).to_dict()
            caso['assercoes'] = assercoes
            caso['extrair'] = [
                {"nome": str(e.get('nome')).strip(), "caminho": str(e.get('caminho') or '').strip()}
                for e in (c.get('extrair') or []) if isinstance(e, dict) and e.get('nome')
            ]
            novos.append(caso)
        if not novos:
            self._flash_error("A IA não devolveu nenhum caso válido. Revise a especificação e tente de novo.")
            return

        por_nome = {v['nome']: v for v in (self.state.get('api_variaveis') or [])}
        usadas = set()
        for c in novos:
            texto = " ".join([c['url'], c['body']] + list(c['headers'].values()))
            usadas.update(ApiTestRunner._RE_VAR.findall(texto))
        for v in resp.get('variaveis') or []:
            nome = str(v.get('nome') or '').strip()
            if not nome or nome == 'base_url':
                continue
            atual = por_nome.get(nome, {"nome": nome, "valor": "", "secreto": False})
            atual['secreto'] = bool(atual['secreto'] or v.get('secreto'))
            por_nome[nome] = atual
        extraidas = {e['nome'] for c in novos for e in c['extrair']}
        for nome in usadas - set(por_nome) - extraidas - {'base_url'}:
            por_nome[nome] = {"nome": nome, "valor": "", "secreto": any(t in nome.lower() for t in ('password', 'senha', 'token', 'secret'))}
        self.state.set('api_variaveis', list(por_nome.values()))
        if not self.state.get('api_projeto') and resp.get('nome_sugerido'):
            self.state.set('api_projeto', resp['nome_sugerido'])
            # o text_input guarda o valor vazio dele; sem isso, sobrescreve o nome no próximo render
            st.session_state.pop('apiw_projeto', None)
        self.state.set('api_casos', novos if substituir else (self.state.get('api_casos') or []) + novos)
        self.state.set('api_resultados', None)
        self._api_invalidar_evidencias()
        msg = f"{len(novos)} caso(s) gerado(s) pela IA. Revise as asserções e preencha as variáveis secretas."
        if invalidos:
            msg += f" {invalidos} caso(s) vieram inválidos e foram descartados."
        if resp.get('observacoes'):
            msg += f" Observações da IA: {resp['observacoes']}"
        self._flash_success(msg)
        for k in list(st.session_state.keys()):
            if isinstance(k, str) and k.startswith('apiw_') and k not in ('apiw_projeto', 'apiw_base_url', 'apiw_ambiente', 'apiw_timeout', 'apiw_origem', 'apiw_ia_spec', 'apiw_ia_obs', 'apiw_ia_replace', 'apiw_docs'):
                del st.session_state[k]

    def _api_render_variaveis(self):
        st.markdown("##### 🔤 Variáveis (`{{nome}}` em URL, headers e body)")
        variaveis = self.state.get('api_variaveis') or []
        origem = st.session_state.get('apiw_origem') or ''
        if not variaveis:
            # Quem chega aqui antes de ter casos não sabe se precisa digitar
            # algo. Diz de onde as variáveis vão vir, conforme a origem escolhida.
            if origem.startswith("🤖"):
                st.info(
                    "**Não precisa preencher nada aqui agora.** Ao clicar em **\"Gerar casos com IA\"** (acima), a IA "
                    "cria as variáveis sozinha (ex.: `valid_email`, `valid_password`) e marca as senhas como secretas. "
                    "Depois disso, volte aqui só pra informar os **valores**."
                )
            elif origem.startswith("📮"):
                st.info(
                    "**Não precisa preencher nada aqui agora.** As variáveis vêm da collection/environment do Postman ao "
                    "clicar em **\"Importar\"**. Depois disso, volte aqui só pra informar os valores que estiverem vazios."
                )
            elif origem.startswith("🧩"):
                st.info("**Não precisa preencher nada aqui agora.** As variáveis vêm junto da definição ao clicar em **\"Carregar definição\"** — só as senhas precisam ser digitadas de novo.")
            else:
                st.info(
                    "Adicione aqui só o que você usar como `{{nome}}` nos casos (ex.: `valid_email`). "
                    "`{{base_url}}` já existe (é o campo Base URL acima) — não precisa cadastrar."
                )
        else:
            st.info(
                "**O que fazer aqui:** preencha a coluna **Valor** das variáveis normais (ex.: `valid_email`) e os campos "
                "🔒 de senha logo abaixo. Deixe em branco as marcadas como **opcional** — são preenchidas pelo próprio "
                "teste (ex.: `auth_token`) ou usadas só por casos desabilitados."
            )
        st.caption("Marque **Secreto** para senhas/tokens: o valor é pedido abaixo, fica só nesta sessão e sai mascarado de toda evidência.")
        # Só na origem "Criar manualmente" a pessoa define variáveis à mão.
        # Nas outras (IA, Postman, definição) elas chegam prontas: a tabela
        # não aceita linha nova e só a coluna Valor é editável — evita que
        # alguém invente nome/segredo que nenhum caso usa.
        manual = origem.startswith("✍️")
        if not variaveis and not manual:
            st.caption("A tabela aparece aqui assim que os casos forem gerados/importados.")
            self.state.set('api_variaveis', [])
            self.state.set('api_segredos', {})
            return
        df = pd.DataFrame(variaveis or [{"nome": "", "valor": "", "secreto": False}], columns=["nome", "valor", "secreto"])
        df["valor"] = df.apply(lambda r: "" if r["secreto"] else r["valor"], axis=1)
        # Tudo dentro de um formulário: a tabela e os campos de senha só são
        # enviados ao clicar em "Salvar variáveis". Sem isso, a tabela grava
        # ao perder o foco e dispara um rerun no meio da digitação da senha
        # (ou o clique em "Próxima etapa" chega antes de a célula ser gravada)
        # — e o valor digitado some.
        form = st.form("apiw_vars_form", border=True)
        with form:
            edit = st.data_editor(
                df, num_rows="dynamic" if manual else "fixed", width="stretch", hide_index=True, key="apiw_vars_editor",
                disabled=[] if manual else ["nome", "secreto"],
                column_config={
                    "nome": st.column_config.TextColumn("Nome", required=True),
                    "valor": st.column_config.TextColumn("Valor (vazio se secreto)"),
                    "secreto": st.column_config.CheckboxColumn("Secreto", default=False),
                },
            )
            novas = []
            for _, row in edit.iterrows():
                nome = str(row.get("nome") or "").strip()
                if not nome:
                    continue
                secreto = bool(row.get("secreto"))
                valor = "" if secreto else str(row.get("valor") if row.get("valor") is not None else "")
                novas.append({"nome": nome, "valor": valor, "secreto": secreto})

            # Campos de senha: a lista de secretas vem do estado salvo (não da
            # edição ainda não enviada), pra não mudar o formulário no meio.
            secretas = [v['nome'] for v in variaveis if v['secreto']] if not manual else [v['nome'] for v in novas if v['secreto']]
            segredos = dict(self.state.get('api_segredos') or {})
            if secretas:
                usados, extraidos = self._api_uso_de_variaveis()
                st.caption("Valores das variáveis secretas (só nesta sessão):")
                cols = st.columns(min(3, len(secretas)))
                for i, nome in enumerate(secretas):
                    # Deixa explícito o que é obrigatório e o que não é: variável
                    # que um caso extrai da resposta (ex.: auth_token) ou que nenhum
                    # caso habilitado usa (ex.: senha de um caso desmarcado) é opcional.
                    if nome in extraidos:
                        rotulo, ajuda = f"🔒 {nome} — opcional", "Preenchida automaticamente por um caso que extrai esse valor da resposta. Deixe em branco."
                    elif nome not in usados:
                        rotulo, ajuda = f"🔒 {nome} — opcional", "Nenhum caso habilitado usa esta variável no momento."
                    else:
                        rotulo, ajuda = f"🔒 {nome} — obrigatória", "Usada por pelo menos um caso habilitado."
                    with cols[i % len(cols)]:
                        segredos[nome] = st.text_input(rotulo, value=segredos.get(nome, ""), type="password",
                                                       key=f"apiw_secret_{nome}", help=ajuda)
            salvar = st.form_submit_button("💾 Salvar variáveis", type="primary", width="stretch")
        if salvar:
            self.state.set('api_variaveis', novas)
            secretas_finais = [v['nome'] for v in novas if v['secreto']]
            self.state.set('api_segredos', {k: v for k, v in segredos.items() if k in secretas_finais and v})
            self._api_invalidar_evidencias()
            self._flash_success("Variáveis salvas.")
            st.rerun()
        else:
            faltam = [v['nome'] for v in variaveis if not v['secreto'] and not (v.get('valor') or '').strip()]
            faltam += [n for n in [v['nome'] for v in variaveis if v['secreto']] if not (self.state.get('api_segredos') or {}).get(n)]
            if faltam:
                st.caption("⚠️ Preencha e clique em **Salvar variáveis** antes de seguir — valores ainda não salvos: " + ", ".join(faltam))

    def _api_uso_de_variaveis(self):
        """(variáveis usadas por casos habilitados, variáveis produzidas por extração)."""
        casos = [c for c in (self.state.get('api_casos') or []) if c.get('habilitado', True)]
        usados = set()
        for c in casos:
            texto = " ".join([c.get('url') or '', c.get('body') or ''] + list((c.get('headers') or {}).values()))
            usados.update(ApiTestRunner._RE_VAR.findall(texto))
        extraidos = {e.get('nome') for c in casos for e in (c.get('extrair') or [])}
        return usados, extraidos

    def _api_render_casos(self):
        casos = self.state.get('api_casos') or []
        st.markdown(f"##### 🧪 Casos de teste ({len(casos)})")
        if not casos:
            st.info("Nenhum caso ainda. Gere com IA (acima), importe uma collection do Postman ou adicione um caso manualmente.")
            return

        for idx, caso in enumerate(casos):
            cid = caso['id']
            icone = "✅" if caso.get('habilitado', True) else "⏸️"
            with st.expander(f"{icone} {idx + 1}. {caso.get('nome') or '(sem nome)'}  —  `{caso.get('metodo')}`", expanded=False):
                if caso.get('avisos'):
                    for aviso in caso['avisos']:
                        st.warning(f"⚠️ {aviso}")
                c1, c2, c3 = st.columns([4, 1.2, 1])
                with c1:
                    caso['nome'] = st.text_input("Nome", value=caso.get('nome', ''), key=f"apiw_nome_{cid}")
                with c2:
                    caso['metodo'] = st.selectbox("Método", HTTP_METHODS, index=HTTP_METHODS.index(caso.get('metodo', 'GET')) if caso.get('metodo') in HTTP_METHODS else 0, key=f"apiw_met_{cid}")
                with c3:
                    caso['habilitado'] = st.checkbox("Habilitado", value=caso.get('habilitado', True), key=f"apiw_hab_{cid}")
                caso['url'] = st.text_input("URL", value=caso.get('url', ''), placeholder="{{base_url}}/api/v1/recurso", key=f"apiw_url_{cid}")
                ch, cb = st.columns(2)
                with ch:
                    headers_txt = "\n".join(f"{k}: {v}" for k, v in (caso.get('headers') or {}).items())
                    headers_txt = st.text_area("Headers (um por linha: `Chave: valor`)", value=headers_txt, height=110, key=f"apiw_hdr_{cid}")
                    headers = {}
                    for linha in headers_txt.splitlines():
                        if ":" in linha:
                            k, v = linha.split(":", 1)
                            if k.strip():
                                headers[k.strip()] = v.strip()
                    caso['headers'] = headers
                with cb:
                    caso['body'] = st.text_area("Body", value=caso.get('body', ''), height=110, key=f"apiw_body_{cid}")
                caso['descricao'] = st.text_input("Descrição / objetivo (opcional)", value=caso.get('descricao', ''), key=f"apiw_desc_{cid}")

                st.markdown("**Asserções**")
                df_a = pd.DataFrame(caso.get('assercoes') or [{"tipo": "status", "alvo": "", "valor": "200", "descricao": ""}],
                                    columns=["tipo", "alvo", "valor", "descricao"])
                edit_a = st.data_editor(
                    df_a, num_rows="dynamic", width="stretch", hide_index=True, key=f"apiw_asr_{cid}",
                    column_config={
                        "tipo": st.column_config.SelectboxColumn("Tipo", options=ASSERTION_TYPES, required=True,
                                                                 help="; ".join(f"{k}: {v}" for k, v in ASSERTION_LABELS.items())),
                        "alvo": st.column_config.TextColumn("Alvo (caminho JSON / header)"),
                        "valor": st.column_config.TextColumn("Valor esperado"),
                        "descricao": st.column_config.TextColumn("Descrição (opcional)"),
                    },
                )
                caso['assercoes'] = [
                    {"tipo": str(r["tipo"]), "alvo": str(r.get("alvo") or ""), "valor": str(r.get("valor") if r.get("valor") is not None else ""), "descricao": str(r.get("descricao") or "")}
                    for _, r in edit_a.iterrows() if str(r.get("tipo") or "").strip()
                ]

                st.markdown("**Extrair variáveis da resposta** (para usar nos próximos casos)")
                df_e = pd.DataFrame(caso.get('extrair') or [], columns=["nome", "caminho"])
                edit_e = st.data_editor(
                    df_e, num_rows="dynamic", width="stretch", hide_index=True, key=f"apiw_ext_{cid}",
                    column_config={
                        "nome": st.column_config.TextColumn("Variável"),
                        "caminho": st.column_config.TextColumn("Caminho JSON (ex.: data.token)"),
                    },
                )
                caso['extrair'] = [
                    {"nome": str(r["nome"]).strip(), "caminho": str(r.get("caminho") or "").strip()}
                    for _, r in edit_e.iterrows() if str(r.get("nome") or "").strip()
                ]

                b1, b2, b3, b4 = st.columns(4)
                with b1:
                    if st.button("⬆️ Subir", key=f"apiw_up_{cid}", disabled=idx == 0, width="stretch"):
                        casos[idx - 1], casos[idx] = casos[idx], casos[idx - 1]
                        self.state.set('api_casos', casos)
                        st.rerun()
                with b2:
                    if st.button("⬇️ Descer", key=f"apiw_down_{cid}", disabled=idx == len(casos) - 1, width="stretch"):
                        casos[idx + 1], casos[idx] = casos[idx], casos[idx + 1]
                        self.state.set('api_casos', casos)
                        st.rerun()
                with b3:
                    if st.button("📋 Duplicar", key=f"apiw_dup_{cid}", width="stretch"):
                        novo = json.loads(json.dumps(caso))
                        novo['id'] = str(uuid.uuid4())
                        novo['nome'] = f"{caso.get('nome', '')} (cópia)"
                        casos.insert(idx + 1, novo)
                        self.state.set('api_casos', casos)
                        st.rerun()
                with b4:
                    if st.button("🗑️ Excluir", key=f"apiw_del_{cid}", width="stretch"):
                        casos.pop(idx)
                        self.state.set('api_casos', casos)
                        st.rerun()
                if caso.get('avisos') and st.button("Entendi, limpar avisos", key=f"apiw_clr_{cid}"):
                    caso['avisos'] = []
                    st.rerun()
        self.state.set('api_casos', casos)

        if st.button("➕ Adicionar caso", key="btn_api_add_case_bottom"):
            self._api_adicionar_caso()
            st.rerun()

    def _api_adicionar_caso(self):
        casos = self.state.get('api_casos') or []
        casos.append(ApiTestCase(
            id=str(uuid.uuid4()), nome=f"Caso {len(casos) + 1}", metodo="GET", url="{{base_url}}/",
            headers={"Accept": "application/json"}, assercoes=[], extrair=[],
        ).to_dict())
        casos[-1]['assercoes'] = [{"tipo": "status", "alvo": "", "valor": "200", "descricao": ""}]
        self.state.set('api_casos', casos)
        self._api_invalidar_evidencias()

    def _api_importar_postman(self, col_file, env_file, substituir: bool):
        try:
            col = PostmanImporter.parse_collection(col_file.getvalue())
            env = PostmanImporter.parse_environment(env_file.getvalue()) if env_file is not None else []
        except PostmanImportError as error:
            self._flash_error(str(error))
            return
        if not self.state.get('api_projeto'):
            self.state.set('api_projeto', col['nome'])
            st.session_state.pop('apiw_projeto', None)
        # variáveis: environment sobrescreve collection
        por_nome = {v['nome']: v for v in (self.state.get('api_variaveis') or [])}
        for v in col['variaveis'] + env:
            if v['nome'] == 'base_url':
                # placeholder da collection ("SUBSTITUA...") não conta
                if v['valor'].startswith(('http://', 'https://')):
                    self.state.set('api_base_url', v['valor'].rstrip('/'))
                continue
            atual = por_nome.get(v['nome'], {"nome": v['nome'], "valor": "", "secreto": False})
            atual['secreto'] = atual['secreto'] or v['secreto']
            if v['valor'] and not v['secreto']:
                atual['valor'] = v['valor']
            por_nome[v['nome']] = atual
        self.state.set('api_variaveis', list(por_nome.values()))
        # segredos vindos do environment ficam só em memória
        segredos = dict(self.state.get('api_segredos') or {})
        for v in env:
            if v['secreto'] and v['valor']:
                segredos[v['nome']] = v['valor']
        self.state.set('api_segredos', segredos)

        novos = [c.to_dict() for c in col['casos']]
        self.state.set('api_casos', novos if substituir else (self.state.get('api_casos') or []) + novos)
        self._api_invalidar_evidencias()
        self.state.set('api_resultados', None)
        avisos = sum(len(c.get('avisos') or []) for c in novos)
        msg = f"{len(novos)} caso(s) importado(s) de '{col['nome']}'."
        if avisos:
            msg += f" {avisos} aviso(s) de conversão — abra os casos marcados e revise as asserções."
        (self._flash_success if not avisos else self._flash_warning)(msg)
        for k in list(st.session_state.keys()):
            if isinstance(k, str) and k.startswith('apiw_') and k not in ('apiw_projeto', 'apiw_base_url', 'apiw_ambiente', 'apiw_timeout'):
                del st.session_state[k]

    def _api_exportar_definicao(self) -> str:
        return json.dumps({
            "formato": "qa_testgen.api_tests.v1",
            "projeto": self.state.get('api_projeto'),
            "ambiente": self.state.get('api_ambiente'),
            "base_url": self.state.get('api_base_url'),
            "timeout": self.state.get('api_timeout'),
            "variaveis": [{"nome": v['nome'], "valor": "" if v['secreto'] else v['valor'], "secreto": v['secreto']}
                          for v in (self.state.get('api_variaveis') or [])],
            "contexto": self.state.get('api_contexto'),
            "observacoes": self.state.get('api_observacoes'),
            "casos": self.state.get('api_casos') or [],
        }, ensure_ascii=False, indent=2)

    def _api_importar_definicao(self, def_file):
        try:
            data = json.loads(def_file.getvalue().decode("utf-8-sig"))
            assert data.get("formato") == "qa_testgen.api_tests.v1"
        except Exception:
            self._flash_error("Arquivo não é uma definição exportada por este módulo.")
            return
        self.state.set('api_projeto', data.get('projeto') or self.state.get('api_projeto'))
        if data.get('ambiente') in _AMBIENTES:
            self.state.set('api_ambiente', data['ambiente'])
        self.state.set('api_base_url', data.get('base_url') or '')
        self.state.set('api_timeout', int(data.get('timeout') or 30))
        self.state.set('api_variaveis', [v for v in data.get('variaveis') or [] if v.get('nome')])
        self.state.set('api_contexto', data.get('contexto') or '')
        self.state.set('api_observacoes', data.get('observacoes') or '')
        casos = [ApiTestCase.from_dict(c).to_dict() for c in data.get('casos') or []]
        for c in casos:
            c['id'] = c['id'] or str(uuid.uuid4())
        self.state.set('api_casos', casos)
        self.state.set('api_resultados', None)
        self._api_invalidar_evidencias()
        self._flash_success(f"Definição carregada: {len(casos)} caso(s).")
        for k in list(st.session_state.keys()):
            if isinstance(k, str) and k.startswith('apiw_'):
                del st.session_state[k]

    # ------------------------------------------------------------ 2. Execução
    def _api_validar_definicao(self) -> list:
        erros = []
        if not (self.state.get('api_projeto') or '').strip():
            erros.append("Dê um nome ao projeto / execução (campo no topo da etapa 1).")
        if not (self.state.get('api_base_url') or '').startswith(('http://', 'https://')):
            erros.append("Informe a Base URL da API começando com http:// ou https:// (campo no topo da etapa 1).")
        casos = [c for c in (self.state.get('api_casos') or []) if c.get('habilitado', True)]
        if not casos:
            erros.append("Não há nenhum caso de teste habilitado — gere com IA, importe do Postman ou crie manualmente (seção Casos de teste).")
        for c in casos:
            if not (c.get('url') or '').strip():
                erros.append(f"O caso '{c.get('nome')}' está sem URL — preencha ou desabilite o caso.")
            if not c.get('assercoes'):
                erros.append(f"O caso '{c.get('nome')}' não tem nenhuma asserção — adicione ao menos o status esperado, ou desabilite o caso.")
        # Só cobra segredo que algum caso habilitado realmente usa e que
        # nenhum caso produz por extração (ex.: auth_token vem do login).
        usados, extraidos = self._api_uso_de_variaveis()
        secretas_vazias = [
            v['nome'] for v in (self.state.get('api_variaveis') or [])
            if v['secreto'] and v['nome'] in usados and v['nome'] not in extraidos
            and not (self.state.get('api_segredos') or {}).get(v['nome'])
        ]
        if secretas_vazias:
            erros.append("Senhas/segredos sem valor: " + ", ".join(secretas_vazias) + " — preencha os campos 🔒 na seção Variáveis e clique em Salvar variáveis.")
        # Variáveis normais usadas por casos habilitados e sem valor: o request
        # sairia com "" no lugar (ex.: "email": "") e todo caso falharia.
        normais_vazias = [
            v['nome'] for v in (self.state.get('api_variaveis') or [])
            if not v['secreto'] and v['nome'] in usados and v['nome'] not in extraidos and not (v.get('valor') or '').strip()
        ]
        if normais_vazias:
            erros.append("Variáveis sem valor: " + ", ".join(normais_vazias) + " — preencha a coluna Valor na seção 🔤 Variáveis e clique em Salvar variáveis.")
        # Variável usada por algum caso mas que não existe na tabela nem é extraída
        conhecidas = {v['nome'] for v in (self.state.get('api_variaveis') or [])} | extraidos | {'base_url'}
        desconhecidas = sorted(usados - conhecidas)
        if desconhecidas:
            erros.append("Os casos usam variáveis que não existem na tabela: " + ", ".join(desconhecidas) + " — corrija o nome no caso ou crie a variável.")
        return erros

    def _api_render_execucao(self):
        casos = self.state.get('api_casos') or []
        habilitados = [c for c in casos if c.get('habilitado', True)]
        st.markdown(f"##### ▶️ Execução — {len(habilitados)} caso(s) habilitado(s) de {len(casos)}")
        st.caption(f"Projeto: **{self.state.get('api_projeto') or '—'}** · Ambiente: **{self.state.get('api_ambiente')}** · Base URL: `{self.state.get('api_base_url') or '—'}`")

        erros = self._api_validar_definicao()
        for e in erros:
            st.warning(f"⚠️ {e}")

        modo = self._api_modo_execucao()
        if modo == "navegador":
            st.caption("🌐 As chamadas saem **do seu navegador** (mesmo IP que você usa no Postman) — configuração definida pelo administrador.")
        else:
            st.caption("🖥️ As chamadas saem **do servidor do app** — configuração definida pelo administrador.")

        job = self.state.get('api_browser_job')
        if st.button("▶️ Executar testes", key="azure_blue_btn_api_run", disabled=bool(erros) or bool(job), width="stretch"):
            if modo == "navegador":
                self._api_iniciar_execucao_navegador()
            else:
                self._api_executar()
            st.rerun()

        if job:
            self._api_render_execucao_navegador(job)

        resultados = self.state.get('api_resultados')
        if not resultados:
            return
        resumo = ApiEvidenceBuilder.resumo(resultados)
        st.divider()
        icone = "✅" if resumo['status_geral'] == 'Aprovado' else "❌"
        st.markdown(
            f"{icone} **Status geral: {resumo['status_geral']}** — "
            f"**{resumo['aprovados']}** aprovado(s) · **{resumo['reprovados'] + resumo['erros']}** reprovado(s)/erro(s)"
            + (f" · {resumo['pulados']} não executado(s)" if resumo['pulados'] else "")
            + f" · asserções **{resumo['assercoes_ok']}/{resumo['assercoes']}** · tempo médio **{resumo['tempo_medio_ms']} ms**"
        )

        segredos = self._api_lista_segredos()
        for idx, r in enumerate(resultados, start=1):
            icone = "✅" if r.passou else ("⏸️" if r.pulado else "❌")
            ok = sum(1 for a in r.assercoes if a.passou)
            with st.expander(f"{icone} {idx}. {r.nome} — HTTP {r.status_code if r.status_code is not None else '—'} · {r.tempo_ms} ms · {ok}/{len(r.assercoes)} asserções", expanded=not r.passou and not r.pulado):
                for a in r.assercoes:
                    st.markdown(f"- {'✅' if a.passou else '❌'} {a.descricao}" + (f" — _{a.detalhe}_" if (not a.passou and a.detalhe) else ""))
                if r.erro:
                    st.error(r.erro)
                if not r.pulado:
                    with st.expander("📤 Request enviado"):
                        st.code(ApiEvidenceBuilder.texto_request(r, segredos), language="http")
                    with st.expander("📥 Response recebido"):
                        st.code(ApiEvidenceBuilder.texto_response(r, segredos), language="http")
        self._api_botao_proxima_etapa(_ETAPAS[2], "➡️ Próxima etapa: 3. Evidências (gerar e baixar relatórios)", "btn_api_next_2")

    def _api_iniciar_execucao_navegador(self):
        casos = [c for c in (self.state.get('api_casos') or []) if c.get('habilitado', True)]
        self.state.set('api_browser_job', {
            "run_id": str(uuid.uuid4()),
            "casos": [{"id": c['id'], "nome": c['nome'], "metodo": c['metodo'], "url": c['url'],
                       "headers": c.get('headers') or {}, "body": c.get('body') or "", "extrair": c.get('extrair') or []} for c in casos],
            "variaveis": self._api_variaveis_resolvidas(),
            "timeout_s": int(self.state.get('api_timeout') or 30),
        })
        self.state.set('api_resultados', None)
        self._api_invalidar_evidencias()

    def _api_render_execucao_navegador(self, job: dict):
        """
        Renderiza o componente que executa no navegador e, quando ele devolve
        as respostas, avalia tudo em Python (mesma lógica da execução direta).
        """
        st.info("⏳ Executando no seu navegador — não feche nem troque de aba até concluir.")
        retorno = _BROWSER_RUNNER(**job, key=f"api_browser_runner_{job['run_id']}", default=None)
        if not retorno or retorno.get("run_id") != job["run_id"]:
            if st.button("✖ Cancelar execução", key="btn_api_cancel_browser"):
                self.state.set('api_browser_job', None)
                st.rerun()
            return
        self.state.set('api_browser_job', None)
        if retorno.get("erro_geral"):
            self._flash_error(f"Falha na execução pelo navegador: {retorno['erro_geral']}")
            st.rerun()
        runner = ApiTestRunner(job["variaveis"], timeout=job["timeout_s"])
        casos = [ApiTestCase.from_dict(c) for c in (self.state.get('api_casos') or [])]
        resultados = runner.avaliar_execucao_externa(casos, retorno.get("respostas") or [])
        self._api_concluir_execucao(resultados)
        st.rerun()

    def _api_concluir_execucao(self, resultados: list):
        self.state.set('api_resultados', resultados)
        self._api_invalidar_evidencias()
        resumo = ApiEvidenceBuilder.resumo(resultados)
        try:
            log_action(self.config, st.session_state.get(SESSION_USER_KEY, ""), "Executar Testes de API",
                       "Testes de API", f"{self.state.get('api_projeto')} — {resumo['aprovados']}/{resumo['total'] - resumo['pulados']} aprovados")
        except Exception:
            pass
        (self._flash_success if resumo['status_geral'] == 'Aprovado' else self._flash_warning)(
            f"Execução concluída: {resumo['aprovados']} aprovado(s), {resumo['reprovados'] + resumo['erros']} reprovado(s)/erro(s).")

    def _api_executar(self):
        casos = [ApiTestCase.from_dict(c) for c in (self.state.get('api_casos') or [])]
        runner = ApiTestRunner(self._api_variaveis_resolvidas(), timeout=int(self.state.get('api_timeout') or 30))
        barra = st.progress(0.0, text="Executando...")

        def _prog(i, total, res):
            barra.progress(i / total, text=f"{i}/{total} — {res.nome}: {res.resultado_label}")

        with st.spinner("Executando os casos..."):
            resultados = runner.executar(casos, on_progress=_prog)
        barra.empty()
        self._api_concluir_execucao(resultados)

    # ---------------------------------------------------------- 3. Evidências
    def _api_render_evidencias(self):
        resultados = self.state.get('api_resultados')
        if not resultados:
            st.info("Execute os testes na etapa **2. Execução** antes de gerar as evidências.")
            self._api_botao_proxima_etapa(_ETAPAS[1], "⬅️ Ir para 2. Execução", "btn_api_goto_2")
            return
        st.markdown("##### 🖼️ Imagens complementares por caso (opcional)")
        st.caption("Prints seus (Postman, tela do sistema, etc.) que entram no PDF e no .zip junto do caso.")
        imagens = dict(self.state.get('api_imagens') or {})
        for idx, r in enumerate(resultados, start=1):
            ups = st.file_uploader(f"{idx}. {r.nome}", type=["png", "jpg", "jpeg"], accept_multiple_files=True, key=f"apiw_img_{r.case_id}")
            if ups:
                imagens[r.case_id] = [(u.name, u.getvalue()) for u in ups]
            elif r.case_id in imagens and not ups:
                imagens.pop(r.case_id, None)
        self.state.set('api_imagens', imagens)

        st.divider()
        self.state.set('api_observacoes', st.text_area(
            "Observações / divergências / próximos passos (entram no relatório)",
            value=self.state.get('api_observacoes') or '', height=120, key="apiw_obs"))

        if st.button("📝 Gerar relatórios (.md + .pdf + .zip)", key="azure_blue_btn_api_gen", width="stretch"):
            self._api_gerar_relatorios()
            st.rerun()

        if self.state.get('api_md'):
            st.success("Relatórios gerados. Senhas, tokens e headers sensíveis saem mascarados.")
            slug = ApiEvidenceBuilder.slug(self.state.get('api_projeto') or 'testes-api', 40)
            d1, d2, d3, d4 = st.columns(4)
            with d1:
                st.download_button("⬇️ RELATORIO.md", self.state.get('api_md').encode('utf-8'), file_name=f"{slug}_RELATORIO.md", mime="text/markdown", width="stretch", key="dl_api_md", on_click=self._api_marcar_baixado)
            with d2:
                st.download_button("⬇️ RELATORIO.pdf", self.state.get('api_pdf'), file_name=f"{slug}_RELATORIO.pdf", mime="application/pdf", width="stretch", key="dl_api_pdf", on_click=self._api_marcar_baixado)
            with d3:
                st.download_button("⬇️ Evidências .zip", self.state.get('api_zip'), file_name=f"{slug}_evidencias.zip", mime="application/zip", width="stretch", key="dl_api_zip", on_click=self._api_marcar_baixado)
            with d4:
                st.download_button("⬇️ Definição .json", self._api_exportar_definicao().encode('utf-8'), file_name=f"{slug}_definicao.json", mime="application/json", width="stretch", key="dl_api_def", on_click=self._api_marcar_baixado,
                                   help="Reimporte na etapa Definição pra repetir esta bateria depois (senhas não são salvas).")

            if self.config.turso_database_url and self._get_permission_cached("documentos_armazenados"):
                if st.button("🗄️ Salvar em Documentos Armazenados", key="btn_api_store"):
                    self._api_salvar_documentos()
            with st.expander("👁️ Pré-visualizar RELATORIO.md"):
                st.markdown(self.state.get('api_md'))

        st.divider()
        self._api_render_levar_para_assistente()

    def _api_render_levar_para_assistente(self):
        """
        Transforma a bateria em Matriz + Casos + Planos do assistente, pra
        seguir pelo Passo 7 (vincular a Work Items, suítes estáticas ou
        reconciliar) como qualquer outra documentação.
        """
        st.markdown("##### 🧱 Levar para o assistente (Matriz, Casos e Planos → Azure DevOps)")
        st.caption(
            "Cada caso da bateria vira um **Caso de Teste** (passos = requisição, resultado esperado = asserções), "
            "com uma linha de **Matriz de Cobertura** e um **Plano** com uma suíte por endpoint. Você cai no Passo 5 "
            "pra revisar e usa o Passo 7 normalmente pra criar tudo no Azure DevOps."
        )
        incluir_resultado = st.checkbox(
            "Incluir o resultado da última execução no texto dos Casos de Teste (ex.: Última execução (HML): Aprovado — 6/6)",
            value=True, key="apiw_levar_incluir_resultado",
        )
        st.caption("Independentemente disso, depois que o Passo 7 criar os Test Cases, o app oferece registrar a execução como **Test Run** oficial no Azure DevOps.")

        existentes = len(self.state.get('test_cases') or []) + len(self.state.get('matriz') or []) + len(self.state.get('test_plans') or [])
        modo = "substituir"
        if existentes:
            st.warning(
                f"Há uma análise na sessão do assistente ({len(self.state.get('test_cases') or [])} caso(s), "
                f"{len(self.state.get('matriz') or [])} linha(s) de Matriz, {len(self.state.get('test_plans') or [])} plano(s)). "
                "Os casos já existentes são da **mesma funcionalidade** desta bateria? → *Acrescentar* (tudo vai junto no mesmo Test Plan). "
                "São de **outra** funcionalidade? → *Substituir* (ou envie a análise anterior pelo Passo 7 antes)."
            )
            modo = st.radio("O que fazer com o que já está na sessão?", ["acrescentar", "substituir"],
                            format_func=lambda m: "➕ Acrescentar (junta na mesma sessão)" if m == "acrescentar" else "♻️ Substituir (apaga o que está lá)",
                            horizontal=True, key="apiw_levar_modo")
        wis = self.state.get('api_work_items') or []
        if wis:
            st.caption("Pré-vínculo: os casos nascem marcados com o Work Item **#" + str(wis[0]['id']) + f" — {wis[0]['title']}**; no Passo 7 (modo Vincular) isso já vem sugerido.")
        if st.button("🧱 Levar para o assistente", type="primary", key="btn_api_levar", width="stretch"):
            self._api_levar_para_assistente(incluir_resultado, modo)
            st.rerun()

    def _api_levar_para_assistente(self, incluir_resultado: bool, modo: str):
        casos = self.state.get('api_casos') or []
        resultados = self.state.get('api_resultados') or []
        matriz_atual = list(self.state.get('matriz') or []) if modo == "acrescentar" else []
        casos_atuais = list(self.state.get('test_cases') or []) if modo == "acrescentar" else []
        planos_atuais = list(self.state.get('test_plans') or []) if modo == "acrescentar" else []
        # remove uma versão anterior desta mesma bateria (mesmos títulos, origem testes_api) pra não duplicar
        titulos_api = {c.get('nome') for c in casos}
        anteriores = [tc for tc in casos_atuais if tc.get('origem') == 'testes_api' and tc.get('titulo') in titulos_api]
        ids_removidos = {rid for tc in anteriores for rid in (tc.get('requisitos_relacionados') or [])}
        casos_atuais = [tc for tc in casos_atuais if tc not in anteriores]
        matriz_atual = [m for m in matriz_atual if m.get('id') not in ids_removidos]
        planos_atuais = [p for p in planos_atuais if not str(p.get('nome', '')).startswith("Testes de API — ")]

        out = converter_bateria(
            self.state.get('api_projeto') or 'API', self.state.get('api_ambiente'), self.state.get('api_base_url'),
            casos, resultados, variaveis=self.state.get('api_variaveis') or [], work_items=self.state.get('api_work_items') or [],
            incluir_resultado=incluir_resultado, mc_inicio=len(matriz_atual) + 1,
        )
        if not out["test_cases"]:
            self._flash_error("Nenhum caso habilitado pra levar ao assistente.")
            return
        self.state.set('matriz', matriz_atual + out["matriz"])
        self.state.set('test_cases', casos_atuais + out["test_cases"])
        self.state.set('test_plans', planos_atuais + out["test_plans"])
        if not self.state.get('project_name'):
            self.state.set('project_name', self.state.get('api_projeto') or 'Testes de API')
        if not self.state.get('ambiente_testes'):
            self.state.set('ambiente_testes', self.state.get('api_ambiente') or '')
        # resultado da execução guardado pra virar Test Run depois do Passo 7
        self.state.set('api_test_run_pendente', {
            "projeto": self.state.get('api_projeto'), "ambiente": self.state.get('api_ambiente'),
            "resultados": resultados_para_test_run(casos, resultados), "pdf": self.state.get('api_pdf'),
            "quando": datetime.now(TZ_BR).strftime("%d/%m/%Y %H:%M"),
        })
        self.state.set('api_baixado', True)   # levou pro assistente: não é "perda" ao sair da tela
        # entra no assistente já no Passo 5 (Planos), com 1–4 marcados como feitos
        self.state.set('completed_steps', sorted(set(self.state.get('completed_steps') or []) | {1, 2, 3, 4}))
        self.state.set('max_step', max(self.state.get('max_step') or 1, 5))
        self.state.set('step', 5)
        self.state.set('show_api_tests_page', False)
        self._flash_success(
            f"{len(out['test_cases'])} caso(s) de API viraram Casos de Teste, {len(out['matriz'])} linha(s) de Matriz e "
            f"1 plano com {len(out['test_plans'][0]['suites'])} suíte(s). Revise e siga pro Passo 6/7."
        )

    def _api_render_registrar_test_run(self, ado_client):
        """
        Aparece no Passo 7, abaixo do resultado da integração, quando a
        sessão tem uma bateria de API executada e os Test Cases dela acabaram
        de ser criados no plano: registra a execução como Test Run oficial
        (Passed/Failed por caso) e anexa o PDF de evidências ao run.
        """
        pend = self.state.get('api_test_run_pendente')
        plan_id = self.state.get('ado_last_plan_id')
        case_ids = self.state.get('ado_test_case_ids') or {}
        if not pend or not plan_id or not case_ids:
            return
        mapeados = {t: case_ids[t] for t in pend.get("resultados", {}) if t in case_ids}
        if not mapeados:
            return
        st.divider()
        st.markdown("##### 📤 Registrar a execução dos Testes de API como Test Run")
        st.caption(
            f"{len(mapeados)} Test Case(s) desta bateria estão no plano (ID {plan_id}). O app cria um Test Run, marca cada um "
            f"como **Passed/Failed** conforme a execução de {pend.get('quando')} ({pend.get('ambiente')}) e anexa o PDF de evidências. "
            "Isso aparece na aba *Execute* do Test Plan, com histórico."
        )
        ja = self.state.get('api_test_run_registrado')
        if ja and ja.get("plan_id") == plan_id:
            st.success(f"✅ Test Run #{ja['run_id']} registrado" + (f" — [abrir no Azure DevOps]({ja['url']})" if ja.get('url') else ""))
            return
        if st.button("📤 Registrar Test Run no Azure DevOps", key="azure_blue_btn_api_test_run", width="stretch",
                     disabled=self.state.get('is_processing')):
            try:
                with st.spinner("Localizando os Test Points no plano..."):
                    pontos = {}
                    for suite in ado_client.list_plan_suites(plan_id):
                        for pt in ado_client.list_test_points(plan_id, suite["id"]):
                            pontos.setdefault(pt["test_case_id"], pt["id"])
                point_ids = [pontos[cid] for cid in mapeados.values() if cid in pontos]
                if not point_ids:
                    self._flash_error("Nenhum Test Point encontrado pros Test Cases desta bateria — os casos precisam estar numa suíte do plano.")
                    st.rerun()
                nome_run = f"Testes de API — {pend.get('projeto')} — {pend.get('ambiente')} — {pend.get('quando')}"
                with st.spinner("Criando o Test Run e gravando os resultados..."):
                    run = ado_client.create_test_run(plan_id, nome_run, point_ids, comment="Registrado automaticamente pelo QA TestGen (Testes de API)")
                    resultados_run = ado_client.get_test_run_results(run["id"])
                    por_case = {r["test_case_id"]: r["id"] for r in resultados_run}
                    payload = []
                    for titulo, cid in mapeados.items():
                        if cid in por_case:
                            info = pend["resultados"][titulo]
                            payload.append({"id": por_case[cid], "outcome": info["outcome"], "comment": info.get("comentario", "")})
                    ado_client.update_test_run_results(run["id"], payload)
                    aviso_anexo = ""
                    if pend.get("pdf"):
                        try:
                            ado_client.attach_file_to_test_run(run["id"], "RELATORIO-testes-de-api.pdf", pend["pdf"], comment="Evidências (QA TestGen)")
                        except Exception as error:
                            aviso_anexo = f" (PDF não anexado: {error})"
                    ado_client.complete_test_run(run["id"], comment=f"{sum(1 for x in payload if x['outcome'] == 'Passed')} aprovado(s) de {len(payload)}")
                self.state.set('api_test_run_registrado', {"plan_id": plan_id, "run_id": run["id"], "url": run.get("url")})
                try:
                    self._log("Testes de API", "Registrar Test Run", f"Run #{run['id']} no plano {plan_id}: {len(payload)} resultado(s)")
                except Exception:
                    pass
                self._flash_success(f"Test Run #{run['id']} criado com {len(payload)} resultado(s){aviso_anexo}.")
            except Exception as error:
                self._flash_error(f"Não foi possível registrar o Test Run: {error}")
            st.rerun()

    def _api_gerar_relatorios(self):
        resultados = self.state.get('api_resultados') or []
        segredos = self._api_lista_segredos()
        E = ApiEvidenceBuilder
        autor = st.session_state.get(SESSION_USER_KEY, "")
        projeto = self.state.get('api_projeto') or 'Testes de API'
        imagens = self.state.get('api_imagens') or {}
        comuns = dict(
            contexto=self.state.get('api_contexto') or '', documentos=self.state.get('api_docs_nomes') or [],
            observacoes=self.state.get('api_observacoes') or '', imagens_por_caso=imagens,
        )
        md = E.gerar_markdown(projeto, self.state.get('api_ambiente'), self.state.get('api_base_url'), resultados,
                              autor=autor, segredos=segredos, **comuns)
        textos = {r.case_id: {"request": E.texto_request(r, segredos), "response": E.texto_response(r, segredos)} for r in resultados}
        try:
            pdf = PdfReportGenerator.generate_api_test_report(
                projeto, self.state.get('api_ambiente'), self.state.get('api_base_url'), resultados,
                E.resumo(resultados), textos, author_name=autor, **comuns)
        except Exception as error:
            self._flash_error(f"Falha ao gerar o PDF: {error}")
            return
        z = E.gerar_zip(projeto, resultados, md, pdf, segredos=segredos, imagens_por_caso=imagens,
                        definicao_json=self._api_exportar_definicao())
        self.state.set('api_md', md)
        self.state.set('api_pdf', pdf)
        self.state.set('api_zip', z)
        try:
            log_action(self.config, autor, "Gerar evidências de Testes de API", "Testes de API", projeto)
        except Exception:
            pass

    def _api_salvar_documentos(self):
        slug = ApiEvidenceBuilder.slug(self.state.get('api_projeto') or 'testes-api', 40)
        store = DocumentStore(self.config.turso_database_url, self.config.turso_auth_token)
        try:
            store.ensure_schema()
            store.salvar_grupo(
                "Testes de API", self.state.get('api_projeto') or 'Testes de API',
                [
                    {"tipo": "pdf", "nome_arquivo": f"{slug}_RELATORIO.pdf", "conteudo": self.state.get('api_pdf')},
                    {"tipo": "md", "nome_arquivo": f"{slug}_RELATORIO.md", "conteudo": self.state.get('api_md').encode('utf-8')},
                    {"tipo": "zip", "nome_arquivo": f"{slug}_evidencias.zip", "conteudo": self.state.get('api_zip')},
                ],
                criado_por=st.session_state.get(SESSION_USER_KEY, ""),
            )
            self.state.set('api_baixado', True)
            st.success("✅ Salvo em Documentos Armazenados (grupo 'Testes de API').")
        except DocumentStoreError as error:
            st.error(f"❌ {error}")
        except Exception as error:
            st.error(f"❌ Não foi possível salvar: {error}")
