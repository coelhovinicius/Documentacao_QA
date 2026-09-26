# 🧪 QA Automation – Azure DevOps

Aplicação Streamlit que automatiza a geração de documentação de QA (Matriz de Cobertura, Casos de Teste e Planos de Teste) a partir de documentos de requisitos ou de Work Items do Azure DevOps, usando IA (via n8n), e integra tudo diretamente com o Azure DevOps — com controle de acesso, PAT pessoal por usuário, três modos de envio conforme o estágio do projeto, e relatórios de execução baseados em dados reais do board.

---

## Sumário

- [Visão geral](#visão-geral)
- [Arquitetura](#arquitetura)
- [Funcionalidades](#funcionalidades)
  - [O assistente de 7 passos](#o-assistente-de-7-passos)
  - [Passo 7 — os 3 modos de envio ao Azure DevOps](#passo-7--os-3-modos-de-envio-ao-azure-devops)
  - [Recursos adicionais (sidebar)](#recursos-adicionais-sidebar)
  - [Controle de acesso e governança](#controle-de-acesso-e-governança)
- [Stack técnica](#stack-técnica)
- [Estrutura do projeto](#estrutura-do-projeto)
- [Configuração](#configuração)
- [Como rodar localmente](#como-rodar-localmente)
- [Deploy](#deploy)
- [Conceitos importantes](#conceitos-importantes)
- [Limitações conhecidas](#limitações-conhecidas)
- [Manutenção](#manutenção)

---

## Visão geral

O app resolve um problema recorrente de QA: transformar uma especificação (documento, ou Work Items já existentes no Azure DevOps) em documentação de teste estruturada — Matriz de Cobertura, Casos de Teste e Planos de Teste — e publicar tudo isso diretamente no Azure DevOps, mantendo rastreabilidade, evitando duplicidade, e registrando quem fez o quê.

A geração de conteúdo usa IA (até 5 provedores em cadeia de fallback, dependendo do workflow) orquestrada via **n8n**, que é o único lugar onde as chaves de API de IA ficam configuradas — o app Streamlit nunca lida com essas chaves diretamente, só troca dados com o n8n por webhook.

---

## Arquitetura

```
 Usuário (Time de QA)
        │
        ▼
 Login (aprovação + sessão via ID opaco na URL)
        │
        ▼
 App QA Automation (Streamlit) — PAT pessoal de cada um
        │
        ▼
 n8n  (IA + Controle de Acesso/Logs/Sessões)
        │
        ▼
 Azure DevOps (Board, Test Plans, Queries)
```

- O app nunca fala direto com nenhum provedor de IA — tudo passa pelo n8n.
- O n8n guarda **controle de acesso** (aprovadores, permissões, logs de auditoria, sessões) usando *workflow static data* — sem banco de dados separado.
- A sessão do usuário é um **ID aleatório opaco** na URL — o dado real (quem é, quando expira) fica no n8n, revogável remotamente a qualquer momento.
- A integração com o **Azure DevOps** é feita direto do app, usando o **PAT pessoal** de quem está logado.

---

## Funcionalidades

### O assistente de 7 passos

| Passo | O que faz |
|---|---|
| **1. Upload** | Envio de documento(s), geração a partir de Work Items existentes, **ou** a partir de uma query já salva no Azure DevOps. Exige escolher **Ambiente** (Homologação/Produção) **e Tipo de Documento** (Visão / Requisitos Funcionais / Especificações Funcionais / Outros) — o tipo calibra o nível de detalhe que a IA assume ao gerar Matriz/Casos, e sugere o modo de envio do Passo 7. Extrai imagens do corpo do documento e interpreta cada uma via IA. |
| **2. Dúvidas** | A IA faz até 7 perguntas de esclarecimento sobre a especificação. |
| **3. Matriz** | Gera a Matriz de Cobertura (MC-001...), com etiqueta de Ambiente (`MC-001 HML`/`PROD`). Editável. |
| **4. Casos** | Gera Casos de Teste a partir da Matriz. Editável. |
| **5. Planos** | Organiza os Casos em Planos → Suítes. Editável. |
| **6. Download** | Exporta CSV e PDF "Documentação QA". |
| **7. Azure DevOps** | Três modos de envio — ver seção abaixo. |

### Passo 1 — as 3 formas de fornecer a especificação

**📄 Enviar Documento(s)** — PDF, DOCX ou TXT. O texto (e imagens, interpretadas via IA) viram a especificação de entrada.

**🎯 Gerar a partir de Work Items** — usa Descrição + Critérios de Aceite de Work Items existentes como especificação, em vez de um documento. Varre o board por Area Path (opcional — vazio considera o projeto inteiro), lista os Work Items encontrados, e a pessoa escolhe quais entram. Com uma Area Path específica escolhida, aparecem filtros opcionais de **Coluna do Board** e/ou **Tag**, derivados dos itens realmente encontrados. Disponível pra qualquer pessoa logada, usa o PAT pessoal.

**🔎 Gerar a partir de uma Query** *(permissão `azure_query`)* — em vez de varrer o board, parte de uma query **já salva** no Azure DevOps (My Queries ou Shared Queries). Roda a query, traz os Work Items que ela retorna, e a pessoa escolhe quais entram — mesma tela de inclusão/exclusão do modo anterior (sem filtro de Coluna/Tag aqui — query já é escopada por Projeto, não por Area Path). Dali em diante, o fluxo é idêntico (Nome do Test Plan, Ambiente, documentos complementares, Tipo de Documento).

### Passo 7 — os 3 modos de envio ao Azure DevOps

Escolhidos na tela, com sugestão automática baseada no Tipo de Documento do Passo 1 (sempre trocável manualmente):

**🔗 Vincular a Work Items** — o fluxo clássico, pra quando os Work Items já existem no board. A IA sugere quais Casos de Teste se relacionam a quais Work Items; a pessoa revisa e ajusta antes de confirmar. Cria Test Cases, cria ou **reaproveita** um Test Plan existente (sem duplicar Suítes já existentes), e vincula tudo via Requirement-based Suites. Com uma Area Path específica escolhida, também aparecem os filtros opcionais de Coluna do Board e/ou Tag.

**📋 Sem Work Items** — pra projetos no início, quando só existe um Documento de Visão (e no máximo um Épico/Backlog genérico no board). Usa os Planos/Suítes/Casos que o próprio Passo 5 gerou e cria um Test Plan com **Suítes Estáticas**, sem depender de nenhum Work Item.

**🔄 Reconciliar Test Plan Anterior** — pra quando os Work Items forem criados *depois* de um envio "Sem Work Items". Busca os Casos de Teste que já existem no Test Plan antigo, e a IA sugere quais Work Items novos correspondem a quais Casos já criados — sem duplicar nenhum Caso, só cria o vínculo e a Requirement Suite.

**Regras que valem nos 3 modos:**
- **Responsável (Assigned To) dos Test Cases**: escolhido no Passo 7 ("👤 Atribuir os Test Cases a"), pré-selecionando a pessoa cujo e-mail no Azure DevOps bate com o e-mail cadastrado no app. Sem essa escolha, o Azure DevOps atribui ao *criador* — que, no modo de PAT compartilhado, é sempre o dono do PAT. A tag `criado-por:<usuário>` é calculada antes das threads de criação (antes saía `criado-por:desconhecido`).
- Um Caso de Teste só pode ficar vinculado a **um** Work Item por vez — uma vez escolhido em algum, some das opções dos demais.
- Antes de qualquer chamada real à API do Azure DevOps, o app sempre mostra uma **lista detalhada** (quais Casos serão criados, quais Suítes/Work Items serão afetados) num modal de confirmação.
- **Regra do app: todo relatório em PDF pode ser baixado antes da integração com o Azure DevOps** — o PDF "Documentação QA" no Passo 6 (antes do Passo 7), o relatório dos Testes de API na etapa 3 (antes de enviar os Bugs) e na etapa 4 (antes de "Levar para o assistente"). Relatório de Testes, Manual e Mapa Mental não gravam nada no Azure.
- Dá pra excluir Casos específicos do envio (Casos sem nenhum Work Item vinculado já vêm pré-marcados pra exclusão, por padrão).

### Recursos adicionais (sidebar)

Ao escolher uma área (ou trocar de passo), a sidebar se recolhe sozinha e a tela volta ao topo (`_tela_atual` = passo + área aberta; `_force_sidebar_collapsed` só roda quando ela muda, não a cada interação).

- **🔎 Criar Query com IA** *(permissão `azure_devops`)* — descreve em português o que quer consultar no Azure DevOps; a IA traduz pra WIQL, mostra preview real dos resultados antes de **salvar a query no Azure DevOps** (não gera nenhum teste — é o caminho inverso do modo "Gerar a partir de uma Query" do Passo 1, que parte de uma query que você já tem salva). Depois de gerada, dois atalhos pulam a etapa de salvar e já aplicam o resultado direto: **"Usar pra Gerar Testes"** (leva pro Passo 1, modo Query, com os Work Items já buscados) e **"Usar pra Criar Manual"** (mesma coisa, mas pro Manual de Testes) — cada atalho só aparece pra quem também tem a permissão do destino (`azure_query` ou `manual_testes`, respectivamente).
- **📘 Manual de Testes (UAT)** *(permissão `manual_testes`)* — gera um manual de reprodução em linguagem simples, pra times não-técnicos (Produto/Marketing) em UAT. Não tira print ao vivo — só reaproveita imagens já existentes (anexadas em documentos ou já presentes nos Work Items). Origem do conteúdo: Documentos, Work Items do Azure DevOps, ou Mesclado. Quando a origem inclui Work Items, escolhe entre buscar pelo **Board (Area Path)** ou por uma **Query salva** — mesmo padrão do Passo 1.
- **🗄️ Documentos Armazenados** *(permissão `documentos_armazenados`)* — guarda CSVs/PDFs gerados no banco de documentos, organizados por grupo, pra buscar depois sem precisar gerar de novo. Qualquer pessoa com a permissão salva e visualiza; **excluir um grupo é exclusivo do dono do app**, mesmo para quem tem a permissão.
- **🧠 Mapa Mental** *(permissão `mapa_mental`)* — visualização em árvore (Work Item → Suítes → Casos), navegável e com zoom. Exporta em SVG (direto do navegador) ou PDF (gerado no servidor), sempre com tudo expandido no arquivo exportado, independente do que estiver expandido/recolhido na tela.
- **🔌 Testes de API** *(permissão `testes_api`)* — executa testes de API direto do app, em Python puro (sem Node/Newman — funciona no Streamlit Cloud), em 4 etapas (Definição → Execução → **Análise e Bugs** → Evidências).
  - **De onde vêm os casos**: (a) **gerados por IA** — escolha o(s) Work Item(s) do Azure DevOps (Descrição + Critérios de Aceite viram a especificação; mesma conexão PAT/Organização/Projeto dos outros fluxos; filtro por **Tag**/**Coluna do Board** e botão pra marcar todos os filtrados; com mais itens que o tamanho do lote — padrão 2 — a geração vira **uma chamada por lote**, cada lote recebe o resumo dos casos dos anteriores pra reusar `{{auth_token}}` em vez de repetir o login, e no fim os casos repetidos entre lotes são juntados — `infrastructure/api_generation_batches.py`) ou cole o texto / anexe documentos, informe a Base URL, garanta o **🗺️ catálogo de rotas reais** (importado do código do front pela própria Base URL, de um Swagger/OpenAPI, de uma collection do Postman ou colado — a IA só pode usar rotas do catálogo e todo caso fora dele é desabilitado com aviso), clique em **🔎 Reconhecer a API** (sonda as rotas citadas sem credencial e preenche as Observações com a rota real, o formato de erro e as rotas protegidas) e em **Gerar casos com IA**; o painel **prontidão** diz exatamente o que ainda falta pra gerar com precisão, e **🔎 Verificar rotas dos casos** pergunta à API se cada rota existe (404 "route could not be found" = não existe → caso desabilitado). Rotas que respondem nas execuções entram no catálogo sozinhas — o workflow `Doc_QA_ApiTest_Generation` devolve casos, asserções e variáveis prontos; segredos nunca vão pra IA, ela só declara as variáveis; (b) **collection do Postman** (v2.1, environment opcional; os `pm.test` mais comuns viram asserções declarativas, o resto vira aviso no caso); (c) **definição salva** (.json exportado pelo módulo); (d) **criados na tela**.
  - **Variáveis** `{{nome}}` em URL/headers/body; **secretas** (senhas/tokens) pedidas em campo de senha, só na sessão, mascaradas em toda evidência; extração de valores da resposta pra encadear casos (token do login → rota protegida). Nas origens IA/Postman/definição a tabela só aceita preencher o **Valor**; nome/secreto só na criação manual — mas variável digitada num caso entra na tabela sozinha, e a coluna **Usada em** diz quais casos usam cada uma. **Preenchimento automático** (`infrastructure/api_autofill.py`, roda sozinho ao fim de toda geração com IA e pelo botão **🤖 Completar automaticamente** na seção Variáveis): token de outro perfil (ex.: `{{gestor_token}}`) que nenhum caso extrai ganha um caso **Login (gestor)** copiado do login da bateria (mesma rota e caminho do token), antes do primeiro uso — sobra só `gestor_email`/`gestor_password`; dados de teste negativo (token inválido, e-mail inexistente/inválido, senha errada, ID inexistente) recebem valores fixos, não secretos. **Variável sem valor não trava a execução**: só os casos que a usam saem como **Bloqueado** (os dois executores pulam o caso sem mandar a requisição), e a etapa avisa quais. Depois de executar, o quadro **🔎 Variáveis sem valor × respostas desta execução** procura cada ID que faltou nas respostas 2xx reais (`gestor_*` só em caso autenticado como gestor; credenciais e `other_*` nunca) e, com um clique, vira extração automática no caso de origem.
  - **De onde saem as chamadas** (configuração global, só do dono, em Administração → ⚙️ Configurações): **Navegador do usuário** (padrão) — componente HTML próprio (`ui/components/api_browser_runner`) faz as requisições no browser de quem usa o app, porque WAFs como o CloudFront do HML bloqueiam IPs de provedores de nuvem (Streamlit Cloud, n8n) mas aceitam o IP do usuário; exige CORS liberado na API. Ou **Servidor do app** (chamada direta). Nos dois modos as asserções, extração e evidências são avaliadas em Python, pelo mesmo código.
  - **Análise e Bugs** (etapa 3; `infrastructure/api_triage.py` + `ui/api_bugs_page.py`): classifica cada resultado que não passou — **🐞 possível bug da API** (5xx, resposta que expõe detalhe interno como nome de classe/SQL/stack trace, acesso sem credencial aceito, dado inválido aceito, campo sensível na resposta, lentidão > 3 s), **👤 perfil/credencial** (403 com a mensagem de papel exigido; o `/me` sem vínculo de empresa é apontado), **📋 a confirmar com o PO** (status/mensagem divergente — só é "requisito" se o texto do card citar aquele status/mensagem; sem card, na dúvida vai pro PO), **🧰 bateria** (token esquecido, campo com nome diferente do que a API valida, caminho presumido, rota inexistente, sintaxe) e **⏸️ bloqueado** (com a causa-raiz: qual variável e qual caso deveria produzi-la). Cada achado traz **é bug mesmo? / como confirmar / o que fazer / com quem falar / o que dizer** (texto pronto, com o nome do responsável pelo Work Item quando buscado no Azure). **Rascunhos de Bug** com ver/alterar/excluir e criação a partir de qualquer caso; **envio ao Azure DevOps** no padrão do app (conexão, board, coluna, tags, responsável → confirmação com tudo que vai ser criado → overlay de processamento → tela de resultado com link por Bug, consulta WIQL com todos e resumo .md): request/response mascarados de cada caso como anexo `.txt`, `RELATORIO.pdf` anexado, prints da etapa 4 embutidos no System Info, vínculo *Related* aos Work Items testados e *Tested By* aos Test Cases da bateria já criados; **🔄 Verificar no Azure** traz State/coluna/responsável dos Bugs enviados. Nada é excluído no Azure pelo app. **📄 Relatório completo antes de enviar**: na própria etapa 3, gera e baixa o PDF/MD com a execução, a análise (inclusive "o que dizer"), as correções aplicadas e cada Bug com a situação (rascunho ainda não enviado / enviado #id); se rascunhos, correções ou responsáveis mudarem depois, a tela avisa que o PDF está desatualizado.
  - **Correção da bateria pelas respostas reais** (`infrastructure/api_contracts.py`): sugestões só de **formato**, cada uma com a evidência — header `Authorization` esquecido (401 em caso que não testa "sem token"; nunca em login), campo renomeado pro nome que o 422 lista (`startDate` → `start_date`), caminho real do campo na resposta (`data.user.email` → `data.email`, `error.message` → `message`), id extraído de outra resposta 2xx e comparação circular de mensagens trocada por "não vazio". Status esperado e texto de mensagem **nunca** são alterados. Aplicar pede confirmação e marca a análise como "da execução anterior" até executar de novo.
  - **📐 Catálogo de formatos reais** por host (`app_config` chave `api_contratos::<host>`): cada execução aprende campos validados (e valores recusados), campos aceitos, caminhos das respostas 2xx e mensagens de erro por status; o bloco vai junto das rotas nas Observações de toda geração com IA (chamada única e lotes).
  - **Caminhos JSON**: além de `data.campo`/`lista[0]`, o executor (Python e navegador) aceita `lista[-1]`, `lista[*].campo`, `lista[?(@.campo=='x')].campo`, `lista.length`, `['chave']` e `$.`; sintaxe não suportada aparece como tal na asserção, nunca como "campo ausente".
  - **Evidências** (etapa 4): `RELATORIO.md`, `RELATORIO.pdf` (padrão QA TestGen, com a seção **Análise automática**, as correções aplicadas e os Bugs — rascunhos e enviados), `.zip` com uma pasta por caso (`1_request.txt`, `2_response.txt`, `3_resultado.txt` + prints anexados), definição `.json` pra repetir a bateria, e opção de guardar em Documentos Armazenados.
  - **📙 Manual para desenvolvedores** (`docs/Guia_Testes_API.docx/.pdf`, baixável na própria tela): como as chamadas saem (navegador × servidor, CORS, User-Agent, cookies, sondas sem credencial), formato `qa_testgen.api_tests.v1` dos casos, tipos de asserção e regras de comparação, caminhos JSON aceitos, importação do Postman, regras de classificação e de detecção de bug, mascaramento, o que chega num Bug e receitas pra reproduzir uma chamada e comparar com o código — com uma seção inicial sobre quando a ferramenta agrega e quando não precisa.
  - **Azure DevOps (opcional)**: **Levar para o assistente** converte a bateria em Matriz + Casos de Teste + Plano (uma suíte por endpoint, pela rota canônica — id diferente, id inexistente ou query string não criam suíte nova; o passo do Caso mantém a URL concreta; a pré-condição "Última execução" traz o horário real da execução; `infrastructure/api_to_assistant.py`) e entra no Passo 5 — daí o Passo 7 cria tudo no Azure DevOps pelos modos de sempre (o Work Item usado na geração vem como pré-vínculo). Se a sessão já tiver uma análise, a pessoa escolhe **acrescentar** (mesma funcionalidade → mesmo Test Plan) ou **substituir**. O resultado da execução pode ir como texto nos Test Cases e, depois do Passo 7, como **Test Run** oficial (`AzureDevOpsClient.create_test_run`/`update_test_run_results`/`attach_file_to_test_run`): Passed/Failed por caso + PDF de evidências anexado, na aba *Execute* do plano.
- **📊 Relatório de Testes** *(permissão `execution_report`)* — documenta o que foi **executado**. Status calculado pela **coluna do board (Kanban)** de cada Work Item vinculado (não pelo outcome do Test Point), com Status geral escolhido manualmente. Monta uma Matriz de Cobertura independente quando a sessão não tem uma.
- **🐛 Criar Bug** *(permissão `criar_bug`)* — cria um Bug diretamente no Azure DevOps: livremente, ou a partir de um Work Item com Casos de Teste relacionados. Nesse segundo modo, dá pra escolher vincular a um **Caso de Teste específico** (título e Passos de Reprodução já vêm pré-preenchidos, e o Bug fica vinculado de volta ao Caso e ao Work Item) **ou** abrir direto no **Work Item principal**, sem depender de nenhum Caso específico (o Bug fica vinculado só ao Work Item) — útil quando o problema não é de um Caso isolado, mas do item como um todo. Campos extras opcionais: **System Info**, **Acceptance Criteria**, **Discussion**, e evidências em **imagem** (sobem como anexo formal do Bug e ficam também embutidas no System Info).
- **🧱 Criar Work Item** *(permissão `criar_work_item`)* — cria Work Items de **qualquer tipo** que o processo do projeto permita (User Story, Bug, Epic, Feature, Task, Spike, Improvement, UX Story, tipos customizados da organização...), com o formulário montado **dinamicamente a partir dos metadados do próprio projeto** — então campos customizados aparecem sozinhos, sem precisar mexer no código. Projeto é obrigatório; Area Path, Iteration/Sprint e coluna do board são opcionais. Tags: escolhe entre as existentes **e/ou cria novas** (o Azure DevOps cria a tag junto com o item). Dá pra atribuir responsável, anexar **evidências em imagem** (mesmo mecanismo do Criar Bug: sobem como anexo formal e ficam embutidas no fim da Descrição) e preencher qualquer campo editável do tipo (os obrigatórios do tipo aparecem em destaque; o resto fica num expander). Três modos:
  - **📝 Um Work Item** — formulário completo, com vínculo opcional a um Work Item **pai** (Parent/Child).
  - **🧩 Vários filhos de um Work Item** — escolhe a User Story (mostrando Descrição e Critérios de Aceite dela como contexto), lista várias Tasks e cria **todas de uma vez** já vinculadas como filhas — é o fluxo de "quebrar a story em pedaços" que aparece como checklist de filhos dentro do item no Azure DevOps.
  - **📋 Fila / planilha**: monta uma fila com vários Work Items de qualquer tipo — pelo botão **➕ Adicionar à fila** do formulário ou subindo **CSV/XLSX/TXT** — e envia tudo de uma vez, com confirmação. O **modelo de planilha** é gerado a partir do projeto selecionado (tipos, Area Paths, Iterations e pessoas na aba *Listas*; campos obrigatórios de cada tipo como colunas extras; aba *Instruções*). Validação linha a linha antes de entrar na fila; a coluna **Pai** aceita ID existente ou `#Ref` de outra linha (pais criados antes dos filhos); ao subir o arquivo o app mostra quantas linhas estão prontas e, por linha, a coluna que falta e os valores aceitos. Depois do envio, resumo no topo com ID/tipo/título, link direto por item, link de consulta com todos os IDs e download em `.md`; itens que falharem ficam na fila com o motivo. Lógica em `infrastructure/work_item_batch.py`, tela em `ui/work_item_batch_page.py`; XLSX via `openpyxl`.
- **🛡️ Administração** *(dono do app)* — cadastro de usuários (nome, e-mail, senha, modo de acesso, status de aprovador e todas as permissões granulares, tudo num único formulário por pessoa), **Sessões Ativas** (revogação remota), e **Logs de Auditoria**.

### Controle de acesso e governança

- **Login com aprovação**: só o dono entra direto; demais usuários precisam de aprovação a cada sessão.
- **Sessão via ID opaco**: a URL não revela usuário nem senha — o dado fica no n8n, revogável a qualquer momento (a própria sessão, ou a de outra pessoa).
- **PAT do Azure DevOps**: por padrão, cada usuário informa o próprio — nunca salvo em disco, só na memória da sessão. Opcionalmente, o dono pode configurar `AZURE_DEVOPS_PAT` nos Secrets pra usar um PAT compartilhado (ninguém mais digita token; a rastreabilidade de quem fez o quê passa a vir da tag automática `criado-por:<usuário>` em cada Bug/Test Case criado).
- **Permissões granulares cobrindo TODAS as funcionalidades** — inclusive o próprio assistente de QA (Passos 1–6), que **não** é um piso liberado pra quem loga: é a permissão `assistente_qa`. Logo, dá pra ter um usuário que **só** abre Bug (ou só cria Work Item) — ele loga e cai direto naquela área, sem ver o fluxo de documentação. Quem não tem nenhuma funcionalidade liberada vê uma tela explicando isso, em vez de um app pela metade. Lista completa em [Conceitos importantes](#conceitos-importantes).
- **Logs de auditoria**: últimos 500 eventos, visíveis só ao dono.

---

## Stack técnica

- **Frontend/Backend**: [Streamlit](https://streamlit.io/) (Python)
- **Orquestração de IA**: [n8n](https://n8n.io/) (self-hosted), com fallback entre até 5 provedores por workflow: Google Gemini, Groq (x2), OpenAI, Mistral
- **PDF**: ReportLab · **Extração de documentos**: PyMuPDF (PDF), python-docx (DOCX)
- **Azure DevOps**: REST API (`dev.azure.com`), autenticação via PAT

---

## Estrutura do projeto

```
app.py                              # ponto de entrada
.streamlit/secrets.toml             # nunca commitado
requirements.txt

qa_testgen/
├── config/
│   ├── settings.py                 # AppConfiguration (lê st.secrets)
│   └── constants.py                # cores, caminhos de logo, timezone
├── ui/
│   ├── application.py              # UserInterface — toda a lógica de tela (7 passos + sidebar)
│   ├── api_tests_page.py           # página Testes de API (mixin de UserInterface)
│   ├── api_bugs_page.py            # Testes de API — etapa 3: análise, rascunhos de Bug e envio ao Azure (mixin)
│   ├── ia_retry.py                 # regra única de retentativa das chamadas de IA (mixin)
│   ├── work_item_batch_page.py     # Criar Work Item — modo Fila / planilha (mixin)
│   ├── auth.py                     # login, sessão (ID opaco), permissões, logout, Administração
│   └── dialogs.py                  # modais de confirmação
├── domain/
│   ├── models/                     # MatrixRow, TestCase, TestPlan, TestStep, api_test (Testes de API)
│   └── validators/                 # validação de campos obrigatórios (Matriz/Caso/Plano)
├── infrastructure/
│   ├── webhook_client.py           # chamadas aos webhooks de IA do n8n
│   ├── azure_devops_client.py      # cliente REST completo do Azure DevOps
│   ├── access_control_client.py    # controle de acesso/logs/sessões (n8n)
│   ├── document_processor.py       # extração de texto + imagens
│   ├── document_store.py           # Documentos Armazenados (Turso/libsql)
│   ├── csv_formatter.py            # exportação CSV
│   ├── pdf_report.py               # PDFs (Documentação QA e Relatório de Testes)
│   ├── manual_pdf.py               # PDF do Manual de Testes (UAT)
│   ├── postman_importer.py         # collection/environment Postman -> casos do módulo Testes de API
│   ├── api_test_runner.py          # executor dos Testes de API (requests, asserções, variáveis)
│   ├── api_evidence.py             # evidências dos Testes de API (mascaramento, .md, .zip)
│   ├── api_discovery.py            # "Reconhecer a API" + catálogo de rotas reais (bundle/OpenAPI/Postman, casamento, verificação)
│   ├── api_to_assistant.py         # bateria de API → Matriz/Casos/Planos do assistente
│   ├── api_triage.py               # triagem do resultado: bug provável × bateria × perfil × a confirmar; rascunho de Bug
│   ├── api_contracts.py            # catálogo de formatos reais + correções de formato da bateria
│   ├── api_autofill.py             # logins de outros perfis, dados negativos, ids descobertos nas respostas
│   ├── api_generation_batches.py   # geração com IA em lotes de Work Items
│   └── work_item_batch.py          # modelo de planilha, leitura e validação de Work Items em lote
├── assets/                         # Guia_Usuario.pdf, Guia_Administrador.pdf (baixáveis em "Sobre o App"); Guia_Testes_API.pdf (baixável na tela de Testes de API)
└── application/session.py          # SessionState (defaults do st.session_state)
```

### Workflows do n8n

| Workflow | Função |
|---|---|
| `Doc_QA_Analysis` | Perguntas de esclarecimento (Passo 2) |
| `Doc_QA_Matrix` | Matriz de Cobertura (Passo 3), calibrada pelo Tipo de Documento |
| `Doc_QA_Generation` | Casos de Teste (Passo 4), calibrado pelo Tipo de Documento |
| `Doc_QA_Plans` | Planos de Teste (Passo 5) |
| `Doc_QA_Matching` | Sugere vínculos Caso↔Work Item (reaproveitado nos modos "Vincular" e "Reconciliar") |
| `Doc_QA_Access_Control` | Aprovações de login, permissões, logs, sessões |
| `Doc_QA_Image_Interpretation` | Interpreta imagens extraídas dos documentos |
| `Doc_QA_Execution_Report_Narrative` | Sugere textos do Relatório de Testes |
| `Doc_QA_WIQL_Generation` | Traduz descrição em linguagem natural para WIQL |
| `Doc_QA_ApiTest_Generation` | Monta a bateria de Testes de API (casos, asserções, variáveis) a partir de uma User Story/Work Item/documento |
| `Doc_QA_Manual_Generation` | Gera o Manual de Testes (UAT) em linguagem simples |
| `Doc_QA_Duplicate_Comparison` | Compara o conteúdo de Casos "parecidos" (candidatos a duplicata) antes de decidir se vincula/cria |

---

## Configuração

### Dependências Python

```
streamlit
requests
reportlab
pymupdf
python-docx
pillow
bcrypt
libsql-client            # Documentos Armazenados (Turso)
extra-streamlit-components
```

Lista completa e travada por versão em `requirements.txt`.

### `secrets.toml`

```toml
N8N_WEBHOOK_URL_ANALYSIS = "http://SEU-N8N/webhook/qa-testgen-analysis"
N8N_WEBHOOK_URL_MATRIX = "http://SEU-N8N/webhook/qa-testgen-matrix"
N8N_WEBHOOK_URL_GENERATION = "http://SEU-N8N/webhook/qa-testgen-generation"
N8N_WEBHOOK_URL_PLANS = "http://SEU-N8N/webhook/qa-testgen-plans"
N8N_WEBHOOK_URL_MATCHING = "http://SEU-N8N/webhook/qa-testgen-matching"
N8N_WEBHOOK_URL_ACCESS_CONTROL = "http://SEU-N8N/webhook/qa-testgen-access-control"
N8N_WEBHOOK_URL_IMAGE_INTERPRETATION = "http://SEU-N8N/webhook/qa-testgen-image-interpretation"
N8N_WEBHOOK_URL_EXECUTION_REPORT_NARRATIVE = "http://SEU-N8N/webhook/qa-testgen-execution-report-narrative"
N8N_WEBHOOK_URL_WIQL_GENERATION = "http://SEU-N8N/webhook/qa-testgen-wiql-generation"
N8N_WEBHOOK_URL_APITEST_GENERATION = "http://SEU-N8N/webhook/qa-testgen-apitest-generation"
N8N_WEBHOOK_URL_MANUAL_GENERATION = "http://SEU-N8N/webhook/qa-testgen-manual-generation"
N8N_WEBHOOK_URL_DUPLICATE_COMPARISON = "http://SEU-N8N/webhook/qa-testgen-duplicate-comparison"
N8N_API_KEY = "GERE_UM_VALOR_ALEATORIO_LONGO"
APP_OWNER_USERNAME = "admin"
AZURE_DEVOPS_ORG = "sua-organizacao"

# AZURE_DEVOPS_PAT = "..."  # opcional — liga o modo de PAT compartilhado (ver nota abaixo)

# Documentos Armazenados (opcional — feature fica indisponível sem isso)
TURSO_DATABASE_URL = "libsql://seu-banco.turso.io"
TURSO_AUTH_TOKEN = "..."

[credentials]
[credentials.usernames]
admin = "$2b$12$...hash-bcrypt-aqui..."
```

> `AZURE_DEVOPS_PAT` é opcional: por padrão não existe (cada pessoa usa o próprio PAT), mas configurar essa chave liga o modo de PAT compartilhado pra todo mundo (ver [Controle de acesso e governança](#controle-de-acesso-e-governança)). `cookie_secret` não é mais usado (a sessão não usa mais assinatura local — valida direto no n8n).

Gerando um hash bcrypt: `bcrypt.hashpw(b"senha", bcrypt.gensalt()).decode()`

### Workflows do n8n

Importe cada workflow, vincule as credenciais de IA em cada node, configure a credencial Header Auth (mesmo valor de `N8N_API_KEY`), e ative todos.

---

## Como rodar localmente

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
# preencha .streamlit/secrets.toml
python -m streamlit run app.py
```

## Deploy

Streamlit Community Cloud com auto-deploy a partir do `main` — Secrets configurados nas configurações do app.

---

## Conceitos importantes

**Ambiente (Homologação/Produção)** — Passo 1, obrigatório, sem pré-seleção. Define a etiqueta HML/PROD em Casos, Matriz e documentação.

**Tipo de Documento** — Passo 1, obrigatório. Calibra o nível de detalhe da IA (Visão = mais exploratório; Especificações Funcionais = mais granular) e sugere o modo de envio do Passo 7.

**PAT pessoal** — Work Items (Read & Write) + Test Management (Read & Write). Nunca salvo em disco.

**Permissões granulares** — `assistente_qa` (o assistente de QA, Passos 1–6), `azure_devops` (Passo 7, Criar Query com IA), `execution_report` (Relatório de Testes), `azure_query` (Passo 1 — Gerar a partir de uma Query), `manual_testes` (Manual de Testes), `documentos_armazenados` (salvar/ver Documentos Armazenados — excluir continua exclusivo do dono, mesmo com a permissão), `mapa_mental` (Mapa Mental), `criar_bug` (Criar Bug), `criar_work_item` (Criar Work Item) e `testes_api` (Testes de API), concedidas individualmente — tudo num único cadastro por usuário, na aba "Usuários" da Administração.

> ⚠️ **Nenhuma funcionalidade é liberada só por logar.** Sem `assistente_qa`, a pessoa não vê os Passos 1–6: ao entrar, vai direto pra área que tem permissão (ou pra uma tela de atalhos, se tiver mais de uma). O dono do app (`APP_OWNER_USERNAME`) sempre tem tudo, independente de cadastro.

**Status de QA via coluna do board** — no Relatório de Testes, vem da coluna do Kanban do Work Item vinculado, não do outcome do Test Point:

| Status | Colunas |
|---|---|
| **Aprovado** | Pronto para UAT, Teste UAT, Aguardando CAB, Aguardando Subida em Produção, Testes em Produção, Finalizado |
| **Cancelado** | Cancelados |
| **Pendente** | Backlog, Em/Pronto para Refinamento de Negócios/Técnico, Em/Pronto para Validação de Produtos, Pronto para Dev, Em Desenvolvimento, Pronto/Em Code Review, Pronto para QA, Teste QA |

O Status **geral** do relatório é escolhido manualmente.

**Exclusividade Caso↔Work Item** — nos 3 modos do Passo 7, um Caso de Teste só pode estar vinculado a um Work Item por vez.

---

## Limitações conhecidas

- **Testes de API — scripts do Postman**: o app não executa JavaScript. O importador converte por padrão de texto os `pm.test` mais comuns (`to.have.status`, `to.have.property`, `to.eql`, `to.be.a`, `.not.empty`, `collectionVariables.set` e aliases como `const d = pm.response.json().data`). Lógica JS arbitrária (`forEach`, `oneOf`, cálculos) precisa ser reescrita nas asserções declarativas da tela.
- **Testes de API — geração por IA**: cada chamada é grande (prompt + bateria inteira) e conta contra os limites por minuto/dia dos planos gratuitos dos provedores; quando todos falham, a mensagem na tela traz o erro de cada um (ex.: `Gemini: too many requests`) — basta tentar de novo alguns minutos depois. Documentos de contexto vão só em trecho (6 mil caracteres).
- **Testes de API — modo Navegador**: depende de a API responder CORS (`Access-Control-Allow-Origin`) para a origem do app; sem isso, o navegador bloqueia a chamada (o caso aparece como "Erro: Failed to fetch") e o administrador deve trocar para o modo Servidor.
- **Testes de API — triagem**: é por regra (sem IA), a partir do que a API respondeu. Ela não sabe o que o card exige além do texto dos Work Items usados na geração: sem esse texto, toda divergência de status/mensagem vai pra "a confirmar com o PO" (conservador de propósito). "Com quem falar" usa papéis (dev backend, Tech Lead, PO) até você clicar em **👤 Buscar responsáveis** — aí aparece o nome de quem está com cada Work Item no Azure.
- **Testes de API — Bugs no Azure**: criar exige a permissão **Criar Bug**. O app cria e vincula, mas nunca altera nem exclui um Bug já criado — isso é feito no Azure DevOps (o cartão do rascunho tem o link e o "🔄 Verificar no Azure").
- **Testes de API — asserções por caso**: um caso sem asserção é reprovado de propósito ("Nenhuma asserção definida") — o mínimo é o status HTTP esperado.
- **Testes de API — rota presumida ≠ API quebrada**: um caso cuja rota não existe (404 "route could not be found") sai como **Erro** ("Rota inexistente"), não como Reprovado, e a leitura do resultado separa rota inexistente / bloqueado / sem resposta / reprovado de verdade. O catálogo de rotas por host fica em `app_config` (`api_rotas::<host>`), compartilhado por todos.
- **Testes de API — caso Bloqueado**: um caso que usa `{{variavel}}` que um caso anterior deveria extrair (ex.: `{{survey_id}}` vindo de `data.survey.id`) e que ficou vazia porque a extração falhou não roda — aparece como **Bloqueado** com o motivo, nos dois modos de execução (navegador e servidor), em vez de sair com a URL quebrada (`/surveys//questions`) e reprovar por um motivo que não é dele. Variável preenchida à mão nunca bloqueia. A `definicao-testes.json` do `.zip` mantém `Authorization: Bearer {{auth_token}}` (só valores secretos reais são mascarados), pra poder repetir a bateria.
- **Matriz de Cobertura** nunca é enviada ao Azure DevOps — só existe no PDF quando gerada na mesma sessão, ou reconstruída de forma independente a partir dos Work Items vinculados.
- **Reconciliação com Test Plan Anterior** faz o match por **título apenas** (não tem acesso fácil aos passos detalhados dos Casos já existentes) — revise as sugestões com mais atenção que no fluxo direto.
- **Logs de auditoria e sessões** guardam um histórico limitado (armazenamento via n8n static data, sem banco de dados dedicado).
- Algumas áreas da API do Azure DevOps foram implementadas com base na documentação pública e ajustadas a partir de testes reais — se o formato variar entre organizações/processos, pode precisar de ajuste fino.

## Manutenção

- **Modelos de IA**: os workflows do n8n fixam versões de modelo — provedores mudam/depreciam modelos com frequência.
- **Credenciais do n8n**: o fallback entre provedores cobre a maioria dos casos de expiração, mas vale monitorar os logs de execução do n8n.
- **Rate limit dos provedores de IA — regra única de retentativa** (`ui/ia_retry.py`, vale pra TODAS as chamadas de IA: Matriz/Casos/Planos em lote, imagens do documento, Análise, Vínculos com IA, Narrativa do relatório, WIQL, Manual e Testes de API): entre unidades que deram certo o intervalo é curto (5s nos lotes de geração; nenhum nas demais); quando uma chamada falha, o app espera ~62s (a janela real do rate limit), tenta a MESMA de novo — até 3 vezes — e ainda espera 62s antes da próxima. Só a comparação de duplicados (Passo 7) fica de fora: falhar ali não bloqueia nada. Confirmado, por teste real, que falhas do tipo "Todos os provedores de IA falharam" costumam ser rate limit passageiro, resolvido tentando de novo.
- **Timeout do proxy na frente do n8n**: se o n8n estiver atrás de Nginx/Nginx Proxy Manager, aumente `proxy_read_timeout`/`proxy_connect_timeout`/`proxy_send_timeout` pra pelo menos 300s (mesmo valor do timeout do app pra cada chamada) — sem isso, o proxy pode cortar a conexão antes do n8n terminar, mesmo quando a IA responderia a tempo.
