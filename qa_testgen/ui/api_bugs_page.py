"""
Etapa 3 dos Testes de API — Análise e Bugs (mixin de UserInterface).

Depois da execução, o app:
  1. classifica cada resultado (api_triage): possível bug da API, perfil do
     usuário de teste, divergência a confirmar com o PO, problema da própria
     bateria, bloqueado em cascata;
  2. pra cada suspeita de bug diz se é bug mesmo, como confirmar, o que
     fazer, com quem falar e o texto pronto do que dizer;
  3. sugere correções de FORMATO da bateria a partir das respostas reais
     (api_contracts) — nunca de status esperado nem de texto de requisito;
  4. mantém rascunhos de Bug (ver, editar, excluir, criar a partir de
     qualquer caso) e envia ao Azure DevOps no padrão do app: mostra tudo
     o que vai ser criado, pede confirmação, processa com o overlay e
     termina na tela de resultado com o link de cada Bug.

Reaproveita do Criar Bug: _setup_azure_devops_connection,
_render_bug_metadata_picker (board/coluna/tags/responsável) e
_criar_bug_via_azure (criação + evidência em imagem + comentário).
"""
import html
import json
from datetime import datetime

import pandas as pd
import streamlit as st

from qa_testgen.config import TZ_BR
from qa_testgen.infrastructure import api_contracts as ct
from qa_testgen.infrastructure import api_triage as tri
from qa_testgen.infrastructure.document_store import CONFIG_API_CONTRATOS_PREFIXO
from qa_testgen.ui.auth import SESSION_USER_KEY

_ICONE_GRAVIDADE = {"Crítica": "🟥", "Alta": "🟧", "Média": "🟨", "Baixa": "🟦"}
_GRAVIDADES = list(tri.SEVERIDADE_AZURE.keys())


class ApiAnaliseBugsMixin:

    # ------------------------------------------------------------ contexto e análise
    def _api_usuario_teste(self) -> str:
        for v in self.state.get('api_variaveis') or []:
            nome = v.get('nome', '').lower()
            if 'email' in nome and not v.get('secreto') and (v.get('valor') or '').strip() and not any(
                    p in nome for p in ('invalid', 'inexist', 'inativ', 'inactive', 'gestor', 'other', 'outro', 'wrong', 'errad')):
                return v['valor'].strip()
        return ''

    def _api_contexto_triagem(self) -> dict:
        return {
            "base_url": self.state.get('api_base_url') or '', "ambiente": self.state.get('api_ambiente') or '',
            "projeto": self.state.get('api_projeto') or '', "usuario_teste": self._api_usuario_teste(),
            "work_items": self.state.get('api_work_items') or [],
            "responsaveis": self.state.get('api_responsaveis') or [],
            "especificacao": self._api_especificacao_efetiva(),
        }

    def _api_casos_da_execucao(self) -> list:
        """A bateria como estava NA execução (a triagem compara resultado com o que foi pedido)."""
        return self.state.get('api_casos_executados') or self.state.get('api_casos') or []

    def _api_analise(self) -> dict:
        resultados = self.state.get('api_resultados') or []
        if not resultados:
            return {}
        return tri.analisar(self._api_casos_da_execucao(), resultados, self._api_contexto_triagem())

    # ------------------------------------------------------------ catálogo de formatos
    def _api_contratos(self) -> dict:
        host = self._api_host()
        if not host:
            return {}
        cache = self.state.get('api_contratos')
        if cache and cache.get('host') == host:
            return cache.get('rotas') or {}
        rotas = {}
        store = self._api_catalogo_store()
        if store is not None:
            try:
                raw = store.get(CONFIG_API_CONTRATOS_PREFIXO + host)
                rotas = (json.loads(raw) or {}).get('rotas') or {} if raw else {}
            except Exception:
                rotas = {}
        self.state.set('api_contratos', {"host": host, "rotas": rotas})
        return rotas

    def _api_contratos_aprender(self, casos: list, resultados: list):
        """Chamado ao fim de cada execução: junta o formato visto ao catálogo do host e persiste."""
        host = self._api_host()
        if not host:
            return
        agora = datetime.now(TZ_BR).strftime("%d/%m/%Y %H:%M")
        rotas = ct.aprender(self._api_contratos(), casos, resultados, self.state.get('api_base_url') or '', agora)
        self.state.set('api_contratos', {"host": host, "rotas": rotas})
        store = self._api_catalogo_store()
        if store is not None:
            try:
                store.set(CONFIG_API_CONTRATOS_PREFIXO + host, json.dumps({"rotas": rotas, "atualizado_em": agora}, ensure_ascii=False),
                          st.session_state.get(SESSION_USER_KEY, ""))
            except Exception:
                pass   # fica só nesta sessão; a geração ainda usa o cache

    # ------------------------------------------------------------ página
    def _api_render_analise(self):
        from qa_testgen.ui.api_tests_page import _ETAPAS
        self._processing_banner()
        resultados = self.state.get('api_resultados')
        if not resultados:
            st.info("Execute os testes na etapa **2. Execução** — a análise é feita a partir do resultado.")
            self._api_botao_proxima_etapa(_ETAPAS[1], "⬅️ Ir para 2. Execução", "btn_api_goto_2_from_3")
            return
        an = self._api_analise()
        casos = self._api_casos_da_execucao()
        if self.state.get('api_bateria_alterada'):
            st.warning("A bateria foi alterada depois desta execução (correções aplicadas). A análise abaixo ainda é da "
                       "execução anterior — **execute de novo** (etapa 2) pra medir a bateria corrigida.")
        self._api_render_resumo_analise(an)
        st.divider()
        self._api_render_achados(an, casos, resultados)
        st.divider()
        self._api_render_orientacoes(an, casos, resultados)
        with st.expander("📋 Classificação caso a caso"):
            if an["itens"]:
                st.dataframe(pd.DataFrame([{"#": i["n"], "Caso": i["nome"],
                                            "Classificação": " ".join(tri.CATEGORIAS[i["categoria"]][:2]),
                                            "Por quê": i["explicacao"]} for i in an["itens"]]),
                             hide_index=True, width="stretch")
            else:
                st.caption("Todos os casos executados passaram.")
        st.divider()
        self._api_render_correcoes(resultados)
        st.divider()
        self._api_render_rascunhos(an, casos, resultados)
        st.divider()
        self._api_render_relatorio_previo()
        st.divider()
        self._api_render_envio_bugs(casos, resultados)
        self._api_botao_proxima_etapa(_ETAPAS[3], "➡️ Próxima etapa: 4. Evidências (relatórios com a análise e os bugs)", "btn_api_next_3")

    def _api_render_resumo_analise(self, an: dict):
        cont = an["contagem"]
        bateria = sum(cont[k] for k in ("bateria_auth", "bateria_requisicao", "bateria_formato", "sintaxe", "rota_inexistente"))
        st.markdown("##### 🔍 Leitura do resultado")
        c = st.columns(5)
        c[0].metric("🐞 Possíveis bugs da API", len(an["achados"]), help="Achados agrupados; cada um pode cobrir vários casos.")
        c[1].metric("📋 A confirmar com o PO", cont["a_confirmar"])
        c[2].metric("👤 Perfil / credencial", cont["perfil"])
        c[3].metric("🧰 Problema da bateria", bateria)
        c[4].metric("⏸️ Bloqueados", cont["bloqueado"])
        reprovados = sum(v for k, v in cont.items() if k != "bloqueado")
        if reprovados or cont["bloqueado"]:
            st.caption(
                f"{an['aprovados']} aprovado(s) de {an['executados']} executado(s). Dos {reprovados} que não passaram, "
                f"{cont['bug_api']} apontam para a API; o resto é bateria, perfil ou regra a confirmar — por isso não saia "
                "abrindo bug a partir do \"Reprovado\": use os achados abaixo."
            )

    # ------------------------------------------------------------ achados
    def _api_render_achados(self, an: dict, casos: list, resultados: list):
        st.markdown("##### 🐞 Possíveis bugs da API")
        if not an["achados"]:
            st.success("Nenhum comportamento da API com cara de bug nesta execução (5xx, vazamento de detalhe interno, acesso "
                       "sem credencial aceito, validação ausente, dado sensível ou lentidão fora do comum).")
            return
        if self.state.get('api_work_items') and not self.state.get('api_responsaveis'):
            st.caption("💡 Em **Enviar ao Azure DevOps** (mais abaixo), \"👤 Buscar responsáveis\" troca \"o dev responsável\" pelo "
                       "nome de quem está com o Work Item testado.")
        rascunhos = self.state.get('api_bug_rascunhos') or []
        for a in an["achados"]:
            titulo = f"{_ICONE_GRAVIDADE.get(a['gravidade'], '⬜')} {a['titulo']} — gravidade {a['gravidade']} · {a['confianca']} · {tri._lista_casos(a['casos'])}"
            with st.expander(titulo, expanded=True):
                st.markdown("**Evidência**")
                st.code(a["evidencia"], language=None, wrap_lines=True)
                st.markdown("**É bug mesmo?** " + a["analise"])
                if a["como_confirmar"]:
                    st.markdown("**Como confirmar:**\n" + "\n".join(f"- {x}" for x in a["como_confirmar"]))
                st.markdown("**O que fazer:**\n" + "\n".join(f"- {x}" for x in a["o_que_fazer"]))
                st.markdown("**Com quem falar:**\n" + "\n".join(f"- **{q['quem']}** — {q['por_que']}" for q in a["com_quem_falar"]))
                for m in a["mensagens"]:
                    st.markdown(f"**O que dizer** (para {m['para']}) — copie pelo ícone no canto do quadro:")
                    st.code(m["texto"], language=None, wrap_lines=True)
                existente = next((b for b in rascunhos if b.get("origem") == a["id"]), None)
                if existente:
                    st.success(f"📝 Rascunho já criado ({'enviado — Bug #' + str(existente.get('azure_id')) if existente.get('status') == 'enviado' else 'em Rascunhos de bug, abaixo'}).")
                elif st.button("📝 Criar rascunho de bug", key=f"btn_api_rasc_{a['id']}", width="stretch",
                               disabled=self.state.get('is_processing')):
                    rb = tri.rascunho_de_achado(a, casos, resultados, self._api_contexto_triagem(), self._api_lista_segredos())
                    self.state.set('api_bug_rascunhos', rascunhos + [rb])
                    self._flash_success("Rascunho criado — revise em **📝 Rascunhos de bug** e envie em **☁️ Enviar ao Azure DevOps**.")
                    st.rerun()

    # ------------------------------------------------------------ orientações
    def _api_render_orientacoes(self, an: dict, casos: list, resultados: list):
        if not an["orientacoes"]:
            return
        st.markdown("##### 🧭 O que não é bug (ou ainda não se sabe) — e o que fazer")
        for o in an["orientacoes"]:
            with st.container(border=True):
                st.markdown(f"**{o['titulo']}**")
                st.markdown(o["resumo"])
                if o.get("itens"):
                    st.markdown("\n".join(f"- {l}" for l in o["itens"][:15]))
                if o.get("o_que_fazer"):
                    st.markdown("**O que fazer:**\n" + "\n".join(f"- {x}" for x in o["o_que_fazer"]))
                if o.get("com_quem_falar"):
                    st.markdown("**Com quem falar:**\n" + "\n".join(f"- **{q['quem']}** — {q['por_que']}" for q in o["com_quem_falar"]))
                for m in o.get("mensagens") or []:
                    st.markdown(f"**O que dizer** (para {m['para']}):")
                    st.code(m["texto"], language=None, wrap_lines=True)
                st.caption(tri._lista_casos(o.get("casos") or []).capitalize())
                if o["categoria"] == "a_confirmar":
                    self._api_render_rascunho_de_divergencia(an, casos, resultados)

    def _api_render_rascunho_de_divergencia(self, an: dict, casos: list, resultados: list):
        itens = [i for i in an["itens"] if i["categoria"] == "a_confirmar"]
        rotulos = {f"{i['n']}. {i['nome']}": i for i in itens}
        c1, c2 = st.columns([3, 2])
        with c1:
            escolha = st.selectbox("O PO confirmou que o card está certo? Abra o bug a partir do caso:", list(rotulos.keys()),
                                   index=None, placeholder="Escolha o caso...", key="apiw_rasc_diverg_sel")
        with c2:
            st.markdown("<div style='margin-top:1.8rem'></div>", unsafe_allow_html=True)
            if st.button("📝 Criar rascunho deste caso", key="btn_api_rasc_diverg", disabled=not escolha, width="stretch"):
                self._api_criar_rascunho_de_caso(rotulos[escolha]["case_id"], casos, resultados,
                                                 rotulos[escolha]["explicacao"] + " O PO confirmou que o card está certo.")

    def _api_criar_rascunho_de_caso(self, case_id: str, casos: list, resultados: list, explicacao: str = ""):
        caso = next((c for c in casos if c["id"] == case_id), None)
        r = next((x for x in resultados if x.case_id == case_id), None)
        if caso is None:
            return
        rb = tri.rascunho_de_caso(caso, r, casos, self._api_contexto_triagem(), self._api_lista_segredos(), explicacao)
        self.state.set('api_bug_rascunhos', (self.state.get('api_bug_rascunhos') or []) + [rb])
        self._flash_success("Rascunho criado a partir do caso — revise em **📝 Rascunhos de bug**.")
        st.rerun()

    # ------------------------------------------------------------ correções da bateria
    @staticmethod
    def _api_chave_sugestao(s: dict) -> str:
        return f"{s['tipo']}|{s['case_id']}|{json.dumps(s['patch'], sort_keys=True, ensure_ascii=False)}"

    def _api_render_correcoes(self, resultados: list):
        st.markdown("##### 🔧 Corrigir a bateria pelas respostas reais")
        st.caption(
            "O app compara o que cada caso pediu com o que a API realmente respondeu e sugere só correções de **formato**: "
            "nome de campo que a API valida, caminho real do campo na resposta, header de token esquecido, id extraído de "
            "uma resposta que funcionou, comparação sem sentido entre mensagens. **Status esperado e texto de mensagem do "
            "card nunca são alterados** — divergência de regra fica em \"A confirmar com o PO\"."
        )
        sugestoes = ct.sugerir_correcoes(self.state.get('api_casos') or [], resultados)
        if not sugestoes:
            st.success("Nenhuma correção de formato pendente para esta bateria.")
            return
        por_chave = {self._api_chave_sugestao(s): s for s in sugestoes}
        df = pd.DataFrame([{"Aplicar": True, "Caso": f"{s['n']}. {s['nome']}", "Correção": s["titulo"], "Antes": s["antes"],
                            "Depois": s["depois"], "Por quê (evidência real)": s["evidencia"], "_chave": k}
                           for k, s in por_chave.items()])
        editado = st.data_editor(
            df, hide_index=True, width="stretch", key=f"apiw_corr_{self.state.get('api_execucao_id')}_{len(df)}",
            disabled=[c for c in df.columns if c != "Aplicar"], column_config={"_chave": None},
        )
        escolhidas = [por_chave[k] for k, marcado in zip(editado["_chave"], editado["Aplicar"]) if marcado]
        if st.button(f"🔧 Aplicar {len(escolhidas)} correção(ões) na bateria", key="azure_blue_btn_api_corr", width="stretch",
                     disabled=not escolhidas or self.state.get('is_processing')):
            self.state.set('api_correcoes_confirmar', [self._api_chave_sugestao(s) for s in escolhidas])
            st.rerun()
        pendentes = self.state.get('api_correcoes_confirmar')
        if pendentes:
            confirmar = [por_chave[k] for k in pendentes if k in por_chave]
            with st.container(border=True):
                st.markdown("##### ⚠️ Confirmar alteração da bateria")
                st.markdown("Estas alterações vão para os casos da etapa 1 (dá pra editar de novo lá, a qualquer momento):")
                st.markdown("\n".join(f"- **Caso {s['n']}** — {s['titulo']}: `{s['antes']}` → `{s['depois']}`" for s in confirmar[:40])
                            + ("\n- …" if len(confirmar) > 40 else ""))
                st.caption("Os resultados na tela continuam sendo da execução anterior até você executar de novo.")
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("✅ Sim, aplicar", type="primary", width="stretch", key="btn_api_corr_sim"):
                        novos, feito = ct.aplicar_correcoes(self.state.get('api_casos') or [], confirmar)
                        self.state.set('api_casos', novos)
                        self.state.set('api_correcoes_aplicadas', (self.state.get('api_correcoes_aplicadas') or []) + feito)
                        self.state.set('api_bateria_alterada', True)
                        self.state.set('api_correcoes_confirmar', None)
                        for k in [k for k in st.session_state.keys() if isinstance(k, str) and k.startswith(("apiw_hdr_", "apiw_body_", "apiw_asr_", "apiw_ext_"))]:
                            del st.session_state[k]   # os editores dos casos passam a mostrar a versão corrigida
                        self._log("Testes de API", "Corrigir bateria pelas respostas reais", f"{len(feito)} correção(ões) em '{self.state.get('api_projeto')}'")
                        self._flash_success(f"{len(feito)} correção(ões) aplicada(s). Execute de novo (etapa 2) para medir a bateria corrigida.")
                        st.rerun()
                with c2:
                    if st.button("❌ Cancelar", width="stretch", key="btn_api_corr_nao"):
                        self.state.set('api_correcoes_confirmar', None)
                        st.rerun()

    # ------------------------------------------------------------ rascunhos (CRUD)
    def _api_render_rascunhos(self, an: dict, casos: list, resultados: list):
        st.markdown("##### 📝 Rascunhos de bug")
        rascunhos = list(self.state.get('api_bug_rascunhos') or [])
        with st.expander("➕ Novo rascunho a partir de qualquer caso executado"):
            executados = [r for r in resultados if not r.pulado]
            nomes = {r.case_id: n for n, r in enumerate(resultados, 1)}
            rot = {f"{nomes[r.case_id]}. {r.nome} — {r.resultado_label}": r.case_id for r in executados}
            escolha = st.selectbox("Caso", list(rot.keys()), index=None, placeholder="Escolha o caso...", key="apiw_rasc_caso_sel")
            if st.button("📝 Criar rascunho", key="btn_api_rasc_caso", disabled=not escolha):
                self._api_criar_rascunho_de_caso(rot[escolha], casos, resultados)
        if not rascunhos:
            st.caption("Nenhum rascunho ainda. Crie a partir de um achado (acima) ou de qualquer caso.")
            return
        st.caption("Revise antes de enviar: 👁️ mostra exatamente o que vai para o Azure DevOps, ✏️ altera, 🗑️ exclui o rascunho.")
        rotulo_caso = {c["id"]: f"{n}. {c.get('nome', '')}" for n, c in enumerate(casos, 1)}
        for b in rascunhos:
            enviado = b.get("status") == "enviado"
            with st.container(border=True):
                cab, a1, a2, a3 = st.columns([6, 1, 1, 1])
                with cab:
                    estado = f"✅ Enviado — Bug #{b.get('azure_id')}" if enviado else "📝 Rascunho"
                    st.markdown(f"**{b['titulo']}**  \n{estado} · {_ICONE_GRAVIDADE.get(b.get('gravidade'), '')} {b.get('gravidade', '')} · "
                                f"{b.get('severidade')} · prioridade {b.get('prioridade')} · {len(b.get('case_ids') or [])} caso(s) de evidência")
                    az = b.get("azure_estado")
                    if az:
                        st.caption(f"No Azure (verificado {az.get('verificado_em')}): State **{az.get('state')}** · coluna "
                                   f"**{az.get('board_column') or '—'}** · responsável **{az.get('assigned_to') or 'ninguém'}**")
                with a1:
                    if st.button("👁️", key=f"btn_api_rasc_ver_{b['id']}", help="Ver como vai para o Azure DevOps", width="stretch"):
                        self.state.set('api_bug_vendo', None if self.state.get('api_bug_vendo') == b['id'] else b['id'])
                        st.rerun()
                with a2:
                    if enviado:
                        st.link_button("🔗", b.get("azure_url") or "#", help="Abrir no Azure DevOps", width="stretch")
                    elif st.button("✏️", key=f"btn_api_rasc_edit_{b['id']}", help="Alterar", width="stretch",
                                   disabled=self.state.get('is_processing')):
                        self.state.set('api_bug_editando', b['id'])
                        st.rerun()
                with a3:
                    if st.button("🗑️", key=f"btn_api_rasc_del_{b['id']}", width="stretch",
                                 help="Tirar da lista (o Bug continua no Azure)" if enviado else "Excluir rascunho",
                                 disabled=self.state.get('is_processing')):
                        self.state.set('api_bug_excluir', b['id'])
                        st.rerun()

                if self.state.get('api_bug_excluir') == b['id']:
                    st.warning("Tirar este item da lista? O Bug **continua no Azure DevOps** (o app não exclui nada lá)." if enviado
                               else "Excluir este rascunho? Isso não pode ser desfeito.")
                    d1, d2 = st.columns(2)
                    with d1:
                        if st.button("🗑️ Sim, excluir", type="primary", width="stretch", key=f"btn_api_rasc_del_sim_{b['id']}"):
                            self.state.set('api_bug_rascunhos', [x for x in rascunhos if x['id'] != b['id']])
                            self.state.set('api_bug_excluir', None)
                            st.rerun()
                    with d2:
                        if st.button("Cancelar", width="stretch", key=f"btn_api_rasc_del_nao_{b['id']}"):
                            self.state.set('api_bug_excluir', None)
                            st.rerun()

                if self.state.get('api_bug_editando') == b['id'] and not enviado:
                    self._api_render_form_rascunho(b, rotulo_caso)
                elif self.state.get('api_bug_vendo') == b['id']:
                    self._api_render_preview_rascunho(b, rotulo_caso, resultados)

    def _api_render_form_rascunho(self, b: dict, rotulo_caso: dict):
        with st.form(f"apiw_rasc_form_{b['id']}"):
            titulo = st.text_input("Título *", value=b["titulo"])
            g1, g2, g3 = st.columns(3)
            with g1:
                gravidade = st.selectbox("Gravidade", _GRAVIDADES, index=_GRAVIDADES.index(b.get("gravidade", "Média")))
            with g2:
                sevs = list(tri.SEVERIDADE_AZURE.values())
                severidade = st.selectbox("Severidade (Azure)", sevs, index=sevs.index(b.get("severidade")) if b.get("severidade") in sevs else 2)
            with g3:
                prioridade = st.selectbox("Prioridade (Azure)", [1, 2, 3, 4], index=[1, 2, 3, 4].index(b.get("prioridade", 2)))
            descricao = st.text_area("Descrição *", value=b.get("descricao", ""), height=180)
            passos = st.text_area("Passos de Reprodução * (um por linha)", value="\n".join(b.get("passos") or []), height=150)
            esperado = st.text_area("Resultado esperado (vai em Acceptance Criteria)", value=b.get("esperado", ""), height=70)
            obtido = st.text_area("Resultado obtido", value=b.get("obtido", ""), height=70)
            ids_validos = [i for i in (b.get("case_ids") or []) if i in rotulo_caso]
            evid = st.multiselect("Casos cuja evidência (request/response) vai anexada", list(rotulo_caso.keys()),
                                  default=ids_validos, format_func=lambda i: rotulo_caso[i])
            anexar_pdf = st.checkbox("Anexar também o RELATORIO.pdf da execução", value=b.get("anexar_pdf", True))
            discussion = st.text_area("Primeiro comentário no Bug (Discussion, opcional)", value=b.get("discussion", ""), height=70)
            s1, s2 = st.columns(2)
            salvar = s1.form_submit_button("💾 Salvar alterações", type="primary", width="stretch")
            cancelar = s2.form_submit_button("Cancelar", width="stretch")
        if cancelar:
            self.state.set('api_bug_editando', None)
            st.rerun()
        if salvar:
            faltam = [n for n, v in (("Título", titulo), ("Descrição", descricao), ("Passos de Reprodução", passos)) if not v.strip()]
            if faltam:
                st.error("Preencha: " + ", ".join(faltam) + ".")
                return
            atualizado = {**b, "titulo": titulo.strip(), "gravidade": gravidade, "severidade": severidade, "prioridade": prioridade,
                          "descricao": descricao.strip(), "passos": [p.strip() for p in passos.splitlines() if p.strip()],
                          "esperado": esperado.strip(), "obtido": obtido.strip(), "case_ids": evid, "anexar_pdf": anexar_pdf,
                          "discussion": discussion.strip()}
            self.state.set('api_bug_rascunhos', [atualizado if x['id'] == b['id'] else x for x in self.state.get('api_bug_rascunhos') or []])
            self.state.set('api_bug_editando', None)
            self._flash_success("Rascunho salvo.")
            st.rerun()

    def _api_render_preview_rascunho(self, b: dict, rotulo_caso: dict, resultados: list):
        st.markdown(f"**Título:** {b['titulo']}")
        st.markdown(f"**Severidade:** {b.get('severidade')} · **Prioridade:** {b.get('prioridade')}")
        st.markdown("**Descrição:**")
        st.text(self._api_descricao_bug(b))
        st.markdown("**Passos de Reprodução:**\n" + "\n".join(f"{i}. {p}" for i, p in enumerate(b.get("passos") or [], 1)))
        if b.get("esperado"):
            st.markdown(f"**Acceptance Criteria (esperado):** {b['esperado']}")
        st.markdown(f"**System Info:** {self._api_system_info_bug(b)}")
        arquivos = tri.arquivos_de_evidencia(b, resultados, self._api_lista_segredos())
        st.markdown("**Anexos:** " + (", ".join(f"`{n}`" for n, _ in arquivos) or "nenhum")
                    + (" + `RELATORIO-testes-de-api.pdf`" if b.get("anexar_pdf") else ""))
        imgs = self._api_imagens_do_rascunho(b)
        if imgs:
            st.markdown(f"**Imagens (prints da etapa 4):** {len(imgs)} — entram anexadas e embutidas no System Info")
        if b.get("mensagem"):
            st.markdown("**Texto pronto pro chat** (não vai pro Azure):")
            st.code(b["mensagem"], language=None, wrap_lines=True)

    # ------------------------------------------------------------ relatório antes do Azure
    def _api_render_relatorio_previo(self):
        """
        Regra do app: todo relatório em PDF pode ser baixado ANTES de qualquer
        integração com o Azure DevOps. Aqui ele sai com tudo o que foi decidido
        nesta etapa — análise, correções aplicadas e os Bugs (rascunhos ainda
        não enviados inclusive) — pra revisar ou mandar pra alguém antes de subir.
        """
        st.markdown("##### 📄 Relatório completo — antes de enviar ao Azure")
        st.caption("PDF e .md com a execução, a análise (com o que fazer, com quem falar e o que dizer), as correções "
                   "aplicadas na bateria e **todos os Bugs**, com a situação de cada um (rascunho ou enviado). "
                   "É o mesmo relatório da etapa 4 — gerar aqui ou lá dá no mesmo.")
        if st.button("📝 Gerar relatório completo (PDF + MD)", key="azure_blue_btn_api_gen_previo", width="stretch",
                     disabled=self.state.get('is_processing')):
            with st.spinner("Gerando o relatório..."):
                self._api_gerar_relatorios()
            st.rerun()
        self._api_mostrar_erro_relatorio()
        if not self.state.get('api_pdf'):
            return
        if self._api_relatorio_desatualizado():
            st.warning("Os rascunhos, as correções ou os responsáveis mudaram depois deste relatório — gere de novo pra "
                       "ele sair com o que está na tela agora.")
        else:
            st.success(f"Relatório gerado em {self.state.get('api_relatorio_gerado_em') or '—'}, com o que está na tela.")
        from qa_testgen.infrastructure.api_evidence import ApiEvidenceBuilder
        slug = ApiEvidenceBuilder.slug(self.state.get('api_projeto') or 'testes-api', 40)
        c1, c2 = st.columns(2)
        with c1:
            st.download_button("⬇️ RELATORIO.pdf", self.state.get('api_pdf'), file_name=f"{slug}_RELATORIO.pdf", mime="application/pdf",
                               width="stretch", key="dl_api_pdf_previo", on_click=self._api_marcar_baixado)
        with c2:
            st.download_button("⬇️ RELATORIO.md", self.state.get('api_md').encode('utf-8'), file_name=f"{slug}_RELATORIO.md",
                               mime="text/markdown", width="stretch", key="dl_api_md_previo", on_click=self._api_marcar_baixado)

    # ------------------------------------------------------------ montagem do Bug
    @staticmethod
    def _api_descricao_bug(b: dict) -> str:
        partes = [b.get("descricao", "").strip()]
        if b.get("esperado"):
            partes.append(f"Resultado esperado: {b['esperado']}")
        if b.get("obtido"):
            partes.append(f"Resultado obtido: {b['obtido']}")
        return "\n\n".join(p for p in partes if p)

    def _api_system_info_bug(self, b: dict) -> str:
        return (f"Ambiente: {self.state.get('api_ambiente') or '—'} · Base URL: {self.state.get('api_base_url') or '—'} · "
                f"Bateria: {self.state.get('api_projeto') or '—'} · Encontrado pelo QA TestGen (Testes de API) em "
                f"{self.state.get('api_executado_em') or datetime.now(TZ_BR).strftime('%d/%m/%Y %H:%M')}")

    def _api_imagens_do_rascunho(self, b: dict) -> list:
        imagens = self.state.get('api_imagens') or {}
        return [img for cid in b.get("case_ids") or [] for img in imagens.get(cid, [])]

    # ------------------------------------------------------------ envio ao Azure DevOps
    def _api_render_envio_bugs(self, casos: list, resultados: list):
        st.markdown("##### ☁️ Enviar ao Azure DevOps")
        rascunhos = self.state.get('api_bug_rascunhos') or []
        pendentes = [b for b in rascunhos if b.get("status") != "enviado"]
        enviados = [b for b in rascunhos if b.get("status") == "enviado"]
        resultado = self.state.get('api_bug_envio_resultado')
        if not (pendentes or enviados or resultado or self.state.get('current_action') == 'api_enviar_bugs'):
            st.caption("Crie um rascunho de bug (acima) para enviar.")
            return
        if not self._get_permission_cached("criar_bug"):
            st.warning("Para criar Bugs no Azure DevOps a partir daqui você precisa da permissão **Criar Bug** — peça ao administrador. "
                       "Os rascunhos continuam aqui e o texto pronto pode ser copiado.")
            return
        conn = self._setup_azure_devops_connection(show_area_path_picker=False)
        if conn is None:
            return
        ado_client, _org, ado_project, _ = conn
        self._api_processar_envio_bugs(ado_client, casos, resultados)
        self._api_render_resultado_envio(ado_client)

        # responsáveis dos Work Items testados (com quem falar) e estado dos Bugs já enviados
        c1, c2 = st.columns(2)
        with c1:
            wis = self.state.get('api_work_items') or []
            if wis and st.button(f"👤 Buscar responsáveis dos {len(wis)} Work Item(s) testado(s)", key="azure_blue_btn_api_resp",
                                 width="stretch", disabled=self.state.get('is_processing'),
                                 help="Troca \"o dev responsável\" pelo nome de quem está com cada Work Item, nas orientações acima."):
                try:
                    with st.spinner("Consultando o Azure DevOps..."):
                        est = ado_client.get_work_items_status([w['id'] for w in wis])
                    self.state.set('api_responsaveis', [{"id": e["id"], "titulo": e["title"], "responsavel": e["assigned_to"]} for e in est])
                    self._flash_success("Responsáveis: " + "; ".join(f"#{e['id']} → {e['assigned_to'] or 'ninguém'}" for e in est))
                except Exception as error:
                    self._flash_error(f"Não foi possível consultar os Work Items: {error}")
                st.rerun()
        with c2:
            if enviados and st.button(f"🔄 Verificar no Azure os {len(enviados)} Bug(s) enviado(s)", key="azure_blue_btn_api_verif_bugs",
                                      width="stretch", disabled=self.state.get('is_processing')):
                try:
                    with st.spinner("Consultando o Azure DevOps..."):
                        est = {e["id"]: e for e in ado_client.get_work_items_status([b["azure_id"] for b in enviados])}
                    agora = datetime.now(TZ_BR).strftime("%d/%m %H:%M")
                    self.state.set('api_bug_rascunhos', [
                        {**b, "azure_estado": {**est[b["azure_id"]], "verificado_em": agora}} if b.get("azure_id") in est else b
                        for b in rascunhos])
                    faltando = [b["azure_id"] for b in enviados if b.get("azure_id") not in est]
                    (self._flash_warning if faltando else self._flash_success)(
                        "Status atualizado nos cartões dos rascunhos." + (f" Não encontrados (excluídos ou sem acesso): {faltando}" if faltando else ""))
                except Exception as error:
                    self._flash_error(f"Não foi possível consultar os Bugs: {error}")
                st.rerun()

        if not pendentes:
            return
        st.markdown("**Onde criar**")
        if self.state.get('ado_available_area_paths') and self.state.get('ado_area_paths_project') == ado_project:
            area_paths = self.state.get('ado_available_area_paths') or []
        else:
            try:
                with st.spinner("Buscando Area Paths do projeto..."):
                    area_paths = ado_client.list_area_paths()
                self.state.set('ado_available_area_paths', area_paths)
                self.state.set('ado_area_paths_project', ado_project)
            except Exception as error:
                st.error(f"❌ Não foi possível buscar Area Paths: {error}")
                area_paths = []
        board = st.selectbox("Board (Area Path) *", area_paths, index=None, placeholder="Escolha o board...", key="apiw_bug_board",
                             disabled=self.state.get('is_processing'))
        if not board:
            st.caption("Escolha o board pra continuar.")
            return
        metadata = self._render_bug_metadata_picker(ado_client, "api", board)

        rot = {b["id"]: f"{b['titulo']} ({b.get('severidade')})" for b in pendentes}
        escolhidos = st.multiselect("Rascunhos a enviar", list(rot.keys()), default=list(rot.keys()), format_func=lambda i: rot[i],
                                    key="apiw_bug_envio_sel", disabled=self.state.get('is_processing'))
        wis = self.state.get('api_work_items') or []
        vincular_wi = st.checkbox(
            "Vincular cada Bug (Related) aos Work Items testados: " + ", ".join(f"#{w['id']}" for w in wis),
            value=True, key="apiw_bug_vinc_wi", disabled=not wis) if wis else False
        mapa_tc = self._api_mapa_test_cases(casos)
        vincular_tc = st.checkbox(f"Vincular aos Test Cases desta bateria já criados no Azure (Tested By) — {len(mapa_tc)} mapeado(s)",
                                  value=True, key="apiw_bug_vinc_tc") if mapa_tc else False

        pode = bool(escolhidos) and bool(metadata.get("coluna"))
        if st.button(f"🐛 Criar {len(escolhidos)} Bug(s) no Azure DevOps", type="primary", width="stretch", key="btn_api_bug_enviar",
                     disabled=not pode or self.state.get('is_processing')):
            self.state.set('api_bug_envio_snapshot', {
                "ids": escolhidos, "board": board, "coluna": metadata["coluna"], "coluna_team_id": metadata.get("team_id"),
                "coluna_board_id": metadata.get("coluna_board_id"), "coluna_state": metadata.get("coluna_state"),
                "tags": metadata.get("tags") or [], "atribuir_a": metadata.get("atribuir_a"),
                "wi_ids": [w['id'] for w in wis] if vincular_wi else [], "tc_map": mapa_tc if vincular_tc else {},
            })
            self.state.set('api_bug_envio_confirmar', True)
            st.rerun()
        if not pode:
            st.caption("Preencha: " + ", ".join(x for x, ok in (("pelo menos 1 rascunho", escolhidos), ("Coluna do Board (busque acima)", metadata.get("coluna"))) if not ok) + ".")
        self._api_render_confirmacao_envio(resultados)

    def _api_mapa_test_cases(self, casos: list) -> dict:
        """{case_id: test_case_id} — Test Cases desta bateria criados pelo Passo 7 (Levar para o assistente)."""
        ids = self.state.get('ado_test_case_ids') or {}
        return {c["id"]: ids[c.get("nome")] for c in casos if c.get("nome") in ids}

    def _api_render_confirmacao_envio(self, resultados: list):
        snap = self.state.get('api_bug_envio_snapshot')
        if not (self.state.get('api_bug_envio_confirmar') and snap):
            return
        por_id = {b["id"]: b for b in self.state.get('api_bug_rascunhos') or []}
        bugs = [por_id[i] for i in snap["ids"] if i in por_id]
        segredos = self._api_lista_segredos()
        with st.container(border=True):
            st.markdown(f"##### 🐛 Confirmar criação de {len(bugs)} Bug(s)")
            st.markdown(f"**Board (Area Path):** {snap['board']}  \n**Coluna:** {snap['coluna']}"
                        + (f"  \n**Tags:** {', '.join(snap['tags'])}" if snap['tags'] else "")
                        + (f"  \n**Atribuído a:** {snap['atribuir_a']}" if snap['atribuir_a'] else ""))
            for b in bugs:
                arquivos = tri.arquivos_de_evidencia(b, resultados, segredos)
                vinc = []
                if snap["wi_ids"]:
                    vinc.append("Related → " + ", ".join(f"#{w}" for w in snap["wi_ids"]))
                tcs = sorted({snap["tc_map"][c] for c in b.get("case_ids") or [] if c in snap["tc_map"]})
                if tcs:
                    vinc.append("Tested By → " + ", ".join(f"#{t}" for t in tcs))
                with st.expander(f"🐞 {b['titulo']}", expanded=len(bugs) <= 3):
                    st.markdown(f"**Severidade:** {b.get('severidade')} · **Prioridade:** {b.get('prioridade')}")
                    st.text(self._api_descricao_bug(b)[:1500])
                    st.markdown("**Passos:**\n" + "\n".join(f"{i}. {p}" for i, p in enumerate(b.get("passos") or [], 1)))
                    st.markdown(f"**Anexos:** {len(arquivos)} evidência(s) .txt" + (" + RELATORIO.pdf" if b.get("anexar_pdf") else "")
                                + (f" + {len(self._api_imagens_do_rascunho(b))} imagem(ns)" if self._api_imagens_do_rascunho(b) else ""))
                    st.markdown("**Vínculos:** " + ("; ".join(vinc) if vinc else "nenhum"))
            if not self.state.get('api_pdf') or self._api_relatorio_desatualizado():
                st.info("📄 Quer o PDF com tudo isso antes de enviar? Use **Gerar relatório completo**, logo acima.")
            st.warning("Isso cria itens reais no Azure DevOps e **não pode ser desfeito pelo app** — se algo sair errado, a "
                       "exclusão precisa ser feita lá. Tem certeza que deseja prosseguir?")
            c1, c2 = st.columns(2)
            with c1:
                if st.button(f"🐛 Sim, criar {len(bugs)} Bug(s)", type="primary", width="stretch", key="btn_api_bug_sim"):
                    self.state.set('api_bug_envio_confirmar', False)
                    self.state.set('_api_bug_envio_confirmado', True)
                    st.rerun()
            with c2:
                if st.button("❌ Cancelar", width="stretch", key="btn_api_bug_nao"):
                    self.state.set('api_bug_envio_confirmar', False)
                    st.rerun()

    def _api_processar_envio_bugs(self, ado_client, casos: list, resultados: list):
        """Mesmo padrão do Criar Bug: a confirmação só marca; o próximo render liga o processamento (overlay global)."""
        if self.state.get('_api_bug_envio_confirmado'):
            self.state.set('_api_bug_envio_confirmado', False)
            self.state.set('current_action', 'api_enviar_bugs')
            self.state.set('is_processing', True)
            st.rerun()
        if self.state.get('current_action') != 'api_enviar_bugs' or self.state.get('show_interrupt_modal'):
            return
        snap = self.state.get('api_bug_envio_snapshot') or {}
        rascunhos = list(self.state.get('api_bug_rascunhos') or [])
        por_id = {b["id"]: b for b in rascunhos}
        segredos = self._api_lista_segredos()
        precisa_pdf = any(por_id[i].get("anexar_pdf") for i in snap.get("ids", []) if i in por_id)
        if precisa_pdf and not self.state.get('api_pdf'):
            try:
                self._api_gerar_relatorios()
            except Exception:
                pass
        pdf = self.state.get('api_pdf')
        itens = []
        for rid in snap.get("ids", []):
            b = por_id.get(rid)
            if b is None:
                continue
            try:
                dados = {
                    "titulo": b["titulo"], "board": snap["board"],
                    "descricao": html.escape(self._api_descricao_bug(b)).replace("\n", "<br>"),
                    "passos": b.get("passos") or [b["titulo"]], "prioridade": b.get("prioridade"), "severidade": b.get("severidade"),
                    "coluna": snap["coluna"], "coluna_team_id": snap.get("coluna_team_id"), "coluna_board_id": snap.get("coluna_board_id"),
                    "coluna_state": snap.get("coluna_state"), "tags": snap.get("tags") or [], "atribuir_a": snap.get("atribuir_a"),
                    "system_info": self._api_system_info_bug(b), "acceptance_criteria": b.get("esperado", ""),
                    "discussion": b.get("discussion", ""), "imagens": self._api_imagens_do_rascunho(b), "vinculo": None,
                }
                criado, avisos = self._criar_bug_via_azure(ado_client, dados)
                anexos = list(tri.arquivos_de_evidencia(b, resultados, segredos))
                if b.get("anexar_pdf") and pdf:
                    anexos.append(("RELATORIO-testes-de-api.pdf", pdf))
                for nome, conteudo in anexos:
                    try:
                        up = ado_client.upload_attachment(conteudo, nome)
                        ado_client.attach_file_to_work_item(criado["id"], up["url"], comment="Evidência do Testes de API (QA TestGen)")
                    except Exception as error:
                        avisos.append(f"⚠️ Anexo '{nome}' não foi enviado: {error}")
                for wi in snap.get("wi_ids") or []:
                    try:
                        ado_client.link_bug_to_related_work_item(criado["id"], wi)
                    except Exception as error:
                        avisos.append(f"⚠️ Vínculo com o Work Item #{wi} falhou: {error}")
                for tc in sorted({snap.get("tc_map", {})[c] for c in b.get("case_ids") or [] if c in snap.get("tc_map", {})}):
                    try:
                        ado_client.link_bug_to_test_case(criado["id"], tc)
                    except Exception as error:
                        avisos.append(f"⚠️ Vínculo com o Test Case #{tc} falhou: {error}")
                agora = datetime.now(TZ_BR).strftime("%d/%m/%Y %H:%M")
                por_id[rid] = {**b, "status": "enviado", "azure_id": criado["id"], "azure_url": criado.get("url") or ado_client.work_item_url(criado["id"]),
                               "enviado_em": agora}
                itens.append({"ok": True, "id": criado["id"], "titulo": b["titulo"], "url": por_id[rid]["azure_url"],
                              "anexos": len(anexos), "avisos": avisos})
                self._log("Testes de API", "Criar Bug (Testes de API)", f"Bug #{criado['id']} '{b['titulo']}' — {len(anexos)} anexo(s)")
            except Exception as error:
                itens.append({"ok": False, "titulo": b["titulo"], "erro": str(error)})
        self.state.set('api_bug_rascunhos', [por_id.get(b["id"], b) for b in rascunhos])
        self.state.set('api_bug_envio_resultado', {"itens": itens, "quando": datetime.now(TZ_BR).strftime("%d/%m/%Y %H:%M")})
        self.state.set('api_bug_envio_snapshot', None)
        self.state.set('scroll_to_top_pending', True)
        self.clear_action()
        st.rerun()

    def _api_render_resultado_envio(self, ado_client):
        res = self.state.get('api_bug_envio_resultado')
        if not res:
            return
        ok = [i for i in res["itens"] if i["ok"]]
        falhas = [i for i in res["itens"] if not i["ok"]]
        with st.container(border=True):
            st.markdown(f"##### 📋 Resultado da integração — {res['quando']}")
            for i in ok:
                st.markdown(f"✅ **Bug #{i['id']}** — [{i['titulo']}]({i['url']}) · {i['anexos']} anexo(s)")
                for a in i["avisos"]:
                    st.caption(a)
            for i in falhas:
                st.error(f"❌ {i['titulo']}: {i['erro']}")
            url_todos = self._wi_fila_url_consulta(ado_client, [i["id"] for i in ok]) if ok else ""
            if url_todos:
                st.markdown(f"🔗 [Ver todos juntos no Azure DevOps]({url_todos})")
            linhas = [f"# Bugs criados pelos Testes de API — {res['quando']}", "", "| ID | Título | Link |", "|---|---|---|"]
            linhas += [f"| {i['id']} | {str(i['titulo']).replace('|', '/')} | {i['url']} |" for i in ok]
            if url_todos:
                linhas += ["", f"Ver todos juntos: {url_todos}"]
            if falhas:
                linhas += ["", "## Não criados", ""] + [f"- {i['titulo']}: {i['erro']}" for i in falhas]
            c1, c2 = st.columns(2)
            with c1:
                st.download_button("⬇️ Baixar este resumo (.md)", ("\n".join(linhas) + "\n").encode("utf-8"),
                                   file_name="bugs-testes-de-api.md", mime="text/markdown", width="stretch", key="dl_api_bugs_resumo")
            with c2:
                if st.button("Limpar este resumo", width="stretch", key="btn_api_bugs_limpar_res"):
                    self.state.set('api_bug_envio_resultado', None)
                    st.rerun()
