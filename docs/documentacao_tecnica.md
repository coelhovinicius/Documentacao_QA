# QA Automation – Azure DevOps
## Documentação Técnica Completa

**Projeto:** QA TestGen / QA Automation
**Última atualização:** Julho de 2026

---

## 1. Visão Geral

O **QA Automation – Azure DevOps** é uma aplicação web interna que usa Inteligência Artificial para automatizar a documentação de QA de um projeto de software, do início ao fim:

1. Recebe um ou mais documentos de requisitos (PDF, DOCX ou TXT)
2. Usa IA (via n8n) para gerar, em sequência: dúvidas de negócio → Matriz de Cobertura → Casos de Teste → Planos de Teste
3. Permite revisão e edição manual de tudo antes de finalizar (CRUD completo em cada etapa)
4. Exporta CSV e PDF prontos para uso
5. Integra diretamente com o **Azure DevOps** via API — cria Test Cases, Test Plans, Requirement-based Suites, e vincula tudo automaticamente (com sugestão de vínculos via IA)
6. Executa **testes de API** (módulo Testes de API): importa collections do Postman ou casos criados na tela, roda em Python puro e gera evidências (.md, .pdf, .zip) — sem depender do n8n nem do Azure DevOps

A aplicação é usada publicamente via navegador, protegida por login.

---

## 2. Arquitetura Geral

```
┌─────────────────┐        ┌──────────────────┐        ┌───────────────────┐
│   Usuário        │ ────▶  │  Streamlit Cloud  │ ────▶  │   n8n (self-hosted)│
│  (navegador)     │        │  (app Python)     │        │  Docker + nginx    │
└─────────────────┘        └──────────────────┘        │  + DuckDNS          │
                                     │                    └───────────────────┘
                                     │                              │
                                     │                              ▼
                                     │                    ┌───────────────────┐
                                     │                    │  Provedores de IA  │
                                     │                    │  Gemini / Groq /   │
                                     │                    │  OpenAI / Mistral  │
                                     │                    └───────────────────┘
                                     ▼
                           ┌───────────────────┐
                           │   Azure DevOps     │
                           │   (REST API)       │
                           └───────────────────┘
```

- **Frontend + Backend**: uma única aplicação **Streamlit** (Python), hospedada no **Streamlit Community Cloud**
- **Orquestração de IA**: **n8n**, hospedado separadamente (infraestrutura própria, via Docker + nginx + DuckDNS)
- **Repositório de código**: **GitHub**, conectado ao Streamlit Cloud para deploy automático a cada `git push`
- **Integração final**: **Azure DevOps REST API** (organização `refuturiza`)

---

## 3. Hospedagem e Deploy

### 3.1 Streamlit Cloud (a aplicação em si)

- URL pública: `https://quality-assurance-docs.streamlit.app`
- O Streamlit Cloud está conectado a um repositório **GitHub**; qualquer `git push` no branch configurado (normalmente `main`) dispara um **redeploy automático**
- As credenciais e configurações sensíveis (tokens, senhas, URLs de webhook) **não vão para o Git** — ficam nos **Secrets** do Streamlit Cloud (`App → Settings → Secrets`), no mesmo formato do `secrets.toml` local
- **Importante**: alterações feitas nos arquivos locais só valem em produção depois de `git add` → `git commit` → `git push`. O Streamlit Cloud não sincroniza sozinho com edições locais

### 3.2 Ambiente local (desenvolvimento)

- Rodado localmente via `streamlit run app.py`, num ambiente virtual Python (`.venv`)
- Usa um arquivo `.streamlit/secrets.toml`, na raiz do projeto (mesmo nível do `app.py`), **nunca commitado no Git** (deve estar no `.gitignore`)
- Esse arquivo local e os Secrets do Streamlit Cloud devem ter, essencialmente, o mesmo conteúdo — exceto quando se quer usar ambientes/tokens diferentes entre local e produção (ex.: sandbox vs. produção do Azure DevOps)

### 3.3 Git / GitHub

- Controle de versão via Git, hospedado no GitHub
- Arquivos sensíveis (`secrets.toml`, tokens, etc.) ficam fora do controle de versão via `.gitignore`
- Fluxo de trabalho: editar localmente → testar (`streamlit run app.py`) → commitar com mensagens descritivas → `git push` → Streamlit Cloud reimplanta automaticamente

---

## 4. n8n (Orquestração de IA)

### 4.1 Infraestrutura do n8n

- Hospedado via **Docker**, em infraestrutura própria (não é n8n Cloud)
- Exposto publicamente através de um domínio dinâmico via **DuckDNS** (ex.: `vinitestes-qa.duckdns.org`)
- **nginx** atua como reverse proxy na frente do container do n8n
- Autenticado via **Header Auth** (`x-api-key`), configurado tanto no lado do n8n quanto enviado pelo app Python a cada requisição

### 4.2 Workflows existentes

O app depende de **11 workflows** publicados e **ativos** no n8n:

| Workflow | Endpoint (webhook) | Função |
|---|---|---|
| `Doc_QA_Analysis` | `/webhook/qa-testgen-analysis` | Recebe o texto do(s) documento(s) e devolve uma lista de dúvidas/ambiguidades de negócio |
| `Doc_QA_Matrix` | `/webhook/qa-testgen-matrix` | Gera a Matriz de Cobertura (linhas MC-001, MC-002...) |
| `Doc_QA_Generation` | `/webhook/qa-testgen-generation` | Gera os Casos de Teste (com rastreabilidade — `requisitos_relacionados` apontando pra Matriz) |
| `Doc_QA_Plans` | `/webhook/qa-testgen-plans` | Gera os Planos de Teste (Planos → Suites → Casos) |
| `Doc_QA_Matching` | `/webhook/qa-testgen-matching` | Sugere automaticamente o vínculo entre Casos de Teste gerados e Work Items existentes no Azure DevOps |
| `Doc_QA_Access_Control` | `/webhook/qa-testgen-access-control` | Controle de acesso: aprovações de login, permissões granulares, sessões ativas, logs de auditoria (todo o "banco de dados" de acesso mora aqui, via workflow static data) |
| `Doc_QA_Image_Interpretation` | `/webhook/qa-testgen-image-interpretation` | Interpreta (descreve em texto) uma imagem extraída de um documento ou de um Work Item |
| `Doc_QA_Execution_Report_Narrative` | `/webhook/qa-testgen-execution-report-narrative` | Sugere os textos narrativos (Contexto, Escopo, Conclusão, Próximos Passos) do Relatório de Testes |
| `Doc_QA_WIQL_Generation` | `/webhook/qa-testgen-wiql-generation` | Traduz uma descrição em linguagem natural pra uma query WIQL válida do Azure DevOps |
| `Doc_QA_ApiTest_Generation` | `/webhook/qa-testgen-apitest-generation` | Monta a bateria de Testes de API (casos com método/URL/headers/body, asserções declarativas, extração de variáveis) a partir de especificação + Base URL; mesmo encadeamento de 5 provedores |
| `Doc_QA_Manual_Generation` | `/webhook/qa-testgen-manual-generation` | Gera o Manual de Testes (UAT) em linguagem simples, sugerindo quais imagens disponíveis combinam com cada passo |
| `Doc_QA_Duplicate_Comparison` | `/webhook/qa-testgen-duplicate-comparison` | Compara o CONTEÚDO (pré-condições/passos) de um par Caso novo × Caso já existente que pareceu duplicado por título, decidindo se são de fato o mesmo teste |

### 4.3 Padrão interno de cada workflow

> ⚠️ Modelos e ordem exata da cadeia mudam com frequência (o dono ajusta direto no n8n conforme provedores saem do ar/mudam de preço) — os valores abaixo já ficaram desatualizados uma vez neste mesmo documento em poucos meses. Trate como "a mecânica geral não muda", não como um valor fixo — **confira o node de cada Chain direto no n8n** pra saber o modelo/ordem exatos vigentes.

Praticamente todos os workflows de geração (todos, exceto `Doc_QA_Access_Control`, que é lógica pura sem IA) seguem a mesma estrutura, por resiliência:

```
Webhook → Chain (Provedor A) → (erro) → Chain (Provedor B) → (erro) → Chain (Provedor C)
                                                                            │
                                                                        (erro)
                                                                            ▼
                                                    Chain (Provedor D) → (erro) → Chain (Provedor E)
                                                                                       │
                                                                            ┌──────────┴──────────┐
                                                                            ▼                      ▼
                                                              Respond to Webhook      Respond to Webhook1
                                                                (sucesso, 200)      (todos falharam, 502,
                                                                                     JSON {"error","detalhe"})
```

- **Cadeia de fallback entre 5 provedores de IA** (Google Gemini, Groq — normalmente 2x na cadeia, com modelos/posições que podem diferir entre workflows —, OpenAI, e Mistral): se um provedor falhar (erro, schema inválido, rate limit, timeout), o n8n tenta automaticamente o próximo. A ORDEM exata varia de workflow pra workflow (não é sempre a mesma sequência) — confira o diagrama real de cada um em **n8n → workflow → Editor**.
- Cada chain node usa um **Structured Output Parser**, com schema JSON definido e campos marcados como `required` — isso força o modelo a devolver exatamente o formato esperado, e rejeita respostas incompletas (acionando o fallback).
- **Falha de TODOS os provedores**: o nó final de erro (`Respond to Webhook1`, ou nome equivalente) responde com status **502** e um corpo JSON `{"error": "Todos os provedores de IA falharam (...)", "detalhe": "..."}` — o app (`webhook_client.py`) já sabe extrair e mostrar esse `detalhe` como mensagem amigável. **Confirmado, por teste real, que essas falhas costumam ser rate limit passageiro** (ex.: mensagem "OpenAI: Rate limit reached" capturada ao vivo) — o app já tenta cada lote de novo automaticamente (até 3x, esperando entre tentativas) antes de reportar como erro de verdade.
- **Rate limit por lote**: um único lote de geração já pode consumir a maior parte da cota por minuto (TPM) de um provedor — erro real capturado no Groq: `Limit 8000, Used 2331, Requested 6285` (~78% do limite numa chamada só). Por isso o app espaça os lotes em ~62s, alinhado à janela real do rate limit, não um valor arbitrário menor.

### 4.4 Contratos de dados (payloads)

Todas as chamadas do app para o n8n são `POST`, com corpo JSON. Listas/objetos complexos (documento, matriz, casos, etc.) são enviados como **strings JSON** dentro dos campos (não como JSON aninhado nativo), e cada workflow faz o parse internamente no prompt.

**Exemplo — `Doc_QA_Matching`:**
```json
{
  "work_items": "[{\"id\": 123, \"title\": \"...\", \"type\": \"User Story\", \"state\": \"Active\"}]",
  "casos_de_teste": "[{\"titulo\": \"...\", \"pre_condicoes\": \"...\", \"passos\": [...]}]",
  "nome_projeto": "..."
}
```
**Resposta esperada:**
```json
{
  "vinculos": [
    {"work_item_id": "123", "casos": ["Título do Caso 1", "Título do Caso 2"]}
  ]
}
```

### 4.5 Manutenção do n8n

- Cada workflow precisa estar **ativado** (toggle "Active") para que a Production URL exista — se estiver desativado, o app recebe erro de conexão
- Chaves de API dos provedores de IA (Gemini, Groq, OpenAI, Mistral) são credenciais configuradas dentro do próprio n8n, não no app
- Para depurar lentidão ou falhas: **n8n → workflow → Executions** → abrir a execução → o diagrama mostra visualmente qual(is) provedor(es) foram acionados até o sucesso (ou falha)

---

## 5. Integração com Azure DevOps

### 5.1 Autenticação

- Feita via **Personal Access Token (PAT)**, com Basic Auth (usuário vazio + PAT em Base64)
- Escopos necessários no PAT: **Work Items (Read & Write)** e **Test Management (Read & Write)**
- Dois modos, controlados pela presença de `AZURE_DEVOPS_PAT` no `secrets.toml`:
  - **Sem `AZURE_DEVOPS_PAT` (padrão)**: cada usuário digita o próprio PAT num campo de senha, na hora — nunca é salvo em disco, só na memória da sessão (`st.session_state`)
  - **Com `AZURE_DEVOPS_PAT` configurado**: vira um PAT compartilhado por todo mundo, sem precisar digitar nada — a rastreabilidade de quem fez o quê passa a vir da tag automática `criado-por:<usuário>` (`UserInterface._tag_criado_por`), acrescentada a todo Bug/Test Case criado. Um aviso único (`aviso_pat_compartilhado_modal`) informa cada usuário na primeira vez que esse modo estiver ativo pra ela
- **PATs expiram** — é preciso renovar manualmente antes do vencimento (a data é definida na criação do token, em `https://dev.azure.com/{org}/_usersSettings/tokens`)

### 5.2 Seleção de Organização / Projeto / Area Path

Todos os três níveis são obtidos **dinamicamente da API do Azure DevOps**, nunca digitados livremente:

1. **Organização**: carregada automaticamente ao abrir o Passo 7, via API de perfil (`app.vssps.visualstudio.com`). Se o PAT for restrito a uma única organização (não tem escopo "All accessible organizations"), o app cai automaticamente num fallback usando a organização configurada no `secrets.toml` como única opção
2. **Projeto**: buscado sob demanda (botão "Buscar Projetos desta Organização") — lista só os projetos que o PAT consegue visualizar
3. **Area Path**: usa por padrão a **raiz do projeto** (sempre válida, sem chamada extra); uma sub-area específica pode ser escolhida via um expansor opcional, que busca a árvore completa de Areas do projeto (`_apis/wit/classificationnodes/Areas`)

### 5.3 O que a integração cria no Azure DevOps

Para cada análise, o Passo 7 do app permite:

1. **Buscar os Work Items existentes** no Area Path selecionado (exclui automaticamente os tipos `Test Case`, `Test Plan`, `Test Suite`, e os estados `Backlog`/`Finalizado`)
2. **Sugerir vínculos automaticamente**, via IA (workflow `Doc_QA_Matching`), entre os Casos de Teste gerados e os Work Items encontrados
3. **Revisar/ajustar manualmente** os vínculos sugeridos (multiselect por Work Item — um caso pode ser vinculado a mais de um Work Item)
4. **Confirmar a integração**, que executa, nessa ordem:
   - Checa se já existe um Test Plan com o nome escolhido (bloqueia se sim)
   - Cria todos os Casos de Teste ainda não existentes no Azure DevOps (como Work Items do tipo `Test Case`), em paralelo (até 4 chamadas simultâneas)
   - Cria o Test Plan
   - Cria uma **Requirement-based Suite** por Work Item que tenha pelo menos 1 caso vinculado (sequencial — suites concorrentes geram erro de conflito de escrita no Azure DevOps)
   - Cria o vínculo de "Tests" entre cada Caso de Teste e seu(s) Work Item(s), em paralelo entre casos diferentes (mas sequencial quando é o mesmo caso vinculado a múltiplos Work Items, pra evitar escrita concorrente no mesmo item)

### 5.4 Campos e particularidades do Azure DevOps usados

- **Pré-condições**: gravadas num campo **customizado** do processo da organização (`Custom.Precondicoes`) — não é o campo padrão `System.Description`. Esse nome de campo é específico dessa organização; se reaproveitado em outra, é preciso confirmar o nome real (script `list_test_case_fields.py` ajuda a descobrir)
- **Passos do Caso de Teste**: gravados no campo `Microsoft.VSTS.TCM.Steps`, que exige um XML específico (steps com `parameterizedString` em HTML escapado)
- **Numeração dos títulos**: todo Caso de Teste ganha prefixo `CT01 -`, `CT02 -`, etc., tanto nos CSVs quanto na integração direta
- **Estado do Caso de Teste**: o Azure DevOps **não permite definir um estado não-padrão na criação** do work item (é uma regra de workflow, não uma falha do app) — por isso, o Caso é criado primeiro no estado padrão (`Design`), e depois, numa chamada separada, é feita a transição pro estado desejado (ex.: `Ready`). Se essa segunda etapa falhar, o caso continua existindo normalmente, só fica registrado um aviso
- **Rastreabilidade**: cada Caso de Teste carrega um campo `requisitos_relacionados`, com os IDs da Matriz de Cobertura (ex.: `MC-001`) que ele cobre — usado no PDF pra gerar o "Resumo de Rastreabilidade" com destaque automático pra requisitos sem nenhuma cobertura

### 5.5 Robustez técnica da integração

- **Conexão reutilizável**: `requests.Session()` com pool de conexões, em vez de abrir uma conexão nova a cada chamada
- **Retry automático**: até 4 tentativas com espera crescente, pra qualquer falha transitória de conexão (reset, timeout, erros 429/500/502/503/504)
- **Paralelismo controlado**: até 4 chamadas simultâneas via `ThreadPoolExecutor` — ajustado depois de testes reais mostrarem que 8 simultâneas causava reset de conexão

---

## 6. Autenticação e Sessão do App

> ⚠️ Seção reescrita — a versão anterior descrevia um mecanismo de token HMAC assinado na URL que **não existe mais** (foi substituído por sessão via ID opaco, abaixo).

- Login com usuário/senha. Usuários podem vir do `st.secrets["credentials"]["usernames"]` (fixos, definidos no `secrets.toml`) **ou** ser cadastrados dinamicamente pelo dono do app na aba "Usuários" da Administração — em ambos os casos, a senha é sempre um **hash bcrypt**, nunca texto puro.
- **Login com aprovação**: só o dono do app (`APP_OWNER_USERNAME`) entra direto. Qualquer outro usuário, mesmo já cadastrado, precisa que um aprovador aceite a solicitação a **cada nova sessão** — a não ser que o cadastro dele tenha "Acesso direto (sem aprovação)" marcado.
- **Sessão via ID opaco**: depois de aprovado, o app gera um ID aleatório (`sid`) e coloca só ele na URL (`?sid=...`) — não é um token assinado, é uma referência opaca. Todo o dado real da sessão (usuário, validade) fica guardado do lado do **n8n** (`create_session`/`get_session` em `access_control_client.py`), então **revogar remotamente** (a própria sessão ou a de outra pessoa) é possível a qualquer momento pela aba "Sessões Ativas" da Administração — não depende de derrubar cookie/token local nenhum.
- **Logout automático por inatividade**: 60 minutos sem interação expira a sessão (constante `INACTIVITY_TIMEOUT_MINUTES` em `auth.py`) — a expiração é conferida no lado do n8n, não só localmente.
- Um botão "Sair" fixo no rodapé da sidebar encerra a sessão manualmente (remove o `sid` da URL e revoga do lado do n8n).

---

## 7. Estrutura de Pastas e Arquivos

```
projeto/
├── app.py                              # ponto de entrada
├── .streamlit/
│   └── secrets.toml                    # credenciais locais (NUNCA no Git)
├── qa_testgen/
│   ├── config/
│   │   ├── constants.py                # cores, caminhos de logo, timezone
│   │   └── settings.py                 # AppConfiguration (lê st.secrets)
│   ├── ui/
│   │   ├── application.py              # UI principal (fluxo de 7 passos)
│   │   ├── auth.py                     # login/sessão
│   │   └── dialogs.py                  # modais de confirmação
│   ├── application/
│   │   └── session.py                  # wrapper do st.session_state
│   ├── domain/
│   │   ├── models/                     # MatrixRow, TestCase, TestPlan, TestStep
│   │   ├── models/api_test.py          # modelos do módulo Testes de API (caso, asserção, resultado)
│   │   └── validators/                 # validação de campos obrigatórios
│   ├── infrastructure/
│   │   ├── webhook_client.py           # chamadas aos 11 webhooks do n8n
│   │   ├── azure_devops_client.py      # cliente da API do Azure DevOps
│   │   ├── access_control_client.py    # controle de acesso/logs/sessões (n8n)
│   │   ├── csv_formatter.py            # exportação CSV (Azure DevOps import)
│   │   ├── document_processor.py       # extração de texto (PDF/DOCX/TXT)
│   │   ├── document_store.py           # Documentos Armazenados (Turso/libsql)
│   │   ├── pdf_report.py               # PDF (Documentação QA e Relatório de Testes)
│   │   ├── manual_pdf.py               # PDF do Manual de Testes (UAT)
│   │   ├── postman_importer.py         # Postman v2.1 (collection/environment) -> casos de Testes de API
│   │   ├── api_test_runner.py          # executor dos Testes de API (requests; variáveis; asserções)
│   │   └── api_evidence.py             # evidências dos Testes de API (mascaramento, RELATORIO.md, .zip)
│   └── (scripts auxiliares de teste/diagnóstico, fora do fluxo principal do app)
└── docs/
    ├── documentacao_tecnica.md         # este documento
    ├── Guia_Usuario.pdf / .docx        # guia do usuário final
    └── Guia_Administrador.pdf / .docx  # guia do usuário administrador
```

---

## 8. Configuração (`secrets.toml`)

```toml
# --- n8n (11 workflows) ---
N8N_WEBHOOK_URL_ANALYSIS = "http://seu-n8n/webhook/qa-testgen-analysis"
N8N_WEBHOOK_URL_MATRIX = "http://seu-n8n/webhook/qa-testgen-matrix"
N8N_WEBHOOK_URL_GENERATION = "http://seu-n8n/webhook/qa-testgen-generation"
N8N_WEBHOOK_URL_PLANS = "http://seu-n8n/webhook/qa-testgen-plans"
N8N_WEBHOOK_URL_MATCHING = "http://seu-n8n/webhook/qa-testgen-matching"
N8N_WEBHOOK_URL_ACCESS_CONTROL = "http://seu-n8n/webhook/qa-testgen-access-control"
N8N_WEBHOOK_URL_IMAGE_INTERPRETATION = "http://seu-n8n/webhook/qa-testgen-image-interpretation"
N8N_WEBHOOK_URL_EXECUTION_REPORT_NARRATIVE = "http://seu-n8n/webhook/qa-testgen-execution-report-narrative"
N8N_WEBHOOK_URL_WIQL_GENERATION = "http://seu-n8n/webhook/qa-testgen-wiql-generation"
N8N_WEBHOOK_URL_APITEST_GENERATION = "http://seu-n8n/webhook/qa-testgen-apitest-generation"
N8N_WEBHOOK_URL_MANUAL_GENERATION = "http://seu-n8n/webhook/qa-testgen-manual-generation"
N8N_WEBHOOK_URL_DUPLICATE_COMPARISON = "http://seu-n8n/webhook/qa-testgen-duplicate-comparison"
N8N_API_KEY = "..."

APP_OWNER_USERNAME = "admin"

# --- Azure DevOps (Organização é usada como fallback; Projeto vem sempre da API) ---
AZURE_DEVOPS_ORG = "refuturiza"
# AZURE_DEVOPS_PAT = "..."  # opcional — liga o modo de PAT compartilhado (ver seção 5.1)

# --- Documentos Armazenados (opcional — feature fica indisponível sem isso) ---
TURSO_DATABASE_URL = "libsql://seu-banco.turso.io"
TURSO_AUTH_TOKEN = "..."

[credentials.usernames]
admin = "$2b$12$....hash-bcrypt...."
```

⚠️ **Atenção à ordem no TOML**: tudo que vem depois de um cabeçalho `[tabela]` pertence a ela até aparecer outro cabeçalho — por isso as chaves "soltas" (webhooks, Azure DevOps, Turso) ficam sempre **antes** de qualquer `[tabela]` no arquivo.

Não existe mais `cookie_secret` — a sessão não usa assinatura local, valida direto no n8n (ver seção 6).

---

## 9. Fluxo da Aplicação (7 Passos)

| Passo | Nome | O que acontece |
|---|---|---|
| 1 | Upload | Nome do projeto + upload de 1 ou mais documentos (PDF/DOCX/TXT, até 20MB cada e 20MB no total) |
| 2 | Dúvidas | IA identifica ambiguidades no documento; usuário responde |
| 3 | Matriz | Matriz de Cobertura gerada por IA; CRUD completo |
| 4 | Casos | Casos de Teste gerados por IA (com rastreabilidade); CRUD completo |
| 5 | Planos | Planos/Suites de Teste gerados por IA; CRUD completo |
| 6 | Download | Exporta CSV (Casos / Planos+Suites+Casos) e PDF completo |
| 7 | Azure DevOps | Configuração dinâmica (Org/Projeto/Area) → Work Items → sugestão de vínculos por IA → revisão manual → integração real |

---

## 9.1 Módulo Testes de API (fora dos 7 passos)

Área da barra lateral (permissão `testes_api`), implementada em `qa_testgen/ui/api_tests_page.py` como mixin de `UserInterface`; estado em chaves `api_*` do `SessionState`.

| Etapa | O que acontece |
|---|---|
| 1. Definição | Nome, Ambiente, Base URL (`{{base_url}}`), origem dos casos (geração por IA via `Doc_QA_ApiTest_Generation` a partir de especificação/documentos; collection Postman + environment opcional; definição `.json` do próprio módulo; criação manual), variáveis (secretas só em sessão), documentos de contexto opcionais, editor por caso (método, URL, headers, body, asserções declarativas, extração de variáveis) |
| 2. Execução | Modo **Navegador** (padrão; config global em `app_config` no Turso, chave `api_tests_modo_execucao`): o componente `ui/components/api_browser_runner/index.html` faz as chamadas no browser do usuário (fetch + substituição de variáveis + extração), devolve as respostas brutas e `ApiTestRunner.avaliar_execucao_externa` avalia em Python. Modo **Servidor**: `ApiTestRunner` roda os casos habilitados em ordem com `requests` (uma `Session` por execução, certificados do sistema operacional), resolve `{{variáveis}}`, avalia as asserções e propaga valores extraídos (ex.: token) |
| 3. Evidências | `ApiEvidenceBuilder` gera `RELATORIO.md` e `.zip` (pasta por caso: `1_request.txt`, `2_response.txt`, `3_resultado.txt` + imagens); `PdfReportGenerator.generate_api_test_report` gera o PDF no padrão do app; opção de salvar no Documentos Armazenados (tipos `pdf`, `md`, `zip`) |

**Decisões técnicas:**
- Python puro, sem Node/Newman: o app roda no Streamlit Community Cloud. Os scripts JavaScript do Postman **não são executados** — `PostmanImporter.convert_test_script` converte por padrão de texto os `pm.test` mais comuns (`to.have.status`, `to.have.property`, `to.not.have.property`, `to.eql`, `to.be.a/an`, `.not.empty`, `collectionVariables.set`, comparação com variável lida via `.get`, aliases como `const d = pm.response.json().data`); o restante vira aviso no caso.
- Tipos de asserção (`ASSERTION_TYPES` em `domain/models/api_test.py`): `status`, `json_exists`, `json_absent`, `json_equals`, `json_not_empty`, `json_type`, `json_contains`, `json_equals_var`, `header_contains`, `body_contains`, `body_not_contains`, `response_time_max`. Caminho JSON simples: `data.user.email`, `errors.email[0]`.
- Caso sem asserção é reprovado explicitamente; caso desabilitado aparece como "Não Executado".
- Segurança: valores de variáveis secretas, tokens Bearer, JWTs e campos `password`/`token`/`secret` em JSON saem mascarados (`***MASCARADO***`) de todas as evidências; a definição `.json` exportada nunca inclui valores secretos.
- Disco do Streamlit Cloud é efêmero: evidências existem para download ou para o Documentos Armazenados (Turso).
- WAF do HML (CloudFront) bloqueia IPs de provedores de nuvem (testado: Streamlit Cloud/AWS e n8n/Oracle SP recebem 403 "Request blocked"; IP residencial passa). Por isso o modo Navegador é o padrão; exige CORS na API (o HML já responde `Access-Control-Allow-Origin: *`).
- `AppSettingsStore` (document_store.py): tabela chave/valor `app_config` no Turso para configurações globais; alteradas só pelo dono em Administração → Configurações.
- Testes unitários em `tests/test_api_tests_module.py` (importador, runner com servidor HTTP local, mascaramento/zip).

**Próximas fases (não implementadas):** vínculo dos casos a Test Cases do Azure DevOps (existentes ou novos, com Projeto/Area Path/Tags/Atribuído a/Plano/Suíte) e registro de Test Runs com evidência anexada; geração determinística a partir de Swagger/OpenAPI.

---

## 10. Pontos de Atenção / Manutenção

- **PAT do Azure DevOps expira** — verificar a validade periodicamente
- **Modelos de IA no n8n** podem ser descontinuados (ex.: `gpt-4o` foi aposentado em 2026) — vale checar de tempos em tempos se os modelos configurados continuam ativos
- **Workflows do n8n precisam estar ativos** (`Active`) — se desativados, os webhooks somem
- **Campo `Custom.Precondicoes`** é específico da organização `refuturiza` — reaproveitar essa integração em outra organização/processo do Azure DevOps exige confirmar o nome real do campo
- **`secrets.toml` não sobe pro Git** — precisa ser configurado manualmente em cada ambiente (local + Streamlit Cloud)
- **Rate limit dos provedores de IA**: a geração em lote (Matriz/Casos/Planos) espera ~62s entre lotes e tenta cada lote até 3 vezes antes de desistir — confirmado, por teste real, que falhas do tipo "Todos os provedores de IA falharam" costumam ser rate limit passageiro (ex.: Groq TPM), resolvido tentando de novo
- **Timeout do proxy na frente do n8n**: se o n8n estiver atrás de Nginx/Nginx Proxy Manager, aumente `proxy_read_timeout`/`proxy_connect_timeout`/`proxy_send_timeout` pra pelo menos 300s — sem isso, o proxy pode cortar a conexão antes do n8n terminar, mesmo quando a IA responderia a tempo (ver `n8n_workflows/nginx_docker_timeout.md`)
