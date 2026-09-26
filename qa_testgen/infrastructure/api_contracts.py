"""
Catálogo de FORMATOS reais da API (o que ela valida, aceita e devolve) e
correção da bateria a partir das respostas de verdade.

Por quê: o catálogo de rotas garante que a ROTA existe, não que os CAMPOS
existem. Na execução de 25/09 a IA mandou `startDate` (a API usa
`start_date`), procurou `data.user.email` (a API devolve `data.email`) e
esqueceu o token em 3 casos. Cada execução ensina o formato real; esse
formato vai pra geração (a IA para de inventar) e vira sugestão de correção
dos casos já gerados.

Regra de ouro (igual à da triagem): só se corrige FORMATO — nome de campo,
caminho na resposta, header esquecido, extração de id. Status esperado e
texto de mensagem vindos do card são requisito e NUNCA são ajustados aqui.

Funções puras (sem Streamlit). Persistência (Turso app_config, chave
`api_contratos::<host>`) fica na UI.
"""
import copy
import json
import re
import uuid

from qa_testgen.infrastructure import api_autofill as autofill
from qa_testgen.infrastructure.api_discovery import normalizar_caminho, rotas_relevantes

_RE_VAR = re.compile(r"\{\{\s*([\w.\-]+)\s*\}\}")
_MAX_CAMINHOS = 40
_MAX_PROMPT = 2600


def _json(texto):
    try:
        return json.loads(texto or "")
    except (ValueError, TypeError):
        return None


def _rota(caso: dict, base_url: str) -> str:
    return f"{str(caso.get('metodo') or 'GET').upper()} {normalizar_caminho(caso.get('url') or '', base_url)}"


def todos_caminhos(dado, prefixo: str = "", profundidade: int = 0):
    """(caminho, valor) de TODO nó do JSON (não só folhas); em lista, só o 1º item."""
    if profundidade > 8:
        return
    if prefixo:
        yield prefixo, dado
    if isinstance(dado, dict):
        for k, v in dado.items():
            yield from todos_caminhos(v, f"{prefixo}.{k}" if prefixo else str(k), profundidade + 1)
    elif isinstance(dado, list) and dado:
        yield from todos_caminhos(dado[0], f"{prefixo}[0]", profundidade + 1)


def _partes(caminho: str) -> list:
    return [p for p in re.split(r"\.|\[\d+\]|\[\*\]", caminho or "") if p]


def _camel_para_snake(nome: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", nome).lower()


def _mensagem(j) -> str:
    if isinstance(j, dict):
        if isinstance(j.get("message"), str):
            return j["message"]
        e = j.get("error")
        if isinstance(e, dict) and isinstance(e.get("message"), str):
            return e["message"]
    return ""


# ------------------------------------------------------------------ aprender
def aprender(contratos: dict, casos: list, resultados: list, base_url: str, quando: str = "") -> dict:
    """Junta ao catálogo o que esta execução mostrou. Devolve uma cópia atualizada."""
    contratos = copy.deepcopy(contratos or {})
    por_id = {c["id"]: c for c in casos}
    for r in resultados:
        c = por_id.get(r.case_id)
        if c is None or getattr(r, "pulado", False) or getattr(r, "erro", "") or not r.status_code:
            continue
        info = contratos.setdefault(_rota(c, base_url), {"valida": {}, "aceitos": [], "recusados": {}, "resposta_2xx": [], "erros": {}})
        j = _json(r.response_body)
        body = _json(c.get("body") or "")
        if 200 <= r.status_code < 300:
            if isinstance(body, dict):
                info["aceitos"] = sorted(set(info["aceitos"]) | set(body.keys()))
            if j is not None:
                caminhos = [p for p, _ in todos_caminhos(j)]
                info["resposta_2xx"] = list(dict.fromkeys(info["resposta_2xx"] + caminhos))[:_MAX_CAMINHOS]
                info["status_2xx"] = r.status_code
        elif r.status_code >= 400:
            msg = _mensagem(j)
            if msg:
                info["erros"][str(r.status_code)] = msg[:160]
            erros = j.get("errors") if isinstance(j, dict) else None
            if isinstance(erros, dict):
                for campo, msgs in erros.items():
                    lista = msgs if isinstance(msgs, list) else [msgs]
                    atuais = info["valida"].setdefault(campo, [])
                    for m in lista:
                        if isinstance(m, str) and m not in atuais and len(atuais) < 3:
                            atuais.append(m[:140])
                    if isinstance(body, dict) and campo in body and isinstance(body[campo], (str, int, float)) \
                            and any("invalid" in str(m).lower() or "inválid" in str(m).lower() for m in lista):
                        recusados = info["recusados"].setdefault(campo, [])
                        if str(body[campo]) not in recusados and "{{" not in str(body[campo]):
                            recusados.append(str(body[campo]))
        if quando:
            info["visto_em"] = quando
    return contratos


# ------------------------------------------------------------------ prompt
def para_prompt(contratos: dict, especificacao: str = "") -> str:
    """Bloco pras Observações da geração: formato real das rotas mais relevantes."""
    if not contratos:
        return ""
    rotas = []
    for chave in contratos:
        metodo, _, caminho = chave.partition(" ")
        rotas.append({"metodo": metodo, "caminho": caminho, "_chave": chave})
    ordenadas = rotas_relevantes(rotas, especificacao, 25)
    linhas = []
    erros_gerais = {}
    for r in ordenadas:
        info = contratos[r["_chave"]]
        partes = []
        if info.get("valida"):
            partes.append("campos que a API valida: " + ", ".join(
                f"{k} ({'; '.join(v)[:70]})" if v else k for k, v in list(info["valida"].items())[:8]))
        if info.get("aceitos"):
            partes.append("corpo aceito com: " + ", ".join(info["aceitos"][:10]))
        if info.get("recusados"):
            partes.append("valores recusados: " + "; ".join(f"{k}={'/'.join(v[:4])}" for k, v in info["recusados"].items()))
        if info.get("resposta_2xx"):
            partes.append(f"{info.get('status_2xx', '2xx')} devolve: " + ", ".join(info["resposta_2xx"][:14]))
        for st, msg in (info.get("erros") or {}).items():
            vistos = erros_gerais.setdefault(st, [])
            if msg not in vistos and len(vistos) < 3:
                vistos.append(msg)
        if partes:
            linhas.append(f"- {r['_chave']}: " + " | ".join(partes))
    if not linhas and not erros_gerais:
        return ""
    texto = ("FORMATO REAL OBSERVADO NESTA API (fatos de execuções anteriores — use EXATAMENTE estes nomes de campo e "
             "caminhos de resposta; não invente outros; erro vem em `message` no primeiro nível quando indicado):\n" + "\n".join(linhas))
    if erros_gerais:
        texto += "\nMensagens de erro reais por status: " + "; ".join(f"{s} → {' / '.join(m)}" for s, m in sorted(erros_gerais.items()))
    return texto[:_MAX_PROMPT]


# ------------------------------------------------------------------ correções
def _token_principal(casos: list) -> str:
    for c in casos:
        if re.search(r"/(auth/)?login\b", c.get("url") or "", re.I):
            for e in c.get("extrair") or []:
                if "token" in str(e.get("nome") or "").lower() and not autofill.perfil_do_token(e["nome"]):
                    return e["nome"]
    return "auth_token"


def _sem_token_intencional(caso: dict) -> bool:
    return bool(re.search(r"sem\s+(token|autentica|autoriza|login|credencia)|token\s+inv[aá]lid|invalid\s+token|"
                          r"n[aã]o\s+autenticad|unauth|expirad|sem\s+bearer", f"{caso.get('nome', '')} {caso.get('descricao', '')}", re.I))


def _melhor_caminho(alvo: str, dado) -> str:
    """Caminho real com a mesma chave final do `alvo` (o mais parecido); '' se não houver ou for ambíguo."""
    partes_alvo = _partes(alvo)
    if not partes_alvo:
        return ""
    ultima = partes_alvo[-1]
    candidatos = []
    for caminho, _ in todos_caminhos(dado):
        p = _partes(caminho)
        if p and p[-1] == ultima and caminho != alvo:
            comum = 0
            for a, b in zip(p, partes_alvo):
                if a != b:
                    break
                comum += 1
            candidatos.append((comum * 10 - len(p), caminho))
    if not candidatos:
        return ""
    candidatos.sort(reverse=True)
    if len(candidatos) > 1 and candidatos[0][0] == candidatos[1][0]:
        return ""
    return candidatos[0][1]


def _sugestao(tipo, caso, n, titulo, antes, depois, evidencia, patch) -> dict:
    return {"id": str(uuid.uuid4()), "tipo": tipo, "case_id": caso["id"], "n": n, "nome": caso.get("nome", ""),
            "titulo": titulo, "antes": antes, "depois": depois, "evidencia": evidencia, "patch": patch}


def sugerir_correcoes(casos: list, resultados: list) -> list:
    """
    Sugestões de correção de FORMATO, cada uma com a evidência real que a
    justifica. Nunca mexe em status esperado nem em texto de requisito.
    """
    res = {r.case_id: r for r in resultados}
    token = _token_principal(casos)
    extraidas_msg = {e.get("nome") for c in casos for e in (c.get("extrair") or [])
                     if str(e.get("caminho") or "").split(".")[-1] in ("message", "error", "msg")}
    out = []
    for n, c in enumerate(casos, 1):
        r = res.get(c["id"])
        extraidas_aqui = {e.get("nome") for e in (c.get("extrair") or [])}
        # asserções circulares (compara a mensagem com ela mesma, ou com a mensagem de erro de OUTRO caso)
        for i, a in enumerate(c.get("assercoes") or []):
            if a.get("tipo") == "json_equals_var" and (a.get("valor") in extraidas_aqui or a.get("valor") in extraidas_msg):
                out.append(_sugestao("circular", c, n, "Trocar comparação com variável de mensagem por 'campo não vazio'",
                                     f"{a.get('alvo')} igual à variável {{{{{a.get('valor')}}}}}", f"{a.get('alvo')} não vazio",
                                     "A variável é a mensagem de erro guardada por este ou outro caso — comparar mensagens de "
                                     "situações diferentes não testa nada.", {"op": "assert_nao_vazio", "idx": i}))
        if r is None or getattr(r, "pulado", False) or getattr(r, "erro", "") or not r.status_code:
            continue
        j = _json(r.response_body)
        msg = _mensagem(j)
        esperados = [int(str(a.get("valor")).strip()) for a in (c.get("assercoes") or [])
                     if a.get("tipo") == "status" and str(a.get("valor")).strip().isdigit()]
        tem_auth = any(str(k).lower() == "authorization" for k in (c.get("headers") or {}))
        login = bool(re.search(r"/(auth/)?(login|signin|token)\b", c.get("url") or "", re.I))
        # 1) token esquecido
        if r.status_code == 401 and not tem_auth and not login and not _sem_token_intencional(c) and 401 not in esperados:
            out.append(_sugestao("add_auth", c, n, "Incluir o header Authorization",
                                 "sem Authorization", f"Authorization: Bearer {{{{{token}}}}}",
                                 f"A API respondeu 401 ({msg or 'não autenticado'}) e o caso não pedia teste sem token.",
                                 {"op": "set_header", "chave": "Authorization", "valor": f"Bearer {{{{{token}}}}}"}))
        # 2) nome de campo diferente do que a API valida
        body = _json(c.get("body") or "")
        erros = j.get("errors") if isinstance(j, dict) else None
        if r.status_code in (400, 422) and isinstance(body, dict) and isinstance(erros, dict):
            renomes = {k: _camel_para_snake(k) for k in body if k not in erros and _camel_para_snake(k) in erros}
            if renomes:
                out.append(_sugestao("renomear_campos", c, n, "Usar os nomes de campo que a API valida",
                                     ", ".join(renomes.keys()), ", ".join(renomes.values()),
                                     f"A API respondeu {r.status_code} listando os campos: {', '.join(erros.keys())}.",
                                     {"op": "renomear_body", "mapa": renomes}))
        # 3) caminho da resposta errado (só quando o status veio como o caso esperava)
        if j is not None and (not esperados or r.status_code in esperados):
            for i, (a, ar) in enumerate(zip(c.get("assercoes") or [], r.assercoes or [])):
                if ar.passou or not str(a.get("tipo", "")).startswith("json_") or a.get("tipo") in ("json_absent",):
                    continue
                if "ausente" not in (ar.detalhe or ""):
                    continue
                novo = _melhor_caminho(a.get("alvo") or "", j)
                if novo:
                    out.append(_sugestao("corrigir_caminho", c, n, "Apontar a asserção pro caminho real da resposta",
                                         a.get("alvo"), novo, f"A resposta real (HTTP {r.status_code}) tem o campo em '{novo}'.",
                                         {"op": "assert_alvo", "idx": i, "alvo": novo}))
    # 4) id que o produtor original não conseguiu extrair, mas outra resposta real tem
    produzidas = {}
    for n, c in enumerate(casos, 1):
        for e in c.get("extrair") or []:
            produzidas.setdefault(e.get("nome"), (n, c))
    falhadas = [v for v, (n, c) in produzidas.items() if v and (res.get(c["id"]) is None or not (200 <= (res[c["id"]].status_code or 0) < 300))
                and not re.search(r"token|message|msg|error", v, re.I)]
    if falhadas:
        achados, _ = autofill.descobrir_valores(casos, resultados, falhadas)
        por_id = {c["id"]: (n, c) for n, c in enumerate(casos, 1)}
        for a in achados:
            if a["modo"] != "extrair":
                continue
            n, c = por_id[a["caso_id"]]
            out.append(_sugestao("extrair_id", c, n, f"Extrair {{{{{a['var']}}}}} desta resposta",
                                 "—", f"{a['var']} ← {a['caminho']}",
                                 f"O caso que deveria produzir {{{{{a['var']}}}}} falhou; a resposta real deste caso tem "
                                 f"'{a['caminho']}' = {a['valor']}.", {"op": "add_extrair", "nome": a["var"], "caminho": a["caminho"]}))
    return out


def aplicar_correcoes(casos: list, sugestoes: list) -> tuple:
    """Aplica as sugestões escolhidas. Devolve (casos_novos, [frase do que mudou])."""
    casos = copy.deepcopy(casos)
    por_id = {c["id"]: c for c in casos}
    feito = []
    for s in sugestoes:
        c = por_id.get(s["case_id"])
        if c is None:
            continue
        p = s["patch"]
        if p["op"] == "set_header":
            c.setdefault("headers", {})[p["chave"]] = p["valor"]
        elif p["op"] == "renomear_body":
            body = _json(c.get("body") or "")
            if not isinstance(body, dict):
                continue
            c["body"] = json.dumps({p["mapa"].get(k, k): v for k, v in body.items()}, ensure_ascii=False)
        elif p["op"] == "assert_alvo":
            if p["idx"] < len(c.get("assercoes") or []):
                c["assercoes"][p["idx"]]["alvo"] = p["alvo"]
        elif p["op"] == "assert_nao_vazio":
            if p["idx"] < len(c.get("assercoes") or []):
                a = c["assercoes"][p["idx"]]
                a.update({"tipo": "json_not_empty", "valor": "", "descricao": (a.get("descricao") or "Mensagem") + " (presente)"})
        elif p["op"] == "add_extrair":
            if not any(e.get("nome") == p["nome"] for e in c.get("extrair") or []):
                c.setdefault("extrair", []).append({"nome": p["nome"], "caminho": p["caminho"]})
        else:
            continue
        feito.append(f"caso {s['n']}: {s['titulo'][:1].lower() + s['titulo'][1:]} ({s['antes']} → {s['depois']})")
    return casos, feito
