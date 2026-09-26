"""
Geração da bateria de Testes de API em lotes de Work Items.

Por quê: o workflow `Doc_QA_ApiTest_Generation` devolve no máximo 10 casos
por chamada e os provedores gratuitos têm teto de tokens por minuto — com
10+ User Stories numa chamada só, a bateria sai cortada ou rasa. Aqui os
Work Items são divididos em lotes (uma chamada por lote), cada lote recebe
o resumo do que os anteriores já geraram (pra não repetir o login e reusar
`{{auth_token}}`) e, no fim, as respostas são juntadas numa só, sem casos
repetidos.

Funções puras (sem Streamlit) — a orquestração fica em ui/api_tests_page.py.
"""
import json
import re

TAMANHO_LOTE_PADRAO = 2
_MAX_LINHAS_CONTEXTO = 40


def dividir_em_lotes(partes: list, tamanho: int) -> list:
    """Divide as partes (uma por Work Item) em lotes, na ordem em que vieram."""
    tamanho = max(1, int(tamanho or 1))
    return [partes[i:i + tamanho] for i in range(0, len(partes), tamanho)]


def _normalizar_body(body) -> str:
    if isinstance(body, (dict, list)):
        return json.dumps(body, ensure_ascii=False, sort_keys=True)
    texto = str(body or '').strip()
    try:
        return json.dumps(json.loads(texto), ensure_ascii=False, sort_keys=True)
    except (ValueError, TypeError):
        return re.sub(r"\s+", " ", texto)


def _status_esperado(caso: dict) -> str:
    return ",".join(sorted(
        str(a.get('valor') or '').strip() for a in (caso.get('assercoes') or [])
        if isinstance(a, dict) and str(a.get('tipo') or '').strip().lower() == 'status'
    ))


def chave_caso(caso: dict) -> tuple:
    """Mesma requisição + mesmo status esperado = mesmo caso (o nome não conta: cada lote dá o seu)."""
    return (
        str(caso.get('metodo') or 'GET').strip().upper(),
        re.sub(r"\s+", "", str(caso.get('url') or '')),
        _normalizar_body(caso.get('body')),
        _status_esperado(caso),
    )


def _extraidas(resps: list) -> list:
    nomes = []
    for resp in resps:
        for c in resp.get('casos') or []:
            for e in c.get('extrair') or []:
                if isinstance(e, dict) and e.get('nome') and e['nome'] not in nomes:
                    nomes.append(str(e['nome']).strip())
    return nomes


def contexto_lotes_anteriores(resps: list) -> str:
    """
    Bloco que vai nas Observações de um lote: o que os lotes anteriores já
    geraram, pra IA não repetir e reusar as variáveis extraídas (ex.: o
    token do login). Vazio no 1º lote.
    """
    casos = [c for r in resps for c in (r.get('casos') or []) if isinstance(c, dict)]
    if not casos:
        return ""
    linhas = [
        "[Casos já gerados em lotes anteriores — esta bateria roda DEPOIS deles, na mesma sessão]",
        "NÃO repita estes casos. Reutilize as variáveis que eles extraem (não gere outro login só pra obter token).",
    ]
    for c in casos[:_MAX_LINHAS_CONTEXTO]:
        status = _status_esperado(c) or "?"
        extrai = ", ".join("{{" + str(e['nome']) + "}}" for e in (c.get('extrair') or []) if isinstance(e, dict) and e.get('nome'))
        linhas.append(f"- {str(c.get('metodo') or 'GET').upper()} {c.get('url')} → {status}"
                      + (f" (extrai {extrai})" if extrai else ""))
    if len(casos) > _MAX_LINHAS_CONTEXTO:
        linhas.append(f"- … e mais {len(casos) - _MAX_LINHAS_CONTEXTO} caso(s).")
    extraidas = _extraidas(resps)
    if extraidas:
        linhas.append("Variáveis já disponíveis (extraídas acima): " + ", ".join("{{" + n + "}}" for n in extraidas))
    return "\n".join(linhas)


def juntar_variaveis(variaveis_atuais: list, resps: list) -> list:
    """Variáveis da tela + as declaradas pelos lotes anteriores (nome + secreto), sem repetir."""
    por_nome = {}
    for v in list(variaveis_atuais or []) + [v for r in resps for v in (r.get('variaveis') or [])]:
        if not isinstance(v, dict):
            continue
        nome = str(v.get('nome') or '').strip()
        if not nome or nome == 'base_url':
            continue
        atual = por_nome.setdefault(nome, {"nome": nome, "secreto": False})
        atual['secreto'] = bool(atual['secreto'] or v.get('secreto'))
    return list(por_nome.values())


def juntar_respostas(resps: list) -> dict:
    """
    Junta as respostas dos lotes numa resposta única, no mesmo formato de
    uma chamada só ({nome_sugerido, casos, variaveis, observacoes}).

    Caso repetido entre lotes (mesma requisição + mesmo status) fica uma
    vez só, na posição da primeira ocorrência — com as asserções e
    extrações das repetições somadas, pra nada do que um lote verificava
    se perder.
    """
    casos, por_chave, repetidos = [], {}, 0
    for resp in resps:
        for c in resp.get('casos') or []:
            if not isinstance(c, dict):
                continue
            chave = chave_caso(c)
            if chave not in por_chave:
                novo = dict(c)
                novo['assercoes'] = list(c.get('assercoes') or [])
                novo['extrair'] = list(c.get('extrair') or [])
                por_chave[chave] = novo
                casos.append(novo)
                continue
            repetidos += 1
            existente = por_chave[chave]
            vistas = {(str(a.get('tipo')), str(a.get('alvo')), str(a.get('valor'))) for a in existente['assercoes'] if isinstance(a, dict)}
            for a in c.get('assercoes') or []:
                if isinstance(a, dict) and (str(a.get('tipo')), str(a.get('alvo')), str(a.get('valor'))) not in vistas:
                    existente['assercoes'].append(a)
            nomes_extr = {str(e.get('nome')) for e in existente['extrair'] if isinstance(e, dict)}
            for e in c.get('extrair') or []:
                if isinstance(e, dict) and str(e.get('nome')) not in nomes_extr:
                    existente['extrair'].append(e)

    observacoes = [f"Lote {i}: {r['observacoes'].strip()}" for i, r in enumerate(resps, 1)
                   if isinstance(r.get('observacoes'), str) and r['observacoes'].strip()]
    nota = f"Gerado em {len(resps)} lotes."
    if repetidos:
        nota += f" {repetidos} caso(s) repetido(s) entre lotes foram juntados num só."
    return {
        "nome_sugerido": next((r.get('nome_sugerido') for r in resps if r.get('nome_sugerido')), ""),
        "casos": casos,
        "variaveis": [
            {**v, "descricao": next((str(x.get('descricao') or '') for r in resps for x in (r.get('variaveis') or [])
                                     if isinstance(x, dict) and str(x.get('nome') or '').strip() == v['nome'] and x.get('descricao')), "")}
            for v in juntar_variaveis([], resps)
        ],
        "observacoes": " ".join(observacoes),
        "nota_app": nota,
    }
