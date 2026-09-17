import secrets as _secrets_module
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import bcrypt
import streamlit as st
import streamlit.components.v1 as components

from qa_testgen.infrastructure.access_control_client import AccessControlClient, AccessControlError

TZ_BR = ZoneInfo("America/Sao_Paulo")

SESSION_AUTH_KEY = "authenticated"
SESSION_USER_KEY = "auth_user"
SESSION_ID_KEY = "_auth_session_id"
PENDING_USERNAME_KEY = "_access_pending_username"

QUERY_PARAM_NAME = "sid"

# Desloga automaticamente após esse tempo sem nenhuma interação com o app.
# Cada requisição válida "renova" essa janela (sliding window) — é isso que
# controla o logout automático.
INACTIVITY_TIMEOUT_MINUTES = 60


# --------------------------------------------------------------------------- #
# Credenciais (st.secrets)
# --------------------------------------------------------------------------- #
def _get_users() -> dict:
    """
    [credentials]
    [credentials.usernames]
    admin = "$2b$12$....hash-bcrypt....."   # gerado com generate_password_hash.py
    """
    try:
        return dict(st.secrets["credentials"]["usernames"])
    except Exception:
        return {}


# Hash "dummy" só para gastar o mesmo tempo de bcrypt quando o usuário não
# existe, evitando que o tempo de resposta revele se um username é válido.
_DUMMY_HASH = bcrypt.hashpw(b"senha-invalida-placeholder", bcrypt.gensalt())


def _get_all_known_usernames(config, client) -> list:
    """
    Todos os nomes de usuário conhecidos — os fixos do secrets.toml MAIS
    os criados dinamicamente pelo admin (banco do n8n). Usado nos
    dropdowns de "escolher usuário" (aprovador, permissão), pra incluir
    os dois universos.
    """
    nomes = set(_get_users().keys())
    try:
        nomes.update(u["username"] for u in client.list_users())
    except Exception:
        pass  # se o banco dinâmico falhar, ainda mostra os do secrets.toml
    return sorted(nomes)


def _check_credentials(config, username: str, password: str) -> tuple:
    """
    Verifica credenciais em duas fontes, nessa ordem:
    1. secrets.toml (usuários fixos, cadastrados manualmente — inclui o
       dono do app, que continua SÓ aqui, nunca no banco dinâmico).
    2. Banco dinâmico (n8n) — usuários criados pelo admin pela tela de
       Administração, sem precisar editar o secrets.toml.

    Retorna (is_valid, acesso_direto) — acesso_direto só é relevante pra
    usuários do banco dinâmico (indica se esse usuário específico pula a
    fila de aprovação, escolha feita pelo admin no cadastro dele).
    """
    users = _get_users()
    if username in users:
        stored_hash = users[username]
        try:
            is_valid = bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))
        except (ValueError, TypeError):
            # Hash mal formatado no secrets.toml (ex.: alguém colocou senha em texto puro)
            is_valid = False
        return is_valid, False

    try:
        client = AccessControlClient(config)
        login_info = client.get_user_login_info(username)
        stored_hash = login_info["password_hash"]
        acesso_direto = login_info["acesso_direto"]
    except Exception:
        stored_hash, acesso_direto = "", False

    if not stored_hash:
        # Usuário não existe em lugar nenhum — ainda assim faz uma
        # checagem "dummy", pra não vazar por tempo de resposta se o
        # username existe ou não.
        bcrypt.checkpw(password.encode("utf-8"), _DUMMY_HASH)
        return False, False

    try:
        is_valid = bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))
    except (ValueError, TypeError):
        is_valid = False
    return is_valid, acesso_direto


# --------------------------------------------------------------------------- #
# Sessão via ID opaco na URL — a URL só carrega um identificador aleatório
# (ex.: "?sid=k3F9x..."), sem nenhuma informação legível sobre quem está
# logado. O dado de verdade (usuário, validade) fica guardado no n8n, não
# na URL — isso também permite REVOGAR uma sessão remotamente (o ID para
# de funcionar mesmo que a URL continue circulando por aí).
# --------------------------------------------------------------------------- #
def _new_session_id() -> str:
    return _secrets_module.token_urlsafe(24)


def _expires_at_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=INACTIVITY_TIMEOUT_MINUTES)).isoformat()


def _grant_session(config, username: str):
    session_id = _new_session_id()
    try:
        AccessControlClient(config).create_session(session_id, username, _expires_at_iso())
    except Exception:
        pass  # se o n8n estiver fora do ar, a sessão ainda funciona nesta aba (só não sobrevive a F5)
    st.session_state[SESSION_AUTH_KEY] = True
    st.session_state[SESSION_USER_KEY] = username
    st.session_state[SESSION_ID_KEY] = session_id
    st.query_params[QUERY_PARAM_NAME] = session_id
    st.session_state.pop(PENDING_USERNAME_KEY, None)
    log_action(config, username, "Login", "Login", "Sessão iniciada com sucesso")


# --------------------------------------------------------------------------- #
# Controle de acesso — aprovação de login (via n8n)
# --------------------------------------------------------------------------- #
def is_approver(config, username: str) -> bool:
    """O dono do app (config.owner_username) é sempre aprovador implícito."""
    if not username:
        return False
    if username == config.owner_username:
        return True
    try:
        return username in AccessControlClient(config).list_approvers()
    except Exception:
        return False


def log_action(config, username: str, action_name: str, location: str, details: str = "") -> None:
    """
    Registra um evento de auditoria (visível só pro dono do app, na área
    de Logs). Nunca lança exceção — se o log falhar (ex.: n8n fora do ar),
    a ação do usuário continua normalmente, só o registro é perdido.
    """
    try:
        AccessControlClient(config).log_action(username, action_name, location, details)
    except Exception:
        pass


def has_permission(config, username: str, permission: str) -> bool:
    """
    Checa se `username` tem uma permissão granular específica (ex.:
    'azure_devops', 'execution_report'). O dono do app sempre tem todas.
    Em caso de falha ao consultar o n8n, nega por padrão (mais seguro do
    que liberar acesso silenciosamente se a checagem falhar).
    """
    if not username:
        return False
    if username == config.owner_username:
        return True
    try:
        return username in AccessControlClient(config).list_permission(permission)
    except Exception:
        return False


def render_pending_approvals_panel(config):
    """Painel de solicitações pendentes — só visível pra quem é aprovador."""
    username = st.session_state.get(SESSION_USER_KEY, "")
    if not is_approver(config, username):
        st.error("❌ Você não tem permissão para aprovar acessos.")
        return

    st.subheader("🔔 Solicitações Pendentes de Acesso")
    client = AccessControlClient(config)
    try:
        pending = client.list_pending()
    except Exception as error:
        st.error(f"❌ Não foi possível carregar as solicitações: {error}")
        return

    if not pending:
        st.success("✅ Nenhuma solicitação pendente no momento.")
        return

    for req in pending:
        req_user = req.get("username", "")
        requested_at = req.get("requested_at", "")
        with st.container(border=True):
            st.write(f"**{req_user}** — solicitado em {requested_at}")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("✅ Aprovar", key=f"approve_{req_user}", use_container_width=True, type="primary"):
                    try:
                        client.decide(req_user, True, username)
                        log_action(config, username, "Aprovar Acesso", "Solicitações Pendentes", f"Aprovou o acesso de {req_user}")
                        st.success(f"{req_user} aprovado.")
                        st.rerun()
                    except Exception as error:
                        st.error(f"❌ {error}")
            with c2:
                if st.button("🚫 Negar", key=f"deny_{req_user}", use_container_width=True):
                    try:
                        client.decide(req_user, False, username)
                        log_action(config, username, "Negar Acesso", "Solicitações Pendentes", f"Negou o acesso de {req_user}")
                        st.warning(f"{req_user} negado.")
                        st.rerun()
                    except Exception as error:
                        st.error(f"❌ {error}")


_PERMISSOES_CONHECIDAS = [
    # O assistente de QA (Passos 1 a 6) também é permissão, não um "piso"
    # liberado pra todo mundo que loga: assim dá pra ter um usuário que só
    # abre Bug, ou só cria Work Item, e não vê o fluxo de documentação.
    ("assistente_qa", "🧪 Assistente de QA (Passos 1–6)"),
    ("azure_devops", "🔗 Azure DevOps (Passo 7)"),
    ("execution_report", "📊 Relatório de Testes (Passo 8)"),
    ("azure_query", "🔎 Gerar a partir de Query do Azure DevOps"),
    ("manual_testes", "📘 Manual de Testes (UAT)"),
    ("documentos_armazenados", "🗄️ Documentos Armazenados"),
    ("mapa_mental", "🧠 Mapa Mental"),
    ("criar_bug", "🐛 Criar Bug"),
    ("criar_work_item", "🧱 Criar Work Item"),
]


def _render_user_management_section(config, client, current_username: str):
    """
    CRUD completo de usuários — restrito ao dono do app. O dono continua
    definido só no secrets.toml (nunca aparece aqui pra editar/excluir).
    Usuários criados aqui ficam no banco dinâmico (n8n), disponíveis pra
    login imediatamente, sem precisar reiniciar o app.

    Cada usuário tem um formulário único (nome, e-mail, nick de login,
    senha, modo de acesso, status de aprovador, e todas as permissões)
    — um só botão salva tudo de uma vez, com confirmação antes de
    gravar.

    Na CRIAÇÃO, senha é obrigatória. Na EDIÇÃO, não: em branco significa
    "mantém a senha atual" (o hash existente é lido e regravado igual),
    pra não obrigar o admin a redefinir a senha de alguém só pra mexer
    numa permissão ou no e-mail.
    """
    st.subheader("👤 Usuários")
    st.caption(
        "Cria e edita usuários que podem fazer login no app — sem precisar editar o "
        "secrets.toml. Ao editar, a senha só muda se você preencher o campo de senha."
    )

    # Aviso de sucesso do último salvamento — fica FORA dos cartões, porque
    # o cartão de quem foi salvo é fechado logo depois de gravar.
    aviso_salvo = st.session_state.pop('_usuario_salvo_aviso', None)
    if aviso_salvo:
        st.success(aviso_salvo)

    try:
        usuarios = client.list_users()
    except Exception as error:
        st.error(f"❌ Não foi possível carregar os usuários: {error}")
        usuarios = []

    if usuarios:
        for u in usuarios:
            uname = u.get("username", "")
            email_atual = u.get("email", "")
            nome_atual = u.get("nome", "")
            acesso_direto_atual = bool(u.get("acesso_direto"))
            is_approver_atual = bool(u.get("is_approver"))
            permissoes_atuais = set(u.get("permissions") or [])
            criado_em = (u.get("criado_em") or "")[:10]
            criado_por = u.get("criado_por") or ""

            # Atalho "Conceder tudo" — precisa aplicar ANTES dos widgets
            # serem criados, senão o valor novo só aparece no próximo
            # rerender (padrão do Streamlit: mudar session_state depois
            # que o widget já foi instanciado não reflete na tela atual).
            grant_all_key = f"_grant_all_pending_{uname}"
            if st.session_state.get(grant_all_key):
                st.session_state[f"edit_acesso_{uname}"] = "Acesso direto (sem aprovação)"
                st.session_state[f"edit_aprovador_{uname}"] = True
                for perm_key, _label in _PERMISSOES_CONHECIDAS:
                    st.session_state[f"edit_perm_{perm_key}_{uname}"] = True
                st.session_state[grant_all_key] = False

            # Pra conseguir FECHAR o cartão pelo código depois de salvar, o
            # expander precisa de key E de on_change != "ignore": só assim ele
            # é registrado como widget e passa a ler o estado de
            # st.session_state (com o on_change padrão "ignore", o key serve
            # apenas como id de bloco/classe CSS e o aberto/fechado fica só no
            # navegador, fora do alcance do app). O custo é um rerender a cada
            # abrir/fechar, aceitável numa tela de administração.
            card_key = f"user_card_{uname}"
            # O fechamento é pedido no clique de salvar, mas aplicado AQUI, no
            # rerender seguinte, antes do expander existir: escrever em
            # session_state de um widget já instanciado levanta
            # StreamlitAPIException ("cannot be modified after the widget ...
            # is instantiated"). Mesmo motivo do atalho "Conceder tudo" acima.
            if st.session_state.pop(f"_fechar_card_{uname}", False):
                st.session_state[card_key] = False

            with st.expander(f"👤 {nome_atual or uname}  ({uname})", key=card_key,
                              on_change="rerun"):
                info_criacao = f"Criado em {criado_em}" if criado_em else "Criado"
                if criado_por:
                    info_criacao += f" por {criado_por}"
                st.caption(info_criacao)

                novo_nome = st.text_input("Nome *", value=nome_atual, key=f"edit_nome_{uname}")
                novo_email = st.text_input("E-mail *", value=email_atual, key=f"edit_email_{uname}")
                novo_nick = st.text_input("Usuário de login (nick) *", value=uname, key=f"edit_nick_{uname}")
                nova_senha = st.text_input(
                    "Nova senha (opcional)", type="password", key=f"edit_senha_{uname}",
                    placeholder="Deixe em branco para manter a senha atual",
                    help=(
                        "Só preencha se quiser TROCAR a senha desta pessoa. Em branco, a senha "
                        "atual dela é mantida — dá pra mexer em permissões, e-mail ou modo de "
                        "acesso sem precisar saber (nem redefinir) a senha de ninguém."
                    ),
                )

                opcoes_acesso = ["Precisa de aprovação do admin", "Acesso direto (sem aprovação)"]
                if f"edit_acesso_{uname}" not in st.session_state:
                    st.session_state[f"edit_acesso_{uname}"] = opcoes_acesso[1] if acesso_direto_atual else opcoes_acesso[0]
                escolha_acesso = st.radio("Modo de acesso", options=opcoes_acesso, key=f"edit_acesso_{uname}", horizontal=True)
                novo_acesso_direto = escolha_acesso == opcoes_acesso[1]

                novo_is_approver = st.checkbox(
                    "É aprovador (pode aprovar/negar acesso de outros usuários)",
                    value=is_approver_atual, key=f"edit_aprovador_{uname}",
                )

                st.write("**Permissões:**")
                novas_permissoes = []
                cols_perm = st.columns(len(_PERMISSOES_CONHECIDAS))
                for i, (perm_key, perm_label) in enumerate(_PERMISSOES_CONHECIDAS):
                    with cols_perm[i]:
                        if st.checkbox(perm_label, value=perm_key in permissoes_atuais, key=f"edit_perm_{perm_key}_{uname}"):
                            novas_permissoes.append(perm_key)

                if st.button("⭐ Conceder tudo (acesso direto + aprovador + todas as permissões)", key=f"btn_grant_all_{uname}"):
                    st.session_state[grant_all_key] = True
                    st.rerun()

                st.divider()
                pending_key = f"_pending_save_{uname}"
                if st.button("💾 Salvar Alterações", key=f"btn_save_{uname}", type="primary", use_container_width=True):
                    if not novo_nome.strip() or not novo_email.strip() or not novo_nick.strip():
                        st.error("❌ Nome, e-mail e usuário são obrigatórios.")
                    else:
                        st.session_state[pending_key] = {
                            "new_username": novo_nick.strip(), "email": novo_email.strip(), "nome": novo_nome.strip(),
                            "senha": nova_senha, "acesso_direto": novo_acesso_direto,
                            "is_approver": novo_is_approver, "permissions": novas_permissoes,
                        }
                        st.rerun()

                pendente = st.session_state.get(pending_key)
                if pendente:
                    st.warning(
                        f"Confirma salvar? Nome: **{pendente['nome']}** | E-mail: **{pendente['email']}** | "
                        f"Usuário: **{pendente['new_username']}** | Acesso: "
                        f"**{'Direto' if pendente['acesso_direto'] else 'Precisa de aprovação'}** | "
                        f"Aprovador: **{'Sim' if pendente['is_approver'] else 'Não'}** | "
                        f"Senha: **{'TROCA para a nova' if pendente['senha'] else 'mantém a atual'}** | "
                        f"Permissões: **{', '.join(pendente['permissions']) or 'nenhuma'}**"
                    )
                    ccs1, ccs2 = st.columns(2)
                    with ccs1:
                        if st.button("✅ Confirmar e salvar", key=f"confirm_save_{uname}", type="primary", use_container_width=True):
                            try:
                                if pendente["senha"]:
                                    novo_hash = bcrypt.hashpw(
                                        pendente["senha"].encode("utf-8"), bcrypt.gensalt()
                                    ).decode("utf-8")
                                else:
                                    # Senha em branco = manter a atual. update_user_full
                                    # sempre grava o hash que recebe, então aqui a gente
                                    # lê o hash que já existe e devolve ele igual — em vez
                                    # de obrigar o admin a redefinir (ou saber) a senha da
                                    # pessoa só pra mexer numa permissão.
                                    novo_hash = client.get_user_login_info(uname).get("password_hash") or ""
                                    if not novo_hash:
                                        # Nunca gravar hash vazio: isso deixaria a pessoa
                                        # sem conseguir logar. Melhor recusar e explicar.
                                        raise AccessControlError(
                                            "Não foi possível recuperar a senha atual desta pessoa, "
                                            "então salvar agora a deixaria sem acesso. Informe uma "
                                            "nova senha no campo acima pra prosseguir."
                                        )
                                username_final = client.update_user_full(
                                    uname, pendente["new_username"], pendente["email"], pendente["nome"],
                                    novo_hash, pendente["acesso_direto"], pendente["is_approver"], pendente["permissions"],
                                )
                                log_action(
                                    config, current_username, "Editar Usuário", "Administração",
                                    f"Atualizou o cadastro de {uname} (agora {username_final})",
                                )
                                st.session_state[pending_key] = None
                                # Pede o fechamento do cartão (aplicado no
                                # rerender, antes do expander nascer) e avisa
                                # fora dele — senão a mensagem de sucesso
                                # ficava escondida dentro de um cartão aberto.
                                st.session_state[f"_fechar_card_{uname}"] = True
                                st.session_state['_usuario_salvo_aviso'] = (
                                    f"✅ Alterações salvas em **{username_final}**."
                                )
                                st.rerun()
                            except Exception as error:
                                st.error(f"❌ {error}")
                    with ccs2:
                        if st.button("✖ Cancelar", key=f"cancel_save_{uname}", use_container_width=True):
                            st.session_state[pending_key] = None
                            st.rerun()

                st.divider()
                delete_flag_key = f"confirm_delete_user_{uname}"
                if not st.session_state.get(delete_flag_key):
                    if st.button("🗑️ Excluir usuário", key=f"btn_delete_user_{uname}"):
                        st.session_state[delete_flag_key] = True
                        st.rerun()
                else:
                    st.warning(f"Excluir **{uname}**? Remove login, aprovações e permissões dele. Não pode ser desfeito.")
                    cc1, cc2 = st.columns(2)
                    with cc1:
                        if st.button("✅ Sim, excluir", key=f"confirm_del_{uname}", type="primary", use_container_width=True):
                            try:
                                client.delete_user(uname)
                                log_action(config, current_username, "Excluir Usuário", "Administração", f"Excluiu o usuário {uname}")
                                st.success(f"{uname} excluído.")
                            except Exception as error:
                                st.error(f"❌ {error}")
                            st.session_state[delete_flag_key] = False
                            st.rerun()
                    with cc2:
                        if st.button("✖ Cancelar", key=f"cancel_del_{uname}", use_container_width=True):
                            st.session_state[delete_flag_key] = False
                            st.rerun()
    else:
        st.caption("Nenhum usuário cadastrado ainda.")

    st.divider()
    # Mesmo padrão diferido do cartão de edição (ver comentário lá).
    if st.session_state.pop("_fechar_card_novo", False):
        st.session_state["user_card_novo"] = False
    with st.expander("➕ Cadastrar novo usuário", key="user_card_novo", on_change="rerun"):
        with st.form("create_user_form", clear_on_submit=True):
            novo_nome_criar = st.text_input("Nome *")
            novo_email_criar = st.text_input("E-mail *")
            novo_username_criar = st.text_input("Usuário de login (nick) *")
            nova_senha_criar = st.text_input("Senha *", type="password")
            confirmar_senha_criar = st.text_input("Confirmar senha *", type="password")
            acesso_direto_criar = st.radio(
                "Modo de acesso", options=["Precisa de aprovação do admin", "Acesso direto (sem aprovação)"],
                horizontal=True,
            ) == "Acesso direto (sem aprovação)"
            is_approver_criar = st.checkbox("É aprovador (pode aprovar/negar acesso de outros usuários)")
            st.write("**Permissões:**")
            permissoes_criar = []
            cols_criar = st.columns(len(_PERMISSOES_CONHECIDAS))
            for i, (perm_key, perm_label) in enumerate(_PERMISSOES_CONHECIDAS):
                with cols_criar[i]:
                    if st.checkbox(perm_label, key=f"criar_perm_{perm_key}"):
                        permissoes_criar.append(perm_key)

            submitted = st.form_submit_button("➕ Criar Usuário", type="primary")
            if submitted:
                novo_username_criar = novo_username_criar.strip()
                if not novo_nome_criar.strip() or not novo_email_criar.strip() or not novo_username_criar or not nova_senha_criar:
                    st.error("❌ Nome, e-mail, usuário e senha são todos obrigatórios.")
                elif novo_username_criar in _get_all_known_usernames(config, client):
                    st.error("❌ Esse nome de usuário já existe.")
                elif nova_senha_criar != confirmar_senha_criar:
                    st.error("❌ As senhas não coincidem.")
                else:
                    try:
                        novo_hash = bcrypt.hashpw(nova_senha_criar.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
                        client.create_user(
                            novo_username_criar, novo_hash, novo_email_criar.strip(), novo_nome_criar.strip(),
                            acesso_direto=acesso_direto_criar, criado_por=current_username,
                        )
                        if is_approver_criar:
                            client.add_approver(novo_username_criar)
                        for perm_key in permissoes_criar:
                            client.grant_permission(novo_username_criar, perm_key)
                        log_action(config, current_username, "Criar Usuário", "Administração", f"Criou o usuário {novo_username_criar}")
                        # Mesmo tratamento do salvar: fecha o formulário e avisa
                        # fora dele, pra pessoa ver a confirmação sem ter que
                        # rolar dentro de um bloco que continuou aberto.
                        st.session_state["_fechar_card_novo"] = True
                        st.session_state['_usuario_salvo_aviso'] = (
                            f"✅ Usuário **{novo_username_criar}** criado — já pode fazer login."
                        )
                        st.rerun()
                    except Exception as error:
                        st.error(f"❌ {error}")


def render_admin_panel(config):
    """
    Solicitações Pendentes fica visível pra qualquer aprovador (dono do app
    incluso), fora das abas. O resto (usuários, sessões, logs) é restrito
    ao dono do app (config.owner_username), organizado em abas.

    Não existem mais abas separadas de "Aprovadores"/"Permissões" — eram
    100% redundantes com a aba "Usuários": update_user_full já sincroniza
    status de aprovador e a lista completa de permissões (adiciona as que
    faltam, remove as que sobram) numa única chamada, por usuário. Duas
    telas fazendo a mesma escrita por caminhos de código diferentes só
    aumentava a superfície de manutenção sem ganhar nenhuma capacidade
    nova.
    """
    username = st.session_state.get(SESSION_USER_KEY, "")

    render_pending_approvals_panel(config)

    if username != config.owner_username:
        return

    client = AccessControlClient(config)
    st.divider()

    aba_usuarios, aba_sessoes, aba_logs = st.tabs(
        ["👤 Usuários", "🖥️ Sessões Ativas", "📜 Logs de Auditoria"]
    )

    with aba_usuarios:
        _render_user_management_section(config, client, username)

    with aba_sessoes:
        _render_active_sessions(config, client)

    with aba_logs:
        _render_audit_logs(config, client)


def _render_active_sessions(config, client):
    st.subheader("🔑 Sessões Ativas")
    st.caption(
        "Revogar uma sessão invalida o link de acesso dela imediatamente — mesmo que a pessoa "
        "já tenha o link aberto ou salvo, ele para de funcionar na próxima ação/carregamento."
    )
    my_session_id = st.session_state.get(SESSION_ID_KEY, "")
    try:
        sessions = client.list_sessions()
    except Exception as error:
        st.error(f"❌ Não foi possível carregar as sessões: {error}")
        return

    if not sessions:
        st.caption("Nenhuma sessão ativa no momento.")
        return

    for sess in sessions:
        is_mine = sess.get("session_id") == my_session_id
        created = sess.get("created_at", "")
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            created_fmt = created_dt.astimezone(TZ_BR).strftime("%d/%m/%Y %H:%M")
        except Exception:
            created_fmt = created
        c1, c2 = st.columns([4, 1])
        with c1:
            label = f"**{sess.get('username', '')}** — desde {created_fmt}"
            if is_mine:
                label += " *(esta sessão, a sua)*"
            st.write(label)
        with c2:
            if st.button("Revogar", key=f"revoke_session_{sess.get('session_id')}"):
                try:
                    client.revoke_session(sess.get("session_id"))
                    log_action(config, st.session_state.get(SESSION_USER_KEY, ""), "Revogar Sessão", "Administração",
                               f"Revogou a sessão de {sess.get('username', '')}")
                    if is_mine:
                        # Revogou a própria sessão — precisa deslogar localmente também.
                        st.session_state.pop(SESSION_AUTH_KEY, None)
                        st.session_state.pop(SESSION_USER_KEY, None)
                        st.session_state.pop(SESSION_ID_KEY, None)
                        if QUERY_PARAM_NAME in st.query_params:
                            del st.query_params[QUERY_PARAM_NAME]
                    st.rerun()
                except Exception as error:
                    st.error(f"❌ {error}")


def _render_audit_logs(config, client):
    st.subheader("📜 Logs de Auditoria")
    st.caption(
        "Últimos 500 eventos registrados no app (mais recente primeiro). Eventos mais antigos "
        "são descartados automaticamente."
    )
    try:
        logs = client.list_logs()
    except Exception as error:
        st.error(f"❌ Não foi possível carregar os logs: {error}")
        return

    if not logs:
        st.caption("Nenhum evento registrado ainda.")
        return

    usernames = sorted({log.get("username", "") for log in logs if log.get("username")})
    filtro_usuario = st.selectbox("Filtrar por usuário", options=["Todos"] + usernames, key="log_filter_user")
    logs_filtrados = logs if filtro_usuario == "Todos" else [l for l in logs if l.get("username") == filtro_usuario]

    st.caption(f"{len(logs_filtrados)} evento(s)")
    rows = []
    for log in logs_filtrados:
        ts = log.get("timestamp", "")
        try:
            ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if ts_dt.tzinfo is None:
                ts_dt = ts_dt.replace(tzinfo=timezone.utc)
            ts_fmt = ts_dt.astimezone(TZ_BR).strftime("%d/%m/%Y %H:%M:%S")
        except Exception:
            ts_fmt = ts
        rows.append({
            "Data/Hora": ts_fmt,
            "Usuário": log.get("username", ""),
            "Ação": log.get("action", ""),
            "Local": log.get("location", ""),
            "Detalhes": log.get("details", ""),
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)


# --------------------------------------------------------------------------- #
# API pública
# --------------------------------------------------------------------------- #
def require_login(config) -> bool:
    """
    Retorna True se autenticado (app pode prosseguir).
    Retorna False se a tela de login/espera foi exibida (o chamador deve parar a execução).
    """
    if st.session_state.get(SESSION_AUTH_KEY):
        return True

    if not _get_users():
        st.error(
            "⚠️ Nenhuma credencial configurada em `st.secrets['credentials']`. "
            "Configure o `.streamlit/secrets.toml` (local) ou os Secrets do Streamlit Cloud (produção)."
        )
        st.stop()

    session_id = st.query_params.get(QUERY_PARAM_NAME)
    if session_id:
        try:
            data = AccessControlClient(config).get_session(session_id)
        except Exception:
            data = {"valid": False}

        username = data.get("username", "") if data.get("valid") else ""
        if username and username in _get_users():
            st.session_state[SESSION_AUTH_KEY] = True
            st.session_state[SESSION_USER_KEY] = username
            st.session_state[SESSION_ID_KEY] = session_id
            # Renova a janela de inatividade a cada carregamento válido.
            try:
                AccessControlClient(config).renew_session(session_id, _expires_at_iso())
            except Exception:
                pass
            return True
        # ID inválido/expirado/revogado: limpa da URL pra não ficar lixo ali.
        del st.query_params[QUERY_PARAM_NAME]

    pending_username = st.session_state.get(PENDING_USERNAME_KEY)
    if pending_username:
        _render_waiting_screen(config, pending_username)
        return False

    _render_login_form(config)
    return False


def _render_login_form(config):
    st.markdown(
        """
        <style>
            [data-testid="stSidebar"] {display: none;}
            [data-testid="stToolbar"] {visibility: hidden;}
        </style>
        """,
        unsafe_allow_html=True,
    )

    _, col, _ = st.columns([1, 1.2, 1])
    with col:
        st.markdown("## 🧪 QA DevOps Automation")
        st.caption("Acesso restrito. Informe suas credenciais para continuar.")
        with st.form("login_form", clear_on_submit=False):
            username = st.text_input("Usuário")
            password = st.text_input("Senha", type="password")
            submitted = st.form_submit_button("Entrar", use_container_width=True, type="primary")

        # Coloca o cursor automaticamente no campo "Usuário" assim que a
        # tela de login aparece — sem precisar clicar antes de digitar.
        components.html(
            """
            <script>
                (function () {
                    try {
                        var doc = window.parent.document;
                        var inputs = doc.querySelectorAll('input[type="text"]');
                        if (inputs.length > 0) {
                            inputs[0].focus();
                        }
                    } catch (err) {
                        // Se o navegador não permitir acessar window.parent,
                        // só não foca automaticamente — sem quebrar a tela.
                    }
                })();
            </script>
            """,
            height=0,
        )

        if submitted:
            username = username.strip()
            is_valid, acesso_direto = _check_credentials(config, username, password)
            if is_valid:
                if username == config.owner_username or acesso_direto:
                    # Dono do app, ou usuário dinâmico com "acesso direto"
                    # marcado pelo admin no cadastro dele — pula a fila de
                    # aprovação, entra direto.
                    _grant_session(config, username)
                    st.rerun()
                else:
                    try:
                        client = AccessControlClient(config)
                        status = client.check_status(username)
                        if status == "approved":
                            client.consume(username)
                            _grant_session(config, username)
                            st.rerun()
                        else:
                            if status in ("none", "denied", "consumed"):
                                client.create_request(username)
                            st.session_state[PENDING_USERNAME_KEY] = username
                            st.rerun()
                    except Exception as error:
                        st.error(f"❌ Não foi possível verificar a aprovação de acesso: {error}")
            else:
                st.error("❌ Usuário ou senha inválidos.")


def _render_waiting_screen(config, username: str):
    st.markdown(
        """
        <style>
            [data-testid="stSidebar"] {display: none;}
            [data-testid="stToolbar"] {visibility: hidden;}
        </style>
        """,
        unsafe_allow_html=True,
    )

    _, col, _ = st.columns([1, 1.2, 1])
    with col:
        st.markdown("## ⏳ Aguardando aprovação")
        st.info(
            f"Sua solicitação de acesso como **{username}** foi enviada. Um administrador "
            "precisa aprovar antes que você possa entrar. Isso não é automático — clique em "
            "\"Verificar novamente\" depois que alguém tiver aprovado."
        )
        c1, c2 = st.columns(2)
        with c1:
            if st.button("🔄 Verificar novamente", use_container_width=True, type="primary"):
                try:
                    client = AccessControlClient(config)
                    status = client.check_status(username)
                    if status == "approved":
                        client.consume(username)
                        _grant_session(config, username)
                        st.rerun()
                    elif status == "denied":
                        st.error("❌ Sua solicitação de acesso foi negada.")
                    else:
                        st.info("Ainda aguardando aprovação.")
                except Exception as error:
                    st.error(f"❌ Erro ao verificar status: {error}")
        with c2:
            if st.button("← Cancelar", use_container_width=True):
                st.session_state.pop(PENDING_USERNAME_KEY, None)
                st.rerun()


def logout(config=None):
    session_id = st.session_state.get(SESSION_ID_KEY)
    if config is not None and session_id:
        try:
            AccessControlClient(config).revoke_session(session_id)
        except Exception:
            pass  # se falhar, a sessão local ainda é encerrada — só não é revogada remotamente
    if QUERY_PARAM_NAME in st.query_params:
        del st.query_params[QUERY_PARAM_NAME]
    st.session_state.pop(SESSION_AUTH_KEY, None)
    st.session_state.pop(SESSION_USER_KEY, None)
    st.session_state.pop(SESSION_ID_KEY, None)
    st.rerun()


def render_logout_control(config=None):
    """Controle de logout fixado no rodapé da sidebar (usuário logado + botão Sair)."""
    user = st.session_state.get(SESSION_USER_KEY, "")

    st.markdown(
        """
        <style>
            [data-testid="stSidebarUserContent"] {
                display: flex;
                flex-direction: column;
                min-height: 100%;
            }
            div[class*="st-key-sidebar_logout_box"] {
                margin-top: auto;
                padding-top: 1rem;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.sidebar:
        with st.container(key="sidebar_logout_box"):
            if user:
                st.caption(f"👤 Logado como **{user}**")
            if st.button("🚪 Sair", use_container_width=True, key="btn_logout",
                         help="Encerra e revoga esta sessão — o link deixa de funcionar, mesmo se alguém tiver uma cópia dele."):
                st.session_state['_show_logout_confirm'] = True
                st.rerun()

    if st.session_state.get('_show_logout_confirm'):
        _confirm_logout_modal(config)


@st.dialog("⚠️ Confirmar Saída")
def _confirm_logout_modal(config=None):
    has_unsaved_report = bool(
        st.session_state.get('show_execution_report_page') and st.session_state.get('report_pdf_bytes')
    )
    if has_unsaved_report:
        st.markdown(
            "Você tem um Relatório de Testes gerado nesta sessão. Ao sair, essas informações "
            "serão **perdidas** (não ficam salvas em lugar nenhum fora desta sessão), e a "
            "sessão será encerrada — vai precisar logar de novo pra voltar."
        )
    else:
        st.markdown(
            "Isso encerra e revoga sua sessão atual — vai precisar logar de novo pra voltar. "
            "Tem certeza que deseja sair?"
        )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🚪 Sair", use_container_width=True, type="primary", key="confirm_logout_btn"):
            st.session_state.pop('_show_logout_confirm', None)
            logout(config)
    with c2:
        if st.button("✖ Continuar Logado", use_container_width=True, key="cancel_logout_btn"):
            st.session_state['_show_logout_confirm'] = False
            st.rerun()
