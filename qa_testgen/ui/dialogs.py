import streamlit as st

from qa_testgen.ui.auth import SESSION_AUTH_KEY, SESSION_USER_KEY, log_action

# Chaves que NÃO devem ser apagadas ao iniciar uma "Nova Análise":
# autenticação, o componente interno que lê o cookie de sessão, e o PAT
# do Azure DevOps (+ cache de validação) — o PAT é da PESSOA, não da
# análise; digitar de novo a cada "Nova Análise" só pra continuar
# trabalhando no mesmo Azure DevOps não fazia sentido. Só deve mesmo
# sumir se a pessoa sair (logout) ou fechar a aba/navegador — os dois
# casos já encerram a sessão inteira por conta própria, sem precisar de
# nada extra aqui.
_PRESERVE_ON_RESET = {
    SESSION_AUTH_KEY, SESSION_USER_KEY,
    'ado_user_pat', 'ado_last_validated_pat', 'ado_pat_validated',
    'ado_orgs_fetch_done', 'ado_accessible_orgs', 'ado_orgs_fetch_error',
}

DIALOG_PREFIXES = (
    "mid_","mfunc_","mreq_","mcen_","mcat_","mpri_","mcrit_","mobs_","edit_m_",
    "tt_","tp_","ta_","te_","edit_tc_","edit_steps_",
    "pn_","pd_","edit_p_","suite_",
    "newm_","newtc_","newtc_steps","new_steps_","newp_",
    "active_matriz_row", "active_test_case_row", "active_test_plan_row",
)


def clear_widget_states():
    for key in list(st.session_state.keys()):
        if any(key.startswith(prefix) for prefix in DIALOG_PREFIXES):
            del st.session_state[key]


@st.dialog("⚠️ Confirmação de Exclusão")
def confirm_matriz_deletion_modal(index: int):
    st.markdown(
        "A exclusão deste cenário é **irreversível** e não poderá ser recuperada. "
        "Os IDs da Matriz serão renumerados automaticamente. "
        "Tem certeza que deseja prosseguir?"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🗑️ Sim, Excluir", width="stretch", type="primary"):
            matriz = st.session_state['matriz']
            matriz.pop(index)
            # Renumera todos os IDs sequencialmente preservando o prefixo MC-
            for idx, row in enumerate(matriz):
                row['id'] = f"MC-{idx + 1:03d}"
            st.session_state['matriz'] = matriz
            clear_widget_states()
            st.rerun()
    with c2:
        if st.button("❌ Cancelar", width="stretch"):
            st.rerun()


@st.dialog("⚠️ Confirmação de Exclusão")
def confirm_deletion_modal(list_key: str, index: int):
    st.markdown(
        "A exclusão deste item é **irreversível** e não poderá ser recuperada. "
        "Tem certeza que deseja prosseguir?"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🗑️ Sim, Excluir", width="stretch", type="primary"):
            st.session_state[list_key].pop(index)
            clear_widget_states()
            st.rerun()
    with c2:
        if st.button("❌ Cancelar", width="stretch"):
            st.rerun()


@st.dialog("⚠️ Confirmação de Exclusão")
def confirm_step_deletion_modal(steps_state_key: str, step_uid: str):
    st.markdown(
        "A exclusão deste step é **irreversível** e não poderá ser recuperada. "
        "Tem certeza que deseja remover este step?"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🗑️ Sim, Excluir", width="stretch", type="primary", key="confirm_del_step"):
            st.session_state[steps_state_key] = [
                s for s in st.session_state[steps_state_key] if s["uid"] != step_uid
            ]
            st.rerun()
    with c2:
        if st.button("❌ Cancelar", width="stretch", key="cancel_del_step"):
            st.rerun()


@st.dialog("⚠️ Confirmação de Exclusão")
def confirm_suite_deletion_modal(suites_state_key: str, suite_uid: str):
    st.markdown(
        "A exclusão desta Suite é **irreversível** e não poderá ser recuperada. "
        "Tem certeza que deseja remover esta Suite?"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🗑️ Sim, Excluir", width="stretch", type="primary", key="confirm_del_suite"):
            st.session_state[suites_state_key] = [
                s for s in st.session_state[suites_state_key] if s["uid"] != suite_uid
            ]
            st.rerun()
    with c2:
        if st.button("❌ Cancelar", width="stretch", key="cancel_del_suite"):
            st.rerun()


@st.dialog("⚠️ Edição em Aberto")
def confirm_navigate_away_modal(target_step: int):
    st.markdown(
        "Há uma **edição em aberto** neste passo. "
        "Se navegar agora, as alterações não salvas serão **descartadas permanentemente**. "
        "Tem certeza que deseja sair sem salvar?"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🚪 Sim, Sair sem Salvar", width="stretch", type="primary", key="confirm_navigate"):
            clear_widget_states()
            st.session_state['step'] = target_step
            st.rerun()
    with c2:
        if st.button("✖ Voltar a Editar", width="stretch", key="cancel_navigate"):
            st.rerun()


@st.dialog("⚠️ Descartar Novo Registro")
def confirm_discard_new_modal(discard_flag_key: str):
    st.markdown(
        "Os dados preenchidos neste novo registro ainda **não foram salvos**. "
        "Ao cancelar, essas informações serão **perdidas permanentemente** e não poderão ser recuperadas. "
        "Tem certeza que deseja descartar?"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🗑️ Sim, Descartar", width="stretch", type="primary", key="confirm_discard"):
            st.session_state[discard_flag_key] = False
            clear_widget_states()
            st.rerun()
    with c2:
        if st.button("❌ Voltar a Editar", width="stretch", key="cancel_discard"):
            st.rerun()


@st.dialog("🔗 Confirmar Integração com Azure DevOps")
def confirm_azure_devops_full_push_modal(cases_to_create: list, items_display: list, plan_name: str, is_existing_plan: bool):
    """
    cases_to_create: [título, ...] — Casos que ainda não existem no Azure DevOps
    items_display: [(rótulo_do_work_item, [títulos_de_caso]), ...]
    """
    st.markdown(
        f"Você está prestes a integrar com o Azure DevOps, no Test Plan **{plan_name}**"
        + (" *(já existente)*" if is_existing_plan else " *(novo)*") + ":"
    )

    if cases_to_create:
        with st.expander(f"📝 {len(cases_to_create)} Caso(s) de Teste serão criados agora", expanded=True):
            for t in cases_to_create:
                st.write(f"- {t}")
    else:
        st.caption("Nenhum Caso de Teste novo será criado — todos já existem no Azure DevOps.")

    if items_display:
        with st.expander(f"🔗 {len(items_display)} Work Item(s) vão virar Suíte / receber vínculo", expanded=True):
            for label, casos in items_display:
                st.markdown(f"**{label}**")
                for c in casos:
                    st.write(f"　　- {c}")

    st.warning(
        "Essa ação cria itens reais no seu projeto do Azure DevOps (Test Plan, Suites, Test "
        "Cases e vínculos) e **não pode ser desfeita automaticamente** — se algo sair errado, "
        "a exclusão precisa ser feita manualmente lá. Tem certeza que deseja prosseguir?"
    )
    st.markdown(
        """
        <style>
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
        </style>
        """,
        unsafe_allow_html=True,
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🚀 Sim, Integrar", width="stretch", type="primary", key="azure_blue_btn_modal_confirm"):
            st.session_state['show_ado_confirm_modal'] = False
            st.session_state['current_action'] = 'push_azure_devops_full'
            st.session_state['is_processing'] = True
            st.rerun()
    with c2:
        if st.button("❌ Cancelar", width="stretch", key="cancel_ado_full_push"):
            st.session_state['show_ado_confirm_modal'] = False
            st.rerun()


def confirm_static_suites_push_modal(cases_to_create: list, suites_display: list, plan_name: str, is_existing_plan: bool):
    """
    cases_to_create: [título, ...] — Casos que ainda não existem no Azure DevOps
    suites_display: [(nome_da_suíte, [títulos_de_caso]), ...]
    """
    st.markdown(
        f"Você está prestes a integrar com o Azure DevOps (sem Work Items), no Test Plan "
        f"**{plan_name}**" + (" *(já existente)*" if is_existing_plan else " *(novo)*") + ":"
    )

    if cases_to_create:
        with st.expander(f"📝 {len(cases_to_create)} Caso(s) de Teste serão criados agora", expanded=True):
            for t in cases_to_create:
                st.write(f"- {t}")
    else:
        st.caption("Nenhum Caso de Teste novo será criado — todos já existem no Azure DevOps.")

    if suites_display:
        with st.expander(f"📋 {len(suites_display)} Suíte(s) Estática(s) serão criadas ou reaproveitadas", expanded=True):
            for nome, casos in suites_display:
                st.markdown(f"**{nome}**")
                for c in casos:
                    st.write(f"　　- {c}")

    st.warning(
        "Essa ação cria itens reais no seu projeto do Azure DevOps e **não pode ser desfeita "
        "automaticamente**. Tem certeza que deseja prosseguir?"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🚀 Sim, Integrar", width="stretch", type="primary", key="static_blue_btn_modal_confirm"):
            st.session_state['show_static_confirm_modal'] = False
            st.session_state['current_action'] = 'push_static_suites'
            st.session_state['is_processing'] = True
            st.rerun()
    with c2:
        if st.button("❌ Cancelar", width="stretch", key="cancel_static_push"):
            st.session_state['show_static_confirm_modal'] = False
            st.rerun()


def confirm_reconciliation_push_modal(items_display: list, plan_name: str):
    """items_display: [(rótulo_do_work_item, [títulos_de_caso_já_existentes]), ...]"""
    st.markdown(f"Você está prestes a vincular Casos já existentes do Test Plan **{plan_name}** a Work Items novos:")

    if items_display:
        with st.expander(f"🔗 {len(items_display)} Work Item(s) vão virar Suíte / receber vínculo", expanded=True):
            for label, casos in items_display:
                st.markdown(f"**{label}**")
                for c in casos:
                    st.write(f"　　- {c}")

    st.caption("Nenhum Caso de Teste novo é criado neste modo — só cria vínculo com Casos que já existem.")
    st.warning(
        "Essa ação cria Suítes e vínculos reais no seu projeto do Azure DevOps e **não pode ser "
        "desfeita automaticamente**. Tem certeza que deseja prosseguir?"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🚀 Sim, Vincular", width="stretch", type="primary", key="recon_blue_btn_modal_confirm"):
            st.session_state['show_recon_confirm_modal'] = False
            st.session_state['current_action'] = 'push_reconciliation'
            st.session_state['is_processing'] = True
            st.rerun()
    with c2:
        if st.button("❌ Cancelar", width="stretch", key="cancel_recon_push"):
            st.session_state['show_recon_confirm_modal'] = False
            st.rerun()


def _action_interrupt():
    st.session_state['current_action'] = None
    st.session_state['is_processing'] = False
    st.session_state['processing_interrupted'] = True
    st.session_state['show_interrupt_modal'] = False


def _action_cancel_interrupt():
    st.session_state['show_interrupt_modal'] = False


@st.dialog("⚠️ Confirmar Interrupção")
def confirm_interrupt_modal():
    st.markdown(
        "Existe um processamento em andamento. Ao confirmar, a requisição atual será "
        "imediatamente descartada e a aplicação retornará ao estado de edição. Deseja continuar?"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("⏹️ Sim, Interromper", width="stretch", type="primary", key="confirm_int_btn", on_click=_action_interrupt):
            st.rerun()
    with c2:
        if st.button("Cancelar", width="stretch", key="cancel_int_btn", on_click=_action_cancel_interrupt):
            st.rerun()


@st.dialog("⚠️ Confirmar Nova Análise")
def confirm_new_analysis_modal(config=None):
    st.markdown(
        "Todo o progresso atual, incluindo documentos anexados, matriz e casos gerados não exportados, "
        "será **perdido permanentemente**. Tem certeza que deseja iniciar uma nova análise?"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🔄 Sim, Iniciar", width="stretch", type="primary", key="confirm_new_btn"):
            if config is not None:
                username = st.session_state.get(SESSION_USER_KEY, "")
                project = st.session_state.get('project_name', '') or "(sem projeto)"
                log_action(config, username, "Nova Análise", "Nova Análise", f"Reiniciou a análise (projeto anterior: '{project}')")
            for key in list(st.session_state.keys()):
                if key not in _PRESERVE_ON_RESET:
                    del st.session_state[key]
            st.rerun()
    with c2:
        if st.button("Cancelar", width="stretch", key="cancel_new_btn"):
            st.session_state['show_new_analysis_modal'] = False
            st.rerun()

_REPORT_WIDGET_KEYS = [
    'report_plan_select', 'report_contexto_input', 'report_ambiente_select',
    'report_escopo_input', 'report_conclusao_input', 'report_proximos_input',
    'report_status_manual_select',
]


def _clear_report_state():
    st.session_state['report_available_plans'] = []
    st.session_state['report_contexto'] = ''
    st.session_state['report_escopo'] = ''
    st.session_state['report_conclusao'] = ''
    st.session_state['report_proximos'] = ''
    st.session_state['report_pdf_bytes'] = None
    st.session_state['report_warnings'] = []
    for key in _REPORT_WIDGET_KEYS:
        st.session_state.pop(key, None)


@st.dialog("⚠️ Começar um Novo Relatório")
def confirm_new_report_modal():
    st.markdown(
        "Isso vai limpar o Test Plan selecionado, os textos preenchidos (Contexto, Escopo, "
        "Conclusão, Próximos Passos), o Status escolhido e o PDF já gerado nesta tela. "
        "Essas informações serão **perdidas permanentemente**. Tem certeza que deseja começar um "
        "novo relatório?"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🔄 Sim, Novo Relatório", width="stretch", type="primary", key="confirm_new_report_btn"):
            _clear_report_state()
            st.session_state['show_new_report_modal'] = False
            st.rerun()
    with c2:
        if st.button("Cancelar", width="stretch", key="cancel_new_report_btn"):
            st.session_state['show_new_report_modal'] = False
            st.rerun()


@st.dialog("⚠️ Começar uma Nova Execução")
def confirm_new_api_run_modal(reset_fn):
    st.markdown(
        "Isso vai limpar os casos, as variáveis, os resultados e os relatórios gerados na tela de "
        "Testes de API. Essas informações serão **perdidas permanentemente** (baixe a definição "
        ".json na etapa 3 se quiser repetir a bateria depois). Tem certeza?"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🔄 Sim, Nova Execução", width="stretch", type="primary", key="confirm_new_api_run_btn"):
            reset_fn()
            st.session_state['show_new_api_run_modal'] = False
            st.rerun()
    with c2:
        if st.button("Cancelar", width="stretch", key="cancel_new_api_run_btn"):
            st.session_state['show_new_api_run_modal'] = False
            st.rerun()


@st.dialog("⚠️ Sair dos Testes de API")
def confirm_leave_api_tests_modal(reset_fn):
    st.markdown(
        "Você gerou relatórios de Testes de API e ainda não baixou nenhum arquivo. Se sair agora, "
        "eles serão **perdidos** (não ficam salvos fora desta sessão). Deseja continuar mesmo assim?"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🚪 Sair mesmo assim", width="stretch", type="primary", key="confirm_leave_api_btn"):
            pending = st.session_state.get('_pending_navigation_after_api') or {}
            reset_fn()
            for key, value in pending.items():
                st.session_state[key] = value
            st.session_state['show_leave_api_modal'] = False
            st.session_state.pop('_pending_navigation_after_api', None)
            st.rerun()
    with c2:
        if st.button("✖ Continuar nos Testes de API", width="stretch", key="cancel_leave_api_btn"):
            st.session_state['show_leave_api_modal'] = False
            st.session_state.pop('_pending_navigation_after_api', None)
            st.rerun()


@st.dialog("⚠️ Sair do Relatório de Testes")
def confirm_leave_report_modal():
    st.markdown(
        "Você tem um Relatório de Testes gerado nesta tela (ou em andamento). Se sair agora, "
        "essas informações serão **perdidas** (não ficam salvas em lugar nenhum fora desta "
        "sessão). Deseja continuar mesmo assim?"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🚪 Sair mesmo assim", width="stretch", type="primary", key="confirm_leave_report_btn"):
            pending = st.session_state.get('_pending_navigation_after_report') or {}
            for key, value in pending.items():
                st.session_state[key] = value
            _clear_report_state()
            st.session_state['show_leave_report_modal'] = False
            st.session_state.pop('_pending_navigation_after_report', None)
            st.rerun()
    with c2:
        if st.button("✖ Continuar no Relatório", width="stretch", key="cancel_leave_report_btn"):
            st.session_state['show_leave_report_modal'] = False
            st.session_state.pop('_pending_navigation_after_report', None)
            st.rerun()


# confirm_bug_creation_modal e confirm_new_bug_modal foram removidos daqui
# e viraram blocos inline em application.py (_render_bug_confirm_inline e
# _render_new_bug_confirm_inline) — em testes reais, o st.dialog ficava
# sobreposto à tela mesmo depois de "fechado" (show_bug_confirm_modal=False
# + st.rerun()), inclusive por cima do overlay de processamento. Conteúdo
# normal da página (não dialog) não tem essa ambiguidade: passa a
# reaparecer/desaparecer só pela condição normal do Streamlit, sem
# depender de quando o navegador decide desmontar um modal.
#
# aviso_pat_compartilhado_modal, abaixo, continua um st.dialog de verdade
# porque não tem esse problema: é só "mostrar aviso, clicar OK, seguir" —
# nada de trabalho longo rodando logo depois do fechamento.


@st.dialog("📢 Novidades no app", width="large")
def aviso_pat_compartilhado_modal(marcar_nao_mostrar_fn, tem_testes_api: bool = True):
    """
    Aviso de novidades mostrado a quem não é o dono do app, uma vez por
    versão do aviso (ver UserInterface._NOTICE_VERSION). Escrito pra quem
    entende o negócio mas não o app: o que é, pra que serve e onde clicar.

    marcar_nao_mostrar_fn: callable() sem argumentos — grava que essa
    pessoa não quer ver esta versão do aviso de novo (chamado só se ela
    marcar a caixinha e confirmar).
    tem_testes_api: se a pessoa tem a permissão da área — muda só a
    última frase (onde clicar vs. a quem pedir acesso).
    """
    st.markdown("#### 🔌 Nova área: Testes de API")
    st.markdown(
        "Agora dá pra **executar testes de API por dentro do app** e sair com a evidência "
        "pronta — sem Newman, sem montar pasta de print na mão.\n\n"
        "**Como funciona, em 3 etapas:**\n"
        "1. **Definição** — informa a URL base da API, escolhe o **Work Item** do Azure DevOps "
        "(ou cola a descrição), clica em *🔎 Reconhecer a API* (o app descobre as rotas reais e o "
        "formato das respostas) e em *🤖 Gerar casos com IA*: a bateria aparece pronta pra revisar. "
        "Também dá pra importar uma *collection* do Postman ou montar os casos na tela. As senhas "
        "vão num campo protegido e nunca aparecem nas evidências.\n"
        "2. **Execução** — clica em *Executar testes*: as chamadas saem **do seu navegador** (mesmo "
        "IP do Postman), na ordem, e o app mostra o que passou e o que falhou em cada caso.\n"
        "3. **Evidências** — clica em *Gerar relatórios* e baixa o **PDF** (no padrão dos outros "
        "relatórios do app), o **.md** e um **.zip** com uma pasta por caso (o que foi enviado, "
        "o que voltou e o resultado). Dá pra anexar seus prints e guardar tudo em "
        "*Documentos Armazenados*.\n\n"
        "💡 Cada tela tem, no topo, um expansível **\"ℹ️ O que é e como usar esta área\"** — "
        "inclusive com o passo a passo de como exportar a collection no Postman."
    )
    if tem_testes_api:
        st.markdown("**Onde:** botão **🔌 Testes de API** na barra lateral (menu à esquerda).")
    else:
        st.markdown(
            "**Onde:** botão **🔌 Testes de API** na barra lateral — se ele não aparecer pra você, "
            "peça ao administrador a permissão *Testes de API*."
        )
    st.markdown("#### 🆕 Também novo")
    st.markdown(
        "- **Testes de API → Azure DevOps**: no fim da etapa Evidências, *Levar para o assistente* transforma a "
        "bateria em Matriz, Casos e Plano; você segue pelo Passo 7 como sempre e, depois, registra a execução "
        "como **Test Run** (Passed/Failed por caso, com o PDF anexado).\n"
        "- **Criar Work Item → Fila / planilha**: crie vários Work Items de uma vez — pelo botão *Adicionar à fila* "
        "do formulário ou subindo uma planilha (o modelo é baixado na própria tela, já com os tipos, Area Paths e "
        "pessoas do projeto). O app valida linha a linha antes de enviar."
    )
    st.divider()
    st.markdown("#### 🔑 PAT compartilhado do Azure DevOps")
    st.markdown(
        "O app usa um **PAT compartilhado**, configurado pelo administrador — você não "
        "precisa informar nenhum token do Azure DevOps em nenhuma tela. Pra manter a "
        "rastreabilidade de quem fez o quê, todo Bug e Test Case criado recebe "
        "automaticamente a tag `criado-por:<seu usuário>`, visível no próprio item dentro "
        "do Azure DevOps."
    )
    nao_mostrar = st.checkbox("Não mostrar este aviso novamente", key="pat_notice_nao_mostrar_chk")
    if st.button("Entendi", type="primary", width="stretch", key="pat_notice_ok_btn"):
        if nao_mostrar:
            marcar_nao_mostrar_fn()
        st.session_state['_pat_notice_visto_nesta_sessao'] = True
        st.rerun()
