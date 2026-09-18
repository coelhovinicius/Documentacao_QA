"""
Criar Work Item — modo "Fila / planilha": monta uma fila com vários Work
Items (adicionados do formulário manual ou lidos de CSV/XLSX/TXT) e envia
tudo de uma vez ao Azure DevOps, com validação linha a linha, modelo de
planilha gerado a partir do projeto e resultado por item.

Mixin de UserInterface (mesmo padrão de ApiTestsPageMixin).
"""
import uuid

import pandas as pd
import streamlit as st

from qa_testgen.infrastructure import work_item_batch as wib
from qa_testgen.infrastructure.azure_devops_client import AzureDevOpsClient


WI_BATCH_STATE_DEFAULTS = {
    'wi_fila': [],                 # itens aguardando envio
    'wi_fila_resultados': [],      # itens já enviados (sucesso) na última rodada
    'wi_fila_listas': None,        # {"project", "area_paths", "iterations", "pessoas", "extras_por_tipo"}
    'wi_fila_prevalidado': None,   # linhas do último arquivo validado
    'wi_fila_confirmando': False,
}


class WorkItemBatchMixin:

    # ---------------------------------------------------------------- dados
    def _wi_fila_listas(self, ado_client, ado_project: str, tipos: list, catalogo: dict, forcar: bool = False):
        """
        Area Paths, Iterations, pessoas e campos obrigatórios de TODOS os
        tipos — usados pelo modelo e pela validação. Uma carga por projeto.
        """
        atual = self.state.get('wi_fila_listas')
        if atual and atual.get("project") == ado_project and not forcar:
            return atual
        try:
            with st.spinner("Carregando listas do projeto (Area Paths, Iterations, pessoas, campos por tipo)..."):
                area_paths = ado_client.list_area_paths()
                try:
                    iterations = ado_client.list_iteration_paths()
                except Exception:
                    iterations = []
                pessoas_por_chave = {}
                try:
                    for equipe in ado_client.list_teams():
                        try:
                            for m in ado_client.list_team_members(equipe["id"]):
                                chave = m.get("unique_name") or m.get("display_name")
                                if chave:
                                    pessoas_por_chave.setdefault(chave, m)
                        except Exception:
                            continue
                except Exception:
                    pass
                campos_por_tipo = {}
                for t in tipos:
                    nome = t["name"] if isinstance(t, dict) else str(t)
                    campos = self._wi_campos_do_tipo(ado_client, nome)
                    campos_por_tipo[nome] = campos or []
            listas = {
                "project": ado_project,
                "area_paths": area_paths or [],
                "iterations": iterations or [],
                "pessoas": sorted(pessoas_por_chave.values(), key=lambda m: (m.get("display_name") or "").lower()),
                "campos_por_tipo": campos_por_tipo,
                "extras_por_tipo": wib.campos_extras_por_tipo(tipos, campos_por_tipo, catalogo, AzureDevOpsClient.CAMPOS_COM_WIDGET_PROPRIO),
            }
            self.state.set('wi_fila_listas', listas)
            return listas
        except Exception as error:
            st.error(f"❌ Não foi possível carregar as listas do projeto: {error}")
            return None

    def _wi_fila_adicionar(self, item: dict):
        fila = list(self.state.get('wi_fila') or [])
        item.setdefault("uid", str(uuid.uuid4()))
        item.setdefault("erros", [])
        item.setdefault("resultado", None)
        fila.append(item)
        self.state.set('wi_fila', fila)

    # ---------------------------------------------------------------- tela
    def _wi_modo_fila(self, ado_client, ado_project: str, tipos: list, catalogo: dict):
        st.markdown("##### 📋 Fila de Work Items")
        st.caption(
            "Monte a fila de duas formas: pelo formulário do modo **\"Um Work Item\"** (botão **➕ Adicionar à fila**) "
            "e/ou subindo uma planilha. Nada é criado no Azure DevOps até você clicar em **Enviar** e confirmar."
        )
        listas = self._wi_fila_listas(ado_client, ado_project, tipos, catalogo)
        if not listas:
            return

        # ---- Modelo + upload ----
        with st.expander("📄 Planilha: baixar o modelo e subir o arquivo preenchido", expanded=not (self.state.get('wi_fila') or [])):
            st.markdown(
                "O modelo é gerado **a partir deste projeto**: tipos, Area Paths, Iterations e pessoas que existem de verdade "
                "(aba *Listas*), campos obrigatórios de cada tipo como colunas extras, e uma aba *Instruções*. "
                "Colunas obrigatórias: **Tipo** e **Título**. Na coluna **Pai** use o ID de um item existente ou `#Ref` de outra linha."
            )
            c1, c2, c3 = st.columns([1, 1, 2])
            slug = "".join(ch if ch.isalnum() else "-" for ch in ado_project).strip("-").lower() or "projeto"
            with c1:
                try:
                    xlsx = wib.gerar_modelo_xlsx(ado_project, tipos, listas["extras_por_tipo"], listas["area_paths"],
                                                 listas["iterations"], listas["pessoas"])
                    st.download_button("⬇️ Modelo .xlsx", xlsx, file_name=f"modelo-work-items-{slug}.xlsx",
                                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                       width="stretch", key="dl_wi_modelo_xlsx")
                except Exception as error:
                    st.error(f"❌ Não foi possível gerar o modelo .xlsx: {error}")
            with c2:
                st.download_button("⬇️ Modelo .csv", wib.gerar_modelo_csv(tipos, listas["extras_por_tipo"], ado_project).encode("utf-8"),
                                   file_name=f"modelo-work-items-{slug}.csv", mime="text/csv", width="stretch", key="dl_wi_modelo_csv")
            with c3:
                if st.button("🔄 Recarregar listas do projeto", key="btn_wi_fila_reload", width="stretch",
                             help="Use se criou Area Path/Iteration/pessoa nova no Azure DevOps depois de abrir esta tela."):
                    self._wi_fila_listas(ado_client, ado_project, tipos, catalogo, forcar=True)
                    st.rerun()

            arquivo = st.file_uploader("Arquivo preenchido (CSV, XLSX ou TXT separado por tabulação)",
                                       type=["csv", "xlsx", "xlsm", "txt"], key=f"wi_fila_upload_{self.state.get('wi_form_versao') or 0}")
            if arquivo is not None:
                try:
                    linhas = wib.ler_arquivo(arquivo.name, arquivo.getvalue())
                except Exception as error:
                    st.error(f"❌ Não foi possível ler o arquivo: {error}")
                    linhas = []
                if not linhas:
                    st.warning("O arquivo não tem nenhuma linha de dados.")
                else:
                    cols_modelo = wib.colunas_do_modelo(tipos, listas["extras_por_tipo"])
                    mapa = wib.mapear_colunas(list(linhas[0].keys()), cols_modelo)
                    nao_reconhecidas = [c for c in linhas[0].keys() if c not in mapa]
                    if nao_reconhecidas:
                        st.caption("Colunas ignoradas (não reconhecidas): " + ", ".join(f"`{c}`" for c in nao_reconhecidas))
                    validadas = wib.validar_linhas(linhas, cols_modelo, tipos, listas["extras_por_tipo"], catalogo,
                                                   listas["area_paths"], listas["iterations"], listas["pessoas"])
                    ok = [v for v in validadas if not v["erros"]]
                    ruins = [v for v in validadas if v["erros"]]
                    st.dataframe(pd.DataFrame([{
                        "Linha": v["linha"], "Ref": v["ref"], "Tipo": v["tipo"] or "?", "Título": v["titulo"],
                        "Pai": f"#{v['parent_ref']}" if v["parent_ref"] else (v["parent_id"] or ""),
                        "Situação": "✅ OK" if not v["erros"] else "❌ " + " | ".join(v["erros"]),
                    } for v in validadas]), width="stretch", hide_index=True)
                    st.markdown(f"**{len(ok)}** linha(s) válida(s) · **{len(ruins)}** com erro (corrija no arquivo e suba de novo).")
                    if ok and st.button(f"➕ Adicionar {len(ok)} linha(s) válida(s) à fila", key="btn_wi_fila_add_arquivo", type="primary", width="stretch"):
                        refs_na_fila = {i.get("ref") for i in (self.state.get('wi_fila') or []) if i.get("ref")}
                        for v in ok:
                            if v["ref"] and v["ref"] in refs_na_fila:
                                continue   # mesma Ref já está na fila (arquivo subido duas vezes)
                            self._wi_fila_adicionar({
                                "ref": v["ref"], "tipo": v["tipo"], "titulo": v["titulo"], "campos": v["campos"],
                                "parent_id": v["parent_id"], "parent_ref": v["parent_ref"], "state": v["state"],
                                "coluna": None, "imagens": [], "tem_descricao": "System.Description" in v["campos"],
                                "origem": f"arquivo (linha {v['linha']})",
                            })
                        self.state.set('wi_form_versao', (self.state.get('wi_form_versao') or 0) + 1)   # zera o uploader
                        self._flash_success(f"{len(ok)} Work Item(s) adicionados à fila.")
                        st.rerun()

        # ---- Fila ----
        fila = self.state.get('wi_fila') or []
        st.divider()
        st.markdown(f"##### 🧺 Itens na fila ({len(fila)})")
        if not fila:
            st.info("A fila está vazia. Suba uma planilha acima ou volte ao modo \"Um Work Item\" e use **➕ Adicionar à fila**.")
            self._wi_fila_resultados()
            return

        for idx, item in enumerate(fila):
            pai = f"#{item['parent_ref']}" if item.get("parent_ref") else (str(item.get("parent_id")) if item.get("parent_id") else "—")
            rotulo = f"{idx + 1}. **{item['tipo']}** — {item['titulo']}"
            extra = []
            if item.get("ref"):
                extra.append(f"Ref `{item['ref']}`")
            if pai != "—":
                extra.append(f"pai {pai}")
            if item["campos"].get("System.AssignedTo"):
                extra.append(f"→ {item['campos']['System.AssignedTo']}")
            if item.get("imagens"):
                extra.append(f"{len(item['imagens'])} imagem(ns)")
            extra.append(item.get("origem", ""))
            c1, c2 = st.columns([6, 1])
            with c1:
                st.markdown(rotulo + ("  \n" + " · ".join(extra) if extra else ""))
                if item.get("resultado") and item["resultado"].get("erro"):
                    st.error(f"Última tentativa falhou: {item['resultado']['erro']}")
                with st.expander("Ver campos", expanded=False):
                    for ref, valor in item["campos"].items():
                        st.markdown(f"- `{ref}` = {str(valor)[:300]}")
            with c2:
                if st.button("🗑️", key=f"btn_wi_fila_rm_{item['uid']}", help="Remover da fila", width="stretch"):
                    self.state.set('wi_fila', [i for i in fila if i["uid"] != item["uid"]])
                    st.rerun()

        st.divider()
        c1, c2 = st.columns([3, 1])
        with c1:
            if st.button(f"🚀 Enviar {len(fila)} Work Item(s) ao Azure DevOps", type="primary", width="stretch",
                         key="btn_wi_fila_enviar", disabled=self.state.get('is_processing')):
                self.state.set('wi_fila_confirmando', True)
                st.rerun()
        with c2:
            if st.button("🗑️ Limpar fila", key="btn_wi_fila_limpar", width="stretch"):
                self.state.set('wi_fila', [])
                self.state.set('wi_fila_confirmando', False)
                st.rerun()

        if self.state.get('wi_fila_confirmando'):
            self._wi_fila_confirmar_e_enviar(ado_client, fila)
        self._wi_fila_resultados()

    # ---------------------------------------------------------------- envio
    def _wi_fila_confirmar_e_enviar(self, ado_client, fila: list):
        ordenados = wib.ordenar_para_criacao(fila)
        st.warning(
            f"Vai criar **{len(fila)} Work Item(s)** reais no Azure DevOps, nesta ordem (pais antes dos filhos). "
            "Isso **não pode ser desfeito pelo app**. Confirma?"
        )
        with st.expander("🔍 Ver a lista exata", expanded=True):
            for i, item in enumerate(ordenados, start=1):
                pai = f"filho de #{item['parent_ref']}" if item.get("parent_ref") else (f"filho de {item['parent_id']}" if item.get("parent_id") else "sem pai")
                st.markdown(f"{i}. {item['tipo']} — **{item['titulo']}** ({pai}, {len(item['campos'])} campo(s))")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ Sim, enviar todos", type="primary", width="stretch", key="btn_wi_fila_confirm_sim"):
                self.trigger_action('wi_fila_enviar')
                st.rerun()
        with c2:
            if st.button("✖ Cancelar", width="stretch", key="btn_wi_fila_confirm_nao"):
                self.state.set('wi_fila_confirmando', False)
                st.rerun()

        if self.state.get('current_action') == 'wi_fila_enviar' and not self.state.get('show_interrupt_modal'):
            self.state.set('wi_fila_confirmando', False)
            criados_por_ref, sucesso, restantes = {}, [], []
            barra = st.progress(0.0, text="Criando Work Items...")
            for n, item in enumerate(ordenados, start=1):
                barra.progress(n / len(ordenados), text=f"{n}/{len(ordenados)} — {item['tipo']}: {item['titulo']}")
                try:
                    parent_id = item.get("parent_id")
                    if item.get("parent_ref"):
                        if item["parent_ref"] not in criados_por_ref:
                            raise RuntimeError(f"o pai #{item['parent_ref']} não foi criado (falhou ou não está na fila)")
                        parent_id = criados_por_ref[item["parent_ref"]]
                    campos = dict(item["campos"])
                    campos["System.Tags"] = self._tag_criado_por(campos.get("System.Tags"))
                    anexos_urls = []
                    for nome, conteudo in (item.get("imagens") or []):
                        try:
                            anexo = ado_client.upload_attachment(conteudo, nome)
                            if anexo.get("url"):
                                anexos_urls.append(anexo["url"])
                        except Exception:
                            pass
                    if anexos_urls and item.get("tem_descricao"):
                        imgs_html = "".join(f'<div><img src="{u}" style="max-width:100%" /></div>' for u in anexos_urls)
                        campos["System.Description"] = (campos.get("System.Description") or "") + "<br><br>" + imgs_html
                    resultado = ado_client.create_work_item(item["tipo"], campos, parent_id=parent_id, state=item.get("state"))
                    for url in anexos_urls:
                        try:
                            ado_client.attach_file_to_work_item(resultado["id"], url, comment="Evidência anexada via QA TestGen")
                        except Exception:
                            pass
                    if item.get("ref"):
                        criados_por_ref[item["ref"]] = resultado["id"]
                    sucesso.append({"tipo": item["tipo"], "titulo": item["titulo"], "id": resultado["id"],
                                    "url": resultado.get("url", ""), "aviso": resultado.get("state_warning"),
                                    "parent_id": parent_id})
                except Exception as error:
                    item = dict(item)
                    item["resultado"] = {"erro": str(error)}
                    restantes.append(item)
            barra.empty()
            try:
                self._log("Criar Work Item", "Criar Work Item (lote)",
                          f"{len(sucesso)} criado(s), {len(restantes)} com erro: " + ", ".join(f"#{s['id']}" for s in sucesso))
            except Exception:
                pass
            self.state.set('wi_fila', restantes)
            self.state.set('wi_fila_resultados', sucesso)
            self.clear_action()
            if restantes:
                self._flash_warning(f"{len(sucesso)} Work Item(s) criado(s); {len(restantes)} ficaram na fila com erro — corrija e envie de novo.")
            else:
                self._flash_success(f"{len(sucesso)} Work Item(s) criado(s) no Azure DevOps.")
            st.rerun()

    def _wi_fila_resultados(self):
        res = self.state.get('wi_fila_resultados') or []
        if not res:
            return
        st.markdown(f"##### ✅ Criados na última rodada ({len(res)})")
        for r in res:
            linha = f"- **{r['tipo']}** #{r['id']} — {r['titulo']}"
            if r.get("parent_id"):
                linha += f" (filho de {r['parent_id']})"
            if r.get("url"):
                linha += f" · [abrir no Azure DevOps]({r['url']})"
            st.markdown(linha)
            if r.get("aviso"):
                st.caption(f"⚠️ {r['aviso']}")
        if st.button("Limpar este resumo", key="btn_wi_fila_limpar_res"):
            self.state.set('wi_fila_resultados', [])
            st.rerun()
