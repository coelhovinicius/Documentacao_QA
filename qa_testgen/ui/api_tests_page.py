"""
Página "🔌 Testes de API" — Fase 1: definição (import Postman ou manual),
documentos de contexto, execução em Python e evidências (.md, .pdf, .zip,
Documentos Armazenados). Sem integração com o Azure DevOps ainda (Fase 2).

É um mixin: UserInterface herda daqui e chama `_api_tests_page()` no
`run()`. Todo estado fica em chaves `api_*` do SessionState.
"""
import json
import uuid

import pandas as pd
import streamlit as st

from qa_testgen.domain.models.api_test import (
    ASSERTION_TYPES, ASSERTION_LABELS, HTTP_METHODS, ApiTestCase,
)
from qa_testgen.infrastructure.api_evidence import ApiEvidenceBuilder
from qa_testgen.infrastructure.api_test_runner import ApiTestRunner
from qa_testgen.infrastructure.document_processor import DocumentProcessor
from qa_testgen.infrastructure.document_store import DocumentStore, DocumentStoreError
from qa_testgen.infrastructure.pdf_report import PdfReportGenerator
from qa_testgen.infrastructure.postman_importer import PostmanImporter, PostmanImportError
from qa_testgen.ui.auth import SESSION_USER_KEY, log_action


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
    'api_flash': None,
}

_ETAPAS = ['1. Definição', '2. Execução', '3. Evidências']
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

    def _api_invalidar_evidencias(self):
        self.state.set('api_md', None)
        self.state.set('api_pdf', None)
        self.state.set('api_zip', None)

    # ------------------------------------------------------------------ page
    def _api_tests_page(self):
        st.subheader("🔌 Testes de API")
        if st.button("← Voltar", key="btn_api_back"):
            self.state.set('show_api_tests_page', False)
            st.rerun()
        if not self._get_permission_cached("testes_api"):
            st.error("❌ Você não tem permissão pra acessar esta área.")
            return

        st.caption(
            "Executa testes de API (importados do Postman ou criados aqui) direto do app, "
            "sem Node/Newman, e gera evidências organizadas — request, response e asserções por caso, "
            "relatório em Markdown e PDF no padrão QA TestGen, e pacote .zip pra arquivar. "
            "Integração com o Azure DevOps (vincular a Test Cases e registrar resultado) vem na próxima fase."
        )

        flash = self.state.get('api_flash')
        if flash:
            getattr(st, flash[0])(flash[1])
            self.state.set('api_flash', None)

        col_e, col_n = st.columns([4, 1])
        with col_e:
            etapa = st.radio("Etapa", _ETAPAS, index=_ETAPAS.index(self.state.get('api_etapa') or _ETAPAS[0]),
                             horizontal=True, key="apiw_etapa", label_visibility="collapsed")
            self.state.set('api_etapa', etapa)
        with col_n:
            if st.button("🔄 Nova execução", key="btn_api_reset", use_container_width=True):
                self._api_reset()
                st.rerun()

        st.divider()
        if etapa == _ETAPAS[0]:
            self._api_render_definicao()
        elif etapa == _ETAPAS[1]:
            self._api_render_execucao()
        else:
            self._api_render_evidencias()

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
                          ["📮 Importar collection do Postman", "🧩 Importar definição salva (.json deste módulo)", "✍️ Criar manualmente"],
                          horizontal=True, key="apiw_origem", label_visibility="collapsed")
        if origem.startswith("📮"):
            cc, ce = st.columns(2)
            with cc:
                col_file = st.file_uploader("Collection (*.postman_collection.json) *", type=["json"], key="apiw_col_file")
            with ce:
                env_file = st.file_uploader("Environment (*.postman_environment.json) — opcional", type=["json"], key="apiw_env_file")
            substituir = st.checkbox("Substituir os casos já existentes (desmarcado = acrescenta)", value=True, key="apiw_col_replace")
            if st.button("📥 Importar", key="btn_api_import", disabled=col_file is None, type="primary"):
                self._api_importar_postman(col_file, env_file, substituir)
                st.rerun()
        elif origem.startswith("🧩"):
            def_file = st.file_uploader("Definição (.json exportado na etapa Evidências)", type=["json"], key="apiw_def_file")
            if st.button("📥 Carregar definição", key="btn_api_import_def", disabled=def_file is None, type="primary"):
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
            "Contexto da execução", value=self.state.get('api_contexto') or '', height=90,
            placeholder="Ex.: prévia da API em HML, User Story de Login, credenciais de colaborador...", key="apiw_contexto"))

        st.divider()
        self._api_render_casos()

    def _api_render_variaveis(self):
        st.markdown("##### 🔤 Variáveis (`{{nome}}` em URL, headers e body)")
        st.caption("Marque **Secreto** para senhas/tokens: o valor é pedido abaixo, fica só nesta sessão e sai mascarado de toda evidência.")
        variaveis = self.state.get('api_variaveis') or []
        df = pd.DataFrame(variaveis or [{"nome": "", "valor": "", "secreto": False}], columns=["nome", "valor", "secreto"])
        df["valor"] = df.apply(lambda r: "" if r["secreto"] else r["valor"], axis=1)
        edit = st.data_editor(
            df, num_rows="dynamic", use_container_width=True, hide_index=True, key="apiw_vars_editor",
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
        self.state.set('api_variaveis', novas)

        secretas = [v['nome'] for v in novas if v['secreto']]
        segredos = dict(self.state.get('api_segredos') or {})
        if secretas:
            cols = st.columns(min(3, len(secretas)))
            for i, nome in enumerate(secretas):
                with cols[i % len(cols)]:
                    segredos[nome] = st.text_input(f"🔒 {nome}", value=segredos.get(nome, ""), type="password", key=f"apiw_secret_{nome}")
        self.state.set('api_segredos', {k: v for k, v in segredos.items() if k in secretas})

    def _api_render_casos(self):
        casos = self.state.get('api_casos') or []
        st.markdown(f"##### 🧪 Casos de teste ({len(casos)})")
        if not casos:
            st.info("Nenhum caso ainda. Importe uma collection do Postman ou adicione um caso manualmente.")
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
                    df_a, num_rows="dynamic", use_container_width=True, hide_index=True, key=f"apiw_asr_{cid}",
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
                    df_e, num_rows="dynamic", use_container_width=True, hide_index=True, key=f"apiw_ext_{cid}",
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
                    if st.button("⬆️ Subir", key=f"apiw_up_{cid}", disabled=idx == 0, use_container_width=True):
                        casos[idx - 1], casos[idx] = casos[idx], casos[idx - 1]
                        self.state.set('api_casos', casos)
                        st.rerun()
                with b2:
                    if st.button("⬇️ Descer", key=f"apiw_down_{cid}", disabled=idx == len(casos) - 1, use_container_width=True):
                        casos[idx + 1], casos[idx] = casos[idx], casos[idx + 1]
                        self.state.set('api_casos', casos)
                        st.rerun()
                with b3:
                    if st.button("📋 Duplicar", key=f"apiw_dup_{cid}", use_container_width=True):
                        novo = json.loads(json.dumps(caso))
                        novo['id'] = str(uuid.uuid4())
                        novo['nome'] = f"{caso.get('nome', '')} (cópia)"
                        casos.insert(idx + 1, novo)
                        self.state.set('api_casos', casos)
                        st.rerun()
                with b4:
                    if st.button("🗑️ Excluir", key=f"apiw_del_{cid}", use_container_width=True):
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
            self.state.set('api_flash', ('error', f"❌ {error}"))
            return
        if not self.state.get('api_projeto'):
            self.state.set('api_projeto', col['nome'])
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
        msg = f"✅ {len(novos)} caso(s) importado(s) de '{col['nome']}'."
        if avisos:
            msg += f" {avisos} aviso(s) de conversão — abra os casos marcados e revise as asserções."
        self.state.set('api_flash', ('success' if not avisos else 'warning', msg))
        for k in list(st.session_state.keys()):
            if isinstance(k, str) and k.startswith('apiw_') and k not in ('apiw_etapa', 'apiw_projeto', 'apiw_base_url', 'apiw_ambiente', 'apiw_timeout'):
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
            self.state.set('api_flash', ('error', "❌ Arquivo não é uma definição exportada por este módulo."))
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
        self.state.set('api_flash', ('success', f"✅ Definição carregada: {len(casos)} caso(s)."))
        for k in list(st.session_state.keys()):
            if isinstance(k, str) and k.startswith('apiw_') and k != 'apiw_etapa':
                del st.session_state[k]

    # ------------------------------------------------------------ 2. Execução
    def _api_validar_definicao(self) -> list:
        erros = []
        if not (self.state.get('api_projeto') or '').strip():
            erros.append("Informe o nome do projeto / execução.")
        if not (self.state.get('api_base_url') or '').startswith(('http://', 'https://')):
            erros.append("Informe uma Base URL válida (http:// ou https://).")
        casos = [c for c in (self.state.get('api_casos') or []) if c.get('habilitado', True)]
        if not casos:
            erros.append("Nenhum caso habilitado para executar.")
        for c in casos:
            if not (c.get('url') or '').strip():
                erros.append(f"Caso '{c.get('nome')}': URL vazia.")
            if not c.get('assercoes'):
                erros.append(f"Caso '{c.get('nome')}': sem asserções.")
        # Só cobra segredo que algum caso habilitado realmente usa e que
        # nenhum caso produz por extração (ex.: auth_token vem do login).
        usados = set()
        for c in casos:
            texto = " ".join([c.get('url') or '', c.get('body') or ''] + list((c.get('headers') or {}).values()))
            usados.update(ApiTestRunner._RE_VAR.findall(texto))
        extraidos = {e.get('nome') for c in casos for e in (c.get('extrair') or [])}
        secretas_vazias = [
            v['nome'] for v in (self.state.get('api_variaveis') or [])
            if v['secreto'] and v['nome'] in usados and v['nome'] not in extraidos
            and not (self.state.get('api_segredos') or {}).get(v['nome'])
        ]
        if secretas_vazias:
            erros.append("Variáveis secretas sem valor: " + ", ".join(secretas_vazias) + " (preencha na etapa Definição).")
        return erros

    def _api_render_execucao(self):
        casos = self.state.get('api_casos') or []
        habilitados = [c for c in casos if c.get('habilitado', True)]
        st.markdown(f"##### ▶️ Execução — {len(habilitados)} caso(s) habilitado(s) de {len(casos)}")
        st.caption(f"Projeto: **{self.state.get('api_projeto') or '—'}** · Ambiente: **{self.state.get('api_ambiente')}** · Base URL: `{self.state.get('api_base_url') or '—'}`")

        erros = self._api_validar_definicao()
        for e in erros:
            st.warning(f"⚠️ {e}")

        if st.button("▶️ Executar testes", type="primary", key="btn_api_run", disabled=bool(erros)):
            self._api_executar()
            st.rerun()

        resultados = self.state.get('api_resultados')
        if not resultados:
            return
        resumo = ApiEvidenceBuilder.resumo(resultados)
        st.divider()
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Status geral", resumo['status_geral'])
        m2.metric("Aprovados", resumo['aprovados'])
        m3.metric("Reprovados", resumo['reprovados'] + resumo['erros'])
        m4.metric("Asserções OK", f"{resumo['assercoes_ok']}/{resumo['assercoes']}")
        m5.metric("Tempo médio", f"{resumo['tempo_medio_ms']} ms")

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
                    t1, t2 = st.tabs(["Request", "Response"])
                    with t1:
                        st.code(ApiEvidenceBuilder.texto_request(r, segredos), language="http")
                    with t2:
                        st.code(ApiEvidenceBuilder.texto_response(r, segredos), language="http")
        st.info("➡️ Vá para a etapa **3. Evidências** para gerar o relatório (.md/.pdf), anexar imagens e baixar o pacote.")

    def _api_executar(self):
        casos = [ApiTestCase.from_dict(c) for c in (self.state.get('api_casos') or [])]
        runner = ApiTestRunner(self._api_variaveis_resolvidas(), timeout=int(self.state.get('api_timeout') or 30))
        barra = st.progress(0.0, text="Executando...")

        def _prog(i, total, res):
            barra.progress(i / total, text=f"{i}/{total} — {res.nome}: {res.resultado_label}")

        with st.spinner("Executando os casos..."):
            resultados = runner.executar(casos, on_progress=_prog)
        barra.empty()
        self.state.set('api_resultados', resultados)
        self._api_invalidar_evidencias()
        resumo = ApiEvidenceBuilder.resumo(resultados)
        try:
            log_action(self.config, st.session_state.get(SESSION_USER_KEY, ""), "Executar Testes de API",
                       "Testes de API", f"{self.state.get('api_projeto')} — {resumo['aprovados']}/{resumo['total'] - resumo['pulados']} aprovados")
        except Exception:
            pass
        self.state.set('api_flash', ('success' if resumo['status_geral'] == 'Aprovado' else 'warning',
                                     f"Execução concluída: {resumo['aprovados']} aprovado(s), {resumo['reprovados'] + resumo['erros']} reprovado(s)/erro(s)."))

    # ---------------------------------------------------------- 3. Evidências
    def _api_render_evidencias(self):
        resultados = self.state.get('api_resultados')
        if not resultados:
            st.info("Execute os testes na etapa **2. Execução** antes de gerar as evidências.")
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

        if st.button("📝 Gerar relatórios (.md + .pdf + .zip)", type="primary", key="btn_api_gen"):
            self._api_gerar_relatorios()
            st.rerun()

        if self.state.get('api_md'):
            st.success("Relatórios gerados. Senhas, tokens e headers sensíveis saem mascarados.")
            slug = ApiEvidenceBuilder.slug(self.state.get('api_projeto') or 'testes-api', 40)
            d1, d2, d3, d4 = st.columns(4)
            with d1:
                st.download_button("⬇️ RELATORIO.md", self.state.get('api_md').encode('utf-8'), file_name=f"{slug}_RELATORIO.md", mime="text/markdown", use_container_width=True, key="dl_api_md")
            with d2:
                st.download_button("⬇️ RELATORIO.pdf", self.state.get('api_pdf'), file_name=f"{slug}_RELATORIO.pdf", mime="application/pdf", use_container_width=True, key="dl_api_pdf")
            with d3:
                st.download_button("⬇️ Evidências .zip", self.state.get('api_zip'), file_name=f"{slug}_evidencias.zip", mime="application/zip", use_container_width=True, key="dl_api_zip")
            with d4:
                st.download_button("⬇️ Definição .json", self._api_exportar_definicao().encode('utf-8'), file_name=f"{slug}_definicao.json", mime="application/json", use_container_width=True, key="dl_api_def",
                                   help="Reimporte na etapa Definição pra repetir esta bateria depois (senhas não são salvas).")

            if self.config.turso_database_url and self._get_permission_cached("documentos_armazenados"):
                if st.button("🗄️ Salvar em Documentos Armazenados", key="btn_api_store"):
                    self._api_salvar_documentos()
            with st.expander("👁️ Pré-visualizar RELATORIO.md"):
                st.markdown(self.state.get('api_md'))

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
            self.state.set('api_flash', ('error', f"❌ Falha ao gerar o PDF: {error}"))
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
            st.success("✅ Salvo em Documentos Armazenados (grupo 'Testes de API').")
        except DocumentStoreError as error:
            st.error(f"❌ {error}")
        except Exception as error:
            st.error(f"❌ Não foi possível salvar: {error}")
