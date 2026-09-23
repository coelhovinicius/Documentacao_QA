"""
Caminho inverso do AzureCsvFormatter: lê de volta o CSV `QA_Plans_*.csv`
(Planos → Suítes → Casos → Passos) que o próprio app exporta, pra que ele
possa ser enviado ao Azure DevOps depois — sem refazer a geração por IA.

Formato esperado (o mesmo que plans_suites_cases escreve):

    ID,Work Item Type,Title,Test Step,Pre condicoes,Step Action,Step Expected,
    Automation Status,Area Path,Assigned To,State,Requisitos Relacionados,Suite,Plan

  * linha de CASO:  "Work Item Type" = Test Case, com Title/Pre condicoes/
    Requisitos/Suite/Plan preenchidos e "Test Step" vazio;
  * linha de PASSO: só "Test Step" (número), "Step Action" e "Step Expected"
    — pertence ao último caso lido.

A saída tem exatamente o formato que o resto do app já usa (test_cases e
test_plans), pra reaproveitar o envio ao Azure DevOps como está.
"""
import re

from qa_testgen.infrastructure.work_item_batch import ler_arquivo

# "CT01 HML - Nome real do caso" -> o prefixo é posto pelo exportador
# (AzureCsvFormatter._titled) e seria posto DE NOVO no envio, virando
# "CT01 HML - CT01 HML - ...". Guardamos o ambiente e devolvemos o título limpo.
_RE_PREFIXO_CT = re.compile(r"^CT\s*\d+\s*(HML|PROD)?\s*-\s*", re.I)

_SINONIMOS = {
    "work item type": "tipo", "tipo": "tipo",
    "title": "titulo", "titulo": "titulo", "título": "titulo",
    "test step": "passo", "step": "passo", "passo": "passo",
    "pre condicoes": "pre_condicoes", "pré-condições": "pre_condicoes",
    "pre-condicoes": "pre_condicoes", "precondicoes": "pre_condicoes",
    "step action": "acao", "acao": "acao", "ação": "acao",
    "step expected": "esperado", "resultado esperado": "esperado", "esperado": "esperado",
    "requisitos relacionados": "requisitos", "requisitos": "requisitos",
    "suite": "suite", "suíte": "suite",
    "plan": "plano", "plano": "plano",
    "area path": "area_path", "área": "area_path",
    "assigned to": "atribuido", "state": "estado",
    "work item": "work_item", "work item id": "work_item", "id do work item": "work_item",
}


def _chave(cabecalho: str) -> str:
    limpo = (cabecalho or "").strip().lower().lstrip("*").strip()
    return _SINONIMOS.get(limpo, limpo)


def _lista_requisitos(valor: str) -> list:
    return [p.strip() for p in re.split(r"[;,]", valor or "") if p.strip()]


def importar_planos_csv(nome_arquivo: str, conteudo: bytes) -> dict:
    """
    Devolve {"test_cases", "test_plans", "ambiente", "projeto", "avisos", "erro"}.

    test_cases: [{titulo, pre_condicoes, passos:[{numero,acao,resultado_esperado}],
                  requisitos_relacionados:[], work_item_relacionado}]
    test_plans: [{nome, suites:[{nome, casos:[titulo, ...]}]}] — na ordem do arquivo.
    ambiente:   "Homologação" | "Produção" | "" — deduzido do prefixo dos títulos,
                pra que o envio volte a numerar os casos do mesmo jeito.
    projeto:    o que estava na coluna "Area Path" — o exportador grava ali o
                nome do projeto, que o envio usa pra sugerir o nome do Test Plan.
    """
    vazio = {"test_cases": [], "test_plans": [], "ambiente": "", "projeto": "", "avisos": [], "erro": ""}
    try:
        linhas = ler_arquivo(nome_arquivo, conteudo)
    except Exception as error:
        return {**vazio, "erro": f"Não consegui ler o arquivo: {error}"}
    if not linhas:
        return {**vazio, "erro": "O arquivo está vazio (nenhuma linha depois do cabeçalho)."}

    campos = {_chave(c) for c in linhas[0].keys()}
    faltando = [rotulo for chave, rotulo in (("titulo", "Title"), ("acao", "Step Action")) if chave not in campos]
    if faltando:
        return {**vazio, "erro": ("Este arquivo não parece o CSV de Planos do app — não encontrei a(s) coluna(s): "
                                  + ", ".join(faltando) + ". Esperado o cabeçalho de QA_Plans_*.csv "
                                  "(ID, Work Item Type, Title, Test Step, Pre condicoes, Step Action, ...).")}

    avisos, casos, ordem_planos, ambientes, projetos = [], [], {}, set(), []
    atual = None
    for numero_linha, bruta in enumerate(linhas, start=2):   # 2 = primeira linha depois do cabeçalho
        linha = {_chave(k): (v or "").strip() for k, v in bruta.items()}
        titulo_bruto, passo = linha.get("titulo", ""), linha.get("passo", "")

        if titulo_bruto:
            prefixo = _RE_PREFIXO_CT.match(titulo_bruto)
            if prefixo and prefixo.group(1):
                ambientes.add(prefixo.group(1).upper())
            titulo = _RE_PREFIXO_CT.sub("", titulo_bruto).strip() or titulo_bruto
            atual = {
                "titulo": titulo,
                "pre_condicoes": linha.get("pre_condicoes", ""),
                "passos": [],
                "requisitos_relacionados": _lista_requisitos(linha.get("requisitos", "")),
                "work_item_relacionado": linha.get("work_item", ""),
            }
            casos.append(atual)
            if linha.get("area_path") and linha["area_path"] not in projetos:
                projetos.append(linha["area_path"])
            plano = linha.get("plano", "") or "Plano de Teste"
            suite = linha.get("suite", "") or "Casos de Teste"
            ordem_planos.setdefault(plano, {}).setdefault(suite, []).append(titulo)
            continue

        if passo or linha.get("acao") or linha.get("esperado"):
            if atual is None:
                avisos.append(f"Linha {numero_linha}: passo sem nenhum caso de teste antes dele — ignorado.")
                continue
            atual["passos"].append({
                "numero": len(atual["passos"]) + 1,
                "acao": linha.get("acao", ""),
                "resultado_esperado": linha.get("esperado", ""),
            })

    sem_passos = [c["titulo"] for c in casos if not c["passos"]]
    if sem_passos:
        avisos.append(f"{len(sem_passos)} caso(s) sem nenhum passo: " + "; ".join(sem_passos[:3])
                      + (" ..." if len(sem_passos) > 3 else ""))
    vistos, repetidos = set(), []
    for caso in casos:
        if caso["titulo"] in vistos:
            repetidos.append(caso["titulo"])
        vistos.add(caso["titulo"])
    if repetidos:
        avisos.append(f"{len(repetidos)} título(s) repetido(s) no arquivo: " + "; ".join(sorted(set(repetidos))[:3]))

    if not casos:
        return {**vazio, "avisos": avisos, "erro": "Nenhum Caso de Teste encontrado no arquivo."}

    test_plans = [{"nome": plano, "suites": [{"nome": suite, "casos": titulos} for suite, titulos in suites.items()]}
                  for plano, suites in ordem_planos.items()]
    ambiente = ""
    if ambientes == {"HML"}:
        ambiente = "Homologação"
    elif ambientes == {"PROD"}:
        ambiente = "Produção"
    elif len(ambientes) > 1:
        avisos.append("O arquivo mistura casos de HML e PROD no prefixo dos títulos — os casos serão renumerados "
                      "sem sigla de ambiente.")
    return {"test_cases": casos, "test_plans": test_plans, "ambiente": ambiente,
            "projeto": projetos[0] if projetos else "", "avisos": avisos, "erro": ""}


def resumo_importacao(resultado: dict) -> str:
    """Uma linha com o que veio no arquivo — pra mostrar antes de enviar pro Azure."""
    casos = resultado.get("test_cases") or []
    planos = resultado.get("test_plans") or []
    suites = sum(len(p.get("suites") or []) for p in planos)
    passos = sum(len(c.get("passos") or []) for c in casos)
    com_wi = sum(1 for c in casos if str(c.get("work_item_relacionado") or "").strip())
    partes = [f"{len(planos)} plano(s)", f"{suites} suíte(s)", f"{len(casos)} caso(s)", f"{passos} passo(s)"]
    if com_wi:
        partes.append(f"{com_wi} já com Work Item indicado")
    return " · ".join(partes)
