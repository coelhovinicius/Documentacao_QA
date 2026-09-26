"""
Triagem do resultado de uma bateria de Testes de API: separa o que é
PROVÁVEL BUG DA API do que é problema da própria bateria (requisição
montada errado, formato de resposta presumido, token esquecido), de
perfil/credencial do usuário de teste, do que precisa ser confirmado com o
PO (a API faz X, o teste esperava Y) e do que ficou bloqueado em cascata.

Pra cada suspeita de bug, diz: se é bug mesmo (e como confirmar), o que
fazer, com quem falar e o texto pronto do que dizer — e monta o rascunho do
Bug pro Azure DevOps com a evidência.

Regra de ouro: nada aqui "conserta" o esperado pra bater com o obtido.
Status esperado e texto de mensagem vindos da User Story são REQUISITO — se
a API diverge, isso vira "a confirmar com o PO", nunca um ajuste silencioso.

Funções puras (sem Streamlit).
"""
import json
import re
import uuid
from collections import OrderedDict

from qa_testgen.infrastructure.api_discovery import normalizar_caminho
from qa_testgen.infrastructure.api_evidence import ApiEvidenceBuilder

# ----------------------------------------------------------------- categorias
CATEGORIAS = OrderedDict([
    ("bug_api", ("🐞", "Possível bug da API", "Comportamento da API que merece Bug — veja a análise de cada um.")),
    ("perfil", ("👤", "Usuário de teste sem o perfil exigido", "Não é bug: o usuário usado não tem permissão pra essa rota.")),
    ("a_confirmar", ("📋", "Divergência a confirmar com o PO", "A API faz uma coisa, o teste esperava outra — o card decide quem está certo.")),
    ("bateria_auth", ("🧰", "Faltou o token no caso (bateria)", "Não é bug: o caso foi gerado sem o header Authorization.")),
    ("bateria_requisicao", ("🧰", "Requisição montada errado (bateria)", "Não é bug: a bateria mandou campos/valores que a API não usa.")),
    ("bateria_formato", ("🧰", "Formato de resposta presumido (bateria)", "Não é bug: o caso procurou campos/caminhos que a API não devolve.")),
    ("sintaxe", ("🧰", "Caminho com sintaxe não suportada", "Não é bug: a asserção usa uma sintaxe que o executor não entende.")),
    ("desempenho", ("⏱️", "Tempo acima do limite do caso", "Só é bug se a User Story tiver requisito de tempo de resposta.")),
    ("rota_inexistente", ("⛔", "Rota inexistente (bateria)", "Não é bug: a rota não existe na API — o caso presumiu a URL.")),
    ("sem_resposta", ("🔌", "Sem resposta (rede/CORS/timeout)", "A requisição não chegou a ter resposta — repetir antes de concluir.")),
    ("bloqueado", ("⏸️", "Bloqueado por dependência", "Não rodou: depende de um valor que outro caso deveria ter produzido.")),
])
GRAVIDADES = ["Crítica", "Alta", "Média", "Baixa"]
SEVERIDADE_AZURE = {"Crítica": "1 - Critical", "Alta": "2 - High", "Média": "3 - Medium", "Baixa": "4 - Low"}
PRIORIDADE_AZURE = {"Crítica": 1, "Alta": 1, "Média": 2, "Baixa": 3}

_RE_SEM_TOKEN_INTENCIONAL = re.compile(
    r"sem\s+(token|autentica|autoriza|login|credencia)|token\s+inv[aá]lid|invalid\s+token|n[aã]o\s+autenticad|"
    r"unauth|expirad|token\s+(ausente|errado|falso)|sem\s+bearer", re.I)
_RE_INTENCAO_REJEITAR = re.compile(
    r"inv[aá]lid|ausente|obrigat|vazi|fora\s+do|formato|incorret|inexistent|n[aã]o\s+pode|menor|maior|limite|"
    r"duplicad|negad|recusad|proibid|sem\s+permiss|falha|erro|conflit|\(4\d\d\)", re.I)
_RE_CHAVE_I18N = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_\-]+){1,}$")
_VAZAMENTOS = [
    (re.compile(r"SQLSTATE\[\w+\]|syntax error at or near|ORA-\d{5}"), "erro de banco de dados (SQL)", "Média"),
    (re.compile(r"Stack trace:|#\d+\s+/[\w/.\-]+\.php|Traceback \(most recent call last\)|\bat [\w.$]+\([\w.]+:\d+\)"), "stack trace do servidor", "Média"),
    (re.compile(r"/var/www/|/home/[\w.\-]+/|/app/[\w/.\-]+\.(php|py|js)|[A-Z]:\\\\[\w\\\\]+"), "caminho de arquivo do servidor", "Média"),
    (re.compile(r"No query results for model"), "mensagem interna do framework (registro não encontrado)", "Baixa"),
    (re.compile(r"App\\+Models\\+[\w\\]+"), "nome interno de classe/modelo do backend", "Baixa"),
    (re.compile(r"Illuminate\\+[\w\\]+|vendor/laravel|Symfony\\+Component"), "classe interna do framework", "Baixa"),
    (re.compile(r"\b[A-Z]\w+(Exception|Error)\b(?!\s*=)"), "nome de exceção interna", "Baixa"),
]
_CHAVES_SENSIVEIS = re.compile(r'"(password|password_hash|senha|secret|client_secret|api_key|apikey|remember_token|private_key)"\s*:\s*"([^"]+)"', re.I)
_LIMITE_LENTIDAO_MS = 3000


# ------------------------------------------------------------------ utilidades
def _json(r):
    try:
        return json.loads(getattr(r, "response_body", "") or "")
    except (ValueError, TypeError):
        return None


def mensagem_da_resposta(r) -> str:
    j = _json(r)
    if isinstance(j, dict):
        m = j.get("message")
        if isinstance(m, str):
            return m
        e = j.get("error")
        if isinstance(e, dict) and isinstance(e.get("message"), str):
            return e["message"]
        if isinstance(e, str):
            return e
    return ""


def _esperados(caso: dict) -> list:
    out = []
    for a in caso.get("assercoes") or []:
        if str(a.get("tipo")) == "status":
            try:
                out.append(int(str(a.get("valor")).strip()))
            except ValueError:
                pass
    return out


def _tem_auth(caso: dict) -> bool:
    return any(str(k).lower() == "authorization" and str(v).strip() for k, v in (caso.get("headers") or {}).items())


def _texto_caso(caso: dict) -> str:
    return f"{caso.get('nome', '')} {caso.get('descricao', '')}"


def _sem_token_intencional(caso: dict) -> bool:
    return bool(_RE_SEM_TOKEN_INTENCIONAL.search(_texto_caso(caso)))


def _camel_para_snake(nome: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", nome).lower()


def _body_dict(caso: dict):
    try:
        b = json.loads(caso.get("body") or "")
        return b if isinstance(b, dict) else None
    except (ValueError, TypeError):
        return None


def _rota(caso: dict, base_url: str) -> str:
    return f"{str(caso.get('metodo') or 'GET').upper()} {normalizar_caminho(caso.get('url') or '', base_url)}"


def _faixa(n: int) -> str:
    return f"{n // 100}xx"


def _lista_casos(nums: list, limite: int = 12) -> str:
    nums = sorted(set(nums))
    txt = ", ".join(str(n) for n in nums[:limite]) + ("…" if len(nums) > limite else "")
    return ("caso " if len(nums) == 1 else "casos ") + txt


# ------------------------------------------------------------- classificação
def _no_card(trecho: str, especificacao: str) -> bool:
    """
    O card (User Story/Critérios) cita esse texto/status? Sem card disponível,
    responde True — na dúvida trata como requisito (nunca "conserta" o esperado).
    """
    if not (especificacao or "").strip():
        return True
    return str(trecho).strip().lower() in especificacao.lower()


def _classificar(caso: dict, r, base_url: str, especificacao: str = "") -> tuple:
    """(categoria, explicação) de um resultado que NÃO passou."""
    if getattr(r, "bloqueado", False):
        return "bloqueado", r.motivo_pulo.replace("Bloqueado: ", "")
    erro = getattr(r, "erro", "") or ""
    if erro.startswith("Rota inexistente"):
        return "rota_inexistente", erro
    if erro:
        return "sem_resposta", erro
    status = r.status_code
    esperados = _esperados(caso)
    msg = mensagem_da_resposta(r)
    j = _json(r)
    falhas = [a for a in (r.assercoes or []) if not a.passou]
    status_ok = not esperados or status in esperados

    if status and status >= 500:
        return "bug_api", f"A API respondeu {status} (erro interno do servidor)."
    if esperados and all(e >= 400 for e in esperados) and 200 <= (status or 0) < 300:
        if _sem_token_intencional(caso) or 401 in esperados or 403 in esperados:
            return "bug_api", f"O caso tenta acessar sem credencial válida e a API aceitou ({status}) — esperado {esperados[0]}."
        return "bug_api", f"O caso manda um dado que deveria ser recusado e a API aceitou ({status}) — esperado {esperados[0]}."
    rota_de_login = bool(re.search(r"/(auth/)?(login|signin|token)\b", caso.get("url") or "", re.I))
    pediu_token = not msg or bool(re.search(r"unauthenticated|unauthori[sz]ed|token|n[aã]o autenticad", msg, re.I))
    if status == 401 and not _tem_auth(caso) and not _sem_token_intencional(caso) and 401 not in esperados \
            and not rota_de_login and pediu_token:
        return "bateria_auth", f"A API pediu autenticação ({msg or '401'}) e o caso não manda o header Authorization."
    if status == 403 and _tem_auth(caso) and not status_ok:
        return "perfil", f"A API negou o acesso pro usuário de teste: {msg or '403'}."
    if status in (400, 422) and not status_ok and isinstance(j, dict) and isinstance(j.get("errors"), dict):
        body = _body_dict(caso) or {}
        campos_api = set(j["errors"].keys())
        renomeaveis = [k for k in body if k not in campos_api and _camel_para_snake(k) in campos_api]
        recusados = [f"{k}={body[k]!r}" for k in body if k in campos_api]
        if any(e < 300 for e in esperados) or renomeaveis:
            partes = []
            if renomeaveis:
                partes.append("campos com nome diferente do que a API usa: " + ", ".join(f"{k} → {_camel_para_snake(k)}" for k in renomeaveis))
            if recusados:
                partes.append("valores recusados: " + ", ".join(recusados))
            faltam = [c for c in campos_api if c not in body and c not in {_camel_para_snake(k) for k in body}]
            if faltam and any(e < 300 for e in esperados):
                partes.append("a API exige: " + ", ".join(faltam))
            return "bateria_requisicao", "A API recusou a requisição (" + (msg or str(status)) + ")" + (": " + "; ".join(partes) if partes else "") + "."
    if not status_ok:
        obtido = f"A API respondeu {status}{' (' + msg + ')' if msg else ''}; o caso esperava {' ou '.join(map(str, esperados))}"
        if any(_no_card(e, especificacao) for e in esperados):
            return "a_confirmar", obtido + "."
        return "bateria_formato", obtido + " — o card não cita esse status: o teste presumiu (a API usa " + str(status) + ")."
    # status certo, outras asserções falharam
    detalhes = [a.detalhe or "" for a in falhas]
    if falhas and all("levou" in d and "limite" in d for d in detalhes):
        return "desempenho", "; ".join(detalhes)
    if falhas and all("sintaxe de caminho" in d for d in detalhes):
        return "sintaxe", "; ".join(detalhes)
    textos_esperados = []
    for a, orig in zip(r.assercoes or [], caso.get("assercoes") or []):
        if not a.passou and orig.get("tipo") in ("body_contains", "json_equals", "json_contains") and str(orig.get("valor") or "").strip():
            textos_esperados.append(str(orig.get("valor")).strip())
    textos_do_card = [t for t in textos_esperados if not t.startswith("{{") and _no_card(t, especificacao)]
    if textos_do_card and msg and _RE_CHAVE_I18N.match(msg):
        return "a_confirmar", (f"O caso esperava a mensagem '{textos_do_card[0]}'; a API devolve a chave de tradução '{msg}' "
                               "(normalmente o front traduz) — confirme com o PO se o card exige o texto na API.")
    return "bateria_formato", "Campos/caminhos/textos que a API não devolve: " + "; ".join(d for d in detalhes if d)[:400]


# ---------------------------------------------------------------- achados
def _novo_achado(tipo, titulo, gravidade, confianca, **kw) -> dict:
    return {"id": tipo, "titulo": titulo, "gravidade": gravidade, "confianca": confianca, "casos": [], "case_ids": [],
            "evidencia": "", "analise": "", "como_confirmar": [], "o_que_fazer": [], "com_quem_falar": [],
            "mensagens": [], "esperado": "", "obtido": "", **kw}


def _quem(contexto: dict, papel: str) -> str:
    """Nome concreto quando o app sabe (responsável do Work Item), senão o papel."""
    resp = [w for w in (contexto.get("responsaveis") or []) if w.get("responsavel")]
    if papel == "dev" and resp:
        return f"{resp[0]['responsavel']} (responsável pelo Work Item #{resp[0]['id']})"
    return {
        "dev": "o dev backend responsável pela API (quem implementou o endpoint)",
        "tl": "o Tech Lead do time",
        "po": "o PO / dono da User Story",
        "massa": "quem administra os usuários de teste do HML (dev responsável ou Tech Lead)",
        "infra": "o time de DevOps/infra",
    }[papel]


def _descobrir_achados(casos: list, resultados: list, contexto: dict) -> list:
    base = contexto.get("base_url") or ""
    amb = contexto.get("ambiente") or "HML"
    por_id = {c["id"]: (n, c) for n, c in enumerate(casos, 1)}
    exec_ = [(por_id[r.case_id][0], por_id[r.case_id][1], r) for r in resultados
             if r.case_id in por_id and not getattr(r, "pulado", False) and not getattr(r, "erro", "") and r.status_code]
    achados = []

    # 1) vazamento de detalhe interno
    vaz = {}
    for n, c, r in exec_:
        corpo = r.response_body or ""
        for rx, desc, grav in _VAZAMENTOS:
            m = rx.search(corpo)
            if m:
                g = vaz.setdefault(desc, {"grav": grav, "casos": [], "ids": [], "ex": None})
                g["casos"].append(n)
                g["ids"].append(c["id"])
                if g["ex"] is None:
                    g["ex"] = (c, r)
                break
    if vaz:
        grav = "Média" if any(v["grav"] == "Média" for v in vaz.values()) else "Baixa"
        casos_n = sorted({x for v in vaz.values() for x in v["casos"]})
        ids = [i for v in vaz.values() for i in v["ids"]]
        c0, r0 = next(iter(vaz.values()))["ex"]
        msg0 = mensagem_da_resposta(r0) or (r0.response_body or "")[:200]
        padrao_i18n = sum(1 for _, _, r in exec_ if r.status_code >= 400 and _RE_CHAVE_I18N.match(mensagem_da_resposta(r) or "-"))
        rotas = sorted({_rota(c, base) for n, c, r in exec_ if n in casos_n})
        a = _novo_achado(
            "vazamento_interno", "Resposta de erro expõe detalhe interno do backend", grav, "Provável",
            casos=casos_n, case_ids=ids,
            evidencia=f"{r0.metodo} {r0.url_final} → HTTP {r0.status_code}: {msg0}",
            esperado="Erro com mensagem padronizada (ex.: chave i18n como api.errors.not_found), sem nome de classe, "
                     "tabela, caminho de arquivo ou stack trace.",
            obtido=f"HTTP {r0.status_code} com: {msg0}",
        )
        tipos = ", ".join(sorted(vaz.keys()))
        a["analise"] = (
            f"É bug, de gravidade {grav.lower()}: a resposta revela {tipos}. Isso não quebra a funcionalidade, mas entrega a "
            "quem está de fora pistas da estrutura interna (framework, nomes de modelos) — é o tipo de coisa que auditoria de "
            "segurança aponta (OWASP: exposição de informação em mensagens de erro)."
            + (f" Além disso, foge do padrão: {padrao_i18n} outra(s) resposta(s) de erro desta mesma execução usam chave "
               "padronizada (api.errors.*)." if padrao_i18n else "")
        )
        a["como_confirmar"] = [
            "Repita a chamada com um id que não existe (no Postman ou reexecutando só esse caso) — se a mensagem vier igual, está confirmado.",
            "Se tiver acesso, confira se em Produção o comportamento é o mesmo (em HML o debug às vezes está ligado).",
        ]
        a["o_que_fazer"] = [
            "Abrir o Bug (rascunho pronto abaixo) com severidade " + grav + " e anexar o request/response dos casos.",
            "Sugerir a correção: tratar o 'registro não encontrado' no handler de exceções e responder algo como "
            "{\"message\": \"api.errors.not_found\"}, no mesmo padrão das outras mensagens.",
            "Conferir se APP_DEBUG/modo debug está desligado no ambiente.",
        ]
        a["com_quem_falar"] = [{"quem": _quem(contexto, "dev"), "por_que": "é quem corrige o tratamento de erro no backend"},
                               {"quem": _quem(contexto, "tl"), "por_que": "exposição de detalhe interno é assunto de segurança — ele decide a prioridade"}]
        a["mensagens"] = [{
            "para": _quem(contexto, "dev"),
            "texto": (f"Oi! Nos testes de API em {amb} ({base}) encontrei respostas de erro que expõem detalhe interno do backend. "
                      f"Exemplo: {r0.metodo} {r0.url_final} → HTTP {r0.status_code} {msg0}\n"
                      f"Acontece em {len(casos_n)} caso(s), nas rotas: {', '.join(rotas[:6])}. "
                      "O resto da API responde com chave padronizada (ex.: api.errors.unauthenticated). "
                      "É esperado? Se não for, sugiro tratar no handler de exceções e devolver algo como "
                      "{\"message\": \"api.errors.not_found\"}. Vou abrir o bug com o request/response em anexo."),
        }]
        achados.append(a)

    # 2) erro 5xx
    e5 = [(n, c, r) for n, c, r in exec_ if r.status_code >= 500]
    if e5:
        n0, c0, r0 = e5[0]
        infra = all(r.status_code in (502, 503, 504) for _, _, r in e5)
        a = _novo_achado("erro_servidor", f"API responde erro interno ({', '.join(sorted({str(r.status_code) for _, _, r in e5}))})",
                         "Alta", "Provável", casos=[n for n, _, _ in e5], case_ids=[c["id"] for _, c, _ in e5],
                         evidencia=f"{r0.metodo} {r0.url_final} → HTTP {r0.status_code}: {(r0.response_body or '')[:200]}",
                         esperado="Resposta 2xx, ou 4xx explicando o problema da requisição — nunca 5xx.",
                         obtido=f"HTTP {r0.status_code}")
        a["analise"] = ("Erro 5xx é defeito do servidor, não do teste — a requisição pode até estar errada, mas a API deveria "
                        "responder 4xx explicando o porquê." + (" Como são 502/503/504, pode ser instabilidade de infraestrutura." if infra else ""))
        a["como_confirmar"] = ["Execute de novo esses casos: se o 5xx se repetir com a mesma requisição, está confirmado."]
        a["o_que_fazer"] = ["Abrir Bug com severidade Alta anexando request/response.", "Se for 502/503/504 intermitente, avisar infra antes de abrir bug."]
        a["com_quem_falar"] = [{"quem": _quem(contexto, "infra" if infra else "dev"), "por_que": "responsável por " + ("disponibilidade do ambiente" if infra else "o endpoint")}]
        a["mensagens"] = [{"para": a["com_quem_falar"][0]["quem"],
                           "texto": f"Oi! Em {amb}, {r0.metodo} {r0.url_final} está respondendo HTTP {r0.status_code} "
                                    f"({len(e5)} caso(s) na execução). Consegue olhar o log? Abro bug com o request/response."}]
        achados.append(a)

    # 3) acesso sem credencial aceito / 4) validação ausente
    aceitos = [(n, c, r) for n, c, r in exec_ if 200 <= r.status_code < 300 and _esperados(c) and all(e >= 400 for e in _esperados(c))]
    seg = [(n, c, r) for n, c, r in aceitos if _sem_token_intencional(c) or {401, 403} & set(_esperados(c))]
    val = [(n, c, r) for n, c, r in aceitos if (n, c, r) not in seg]
    if seg:
        n0, c0, r0 = seg[0]
        a = _novo_achado("acesso_indevido", "API aceita acesso sem credencial válida", "Crítica", "Provável",
                         casos=[n for n, _, _ in seg], case_ids=[c["id"] for _, c, _ in seg],
                         evidencia=f"{r0.metodo} {r0.url_final} sem token válido → HTTP {r0.status_code}",
                         esperado="401 (sem token/token inválido) ou 403 (sem permissão).", obtido=f"HTTP {r0.status_code} com dados")
        a["analise"] = ("Se o caso realmente mandou a chamada sem token (ou com token inválido) e recebeu 2xx, é falha de segurança: "
                        "a rota está desprotegida. Confira no request do caso se o header Authorization estava mesmo ausente/errado.")
        a["como_confirmar"] = ["Abra o request do caso e veja o header Authorization.", "Repita no Postman sem o header."]
        a["o_que_fazer"] = ["Abrir Bug Crítico imediatamente.", "Avisar o Tech Lead no mesmo dia — não esperar a daily."]
        a["com_quem_falar"] = [{"quem": _quem(contexto, "tl"), "por_que": "falha de segurança"}, {"quem": _quem(contexto, "dev"), "por_que": "corrige a proteção da rota"}]
        a["mensagens"] = [{"para": _quem(contexto, "tl"),
                           "texto": f"Urgente: em {amb}, {r0.metodo} {r0.url_final} responde {r0.status_code} com dados mesmo sem token válido. "
                                    "Abri bug crítico com a evidência — dá pra priorizar?"}]
        achados.append(a)
    if val:
        n0, c0, r0 = val[0]
        a = _novo_achado("validacao_ausente", "API aceita dado que o teste esperava ver recusado", "Média", "Possível",
                         casos=[n for n, _, _ in val], case_ids=[c["id"] for _, c, _ in val],
                         evidencia=f"{c0.get('nome')}: {r0.metodo} {r0.url_final} → HTTP {r0.status_code} (esperado {_esperados(c0)[0]})",
                         esperado=f"HTTP {_esperados(c0)[0]} recusando o dado.", obtido=f"HTTP {r0.status_code} (aceitou)")
        a["analise"] = ("Pode ser bug de validação — ou o teste presumiu uma regra que o card não tem. "
                        "Só é bug se a User Story disser que esse dado deve ser recusado.")
        a["como_confirmar"] = ["Leia os Critérios de Aceite do card: a regra está lá?", "Confira no request se o dado enviado era mesmo inválido."]
        a["o_que_fazer"] = ["Confirmar a regra com o PO; se existir, abrir Bug (severidade Média)."]
        a["com_quem_falar"] = [{"quem": _quem(contexto, "po"), "por_que": "confirma se a regra existe"}, {"quem": _quem(contexto, "dev"), "por_que": "implementa a validação"}]
        a["mensagens"] = [{"para": _quem(contexto, "po"),
                           "texto": "Oi! Nos testes de API a API aceitou dados que o teste esperava ver recusados: "
                                    + "; ".join(f"{c.get('nome')} (HTTP {r.status_code})" for _, c, r in val[:5])
                                    + ". O card exige essas validações? Se sim, abro bug."}]
        achados.append(a)

    # 5) dado sensível na resposta
    sens = []
    for n, c, r in exec_:
        if 200 <= r.status_code < 300:
            for m in _CHAVES_SENSIVEIS.finditer(r.response_body or ""):
                if m.group(2).strip() and m.group(2) != ApiEvidenceBuilder.mascarar("x"):
                    sens.append((n, c, r, m.group(1)))
                    break
    if sens:
        n0, c0, r0, campo = sens[0]
        a = _novo_achado("dado_sensivel", f"Resposta devolve campo sensível ('{campo}')", "Alta", "Provável",
                         casos=[n for n, _, _, _ in sens], case_ids=[c["id"] for _, c, _, _ in sens],
                         evidencia=f"{r0.metodo} {r0.url_final} → campo '{campo}' com valor", esperado=f"Sem o campo '{campo}' na resposta.",
                         obtido=f"Campo '{campo}' preenchido")
        a["analise"] = "A API não deveria devolver senha/segredo (nem hash) em nenhuma resposta."
        a["o_que_fazer"] = ["Abrir Bug (Alta).", "Avisar o Tech Lead."]
        a["com_quem_falar"] = [{"quem": _quem(contexto, "dev"), "por_que": "remove o campo da serialização"}, {"quem": _quem(contexto, "tl"), "por_que": "segurança"}]
        a["mensagens"] = [{"para": _quem(contexto, "dev"), "texto": f"Oi! {r0.metodo} {r0.url_final} devolve o campo '{campo}' preenchido na resposta. Abri bug com a evidência."}]
        achados.append(a)

    # 6) lentidão fora do comum
    lentos = [(n, c, r) for n, c, r in exec_ if (r.tempo_ms or 0) > _LIMITE_LENTIDAO_MS]
    if lentos:
        n0, c0, r0 = max(lentos, key=lambda x: x[2].tempo_ms)
        a = _novo_achado("lentidao", f"Respostas acima de {_LIMITE_LENTIDAO_MS // 1000}s", "Baixa", "Possível",
                         casos=[n for n, _, _ in lentos], case_ids=[c["id"] for _, c, _ in lentos],
                         evidencia=f"{r0.metodo} {r0.url_final} levou {r0.tempo_ms} ms", esperado="Tempo compatível com o requisito do card.",
                         obtido=f"{r0.tempo_ms} ms")
        a["analise"] = "Só é bug se houver requisito de desempenho; em HML, lentidão pontual pode ser do ambiente."
        a["como_confirmar"] = ["Execute de novo e compare os tempos."]
        a["o_que_fazer"] = ["Registrar como observação; abrir bug só se o card tiver meta de tempo."]
        a["com_quem_falar"] = [{"quem": _quem(contexto, "po"), "por_que": "diz se existe meta de tempo"}]
        a["mensagens"] = [{"para": _quem(contexto, "po"), "texto": f"Oi! Algumas chamadas em {amb} passaram de {_LIMITE_LENTIDAO_MS // 1000}s (a mais lenta: {r0.tempo_ms} ms em {r0.url_final}). Existe meta de tempo pra isso?"}]
        achados.append(a)

    ordem = {g: i for i, g in enumerate(GRAVIDADES)}
    return sorted(achados, key=lambda a: (ordem.get(a["gravidade"], 9), -len(a["casos"])))


# ----------------------------------------------------------- orientações
def _orientacoes(itens: list, casos: list, resultados: list, contexto: dict) -> list:
    """O que fazer com o que NÃO é bug (ou ainda não se sabe) — agrupado por categoria."""
    base = contexto.get("base_url") or ""
    amb = contexto.get("ambiente") or "HML"
    por_cat = OrderedDict()
    for it in itens:
        por_cat.setdefault(it["categoria"], []).append(it)
    por_id = {c["id"]: c for c in casos}
    res_por_id = {r.case_id: r for r in resultados}
    out = []

    if "perfil" in por_cat:
        its = por_cat["perfil"]
        msgs = sorted({mensagem_da_resposta(res_por_id[i["case_id"]]) for i in its if i["case_id"] in res_por_id} - {""})
        rotas = sorted({_rota(por_id[i["case_id"]], base) for i in its})
        nota = ""
        for r in resultados:
            j = _json(r)
            roles = j.get("data", {}).get("roles") if isinstance(j, dict) and isinstance(j.get("data"), dict) else None
            if isinstance(roles, dict) and "tenant" in roles and not roles.get("tenant"):
                nota = " Pelo /me, esse usuário não tem vínculo com nenhuma empresa (roles.tenant vazio) — é um usuário de backoffice."
                break
        usuario = contexto.get("usuario_teste") or "o usuário de teste"
        out.append({"categoria": "perfil", "titulo": f"{len(its)} caso(s) negados por perfil — não é bug",
                    "resumo": f"{usuario} recebeu 403 {', '.join(msgs) or ''} em {', '.join(rotas[:6])}.{nota}",
                    "o_que_fazer": ["Rodar esses casos com um usuário do perfil certo (colaborador vinculado a uma empresa; gestor/RH para as rotas de gestão).",
                                    "Guardar o e-mail do usuário certo na variável de login da bateria (a senha continua só na sessão)."],
                    "com_quem_falar": [{"quem": _quem(contexto, "massa"), "por_que": "fornece os usuários de teste de cada perfil"}],
                    "mensagens": [{"para": _quem(contexto, "massa"),
                                   "texto": f"Oi! Pra testar {', '.join(rotas[:4])} em {amb} preciso de usuários de teste com o perfil certo. "
                                            f"Hoje uso {usuario}, que recebe 403 ({', '.join(msgs) or 'sem permissão'}) nessas rotas.{nota} "
                                            "Vocês podem me passar um colaborador vinculado a uma empresa e um gestor/RH da mesma empresa? Obrigado!"}],
                    "casos": [i["n"] for i in its]})

    bateria = [i for c in ("bateria_auth", "bateria_requisicao", "bateria_formato", "sintaxe", "rota_inexistente") for i in por_cat.get(c, [])]
    if bateria:
        out.append({"categoria": "bateria", "titulo": f"{len(bateria)} caso(s) com problema na própria bateria — não é bug",
                    "resumo": "A bateria gerada presumiu coisas que a API não faz (nome de campo, caminho da resposta, token esquecido, rota). "
                              "O app aprende o formato real com esta execução e sugere a correção de cada caso.",
                    "o_que_fazer": ["Clicar em 🔧 Corrigir a bateria pelas respostas reais (logo abaixo) e revisar as sugestões.",
                                    "Depois, executar de novo — os casos passam a medir a API de verdade.",
                                    "Se houver muitos campos desconhecidos, pedir o contrato da API (Swagger/OpenAPI) ao time."],
                    "com_quem_falar": [{"quem": _quem(contexto, "dev"), "por_que": "só se precisar do contrato (Swagger/OpenAPI) da API"}],
                    "mensagens": [{"para": _quem(contexto, "dev"),
                                   "texto": f"Oi! Existe Swagger/OpenAPI (ou uma collection do Postman) da API de {amb}? "
                                            "Com o contrato os testes de API saem certos de primeira, sem presumir nome de campo."}],
                    "casos": [i["n"] for i in bateria]})

    if "a_confirmar" in por_cat:
        its = por_cat["a_confirmar"]
        grupos = OrderedDict()
        for i in its:
            grupos.setdefault(i["explicacao_curta"], []).append(i["n"])
        linhas = [f"{k} — {_lista_casos(v)}" for k, v in grupos.items()]
        wis = ", ".join(f"#{w['id']}" for w in (contexto.get("work_items") or [])[:6])
        out.append({"categoria": "a_confirmar", "titulo": f"{len(its)} divergência(s) a confirmar com o PO",
                    "resumo": "A API se comporta de um jeito e o teste (tirado do card) esperava outro. Se o card estiver certo, vira bug; "
                              "se a API estiver certa, o teste é ajustado. Nunca ajuste o esperado sem essa confirmação.",
                    "itens": linhas,
                    "o_que_fazer": ["Mandar a lista ao PO e perguntar qual comportamento é o certo.",
                                    "Card certo → criar rascunho de bug a partir do caso (botão em cada caso, mais abaixo).",
                                    "API certa → corrigir o esperado do caso na etapa 1 e anotar no card."],
                    "com_quem_falar": [{"quem": _quem(contexto, "po"), "por_que": "é quem decide o comportamento esperado"}],
                    "mensagens": [{"para": _quem(contexto, "po"),
                                   "texto": f"Oi! Nos testes de API{(' das US ' + wis) if wis else ''} a API se comporta diferente do teste em alguns pontos. "
                                            "Qual é o certo?\n" + "\n".join(f"- {l}" for l in linhas[:10])
                                            + "\nSe o card estiver certo, abro bug; se a API estiver certa, ajusto os testes."}],
                    "casos": [i["n"] for i in its]})

    if "desempenho" in por_cat:
        its = por_cat["desempenho"]
        out.append({"categoria": "desempenho", "titulo": f"{len(its)} caso(s) só falharam no limite de tempo",
                    "resumo": "O limite de tempo veio da bateria — só é bug se o card tiver meta de desempenho.",
                    "o_que_fazer": ["Confirmar se o card tem meta de tempo; se não tiver, remover a asserção de tempo desses casos."],
                    "com_quem_falar": [{"quem": _quem(contexto, "po"), "por_que": "diz se existe meta"}], "mensagens": [],
                    "casos": [i["n"] for i in its]})

    if "bloqueado" in por_cat:
        its = por_cat["bloqueado"]
        raizes = OrderedDict()
        for i in its:
            for var, produtor in re.findall(r"variável '([\w.\-]+)' está vazia — o caso \"([^\"]+)\"", i["explicacao"]):
                raizes.setdefault((var, produtor), []).append(i["n"])
            for var in re.findall(r"variável '([\w.\-]+)' está sem valor", i["explicacao"]):
                raizes.setdefault((var, None), []).append(i["n"])
        linhas = [(f"`{v}` — o caso \"{p}\" deveria produzir e falhou" if p else f"`{v}` — sem valor na seção Variáveis") + f" ({_lista_casos(ns)})"
                  for (v, p), ns in raizes.items()]
        out.append({"categoria": "bloqueado", "titulo": f"{len(its)} caso(s) bloqueados em cascata",
                    "resumo": "Não rodaram porque dependem de um valor que não existiu. Resolva a causa (lista abaixo) e execute de novo.",
                    "itens": linhas, "o_que_fazer": ["Corrigir primeiro o caso que produz cada variável (ou preencher o valor)."],
                    "com_quem_falar": [], "mensagens": [], "casos": [i["n"] for i in its]})

    if "sem_resposta" in por_cat:
        its = por_cat["sem_resposta"]
        out.append({"categoria": "sem_resposta", "titulo": f"{len(its)} caso(s) sem resposta",
                    "resumo": "Rede, timeout ou CORS — nada a concluir sobre a API ainda.",
                    "o_que_fazer": ["Executar de novo; se for 'Failed to fetch', pedir ao administrador o modo Servidor."],
                    "com_quem_falar": [], "mensagens": [], "casos": [i["n"] for i in its]})
    return out


# --------------------------------------------------------------- ponto de entrada
def analisar(casos: list, resultados: list, contexto: dict = None) -> dict:
    """
    casos: [dict do caso] (a bateria, na ordem); resultados: [ApiCaseResult].
    contexto: {base_url, ambiente, projeto, usuario_teste, work_items[{id,title}],
               responsaveis[{id, titulo, responsavel}]}.
    """
    contexto = contexto or {}
    base = contexto.get("base_url") or ""
    espec = contexto.get("especificacao") or ""
    por_id = {c["id"]: (n, c) for n, c in enumerate(casos, 1)}
    achados = _descobrir_achados(casos, resultados, contexto)
    no_achado = {cid: a for a in achados for cid in a["case_ids"]}
    itens = []
    for r in resultados:
        if r.case_id not in por_id or r.passou or (getattr(r, "pulado", False) and not getattr(r, "motivo_pulo", "")):
            continue
        n, caso = por_id[r.case_id]
        cat, expl = _classificar(caso, r, base, espec)
        if r.case_id in no_achado and cat not in ("bloqueado", "rota_inexistente", "sem_resposta"):
            cat, expl = "bug_api", f"Faz parte do achado \"{no_achado[r.case_id]['titulo']}\". " + expl
        curta = expl
        if cat == "a_confirmar":
            st, esperados = r.status_code, _esperados(caso)
            msg = mensagem_da_resposta(r)
            curta = (f"o teste esperava {' ou '.join(map(str, esperados))}, a API responde {st}" + (f" ({msg})" if msg else "")) \
                if esperados and st not in esperados else expl
        itens.append({"case_id": r.case_id, "n": n, "nome": caso.get("nome", ""), "categoria": cat, "explicacao": expl,
                      "explicacao_curta": curta})
    contagem = OrderedDict((k, 0) for k in CATEGORIAS)
    for it in itens:
        contagem[it["categoria"]] += 1
    return {"itens": itens, "achados": achados, "orientacoes": _orientacoes(itens, casos, resultados, contexto),
            "contagem": contagem, "aprovados": sum(1 for r in resultados if r.passou),
            "executados": sum(1 for r in resultados if not getattr(r, "pulado", False))}


# --------------------------------------------------------------- relatório
def linhas_relatorio(analise: dict, bugs: list = None, casos: list = None, correcoes: list = None,
                     segredos: list = None) -> dict:
    """
    Conteúdo da seção "Análise automática" dos relatórios (MD e PDF), já em
    frases prontas: {contagem: [(rotulo, n)], achados: [dict], confirmar: [str],
    orientacoes: [(titulo, resumo)], bugs: [(id, titulo, url)] (só enviados),
    rascunhos: [dict] (TODOS os Bugs — rascunho ou enviado — com o conteúdo
    completo, mascarado), correcoes: [str] (correções aplicadas na bateria)}.
    Serve pra revisar tudo num PDF ANTES de subir os Bugs pro Azure DevOps.
    """
    analise = analise or {}
    m = lambda t: ApiEvidenceBuilder.mascarar(str(t or ""), segredos or [])
    num = {c["id"]: n for n, c in enumerate(casos or [], 1)}
    contagem = [(f"{CATEGORIAS[k][0]} {CATEGORIAS[k][1]}", n) for k, n in (analise.get("contagem") or {}).items() if n]
    confirmar = next((o.get("itens") or [] for o in analise.get("orientacoes") or [] if o["categoria"] == "a_confirmar"), [])
    orient = [(o["titulo"], o["resumo"]) for o in analise.get("orientacoes") or [] if o["categoria"] != "a_confirmar"]
    enviados = [(b.get("azure_id"), b.get("titulo"), b.get("azure_url")) for b in (bugs or []) if b.get("status") == "enviado"]
    rascunhos = []
    for b in bugs or []:
        enviado = b.get("status") == "enviado"
        nums = sorted(num[c] for c in b.get("case_ids") or [] if c in num)
        rascunhos.append({
            "titulo": m(b.get("titulo")), "enviado": enviado, "url": b.get("azure_url") or "",
            "status": f"Enviado ao Azure DevOps — Bug #{b.get('azure_id')}" + (f" em {b.get('enviado_em')}" if b.get("enviado_em") else "")
                      if enviado else "Rascunho — ainda não enviado ao Azure DevOps",
            "gravidade": b.get("gravidade", ""), "severidade": b.get("severidade", ""), "prioridade": b.get("prioridade", ""),
            "descricao": m(b.get("descricao")), "passos": [m(p) for p in b.get("passos") or []],
            "esperado": m(b.get("esperado")), "obtido": m(b.get("obtido")),
            "casos": f"{_lista_casos(nums)} (numeração do relatório da execução)" if nums else "nenhum caso de evidência",
            "anexos": ("request/response de cada caso (.txt)" if nums else "") + (" + RELATORIO.pdf" if b.get("anexar_pdf") else ""),
            "discussion": m(b.get("discussion")), "mensagem": m(b.get("mensagem")),
        })
    return {"contagem": contagem, "achados": analise.get("achados") or [], "confirmar": confirmar,
            "orientacoes": orient, "bugs": enviados, "rascunhos": rascunhos, "correcoes": list(correcoes or [])}


def markdown_da_analise(analise: dict, bugs: list = None, numero: int = 0, casos: list = None,
                        correcoes: list = None, segredos: list = None) -> list:
    """Linhas Markdown da seção de análise (vazio se não houver análise nem Bugs)."""
    d = linhas_relatorio(analise, bugs, casos, correcoes, segredos)
    if not (d["contagem"] or d["achados"] or d["rascunhos"] or d["correcoes"]):
        return []
    md = [f"## {numero}. Análise automática do resultado" if numero else "## Análise automática do resultado", "",
          "_Classificação feita pelo QA TestGen: separa possível bug da API de problema da própria bateria, de perfil do "
          "usuário de teste e do que precisa ser confirmado com o PO._", ""]
    if d["contagem"]:
        md += ["| Classificação | Casos |", "|---|---|"] + [f"| {r} | {n} |" for r, n in d["contagem"]] + [""]
    if d["achados"]:
        md += ["### Possíveis bugs da API", ""]
        for a in d["achados"]:
            md += [f"**{a['titulo']}** — gravidade {a['gravidade']}, {a['confianca'].lower()} · {_lista_casos(a['casos'])}  ",
                   f"Evidência: `{a['evidencia'][:300]}`  ", f"É bug? {a['analise']}  ",
                   "Como confirmar: " + " ".join(a.get("como_confirmar") or []) + "  ",
                   "O que fazer: " + " ".join(a["o_que_fazer"]) + "  ",
                   "Com quem falar: " + "; ".join(f"{q['quem']} ({q['por_que']})" for q in a["com_quem_falar"]), ""]
            for msg in a.get("mensagens") or []:
                md += [f"O que dizer (para {msg['para']}):", "", "> " + msg["texto"].replace("\n", "\n> "), ""]
    if d["confirmar"]:
        md += ["### A confirmar com o PO", ""] + [f"- {l}" for l in d["confirmar"]] + [""]
    if d["orientacoes"]:
        md += ["### Demais pontos", ""] + [f"- **{t}** — {r}" for t, r in d["orientacoes"]] + [""]
    if d["correcoes"]:
        md += ["### Correções aplicadas na bateria (só formato)", ""] + [f"- {c}" for c in d["correcoes"]] + [""]
    if d["rascunhos"]:
        pend = sum(1 for b in d["rascunhos"] if not b["enviado"])
        md += ["### Bugs — rascunhos e enviados", "",
               f"_{len(d['rascunhos'])} Bug(s): {pend} rascunho(s) ainda não enviado(s) ao Azure DevOps, "
               f"{len(d['rascunhos']) - pend} enviado(s)._", ""]
        for i, b in enumerate(d["rascunhos"], 1):
            md += [f"#### Bug {i}. {b['titulo']}", "",
                   f"**Situação:** {b['status']}" + (f" — {b['url']}" if b["url"] else "") + "  ",
                   f"**Gravidade:** {b['gravidade']} · **Severidade:** {b['severidade']} · **Prioridade:** {b['prioridade']}  ",
                   f"**Evidência:** {b['casos']}" + (f" · anexos: {b['anexos']}" if b["anexos"] else ""), "",
                   "**Descrição:**", "", b["descricao"], "",
                   "**Passos de reprodução:**", ""] + [f"{k}. {p}" for k, p in enumerate(b["passos"], 1)] + [""]
            if b["esperado"]:
                md += [f"**Resultado esperado:** {b['esperado']}  "]
            if b["obtido"]:
                md += [f"**Resultado obtido:** {b['obtido']}  "]
            if b["discussion"]:
                md += [f"**Primeiro comentário (Discussion):** {b['discussion']}  "]
            if b["mensagem"]:
                md += ["", "**Texto pronto pro chat:**", "", "> " + b["mensagem"].replace("\n", "\n> ")]
            md += [""]
    return md


# --------------------------------------------------------------- rascunhos de bug
def _passos_reproducao(caso: dict, r, casos: list, contexto: dict, segredos: list) -> list:
    base = (contexto.get("base_url") or "").rstrip("/")
    passos = [f"Ambiente {contexto.get('ambiente') or ''}: {base}".strip()]
    usados = set(re.findall(r"\{\{\s*([\w.\-]+)\s*\}\}", json.dumps(caso.get("headers") or {}) + (caso.get("url") or "") + (caso.get("body") or "")))
    for c in casos:
        extraidas = {e.get("nome") for e in (c.get("extrair") or [])}
        tok = [v for v in usados & extraidas if "token" in v.lower()]
        if tok and c["id"] != caso["id"]:
            passos.append(f"Autenticar: {c.get('metodo')} {normalizar_caminho(c.get('url') or '', base)} com um usuário de teste "
                          f"e usar o token retornado ({c.get('extrair')[0].get('caminho')}) no header Authorization: Bearer <token>.")
            break
    url = (r.url_final or caso.get("url") or "").replace(base, "") if r is not None else caso.get("url")
    hdrs = {k: v for k, v in (ApiEvidenceBuilder.mascarar_headers(r.request_headers if r is not None else caso.get("headers") or {}, segredos)).items()
            if k.lower() not in ("accept", "content-type")}
    linha = f"Enviar {caso.get('metodo')} {url}"
    if hdrs:
        linha += " com os headers " + ", ".join(f"{k}: {v}" for k, v in hdrs.items())
    body = ApiEvidenceBuilder.mascarar((r.request_body if r is not None else caso.get("body")) or "", segredos)
    if body.strip():
        linha += f" e o corpo {body.strip()[:600]}"
    passos.append(linha + ".")
    if r is not None and r.status_code:
        passos.append(f"Observar a resposta: HTTP {r.status_code} — {(mensagem_da_resposta(r) or ApiEvidenceBuilder.mascarar(r.response_body or '', segredos))[:300]}")
    return passos


def rascunho_de_achado(achado: dict, casos: list, resultados: list, contexto: dict, segredos: list = None) -> dict:
    """Rascunho de Bug pronto pra revisar — evidência = os casos do achado."""
    por_id = {c["id"]: c for c in casos}
    num = {c["id"]: n for n, c in enumerate(casos, 1)}
    res = {r.case_id: r for r in resultados}
    cid0 = next((i for i in achado["case_ids"] if i in por_id), None)
    caso0, r0 = por_id.get(cid0), res.get(cid0)
    amb = contexto.get("ambiente") or "HML"
    tag_amb = "HML" if amb.lower().startswith("hom") else ("PROD" if amb.lower().startswith("prod") else amb)
    # O número é o do relatório da execução (conta os casos desabilitados); os Test Cases que o Passo 7
    # cria ("CTxx") só contam os habilitados, então o número pode não bater — o nome sempre bate.
    nomes = [f"{num[c]} — {por_id[c].get('nome', '')}" for c in achado["case_ids"] if c in por_id][:12]
    descricao = "\n".join([
        achado["titulo"] + ".",
        "",
        f"Onde: {contexto.get('base_url') or ''} ({amb}) — bateria \"{contexto.get('projeto') or ''}\", {_lista_casos(achado['casos'])}.",
        *([f"Casos (número no relatório da execução — nome do caso): {'; '.join(nomes)}."] if nomes else []),
        f"Evidência: {achado['evidencia']}",
        "",
        "Análise: " + achado["analise"],
        "",
        "Sugestão: " + " ".join(achado["o_que_fazer"][1:2] or achado["o_que_fazer"][:1]),
    ])
    return {
        "id": str(uuid.uuid4()), "origem": achado["id"], "status": "rascunho",
        "titulo": f"[API][{tag_amb}] {achado['titulo']}",
        "descricao": descricao,
        "passos": _passos_reproducao(caso0, r0, casos, contexto, segredos or []) if caso0 else [achado["evidencia"]],
        "esperado": achado["esperado"], "obtido": achado["obtido"],
        "gravidade": achado["gravidade"], "severidade": SEVERIDADE_AZURE.get(achado["gravidade"], "3 - Medium"),
        "prioridade": PRIORIDADE_AZURE.get(achado["gravidade"], 2),
        "case_ids": list(achado["case_ids"]), "anexar_pdf": True, "discussion": "",
        # texto pronto pra mandar no chat (Teams/WhatsApp) — não vai pro Azure
        "mensagem": (achado.get("mensagens") or [{}])[0].get("texto", ""),
    }


def rascunho_de_caso(caso: dict, r, casos: list, contexto: dict, segredos: list = None, explicacao: str = "") -> dict:
    """Rascunho de Bug a partir de um caso qualquer (ex.: divergência confirmada pelo PO)."""
    amb = contexto.get("ambiente") or "HML"
    tag_amb = "HML" if amb.lower().startswith("hom") else ("PROD" if amb.lower().startswith("prod") else amb)
    esperados = _esperados(caso)
    falhas = [f"{a.descricao}: {a.detalhe}" for a in (getattr(r, "assercoes", None) or []) if not a.passou]
    return {
        "id": str(uuid.uuid4()), "origem": "caso", "status": "rascunho",
        "titulo": f"[API][{tag_amb}] {caso.get('nome', '')}",
        "descricao": "\n".join([f"Caso de teste de API: {caso.get('nome', '')}.", (caso.get("descricao") or ""), "",
                                f"Onde: {contexto.get('base_url') or ''} ({amb}).", "",
                                ("Análise: " + explicacao) if explicacao else "",
                                "Verificações que falharam:", *[f"- {f}" for f in falhas[:15]]]).strip(),
        "passos": _passos_reproducao(caso, r, casos, contexto, segredos or []),
        "esperado": ("HTTP " + " ou ".join(map(str, esperados))) if esperados else "Conforme a User Story.",
        "obtido": f"HTTP {r.status_code} — {mensagem_da_resposta(r)[:200]}" if r is not None and r.status_code else "",
        "gravidade": "Média", "severidade": SEVERIDADE_AZURE["Média"], "prioridade": PRIORIDADE_AZURE["Média"],
        "case_ids": [caso["id"]], "anexar_pdf": True, "discussion": "", "mensagem": "",
    }


def arquivos_de_evidencia(rascunho: dict, resultados: list, segredos: list = None) -> list:
    """[(nome_arquivo, bytes)] com request + response + resultado de cada caso do rascunho (mascarados)."""
    res = {r.case_id: r for r in resultados}
    out = []
    for i, cid in enumerate(rascunho.get("case_ids") or [], 1):
        r = res.get(cid)
        if r is None or getattr(r, "pulado", False):
            continue
        slug = ApiEvidenceBuilder.slug(r.nome, 40)
        texto = "\n\n".join([
            "=== REQUEST ===", ApiEvidenceBuilder.texto_request(r, segredos or []),
            "=== RESPONSE ===", ApiEvidenceBuilder.texto_response(r, segredos or []),
            "=== RESULTADO ===", ApiEvidenceBuilder.texto_resultado(r),
        ])
        out.append((f"{i:02d}_{slug}.txt", texto.encode("utf-8")))
    return out
