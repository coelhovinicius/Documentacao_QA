"""
Criação de Work Items em lote: modelo de planilha (gerado a partir do
projeto), leitura de CSV/XLSX/TXT, validação linha a linha e ordenação
pra criar pais antes dos filhos (referência "#Ref" entre linhas).

Nada aqui fala com o Azure DevOps — recebe os metadados já carregados
(tipos, catálogo de campos, campos por tipo, area paths, iterations,
pessoas) e devolve estruturas prontas pra tela criar.
"""
import csv
import io
import re
import unicodedata

# Colunas base do modelo (na ordem em que aparecem). "chave" é o nome
# interno; "titulo" é o cabeçalho na planilha; sinônimos aceitos na leitura.
COLUNAS_BASE = [
    {"chave": "ref", "titulo": "Ref", "obrigatoria": False,
     "descricao": "Apelido da linha (ex.: US1, T1) — só serve pra outra linha apontar pra esta como pai (coluna Pai = #US1).",
     "sinonimos": ["ref", "referencia", "id local", "apelido"]},
    {"chave": "tipo", "titulo": "Tipo", "obrigatoria": True,
     "descricao": "Tipo do Work Item exatamente como existe no projeto (aba Listas).",
     "sinonimos": ["tipo", "work item type", "type", "tipo de work item"]},
    {"chave": "titulo", "titulo": "Título", "obrigatoria": True,
     "descricao": "Título do item.", "sinonimos": ["titulo", "title", "nome"]},
    {"chave": "descricao", "titulo": "Descrição", "obrigatoria": False,
     "descricao": "Texto livre; quebras de linha são preservadas.", "sinonimos": ["descricao", "description", "desc"]},
    {"chave": "area_path", "titulo": "Area Path", "obrigatoria": False,
     "descricao": "Caminho exato (aba Listas). Vazio = raiz do projeto.", "sinonimos": ["area path", "area", "areapath", "area_path"]},
    {"chave": "iteration", "titulo": "Iteration", "obrigatoria": False,
     "descricao": "Sprint/iteração exata (aba Listas). Vazio = raiz do projeto.", "sinonimos": ["iteration", "iteration path", "sprint", "iteracao"]},
    {"chave": "tags", "titulo": "Tags", "obrigatoria": False,
     "descricao": "Separadas por ponto e vírgula (ex.: backend; login). Tags novas são criadas.", "sinonimos": ["tags", "tag", "etiquetas"]},
    {"chave": "atribuido_a", "titulo": "Atribuído a (e-mail)", "obrigatoria": False,
     "descricao": "E-mail da pessoa no Azure DevOps (aba Listas).", "sinonimos": ["atribuido a", "atribuido a (e-mail)", "assigned to", "responsavel", "atribuido"]},
    {"chave": "pai", "titulo": "Pai", "obrigatoria": False,
     "descricao": "ID de um item já existente (ex.: 7040) OU #Ref de outra linha deste arquivo (ex.: #US1) — o pai é criado antes.",
     "sinonimos": ["pai", "parent", "parent id", "id do pai", "pai (id ou #ref)"]},
    {"chave": "estado", "titulo": "Estado", "obrigatoria": False,
     "descricao": "Estado inicial (ex.: New, Active). Vazio = padrão do tipo. Alguns processos só aceitam mudança após criar.",
     "sinonimos": ["estado", "state", "status"]},
]

_CHAVES_BASE = {c["chave"] for c in COLUNAS_BASE}


def _norm(texto) -> str:
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().lower().strip()
    return re.sub(r"\s+", " ", t)


def campos_extras_por_tipo(tipos: list, campos_por_tipo: dict, catalogo: dict, campos_widget_proprio: set) -> dict:
    """
    {nome_do_tipo: [campo_dict]} — campos obrigatórios do tipo que NÃO
    são cobertos pelas colunas base (mesma regra da tela: ignora somente-
    leitura). Esses viram colunas extras no modelo, com o nome do campo.
    """
    saida = {}
    for t in tipos:
        nome = t["name"] if isinstance(t, dict) else str(t)
        extras = []
        for c in (campos_por_tipo.get(nome) or []):
            ref = c["reference_name"]
            if not c.get("always_required") or ref in campos_widget_proprio:
                continue
            meta = catalogo.get(ref) or {}
            # Mesma regra do formulário: somente-leitura não entra; booleano
            # obrigatório também não (o Azure aplica o padrão sozinho).
            if meta.get("read_only") or meta.get("type") == "boolean":
                continue
            extras.append(c)
        saida[nome] = extras
    return saida


def colunas_do_modelo(tipos: list, extras_por_tipo: dict) -> list:
    """Cabeçalhos do modelo: base + campos extras (sem repetir), com metadados."""
    cols = [dict(c) for c in COLUNAS_BASE]
    vistos = set()
    for nome_tipo in [t["name"] if isinstance(t, dict) else str(t) for t in tipos]:
        for c in extras_por_tipo.get(nome_tipo) or []:
            if c["reference_name"] in vistos:
                continue
            vistos.add(c["reference_name"])
            tipos_que_exigem = [n for n, lst in extras_por_tipo.items() if any(x["reference_name"] == c["reference_name"] for x in lst)]
            cols.append({
                "chave": c["reference_name"], "titulo": c["name"], "obrigatoria": False,
                "descricao": f"Obrigatório para: {', '.join(tipos_que_exigem)}."
                             + (f" Valores aceitos: {', '.join(map(str, c['allowed_values']))}." if c.get("allowed_values") else "")
                             + (f" Vazio = padrão '{c['default_value']}'." if c.get("default_value") not in (None, "") else "")
                             + (f" {c['help_text']}" if c.get("help_text") else ""),
                "sinonimos": [_norm(c["name"]), _norm(c["reference_name"])],
                "extra": True, "reference_name": c["reference_name"],
            })
    return cols


# ---------------------------------------------------------------- modelo
def gerar_modelo_xlsx(project: str, tipos: list, extras_por_tipo: dict, area_paths: list,
                      iterations: list, pessoas: list, exemplo_tipo: str = None) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    cols = colunas_do_modelo(tipos, extras_por_tipo)
    nomes_tipos = [t["name"] if isinstance(t, dict) else str(t) for t in tipos]
    wb = Workbook()
    ws = wb.active
    ws.title = "Work Items"
    laranja = PatternFill("solid", fgColor="F15A24")
    cinza = PatternFill("solid", fgColor="F5F5F5")
    for i, c in enumerate(cols, start=1):
        cel = ws.cell(row=1, column=i, value=c["titulo"] + (" *" if c["obrigatoria"] else ""))
        cel.font = Font(bold=True, color="FFFFFF")
        cel.fill = laranja
        cel.alignment = Alignment(vertical="center")
        ws.column_dimensions[get_column_letter(i)].width = max(14, min(45, len(c["titulo"]) + 8))
    # linhas de exemplo: uma User Story (ou 1º tipo) e uma Task filha via #Ref
    tipo_ex = exemplo_tipo or next((n for n in nomes_tipos if "story" in n.lower()), nomes_tipos[0] if nomes_tipos else "User Story")
    tipo_filho = next((n for n in nomes_tipos if n.lower() == "task"), None)
    area_ex = area_paths[0] if area_paths else project
    pessoa_ex = (pessoas[0].get("unique_name") if pessoas else "") or ""
    exemplos = [
        {"ref": "US1", "tipo": tipo_ex, "titulo": "Login com e-mail e senha", "descricao": "Como usuário quero acessar o sistema com e-mail e senha.",
         "area_path": area_ex, "iteration": iterations[0] if iterations else "", "tags": "login; autenticacao", "atribuido_a": pessoa_ex, "pai": "", "estado": ""},
    ]
    if tipo_filho:
        exemplos.append({"ref": "T1", "tipo": tipo_filho, "titulo": "Implementar endpoint POST /auth/login", "descricao": "",
                         "area_path": area_ex, "iteration": "", "tags": "backend", "atribuido_a": pessoa_ex, "pai": "#US1", "estado": ""})
    def _valor_exemplo_extra(col, tipo_linha):
        if not col.get("extra"):
            return ""
        campo = next((x for x in (extras_por_tipo.get(tipo_linha) or []) if x["reference_name"] == col["reference_name"]), None)
        if campo is None:
            return ""
        if campo.get("default_value") not in (None, ""):
            return campo["default_value"]
        return campo["allowed_values"][0] if campo.get("allowed_values") else ""

    for r, ex in enumerate(exemplos, start=2):
        for i, c in enumerate(cols, start=1):
            valor = ex.get(c["chave"], "") if not c.get("extra") else _valor_exemplo_extra(c, ex.get("tipo"))
            cel = ws.cell(row=r, column=i, value=valor)
            cel.fill = cinza
    ws.freeze_panes = "A2"

    # Instruções
    wi = wb.create_sheet("Instruções")
    wi.append(["Coluna", "Obrigatória?", "O que preencher"])
    for cel in wi[1]:
        cel.font = Font(bold=True, color="FFFFFF"); cel.fill = laranja
    for c in cols:
        wi.append([c["titulo"], "Sim" if c["obrigatoria"] else "Não", c["descricao"]])
    wi.append([])
    wi.append(["Regras gerais", "", ""])
    for regra in [
        "Uma linha por Work Item. As linhas de exemplo (cinza) podem ser apagadas ou editadas.",
        "Tipo, Area Path, Iteration e e-mails devem existir no projeto — copie da aba Listas.",
        "Pai: número do item já existente no Azure DevOps, ou #Ref de outra linha deste arquivo (o app cria o pai antes do filho).",
        "Campos obrigatórios de cada tipo aparecem como colunas extras; preencha só nas linhas daquele tipo.",
        "Ao subir o arquivo, o app valida cada linha e mostra o que corrigir antes de enviar. Nada é criado sem confirmação.",
        "Todo item criado recebe a tag criado-por:<seu usuário>.",
    ]:
        wi.append(["", "", regra])
    wi.column_dimensions["A"].width = 28; wi.column_dimensions["B"].width = 14; wi.column_dimensions["C"].width = 110

    # Listas
    wl = wb.create_sheet("Listas")
    wl.append(["Tipos de Work Item", "Area Paths", "Iterations", "Pessoas (e-mail)"])
    for cel in wl[1]:
        cel.font = Font(bold=True, color="FFFFFF"); cel.fill = laranja
    emails = [p.get("unique_name") for p in pessoas if p.get("unique_name")]
    n = max(len(nomes_tipos), len(area_paths), len(iterations), len(emails), 1)
    for i in range(n):
        wl.append([
            nomes_tipos[i] if i < len(nomes_tipos) else "",
            area_paths[i] if i < len(area_paths) else "",
            iterations[i] if i < len(iterations) else "",
            emails[i] if i < len(emails) else "",
        ])
    for col, w in zip("ABCD", (28, 50, 40, 45)):
        wl.column_dimensions[col].width = w

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def gerar_modelo_csv(tipos: list, extras_por_tipo: dict, project: str) -> str:
    cols = colunas_do_modelo(tipos, extras_por_tipo)
    nomes_tipos = [t["name"] if isinstance(t, dict) else str(t) for t in tipos]
    tipo_ex = next((n for n in nomes_tipos if "story" in n.lower()), nomes_tipos[0] if nomes_tipos else "User Story")
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow([c["titulo"] + (" *" if c["obrigatoria"] else "") for c in cols])
    linha = {"ref": "US1", "tipo": tipo_ex, "titulo": "Login com e-mail e senha", "descricao": "Como usuário quero acessar o sistema.",
             "area_path": project, "iteration": "", "tags": "login", "atribuido_a": "", "pai": "", "estado": ""}
    def _ex(c):
        if not c.get("extra"):
            return linha.get(c["chave"], "")
        campo = next((x for x in (extras_por_tipo.get(tipo_ex) or []) if x["reference_name"] == c["reference_name"]), None)
        if campo is None:
            return ""
        return campo["default_value"] if campo.get("default_value") not in (None, "") else (campo["allowed_values"][0] if campo.get("allowed_values") else "")
    w.writerow([_ex(c) for c in cols])
    return "﻿" + buf.getvalue()   # BOM: Excel abre com acentos corretos


# ---------------------------------------------------------------- leitura
def ler_arquivo(nome: str, conteudo: bytes) -> list:
    """
    Lê CSV (; ou , ou tab), TXT (tab ou ;) ou XLSX (1ª aba) e devolve
    [{cabecalho: valor}], sem linhas totalmente vazias.
    """
    nome_l = (nome or "").lower()
    linhas = []
    if nome_l.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True)
        ws = wb["Work Items"] if "Work Items" in wb.sheetnames else wb[wb.sheetnames[0]]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        cab = [str(c or "").strip() for c in rows[0]]
        for r in rows[1:]:
            d = {cab[i]: ("" if v is None else str(v).strip()) for i, v in enumerate(r) if i < len(cab) and cab[i]}
            if any(v for v in d.values()):
                linhas.append(d)
        return linhas
    texto = conteudo.decode("utf-8-sig", errors="replace")
    amostra = texto[:4096]
    if nome_l.endswith(".txt"):
        sep = "\t" if "\t" in amostra else (";" if ";" in amostra else ",")
    else:
        try:
            sep = csv.Sniffer().sniff(amostra, delimiters=";,\t|").delimiter
        except Exception:
            sep = ";" if amostra.count(";") >= amostra.count(",") else ","
    reader = csv.DictReader(io.StringIO(texto), delimiter=sep)
    for r in reader:
        d = {str(k or "").strip(): (v or "").strip() for k, v in r.items() if k is not None}
        if any(v for v in d.values()):
            linhas.append(d)
    return linhas


def mapear_colunas(cabecalhos: list, cols_modelo: list) -> dict:
    """{cabecalho_do_arquivo: chave_interna} usando títulos/sinônimos (sem acento, sem '*')."""
    mapa = {}
    for cab in cabecalhos:
        n = _norm(cab).replace("*", "").strip()
        for c in cols_modelo:
            candidatos = {_norm(c["titulo"])} | {_norm(s) for s in c.get("sinonimos", [])}
            if c.get("reference_name"):
                candidatos.add(_norm(c["reference_name"]))
            if n in candidatos:
                mapa[cab] = c["chave"]
                break
    return mapa


# ---------------------------------------------------------------- validação
def validar_linhas(linhas: list, cols_modelo: list, tipos: list, extras_por_tipo: dict, catalogo: dict,
                   area_paths: list, iterations: list, pessoas: list) -> list:
    """
    Devolve [{"linha": n, "ref", "tipo", "titulo", "campos": {ref_name: valor}, "parent_id", "parent_ref",
              "state", "erros": [..], "avisos": [..]}] — um por linha do arquivo.
    """
    nomes_tipos = {_norm(t["name"] if isinstance(t, dict) else str(t)): (t["name"] if isinstance(t, dict) else str(t)) for t in tipos}
    areas = {_norm(a): a for a in (area_paths or [])}
    iters = {_norm(i): i for i in (iterations or [])}
    emails = {_norm(p.get("unique_name")): p.get("unique_name") for p in (pessoas or []) if p.get("unique_name")}
    nomes_pessoas = {_norm(p.get("display_name")): p.get("unique_name") for p in (pessoas or []) if p.get("display_name")}
    if not linhas:
        return []
    mapa = mapear_colunas(list(linhas[0].keys()), cols_modelo)
    colunas_presentes = set(mapa.values())
    lista_tipos = ", ".join(nomes_tipos.values())
    resultado = []
    refs_vistas = {}
    for n, raw in enumerate(linhas, start=2):   # 2 = primeira linha de dados na planilha
        d = {mapa[k]: v for k, v in raw.items() if k in mapa}
        item = {"linha": n, "ref": (d.get("ref") or "").strip(), "tipo": "", "titulo": (d.get("titulo") or "").strip(),
                "campos": {}, "parent_id": None, "parent_ref": None, "state": None, "erros": [], "avisos": []}
        # tipo
        tipo_raw = (d.get("tipo") or "").strip()
        if not tipo_raw:
            item["erros"].append(f"Coluna 'Tipo' vazia — preencha com um destes: {lista_tipos}.")
        elif _norm(tipo_raw) not in nomes_tipos:
            item["erros"].append(f"Tipo '{tipo_raw}' não existe no projeto — use um destes: {lista_tipos}.")
        else:
            item["tipo"] = nomes_tipos[_norm(tipo_raw)]
        # título
        if not item["titulo"]:
            item["erros"].append("Coluna 'Título' vazia — escreva o título do item.")
        else:
            item["campos"]["System.Title"] = item["titulo"]
        # descrição
        if (d.get("descricao") or "").strip():
            desc = d["descricao"].strip()
            tipo_desc = (catalogo.get("System.Description") or {}).get("type", "html")
            item["campos"]["System.Description"] = desc.replace("\n", "<br>") if tipo_desc == "html" else desc
        # area / iteration
        ap = (d.get("area_path") or "").strip()
        if ap:
            if _norm(ap) in areas:
                item["campos"]["System.AreaPath"] = areas[_norm(ap)]
            else:
                item["erros"].append(f"Area Path '{ap}' não existe no projeto (veja a aba Listas do modelo).")
        it = (d.get("iteration") or "").strip()
        if it:
            if _norm(it) in iters:
                item["campos"]["System.IterationPath"] = iters[_norm(it)]
            else:
                item["erros"].append(f"Iteration '{it}' não existe no projeto.")
        # tags
        tags = [t.strip() for t in re.split(r"[;,]", d.get("tags") or "") if t.strip()]
        if tags:
            item["campos"]["System.Tags"] = "; ".join(tags)
        # atribuído
        quem = (d.get("atribuido_a") or "").strip()
        if quem:
            if _norm(quem) in emails:
                item["campos"]["System.AssignedTo"] = emails[_norm(quem)]
            elif _norm(quem) in nomes_pessoas:
                item["campos"]["System.AssignedTo"] = nomes_pessoas[_norm(quem)]
            else:
                item["erros"].append(f"Pessoa '{quem}' não encontrada no projeto (use o e-mail da aba Listas).")
        # pai
        pai = (d.get("pai") or "").strip()
        if pai:
            if pai.startswith("#"):
                item["parent_ref"] = pai[1:].strip()
            elif re.fullmatch(r"\d+", pai):
                item["parent_id"] = int(pai)
            else:
                item["erros"].append(f"Pai '{pai}' inválido — use o ID numérico ou #Ref de outra linha.")
        # estado
        if (d.get("estado") or "").strip():
            item["state"] = d["estado"].strip()
        # campos extras obrigatórios do tipo
        for c in (extras_por_tipo.get(item["tipo"]) or []):
            valor = (d.get(c["reference_name"]) or "").strip()
            if not valor and c.get("default_value") not in (None, ""):
                valor = str(c["default_value"])
                item["avisos"].append(f"'{c['name']}' vazio — usado o padrão '{valor}'.")
            if not valor:
                aceitos = f" Valores aceitos: {', '.join(map(str, c['allowed_values']))}." if c.get("allowed_values") else ""
                if c["reference_name"] not in colunas_presentes:
                    item["erros"].append(f"Falta a coluna '{c['name']}' no arquivo (obrigatória para {item['tipo']}) — "
                                         f"baixe o modelo atualizado ou acrescente a coluna.{aceitos}")
                else:
                    item["erros"].append(f"Coluna '{c['name']}' vazia (obrigatória para {item['tipo']}) — preencha.{aceitos}")
                continue
            permitidos = c.get("allowed_values") or []
            if permitidos and valor not in [str(p) for p in permitidos]:
                achou = next((str(p) for p in permitidos if _norm(p) == _norm(valor)), None)
                if achou:
                    valor = achou
                else:
                    item["erros"].append(f"'{c['name']}' = '{valor}' não está entre os valores aceitos ({', '.join(map(str, permitidos))}).")
                    continue
            tipo_campo = (catalogo.get(c["reference_name"]) or {}).get("type", "string")
            if tipo_campo in ("integer", "double"):
                try:
                    valor = int(valor) if tipo_campo == "integer" else float(str(valor).replace(",", "."))
                except ValueError:
                    item["erros"].append(f"'{c['name']}' deve ser numérico.")
                    continue
            item["campos"][c["reference_name"]] = valor
        # refs duplicadas
        if item["ref"]:
            if item["ref"] in refs_vistas:
                item["erros"].append(f"Ref '{item['ref']}' repetida (linha {refs_vistas[item['ref']]}).")
            refs_vistas[item["ref"]] = n
        resultado.append(item)
    # pai por #Ref precisa existir no arquivo e não pode ser cíclico
    por_ref = {r["ref"]: r for r in resultado if r["ref"]}
    for r in resultado:
        if r["parent_ref"] and r["parent_ref"] not in por_ref:
            r["erros"].append(f"Pai '#{r['parent_ref']}' não corresponde a nenhuma Ref deste arquivo.")
        elif r["parent_ref"] == r["ref"] and r["ref"]:
            r["erros"].append("Uma linha não pode ser pai dela mesma.")
    return resultado


def problemas_do_arquivo(cabecalhos: list, cols_modelo: list, validadas: list, extras_por_tipo: dict) -> list:
    """
    Problemas do arquivo como um todo (não de uma linha): colunas obrigatórias ausentes e colunas
    de campos obrigatórios que faltam pros tipos usados. Devolve mensagens prontas pra mostrar.
    """
    mapa = mapear_colunas(cabecalhos, cols_modelo)
    presentes = set(mapa.values())
    msgs = []
    for c in cols_modelo:
        if c.get("obrigatoria") and c["chave"] not in presentes:
            msgs.append(f"O arquivo não tem a coluna **{c['titulo']}** (obrigatória). Use o modelo baixado nesta tela.")
    tipos_usados = sorted({v["tipo"] for v in validadas if v.get("tipo")})
    faltando = {}
    for t in tipos_usados:
        for c in extras_por_tipo.get(t) or []:
            if c["reference_name"] not in presentes and c.get("default_value") in (None, ""):
                faltando.setdefault(c["name"], []).append(t)
    for nome, tipos_ in faltando.items():
        msgs.append(f"Falta a coluna **{nome}** — obrigatória para {', '.join(tipos_)}. "
                    "Baixe o modelo atualizado (ele já traz essa coluna) ou acrescente-a ao seu arquivo.")
    return msgs


def resumo_pendencias(validadas: list) -> list:
    """Agrupa os erros iguais entre linhas: [{"mensagem", "linhas": [2, 5, ...]}] — pra mostrar 'o que falta' de uma vez."""
    grupos = {}
    for v in validadas:
        for e in v.get("erros") or []:
            grupos.setdefault(e, []).append(v["linha"])
    return [{"mensagem": m, "linhas": ls} for m, ls in grupos.items()]


def ordenar_para_criacao(itens: list) -> list:
    """
    Ordena pra que todo item cujo pai é '#Ref' venha DEPOIS do pai.
    Ciclos ou refs ausentes ficam por último (e serão reportados na criação).
    """
    por_ref = {i["ref"]: i for i in itens if i.get("ref")}
    ordenados, visitando, feitos = [], set(), set()

    def visitar(item):
        chave = id(item)
        if chave in feitos:
            return
        if chave in visitando:
            return   # ciclo — deixa pra criação apontar o erro
        visitando.add(chave)
        pai = por_ref.get(item.get("parent_ref")) if item.get("parent_ref") else None
        if pai is not None:
            visitar(pai)
        visitando.discard(chave)
        feitos.add(chave)
        ordenados.append(item)

    for i in itens:
        visitar(i)
    return ordenados
