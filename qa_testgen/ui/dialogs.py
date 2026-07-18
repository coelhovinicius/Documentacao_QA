import streamlit as st

from qa_testgen.ui.auth import SESSION_AUTH_KEY, SESSION_USER_KEY

# Chaves que NÃO devem ser apagadas ao iniciar uma "Nova Análise":
# autenticação e o componente interno que lê o cookie de sessão.
_PRESERVE_ON_RESET = {SESSION_AUTH_KEY, SESSION_USER_KEY, "_cookie_manager"}

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
        if st.button("🗑️ Sim, Excluir", use_container_width=True, type="primary"):
            matriz = st.session_state['matriz']
            matriz.pop(index)
            # Renumera todos os IDs sequencialmente preservando o prefixo MC-
            for idx, row in enumerate(matriz):
                row['id'] = f"MC-{idx + 1:03d}"
            st.session_state['matriz'] = matriz
            clear_widget_states()
            st.rerun()
    with c2:
        if st.button("❌ Cancelar", use_container_width=True):
            st.rerun()


@st.dialog("⚠️ Confirmação de Exclusão")
def confirm_deletion_modal(list_key: str, index: int):
    st.markdown(
        "A exclusão deste item é **irreversível** e não poderá ser recuperada. "
        "Tem certeza que deseja prosseguir?"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🗑️ Sim, Excluir", use_container_width=True, type="primary"):
            st.session_state[list_key].pop(index)
            clear_widget_states()
            st.rerun()
    with c2:
        if st.button("❌ Cancelar", use_container_width=True):
            st.rerun()


@st.dialog("⚠️ Confirmação de Exclusão")
def confirm_step_deletion_modal(steps_state_key: str, step_uid: str):
    st.markdown(
        "A exclusão deste step é **irreversível** e não poderá ser recuperada. "
        "Tem certeza que deseja remover este step?"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🗑️ Sim, Excluir", use_container_width=True, type="primary", key="confirm_del_step"):
            st.session_state[steps_state_key] = [
                s for s in st.session_state[steps_state_key] if s["uid"] != step_uid
            ]
            st.rerun()
    with c2:
        if st.button("❌ Cancelar", use_container_width=True, key="cancel_del_step"):
            st.rerun()


@st.dialog("⚠️ Confirmação de Exclusão")
def confirm_suite_deletion_modal(suites_state_key: str, suite_uid: str):
    st.markdown(
        "A exclusão desta Suite é **irreversível** e não poderá ser recuperada. "
        "Tem certeza que deseja remover esta Suite?"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🗑️ Sim, Excluir", use_container_width=True, type="primary", key="confirm_del_suite"):
            st.session_state[suites_state_key] = [
                s for s in st.session_state[suites_state_key] if s["uid"] != suite_uid
            ]
            st.rerun()
    with c2:
        if st.button("❌ Cancelar", use_container_width=True, key="cancel_del_suite"):
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
        if st.button("🚪 Sim, Sair sem Salvar", use_container_width=True, type="primary", key="confirm_navigate"):
            clear_widget_states()
            st.session_state['step'] = target_step
            st.rerun()
    with c2:
        if st.button("✖ Voltar a Editar", use_container_width=True, key="cancel_navigate"):
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
        if st.button("🗑️ Sim, Descartar", use_container_width=True, type="primary", key="confirm_discard"):
            st.session_state[discard_flag_key] = False
            clear_widget_states()
            st.rerun()
    with c2:
        if st.button("❌ Voltar a Editar", use_container_width=True, key="cancel_discard"):
            st.rerun()


@st.dialog("🔗 Confirmar Integração com Azure DevOps")
def confirm_azure_devops_push_modal(scope: str, num_cases: int, num_plans: int):
    st.markdown(f"Você está prestes a enviar os seguintes itens para o Azure DevOps, com o escopo **{scope}**:")

    if scope in ("Somente Test Cases", "Test Cases + Test Plans/Suites"):
        st.markdown(f"- **{num_cases}** Test Case(s)")
    if scope in ("Somente Test Plans/Suites", "Test Cases + Test Plans/Suites"):
        st.markdown(f"- **{num_plans}** Test Plan(s) (com suas Suites e vínculos de casos)")

    st.warning(
        "Essa ação cria itens reais no seu projeto do Azure DevOps e **não pode ser desfeita "
        "automaticamente** — se algo sair errado, a exclusão precisa ser feita manualmente lá. "
        "Tem certeza que deseja prosseguir?"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🚀 Sim, Integrar", use_container_width=True, type="primary", key="confirm_ado_push"):
            st.session_state['current_action'] = 'push_azure_devops'
            st.session_state['is_processing'] = True
            st.rerun()
    with c2:
        if st.button("❌ Cancelar", use_container_width=True, key="cancel_ado_push"):
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
        if st.button("⏹️ Sim, Interromper", use_container_width=True, type="primary", key="confirm_int_btn", on_click=_action_interrupt):
            st.rerun()
    with c2:
        if st.button("Cancelar", use_container_width=True, key="cancel_int_btn", on_click=_action_cancel_interrupt):
            st.rerun()


@st.dialog("⚠️ Confirmar Nova Análise")
def confirm_new_analysis_modal():
    st.markdown(
        "Todo o progresso atual, incluindo documentos anexados, matriz e casos gerados não exportados, "
        "será **perdido permanentemente**. Tem certeza que deseja iniciar uma nova análise?"
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🔄 Sim, Iniciar", use_container_width=True, type="primary", key="confirm_new_btn"):
            for key in list(st.session_state.keys()):
                if key not in _PRESERVE_ON_RESET:
                    del st.session_state[key]
            st.rerun()
    with c2:
        if st.button("Cancelar", use_container_width=True, key="cancel_new_btn"):
            st.session_state['show_new_analysis_modal'] = False
            st.rerun()
