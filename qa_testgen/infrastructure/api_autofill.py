"""
Completa a bateria de Testes de API com o que dá pra derivar sem perguntar
à pessoa — ela só digita o que só ela sabe (credenciais reais).

  * Token de outro perfil (ex.: `{{gestor_token}}`) usado por algum caso e
    que nenhum caso extrai: cria um caso "Login (gestor)" copiado do login
    que já existe na bateria (mesma rota, mesmo caminho do token na
    resposta), antes do primeiro caso que usa o token. Sobram só
    `gestor_email`/`gestor_password` pra preencher.
  * Dado de teste negativo (token inválido, e-mail inexistente/inválido,
    senha errada, ID inexistente) vazio: recebe um valor fixo — de
    propósito não vale nada, então não é segredo.

Nada aqui presume rota: o login do perfil novo reusa a rota e o caminho
de extração do login da própria bateria; sem esse login, não cria nada e
diz o porquê.

Funções puras (sem Streamlit).
"""
import copy
import json
import re
import uuid

_RE_VAR = re.compile(r"\{\{\s*([\w.-]+)\s*\}\}")
_PARTES_DE_TOKEN = {"token", "auth", "access", "bearer", "jwt"}
_NEG_INEXISTENTE = r"(nonexist|inexist|unknown|desconhecid|notfound|not_found|naoexist|nao_exist|unregistered|nao_cadastrad)"
_NEG = r"(invalid|invalido|fake|falso|expired|expirad|wrong|errad|incorrect|incorret|bad|malformed|mal_formad|revoked|revogad|" \
       + _NEG_INEXISTENTE[1:-1] + ")"

VALOR_TOKEN_INVALIDO = "token-invalido-qa-000000"
VALOR_EMAIL_INEXISTENTE = "qa.nao.existe.360@example.com"
VALOR_EMAIL_INVALIDO = "email-invalido-sem-arroba"
VALOR_SENHA_ERRADA = "SenhaErradaQA#2026"
VALOR_ID_INEXISTENTE = "999999999"


def _texto_do_caso(caso: dict) -> str:
    return " ".join([caso.get('url') or '', caso.get('body') or ''] + [str(v) for v in (caso.get('headers') or {}).values()])


def valor_negativo(nome: str):
    """Valor fixo pra variável de teste negativo, ou None se o nome não indica um."""
    n = nome.lower()
    if not re.search(_NEG, n):
        return None
    if "token" in n:
        return VALOR_TOKEN_INVALIDO
    if "email" in n or "e_mail" in n or "login" in n:
        return VALOR_EMAIL_INEXISTENTE if re.search(_NEG_INEXISTENTE, n) else VALOR_EMAIL_INVALIDO
    if "password" in n or "senha" in n or "pass" in n:
        return VALOR_SENHA_ERRADA
    if re.search(r"(^|_)id($|_)", n):
        return VALOR_ID_INEXISTENTE
    return None


def perfil_do_token(nome: str) -> str:
    """'gestor_token' → 'gestor'; 'admin_access_token' → 'admin'; 'auth_token'/'token' → ''."""
    partes = [p for p in re.split(r"[_.\-]", nome.lower()) if p]
    if "token" not in partes:
        return ""
    return "_".join(p for p in partes if p not in _PARTES_DE_TOKEN)


def _login_de_referencia(casos: list):
    """O login da bateria: rota com 'login' que extrai um token. Devolve (caso, caminho_do_token)."""
    for caso in casos:
        if 'login' not in (caso.get('url') or '').lower():
            continue
        for e in caso.get('extrair') or []:
            if 'token' in str(e.get('nome') or '').lower() and e.get('caminho'):
                return caso, e['caminho']
    return None, None


def _body_do_perfil(body: str, perfil: str):
    """Troca os valores do body do login pelas variáveis do perfil. Devolve (body, [variáveis])."""
    try:
        dados = json.loads(body or "")
    except (ValueError, TypeError):
        dados = None
    if isinstance(dados, dict) and dados:
        novo, nomes = {}, []
        for chave, valor in dados.items():
            if isinstance(valor, str):
                nome = f"{perfil}_{re.sub(r'[^a-z0-9]+', '_', chave.lower()).strip('_')}"
                novo[chave] = "{{" + nome + "}}"
                nomes.append(nome)
            else:
                novo[chave] = valor
        return json.dumps(novo, ensure_ascii=False), nomes
    nomes = []

    def _troca(m):
        nome = f"{perfil}_{m.group(1)}"
        nomes.append(nome)
        return "{{" + nome + "}}"
    return _RE_VAR.sub(_troca, body or ""), nomes


def completar_bateria(casos: list, variaveis: list, segredos: dict = None):
    """
    Devolve (casos, variaveis, relatorio) — cópias; as listas de entrada não
    mudam. `relatorio` é a lista de frases do que foi feito (e do que não deu
    pra fazer), pra mostrar na tela. Lista vazia = nada a completar.
    """
    casos = copy.deepcopy(casos or [])
    variaveis = copy.deepcopy(variaveis or [])
    segredos = segredos or {}
    relatorio = []

    # 1) dados de teste negativo
    preenchidas = []
    for v in variaveis:
        if (v.get('valor') or '').strip() or segredos.get(v.get('nome')):
            continue
        valor = valor_negativo(v.get('nome') or '')
        if valor:
            v['valor'], v['secreto'] = valor, False
            preenchidas.append(f"`{v['nome']}` = `{valor}`")
    if preenchidas:
        relatorio.append("Dados de teste negativo preenchidos: " + ", ".join(preenchidas) + ".")

    # 2) token de outro perfil sem caso que o produza
    habilitados = [c for c in casos if c.get('habilitado', True)]
    extraidas = {e.get('nome') for c in casos for e in (c.get('extrair') or [])}
    com_valor = {v['nome'] for v in variaveis if (v.get('valor') or '').strip()} | {k for k, s in segredos.items() if s}
    usadas = []
    for c in habilitados:
        for nome in _RE_VAR.findall(_texto_do_caso(c)):
            if nome not in usadas:
                usadas.append(nome)
    pendentes = [n for n in usadas if perfil_do_token(n) and valor_negativo(n) is None
                 and n not in extraidas and n not in com_valor]
    if pendentes:
        login, caminho = _login_de_referencia(casos)
        if login is None:
            relatorio.append("Não deu pra criar o login de " + ", ".join(f"`{n}`" for n in pendentes)
                             + ": a bateria não tem um caso de login que extrai token pra servir de modelo.")
        for nome in (pendentes if login is not None else []):
            perfil = perfil_do_token(nome)
            body, novas = _body_do_perfil(login.get('body') or '', perfil)
            caso = {
                **copy.deepcopy(login),
                "id": str(uuid.uuid4()),
                "nome": f"Login ({perfil}) — obtém {{{{{nome}}}}}",
                "body": body,
                "habilitado": True,
                "descricao": f"Criado automaticamente: faz login com o usuário de perfil '{perfil}' e guarda o token em {{{{{nome}}}}} para os casos seguintes.",
                "assercoes": [{"tipo": "status", "alvo": "", "valor": "200", "descricao": "Login do perfil"},
                              {"tipo": "json_not_empty", "alvo": caminho, "valor": "", "descricao": "Token retornado"}],
                "extrair": [{"nome": nome, "caminho": caminho}],
                "avisos": [],
            }
            primeiro = next(i for i, c in enumerate(casos) if c.get('habilitado', True) and nome in _RE_VAR.findall(_texto_do_caso(c)))
            casos.insert(primeiro, caso)
            conhecidas = {v['nome'] for v in variaveis}
            for n in novas:
                if n not in conhecidas:
                    variaveis.append({"nome": n, "valor": "", "secreto": any(t in n for t in ("password", "senha", "secret", "pass"))})
            relatorio.append(f"Criado o caso **Login ({perfil})**, logo antes do primeiro caso que usa `{nome}` — ele extrai o token da resposta "
                             f"(`{caminho}`) — falta só preencher " + " e ".join(f"`{n}`" for n in novas) + ".")
    return casos, variaveis, relatorio


# ---------------------------------------------------------------------------
# Valores descobertos nas respostas reais (depois de uma execução)
# ---------------------------------------------------------------------------
_PREFIXOS_OUTRO = ("other_", "outro_", "outra_", "another_", "diferente_")
_PREFIXOS_PROPRIO = ("my_", "meu_", "minha_", "current_", "own_")


def _caminhos(dado, prefixo: str = "", profundidade: int = 0):
    """(caminho, valor) de todo escalar do JSON — em lista, só o 1º item (o caminho vale pra próxima execução)."""
    if profundidade > 7:
        return
    if isinstance(dado, dict):
        for k, v in dado.items():
            yield from _caminhos(v, f"{prefixo}.{k}" if prefixo else str(k), profundidade + 1)
    elif isinstance(dado, list):
        if dado:
            yield from _caminhos(dado[0], f"{prefixo}[0]", profundidade + 1)
    elif dado is not None and not isinstance(dado, bool) and str(dado).strip():
        yield prefixo, dado


def _casa_com(caminho: str, base: str) -> bool:
    """'department_id' casa com '...department_id' e com '...department.id'."""
    partes = re.split(r"\.|\[\d+\]\.?", caminho.lower())
    partes = [p for p in partes if p]
    if not partes:
        return False
    if partes[-1] == base:
        return True
    return base.endswith("_id") and len(partes) >= 2 and partes[-1] == "id" and partes[-2] == base[:-3]


def _base_e_perfil(nome: str, perfis: set):
    n = nome.lower()
    for p in sorted(perfis, key=len, reverse=True):
        if n.startswith(p + "_"):
            return n[len(p) + 1:], p
    for p in _PREFIXOS_PROPRIO:
        if n.startswith(p):
            return n[len(p):], ""
    return n, ""


def descobrir_valores(casos: list, resultados: list, faltantes: list):
    """
    Pra cada variável sem valor, procura o campo nas respostas 2xx da última
    execução. `gestor_department_id` só vale de caso autenticado como gestor
    (usa ou extrai `{{gestor_token}}`); `department_id` só de caso que não é
    de outro perfil. `other_*` precisa de um valor DIFERENTE — o app não
    escolhe sozinho.

    Devolve (achados, nao_achados): achados = [{var, caso_id, caso_n, caso_nome,
    caminho, valor, modo}] — modo 'extrair' quando o caso que tem o campo roda
    antes do 1º que usa a variável (vira extração automática nas próximas
    execuções), 'valor' quando roda depois (o valor real vai direto na tabela).
    nao_achados = [(var, motivo)].
    """
    perfis = {perfil_do_token(e.get('nome') or '') for c in casos for e in (c.get('extrair') or [])} - {""}
    por_id = {getattr(r, 'case_id', None): r for r in resultados or []}
    achados, nao_achados = [], []
    for var in faltantes:
        if re.search(r"(email|e_mail|password|senha|pass|username|usuario|login|cpf)", var.lower()):
            nao_achados.append((var, "credencial / usuário de teste — só você sabe (ou peça aos devs; sem ele, desabilite os casos que a usam)"))
            continue
        if var.lower().startswith(_PREFIXOS_OUTRO):
            nao_achados.append((var, "precisa de um valor diferente do seu (de outro departamento/usuário) — o app não escolhe sozinho"))
            continue
        base, perfil = _base_e_perfil(var, perfis)
        token_perfil = f"{perfil}_token" if perfil else ""
        primeiro_uso = next((i for i, c in enumerate(casos) if c.get('habilitado', True)
                             and var in _RE_VAR.findall(_texto_do_caso(c))), len(casos))
        achado = None
        for i, c in enumerate(casos):
            r = por_id.get(c.get('id'))
            if r is None or getattr(r, 'pulado', False) or not (200 <= (getattr(r, 'status_code', None) or 0) < 300):
                continue
            texto = _texto_do_caso(c) + " " + " ".join(e.get('nome') or '' for e in (c.get('extrair') or []))
            tokens_do_caso = {n for n in _RE_VAR.findall(texto) if perfil_do_token(n)} | \
                             {e.get('nome') for e in (c.get('extrair') or []) if perfil_do_token(e.get('nome') or '')}
            if perfil and token_perfil not in tokens_do_caso:
                continue
            if not perfil and tokens_do_caso:
                continue   # resposta de outro perfil não serve pra variável "sua"
            try:
                dado = json.loads(getattr(r, 'response_body', '') or '')
            except (ValueError, TypeError):
                continue
            for caminho, valor in _caminhos(dado):
                if _casa_com(caminho, base):
                    achado = {"var": var, "caso_id": c['id'], "caso_n": i + 1, "caso_nome": c.get('nome', ''),
                              "caminho": caminho, "valor": str(valor), "modo": "extrair" if i < primeiro_uso else "valor"}
                    break
            if achado:
                break
        if achado:
            achados.append(achado)
        else:
            quem = f"de caso autenticado como '{perfil}'" if perfil else "dos casos executados"
            nao_achados.append((var, f"nenhuma resposta 2xx {quem} tem um campo `{base}`"))
    return achados, nao_achados


def aplicar_descobertas(casos: list, variaveis: list, achados: list):
    """Extração no caso de origem (modo 'extrair') ou valor real na tabela (modo 'valor'). Devolve cópias."""
    casos = copy.deepcopy(casos or [])
    variaveis = copy.deepcopy(variaveis or [])
    por_id = {c['id']: c for c in casos}
    for a in achados:
        if a['modo'] == 'extrair' and a['caso_id'] in por_id:
            caso = por_id[a['caso_id']]
            if not any(e.get('nome') == a['var'] for e in (caso.get('extrair') or [])):
                caso.setdefault('extrair', []).append({"nome": a['var'], "caminho": a['caminho']})
        v = next((v for v in variaveis if v['nome'] == a['var']), None)
        if v is None:
            variaveis.append({"nome": a['var'], "valor": a['valor'], "secreto": False})
        elif not v.get('secreto'):
            v['valor'] = a['valor']   # vale já na próxima execução, mesmo antes da extração rodar
    return casos, variaveis
