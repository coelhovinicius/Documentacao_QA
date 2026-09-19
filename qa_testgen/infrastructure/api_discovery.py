"""
"Reconhecer a API": sondagens sem credencial pra descobrir o que a IA não
tem como saber a partir de uma User Story — qual variante de rota existe de
verdade, como a API formata erros (chave i18n? `errors.<campo>`?), quais
rotas exigem token. O resultado vira texto pras "Observações" da geração.

Só monta as sondas e interpreta as respostas; quem executa é o mesmo
caminho da bateria (navegador ou servidor), pra passar pelo WAF.
"""
import json
import re
import unicodedata
from urllib.parse import urlparse

_RE_METODO_ROTA = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[A-Za-z0-9_\-/{}.:]+)", re.I)
_RE_ROTA_SOLTA = re.compile(r"(?<![\w/])(/api/[A-Za-z0-9_\-/{}.:]+)")
_RE_I18N = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")

# Quando a especificação não cita nenhuma rota, sonda as convencionais.
_ROTAS_PADRAO = [("POST", "/api/v1/auth/login"), ("GET", "/api/v1/me"), ("POST", "/api/v1/auth/logout")]
_MAX_ROTAS = 8


def extrair_rotas(especificacao: str) -> list:
    """[(metodo, caminho)] citados no texto, na ordem, sem repetição."""
    vistos, rotas = set(), []
    for m in _RE_METODO_ROTA.finditer(especificacao or ""):
        par = (m.group(1).upper(), m.group(2).rstrip(".,;:)"))
        if par not in vistos:
            vistos.add(par)
            rotas.append(par)
    for m in _RE_ROTA_SOLTA.finditer(especificacao or ""):
        caminho = m.group(1).rstrip(".,;:)")
        if not any(c == caminho for _, c in rotas):
            metodo = "POST" if re.search(r"login|logout|auth|create|cadastr", caminho, re.I) else "GET"
            rotas.append((metodo, caminho))
    return rotas[:_MAX_ROTAS]


def _variantes(caminho: str) -> list:
    """Variantes plausíveis: como está, com /v1 inserido, com /api/v1 na frente."""
    v = [caminho]
    if caminho.startswith("/api/") and not re.match(r"^/api/v\d+/", caminho):
        v.append("/api/v1" + caminho[len("/api"):])
    if not caminho.startswith("/api"):
        v.append("/api/v1" + caminho)
        v.append("/api" + caminho)
    saida = []
    for c in v:
        if c not in saida:
            saida.append(c)
    return saida


def montar_sondas(especificacao: str, base_url: str, catalogo: list = None) -> list:
    """
    Lista de sondas no formato de caso do runner (sem asserções, sem
    credencial): [{"id","nome","metodo","url","headers","body","extrair","rota_original","variante"}].
    Rotas citadas na especificação ganham variantes (/api/v1 na frente etc.);
    sem rota citada, com catálogo, sonda as rotas reais mais parecidas com o
    card (login/logout/me sempre) — sem variante, porque já são reais; sem
    catálogo, as convencionais.
    """
    base = (base_url or "").rstrip("/")
    rotas = extrair_rotas(especificacao)
    exatas = False
    if not rotas and catalogo:
        rotas = [(r["metodo"], r["caminho"].replace("{id}", "1")) for r in rotas_relevantes(catalogo, especificacao, _MAX_ROTAS)]
        exatas = True
    rotas = rotas or list(_ROTAS_PADRAO)
    sondas = []
    for metodo, caminho in rotas:
        for variante in ([caminho] if exatas else _variantes(caminho)):
            sondas.append({
                "id": f"sonda-{len(sondas) + 1}",
                "nome": f"{metodo} {variante}",
                "metodo": metodo,
                "url": base + variante,
                "headers": {"Content-Type": "application/json", "Accept": "application/json"},
                "body": "{}" if metodo in ("POST", "PUT", "PATCH") else "",
                "extrair": [],
                "rota_original": caminho,
                "variante": variante,
            })
    return sondas


def _json_ou_none(texto):
    try:
        return json.loads(texto)
    except Exception:
        return None


def analisar(sondas: list, respostas: list) -> dict:
    """
    Interpreta as respostas (mesmo formato que o executor devolve) e monta
    {"observacoes": str, "tabela": [{"sonda","status","conclusao"}], "rotas_reais": {rota_original: variante}}.
    """
    tabela, rotas_reais, fatos = [], {}, []
    formato_msg_i18n = None
    tem_errors_por_campo = False
    status_validacao = None
    exige_bearer = []
    for s, r in zip(sondas, respostas or []):
        status = r.get("status")
        corpo = r.get("body") or ""
        ctype = ""
        for k, v in (r.get("response_headers") or r.get("headers") or {}).items():
            if k.lower() == "content-type":
                ctype = str(v).lower()
        js = _json_ou_none(corpo) if corpo else None
        if status is None:
            conclusao = f"sem resposta ({r.get('erro') or 'falha de rede'})"
        elif js is None or "json" not in ctype:
            conclusao = "não é rota da API (resposta não-JSON — estático/CDN)" if status in (403, 404, 405) else f"resposta não-JSON ({status})"
        elif status == 404:
            conclusao = "rota não existe (404 JSON)"
        else:
            conclusao = f"rota existe ({status})"
            if s["rota_original"] not in rotas_reais:
                rotas_reais[s["rota_original"]] = s["variante"]
            msg = js.get("message") if isinstance(js, dict) else None
            if isinstance(msg, str) and formato_msg_i18n is None:
                formato_msg_i18n = bool(_RE_I18N.match(msg.strip()))
            if isinstance(js, dict) and isinstance(js.get("errors"), dict):
                tem_errors_por_campo = True
                status_validacao = status
            if status == 401:
                exige_bearer.append(s["variante"])
        tabela.append({"sonda": s["nome"], "status": status if status is not None else "—", "conclusao": conclusao})

    for original, real in rotas_reais.items():
        if real != original:
            fatos.append(f"A rota documentada {original} não existe; a rota real é {real} — use {{{{base_url}}}}{real}.")
        else:
            fatos.append(f"A rota {real} existe e responde JSON — use {{{{base_url}}}}{real}.")
    inexistentes = [s["rota_original"] for s in sondas if s["rota_original"] not in rotas_reais]
    for rota in sorted(set(inexistentes)):
        fatos.append(f"Nenhuma variante de {rota} respondeu como API — não gere casos para essa rota (ou confirme o caminho antes).")
    if formato_msg_i18n is True:
        fatos.append('As mensagens de erro vêm no campo "message" como CHAVE i18n (ex.: api.auth.invalid_credentials) — não compare textos de mensagem, apenas verifique que "message" existe (json_exists) ou compare com json_equals_var.')
    elif formato_msg_i18n is False:
        fatos.append('As mensagens de erro vêm no campo "message" como texto — prefira json_exists em "message" a comparar o texto exato.')
    if tem_errors_por_campo:
        fatos.append(f'Erros de validação retornam HTTP {status_validacao} (NUNCA 400) com "errors.<campo>" como lista de strings — use status {status_validacao} e json_exists em errors.<campo>; não existe campo "error" no singular.')
    if exige_bearer:
        fatos.append("Rotas protegidas (respondem 401 sem token): " + ", ".join(sorted(set(exige_bearer))) + ' — TODO caso nessas rotas (inclusive os de validação de campos) deve enviar "Authorization: Bearer {{auth_token}}" extraído do login; sem token, o único resultado possível é 401. Não gere caso de cadastro/criação "aberto" nessas rotas.')
    if not fatos:
        fatos.append("Nenhuma rota citada respondeu como API a partir desta Base URL — confira a Base URL e as rotas antes de gerar.")
    return {"observacoes": "\n".join(f"- {f}" for f in fatos), "tabela": tabela, "rotas_reais": rotas_reais}


# ============================================================================
# Catálogo de rotas reais
# ----------------------------------------------------------------------------
# A IA só sabe o que está no texto do Work Item; quando ele não cita rota,
# ela inventa. O catálogo é a lista de rotas que EXISTEM de verdade nessa
# Base URL — vinda do bundle do front, de uma collection do Postman, de um
# Swagger/OpenAPI, de sondagens ou de execuções que responderam — e é usado
# em dois pontos: entra nas Observações da geração (a IA só pode usar rotas
# dele) e, depois de gerar, todo caso com rota fora do catálogo é
# desabilitado com aviso. Nada é "presumido": ou a rota está no catálogo,
# ou foi sondada e respondeu como API.
# ============================================================================
_RE_CHAMADA_HTTP = re.compile(r"\.(get|post|put|patch|delete|head|options|download)\(\s*[`\"']((?:/|https?://)[^`\"']{1,160})[`\"']", re.I)
_RE_METODO_STRING = re.compile(r"method\s*:\s*[`\"'](GET|POST|PUT|PATCH|DELETE)[`\"'][^`\"']{0,80}?url\s*:\s*[`\"']((?:/|https?://)[^`\"']{1,160})[`\"']", re.I)
_RE_LITERAL_API = re.compile(r"[`\"']((?:/api)?/v\d+/[A-Za-z0-9_\-/{}$.:]{1,120})[`\"']")
_RE_PREFIXO_API = re.compile(r"[=:(,]\s*[`\"'](/api(?:/v\d+)?)[`\"']")
_RE_SCRIPT_SRC = re.compile(r"""<(?:script|link)[^>]+?(?:src|href)=["']([^"']+\.m?js(?:\?[^"']*)?)["']""", re.I)
_RE_NAO_API = re.compile(r"\.(m?js|css|map|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|html?|json|xml|txt|pdf)$|^/(assets|static|public|images?|img|fonts?)/", re.I)
_RE_SEG_ID = re.compile(r"^(\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\$\{[^}]*\}|\{[^}]*\}|:[A-Za-z_]+|\{\{[^}]*\}\})$", re.I)
_MAX_ROTAS_NO_PROMPT = 40
# Radicais em português -> pedaços de rota em inglês, pra achar as rotas que têm
# a ver com a especificação (o card fala "pesquisa psicossocial", a rota é
# psychosocial-surveys).
_SINONIMOS_ROTA = {
    "pesquis": ["survey"], "psicossoc": ["psychosocial"], "respost": ["response", "answer"], "pergunt": ["question"],
    "dimens": ["dimension"], "humor": ["mood"], "diari": ["daily"], "plano": ["plan"], "acao": ["action"], "ação": ["action"],
    "alert": ["alert"], "risco": ["risk"], "login": ["auth", "login"], "autentic": ["auth"], "usuari": ["user", "me"],
    "usuário": ["user", "me"], "perfil": ["me", "profile"], "colaborador": ["me", "employee"], "setor": ["department", "sector"],
    "departament": ["department"], "gestor": ["manager"], "resultado": ["result"], "dashboard": ["dashboard"], "mapa": ["heatmap"],
    "calor": ["heatmap"], "export": ["export"], "pulso": ["pulse"], "mensal": ["monthly"], "consent": ["consent", "response"],
    "ciclo": ["survey"], "logout": ["logout"], "sair": ["logout"], "senha": ["password", "auth"], "cadastr": ["register", "create", "account"],
    # genéricos, pra qualquer projeto (o card vem em português, a rota quase sempre em inglês)
    "vaga": ["job", "vacancy", "position"], "candidat": ["candidate", "applicant", "candidato"], "curricul": ["curriculum", "resume", "cv"],
    "empresa": ["company", "employer"], "endere": ["address"], "cidade": ["city"], "estado": ["state"], "pagament": ["payment", "checkout"],
    "cupom": ["coupon"], "convite": ["invitation", "invite"], "foto": ["photo", "avatar"], "experienc": ["experience"],
    "formac": ["formation", "education"], "profission": ["professional"], "academ": ["academic"], "curso": ["course"], "certific": ["certificate"], "trilha": ["trail", "track"],
    "avalia": ["assessment", "test", "evaluation"], "teste": ["test"], "recomend": ["recommendation"], "favorit": ["favorite", "favoritar"],
    "area": ["area"], "carreira": ["career"], "conta": ["account"], "dependente": ["dependent"], "cancel": ["cancel"], "token": ["auth", "refresh"],
    "assinatura": ["subscription", "plan"], "notifica": ["notification"], "arquivo": ["file", "upload", "arquivo"], "relatorio": ["report"],
    "produto": ["product"], "pedido": ["order"], "cliente": ["customer", "client"], "fatura": ["invoice"], "permiss": ["permission", "role"],
    "papel": ["role"], "grupo": ["group"], "equipe": ["team"], "mensagem": ["message"], "comentario": ["comment"], "busca": ["search"],
    "editar": ["update", "edit"], "excluir": ["delete", "remove"],
    "remover": ["delete", "remove"], "criar": ["create", "store"], "atualiz": ["update"], "importa": ["import", "importar"], "exporta": ["export"],
}
_ROTAS_SEMPRE = ("/auth/login", "/auth/logout", "/me")


def descobrir_bundles(html: str, base_url: str) -> list:
    """URLs absolutas dos scripts JS referenciados na página (bundle do front)."""
    base = (base_url or "").rstrip("/")
    saida = []
    for m in _RE_SCRIPT_SRC.finditer(html or ""):
        src = m.group(1)
        if src.startswith("http"):
            url = src
        elif src.startswith("//"):
            url = "https:" + src
        else:
            url = base + ("" if src.startswith("/") else "/") + src
        if url not in saida:
            saida.append(url)
    return saida


def _prefixo_api(js: str) -> str:
    """Prefixo que o front concatena antes das rotas (ex.: `/api`), se existir."""
    for m in _RE_PREFIXO_API.finditer(js or ""):
        return m.group(1)
    return ""


def normalizar_caminho(caminho_ou_url: str, base_url: str = "") -> str:
    """
    Caminho canônico pra comparar rotas: sem base/host, sem query string, sem
    barra final, e todo segmento variável ({{survey_id}}, ${e}, {id}, :id,
    número, uuid) vira {id}.
    """
    c = (caminho_ou_url or "").strip()
    base = (base_url or "").rstrip("/")
    if base and c.startswith(base):
        c = c[len(base):]
    c = c.replace("{{base_url}}", "").replace("{{ base_url }}", "")
    if c.startswith("http"):
        c = "/" + c.split("://", 1)[1].split("/", 1)[1] if "/" in c.split("://", 1)[1] else "/"
    c = c.split("?", 1)[0].split("#", 1)[0]
    if not c.startswith("/"):
        c = "/" + c
    segs = []
    for seg in c.split("/"):
        if seg == "":
            continue
        segs.append("{id}" if _RE_SEG_ID.match(seg) else seg)
    return "/" + "/".join(segs)


def extrair_rotas_de_bundle(js: str) -> list:
    """
    [(metodo, caminho)] a partir do JS do front: chamadas .get(`/v1/x`),
    `.post("/v1/y")`, objetos {method, url} e literais `/api/v1/...`.
    Caminhos relativos ganham o prefixo que o front usa (ex.: `/api`).
    """
    js = js or ""
    prefixo = _prefixo_api(js)
    vistos, rotas = set(), []

    def add(metodo, caminho):
        c = caminho
        if c.startswith("http"):
            c = normalizar_caminho(c)
        elif prefixo and not c.startswith(prefixo + "/") and not c.startswith("/api/"):
            c = prefixo + c
        c = normalizar_caminho(c)
        # Chamada HTTP explícita (.get(`/x`), {method,url}) é rota do front seja qual
        # for o prefixo (/api/v1, /api-candidate, /core/sso, /bff...). Fica de fora
        # só o que claramente não é API: arquivo estático, página, raiz.
        if _RE_NAO_API.search(c) or c == "/" or c.count("/") < 2:
            return
        par = (metodo.upper(), c)
        if par not in vistos:
            vistos.add(par)
            rotas.append(par)

    for m in _RE_CHAMADA_HTTP.finditer(js):
        metodo = m.group(1).upper()
        add("GET" if metodo in ("DOWNLOAD", "HEAD", "OPTIONS") else metodo, m.group(2))
    for m in _RE_METODO_STRING.finditer(js):
        add(m.group(1), m.group(2))
    conhecidos = {c for _, c in rotas}
    for m in _RE_LITERAL_API.finditer(js):
        c = normalizar_caminho((prefixo if not m.group(1).startswith("/api") else "") + m.group(1))
        if c not in conhecidos:
            conhecidos.add(c)
            rotas.append(("GET", c))
    return sorted(rotas, key=lambda r: (r[1], r[0]))


def extrair_rotas_de_openapi(doc: dict) -> list:
    """[(metodo, caminho)] de um Swagger/OpenAPI (paths → métodos), com {param} vira {id}."""
    rotas = []
    base = ""
    servers = doc.get("servers") or []
    if servers and isinstance(servers[0], dict):
        base = normalizar_caminho(str(servers[0].get("url") or "")) if "/" in str(servers[0].get("url") or "") else ""
    if doc.get("basePath"):
        base = str(doc["basePath"]).rstrip("/")
    for caminho, ops in (doc.get("paths") or {}).items():
        if not isinstance(ops, dict):
            continue
        for metodo in ("get", "post", "put", "patch", "delete"):
            if metodo in ops:
                rotas.append((metodo.upper(), normalizar_caminho((base if base != "/" else "") + caminho)))
    return sorted(set(rotas))


def extrair_rotas_de_postman(collection: dict) -> list:
    """[(metodo, caminho)] dos requests de uma collection v2.1 (recursivo nas pastas)."""
    rotas = []

    def visitar(itens):
        for it in itens or []:
            if it.get("item"):
                visitar(it["item"])
                continue
            req = it.get("request") or {}
            url = req.get("url")
            raw = url if isinstance(url, str) else (url or {}).get("raw", "")
            if raw:
                rotas.append((str(req.get("method", "GET")).upper(), normalizar_caminho(str(raw))))

    visitar(collection.get("item") or [])
    return sorted(set(rotas))


def extrair_rotas_de_texto(texto: str) -> list:
    """Linhas 'METODO /caminho' (ou só '/caminho' = GET) de um texto colado."""
    rotas = []
    for linha in (texto or "").splitlines():
        linha = linha.strip().lstrip("-*• ").strip()
        if not linha:
            continue
        m = re.match(r"^(GET|POST|PUT|PATCH|DELETE)\s+(\S+)", linha, re.I)
        if m:
            rotas.append((m.group(1).upper(), normalizar_caminho(m.group(2))))
        elif linha.startswith("/"):
            rotas.append(("GET", normalizar_caminho(linha.split()[0])))
    return sorted(set(rotas))


def casar_com_catalogo(metodo: str, caminho_ou_url: str, catalogo: list, base_url: str = "") -> bool:
    """True se (metodo, caminho) bate com alguma rota do catálogo — {id} casa com qualquer segmento."""
    alvo = normalizar_caminho(caminho_ou_url, base_url).split("/")[1:]
    for r in catalogo or []:
        m = (r.get("metodo") if isinstance(r, dict) else r[0]) or ""
        c = (r.get("caminho") if isinstance(r, dict) else r[1]) or ""
        if m.upper() != (metodo or "").upper():
            continue
        segs = c.split("/")[1:]
        if len(segs) != len(alvo):
            continue
        if all(a == b or a == "{id}" or b == "{id}" for a, b in zip(segs, alvo)):
            return True
    return False


def rota_nao_encontrada(status, body: str) -> bool:
    """
    A API disse que a ROTA não existe (e não que um recurso não foi achado):
    404 com mensagem do tipo "The route x could not be found" / "route not
    found", ou 404 sem JSON nenhum (estático/CDN).
    """
    if status != 404:
        return False
    js = _json_ou_none(body or "")
    if js is None:
        return True
    msg = str((js.get("message") if isinstance(js, dict) else "") or "").lower()
    return "route" in msg and ("could not be found" in msg or "not found" in msg)


def _sem_acento(texto: str) -> str:
    """'currículo' -> 'curriculo': o card vem acentuado, a rota nunca."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", texto or "") if not unicodedata.combining(ch))


def rotas_relevantes(catalogo: list, especificacao: str = "", limite: int = None) -> list:
    """
    Rotas do catálogo ordenadas pela parecença com a especificação (login/
    logout/me sempre na frente), cortadas em `limite`. Serve tanto pro bloco
    do prompt quanto pras sondas do "Reconhecer a API".
    """
    if not catalogo:
        return []
    limite = limite or _MAX_ROTAS_NO_PROMPT
    texto = _sem_acento((especificacao or "").lower())
    palavras = {w for w in re.findall(r"[a-z]{4,}", texto)}
    termos = set(palavras)
    for radical, ingles in _SINONIMOS_ROTA.items():
        if _sem_acento(radical) in texto:
            termos.update(ingles)

    termos = {t for t in termos if len(t) >= 4}

    def parecenca(p, t):
        # palavra igual (ou plural) vale mais que radical em comum, e palavra longa vale
        # mais que curta: "favoritar" no card tem que puxar /jobcandidate/favoritar na
        # frente de /city/lista (que só bate em "lista") e de /candidate/... (só "candi").
        if p == t or p == t + "s" or t == p + "s":
            return min(len(t), 6)
        if len(p) >= 6 and len(t) >= 6 and (p.startswith(t) or t.startswith(p)):
            return 3
        if len(t) >= 7 and len(p) > len(t) and (t in p or t[:7] in p):
            return min(max(3, len(t) // 2), 6)   # palavra colada: "experience" dentro de candidateprofessionalexperience
        return 2 if p[:5] == t[:5] else 0

    def pedacos_de(r):
        # o 1º segmento é prefixo de serviço (/api, /api-candidate, /bff, /core), não conta —
        # senão todo /api-candidate/... ganha ponto num card que fala "candidato"
        segmentos = [seg for seg in _sem_acento(r["caminho"].lower()).split("/") if seg][1:]
        return {p for seg in segmentos for p in re.findall(r"[a-z]+", seg)}

    # peso de cada termo cai quando ele bate em boa parte do catálogo ("candidato" num
    # portal de candidatos não distingue nada; "favoritar" distingue)
    pecas = {r["caminho"]: pedacos_de(r) for r in catalogo}
    peso = {}
    for t in termos:
        freq = sum(1 for ps in pecas.values() if any(parecenca(p, t) for p in ps)) / max(len(catalogo), 1)
        peso[t] = 1.0 if freq <= 0.10 else 0.5 if freq <= 0.30 else 0.25

    def pontos(r):
        caminho = _sem_acento(r["caminho"].lower())
        if any(caminho.endswith(fixa) for fixa in _ROTAS_SEMPRE):
            return 100   # login/logout/me entram sempre: quase toda bateria precisa de token
        total = 0.0
        for p in pecas[r["caminho"]]:
            contrib = sorted((parecenca(p, t) * peso[t] for t in termos), reverse=True)
            total += sum(contrib[:2 if len(p) >= 14 else 1])   # pedaço colado (candidateacademicformation) soma 2 termos
        return total

    ordenado = sorted(catalogo, key=lambda r: (-pontos(r), r["caminho"], r["metodo"]))
    # só as relevantes (pontuação > 0) mais as fixas; se sobrar espaço, completa com as demais
    relevantes = [r for r in ordenado if pontos(r) > 0][:limite]
    if len(relevantes) < limite:
        relevantes += [r for r in ordenado if pontos(r) == 0][:limite - len(relevantes)]
    return relevantes


def rotas_para_prompt(catalogo: list, especificacao: str = "") -> str:
    """
    Bloco pras Observações da geração: as rotas do catálogo (as mais
    parecidas com a especificação primeiro, limitadas pra não estourar a
    cota por minuto da IA) e a regra de só usar essas.
    """
    if not catalogo:
        return ""
    ordenado = rotas_relevantes(catalogo, especificacao, _MAX_ROTAS_NO_PROMPT)
    linhas = [f"{r['metodo']} {r['caminho']}" for r in ordenado]
    return ("ROTAS REAIS DESTA API (catálogo verificado). Gere casos SOMENTE com estas rotas, exatamente como escritas "
            "({id} = um id real vindo de um caso anterior). Se a especificação falar de algo que não está aqui, NÃO invente rota: "
            "cubra só o que existe.\n" + "\n".join(f"- {l}" for l in linhas))


def montar_sondas_para_casos(casos: list, base_url: str) -> list:
    """
    Uma sonda por rota distinta dos casos (método + caminho canônico), com
    {{variável}}/{id} trocados por 1 — serve pra perguntar à API se a rota
    existe (404 "route could not be found" = não existe). Sem credencial.
    """
    base = (base_url or "").rstrip("/")
    vistas, sondas = set(), []
    for c in casos or []:
        metodo = str(c.get("metodo") if isinstance(c, dict) else c.metodo).upper()
        url = c.get("url") if isinstance(c, dict) else c.url
        caminho = normalizar_caminho(url, base)
        par = (metodo, caminho)
        if par in vistas:
            continue
        vistas.add(par)
        sondas.append({
            "id": f"verif-{len(sondas) + 1}", "nome": f"{metodo} {caminho}", "metodo": metodo,
            "url": base + caminho.replace("{id}", "1"),
            "headers": {"Content-Type": "application/json", "Accept": "application/json"},
            "body": "{}" if metodo in ("POST", "PUT", "PATCH") else "", "extrair": [],
            "rota_original": caminho, "variante": caminho,
        })
    return sondas


def verificar_respostas_das_sondas(sondas: list, respostas: list) -> dict:
    """{caminho canônico -> True (existe) / False (não existe) / None (sem resposta)} por (metodo, caminho)."""
    saida = {}
    for s, r in zip(sondas, respostas or []):
        status = r.get("status")
        if status is None:
            saida[(s["metodo"], s["rota_original"])] = None
        else:
            saida[(s["metodo"], s["rota_original"])] = not rota_nao_encontrada(status, r.get("body") or "")
    return saida
