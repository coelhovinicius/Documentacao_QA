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
| **1. Upload** | Envio de documento(s) **ou** geração a partir de Work Items existentes. Exige escolher **Ambiente** (Homologação/Produção) **e Tipo de Documento** (Visão / Requisitos Funcionais / Especificações Funcionais / Outros) — o tipo calibra o nível de detalhe que a IA assume ao gerar Matriz/Casos, e sugere o modo de envio do Passo 7. Extrai imagens do corpo do documento e interpreta cada uma via IA. |
| **2. Dúvidas** | A IA faz até 7 perguntas de esclarecimento sobre a especificação. |
| **3. Matriz** | Gera a Matriz de Cobertura (MC-001...), com etiqueta de Ambiente (`MC-001 HML`/`PROD`). Editável. |
| **4. Casos** | Gera Casos de Teste a partir da Matriz. Editável. |
| **5. Planos** | Organiza os Casos em Planos → Suítes. Editável. |
| **6. Download** | Exporta CSV e PDF "Documentação QA". |
| **7. Azure DevOps** | Três modos de envio — ver seção abaixo. |

### Passo 7 — os 3 modos de envio ao Azure DevOps

Escolhidos na tela, com sugestão automática baseada no Tipo de Documento do Passo 1 (sempre trocável manualmente):

**🔗 Vincular a Work Items** — o fluxo clássico, pra quando os Work Items já existem no board. A IA sugere quais Casos de Teste se relacionam a quais Work Items; a pessoa revisa e ajusta antes de confirmar. Cria Test Cases, cria ou **reaproveita** um Test Plan existente (sem duplicar Suítes já existentes), e vincula tudo via Requirement-based Suites.

**📋 Sem Work Items** — pra projetos no início, quando só existe um Documento de Visão (e no máximo um Épico/Backlog genérico no board). Usa os Planos/Suítes/Casos que o próprio Passo 5 gerou e cria um Test Plan com **Suítes Estáticas**, sem depender de nenhum Work Item.

**🔄 Reconciliar Test Plan Anterior** — pra quando os Work Items forem criados *depois* de um envio "Sem Work Items". Busca os Casos de Teste que já existem no Test Plan antigo, e a IA sugere quais Work Items novos correspondem a quais Casos já criados — sem duplicar nenhum Caso, só cria o vínculo e a Requirement Suite.

**Regras que valem nos 3 modos:**
- Um Caso de Teste só pode ficar vinculado a **um** Work Item por vez — uma vez escolhido em algum, some das opções dos demais.
- Antes de qualquer chamada real à API do Azure DevOps, o app sempre mostra uma **lista detalhada** (quais Casos serão criados, quais Suítes/Work Items serão afetados) num modal de confirmação.
- Dá pra excluir Casos específicos do envio (Casos sem nenhum Work Item vinculado já vêm pré-marcados pra exclusão, por padrão).

### Recursos adicionais (sidebar)

- **🎯 Gerar a partir de Work Items** — usa Descrição + Critérios de Aceite de Work Items existentes como especificação de entrada, em vez de um documento.
- **🔎 Criar Query com IA** — descreve em português o que quer consultar no Azure DevOps; a IA traduz pra WIQL, mostra preview real dos resultados antes de criar a query de verdade.
- **📊 Relatório de Testes** — documenta o que foi **executado**. Status calculado pela **coluna do board (Kanban)** de cada Work Item vinculado (não pelo outcome do Test Point), com Status geral escolhido manualmente. Monta uma Matriz de Cobertura independente quando a sessão não tem uma.
- **🛡️ Administração** *(dono do app)* — aprovadores, permissões granulares, **Sessões Ativas** (revogação remota), e **Logs de Auditoria**.

### Controle de acesso e governança

- **Login com aprovação**: só o dono entra direto; demais usuários precisam de aprovação a cada sessão.
- **Sessão via ID opaco**: a URL não revela usuário nem senha — o dado fica no n8n, revogável a qualquer momento (a própria sessão, ou a de outra pessoa).
- **PAT pessoal**: nunca salvo em disco, só na memória da sessão.
- **Permissões granulares**: acesso à Integração com Azure DevOps e ao Relatório de Testes, liberados individualmente.
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
├── config/settings.py              # AppConfiguration (lê st.secrets)
├── ui/
│   ├── application.py              # UserInterface — toda a lógica de tela
│   ├── auth.py                     # login, sessão (ID opaco), permissões, logout
│   └── dialogs.py                  # modais de confirmação
├── infrastructure/
│   ├── webhook_client.py           # chamadas aos webhooks de IA do n8n
│   ├── azure_devops_client.py      # cliente REST completo do Azure DevOps
│   ├── access_control_client.py    # controle de acesso/logs/sessões (n8n)
│   ├── document_processor.py       # extração de texto + imagens
│   ├── csv_formatter.py            # exportação CSV
│   └── pdf_report.py               # PDFs (Documentação QA e Relatório de Testes)
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
```

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
N8N_API_KEY = "GERE_UM_VALOR_ALEATORIO_LONGO"
APP_OWNER_USERNAME = "admin"
AZURE_DEVOPS_ORG = "sua-organizacao"

[credentials]
[credentials.usernames]
admin = "$2b$12$...hash-bcrypt-aqui..."
```

> Não existe `AZURE_DEVOPS_PAT` (cada pessoa usa o próprio) nem `cookie_secret` (a sessão não usa mais assinatura local — valida direto no n8n).

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

**Permissões granulares** — `azure_devops` (Passo 7, Gerar via Work Items, Criar Query) e `execution_report` (Relatório de Testes), concedidas individualmente.

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

- **Matriz de Cobertura** nunca é enviada ao Azure DevOps — só existe no PDF quando gerada na mesma sessão, ou reconstruída de forma independente a partir dos Work Items vinculados.
- **Reconciliação com Test Plan Anterior** faz o match por **título apenas** (não tem acesso fácil aos passos detalhados dos Casos já existentes) — revise as sugestões com mais atenção que no fluxo direto.
- **Logs de auditoria e sessões** guardam um histórico limitado (armazenamento via n8n static data, sem banco de dados dedicado).
- Algumas áreas da API do Azure DevOps foram implementadas com base na documentação pública e ajustadas a partir de testes reais — se o formato variar entre organizações/processos, pode precisar de ajuste fino.

## Manutenção

- **Modelos de IA**: os workflows do n8n fixam versões de modelo — provedores mudam/depreciam modelos com frequência.
- **Credenciais do n8n**: o fallback entre provedores cobre a maioria dos casos de expiração, mas vale monitorar os logs de execução do n8n.
