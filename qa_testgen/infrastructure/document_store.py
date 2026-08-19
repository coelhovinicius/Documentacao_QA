"""
Armazenamento de documentos gerados (CSV/PDF) em banco Turso (libSQL) —
feature exclusiva do administrador, para guardar seletivamente a
documentação que valer a pena manter. PDF e CSV de um mesmo fluxo (ex.:
Documentação QA do Passo 6) são salvos com o mesmo "grupo_id", pra
ficarem organizados juntos na hora de listar/baixar depois.
"""
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

try:
    import libsql_client
except ImportError:
    libsql_client = None


class DocumentStoreError(Exception):
    pass


class DocumentStore:
    def __init__(self, database_url: str, auth_token: str):
        self.database_url = database_url
        self.auth_token = auth_token

    @contextmanager
    def _client(self):
        if libsql_client is None:
            raise DocumentStoreError(
                "A biblioteca 'libsql-client' não está instalada — adicione "
                "'libsql-client' ao requirements.txt."
            )
        if not self.database_url or not self.auth_token:
            raise DocumentStoreError(
                "TURSO_DATABASE_URL / TURSO_AUTH_TOKEN não configurados no secrets.toml."
            )
        # A biblioteca converte "libsql://" automaticamente pra "wss://"
        # (WebSocket) — em alguns bancos/regiões do Turso, esse caminho
        # falha com "400 Invalid response status" no handshake. Forçamos
        # "https://" aqui, que usa um transporte HTTP simples (sem
        # WebSocket) pra evitar esse problema — mesmo banco, caminho
        # diferente por baixo dos panos.
        url = self.database_url
        if url.startswith("libsql://"):
            url = "https://" + url[len("libsql://"):]
        # Um client novo por operação (não reaproveita conexão entre
        # reruns do Streamlit) — mais simples e seguro num app com vários
        # usuários/sessões, ao custo de uma conexão nova a cada chamada.
        client = libsql_client.create_client_sync(url=url, auth_token=self.auth_token)
        try:
            yield client
        finally:
            client.close()

    def ensure_schema(self) -> None:
        with self._client() as client:
            client.execute(
                """
                CREATE TABLE IF NOT EXISTS documentos (
                    id TEXT PRIMARY KEY,
                    grupo_id TEXT NOT NULL,
                    fluxo_origem TEXT NOT NULL,
                    nome_projeto TEXT,
                    tipo TEXT NOT NULL,
                    nome_arquivo TEXT NOT NULL,
                    conteudo BLOB NOT NULL,
                    tamanho_bytes INTEGER NOT NULL,
                    criado_em TEXT NOT NULL,
                    criado_por TEXT
                )
                """
            )
            client.execute("CREATE INDEX IF NOT EXISTS idx_documentos_grupo ON documentos(grupo_id)")
            client.execute("CREATE INDEX IF NOT EXISTS idx_documentos_criado_em ON documentos(criado_em)")

    def salvar_grupo(self, fluxo_origem: str, nome_projeto: str, arquivos: list, criado_por: str = "") -> str:
        """
        arquivos: [{"tipo": "csv"|"pdf", "nome_arquivo": str, "conteudo": bytes}, ...]
        Salva todos com o MESMO grupo_id, pra ficarem relacionados/
        organizados juntos depois. Retorna o grupo_id gerado.
        """
        if not arquivos:
            raise DocumentStoreError("Nenhum arquivo pra salvar.")
        grupo_id = str(uuid.uuid4())
        criado_em = datetime.now(timezone.utc).isoformat()
        with self._client() as client:
            for arq in arquivos:
                doc_id = str(uuid.uuid4())
                conteudo = arq["conteudo"]
                client.execute(
                    "INSERT INTO documentos "
                    "(id, grupo_id, fluxo_origem, nome_projeto, tipo, nome_arquivo, conteudo, tamanho_bytes, criado_em, criado_por) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [doc_id, grupo_id, fluxo_origem, nome_projeto, arq["tipo"], arq["nome_arquivo"],
                     conteudo, len(conteudo), criado_em, criado_por],
                )
        return grupo_id

    def listar_grupos(self) -> list:
        """
        Retorna os grupos armazenados, mais recente primeiro. Cada grupo:
        {"grupo_id","fluxo_origem","nome_projeto","criado_em","criado_por",
         "arquivos":[{"id","tipo","nome_arquivo","tamanho_bytes"}]}
        Não traz o conteúdo (BLOB) aqui — isso só é buscado na hora do
        download específico, pra não pesar a listagem.
        """
        with self._client() as client:
            result = client.execute(
                "SELECT id, grupo_id, fluxo_origem, nome_projeto, tipo, nome_arquivo, tamanho_bytes, criado_em, criado_por "
                "FROM documentos ORDER BY criado_em DESC"
            )
            grupos = {}
            ordem = []
            for row in result:
                gid = row["grupo_id"]
                if gid not in grupos:
                    grupos[gid] = {
                        "grupo_id": gid,
                        "fluxo_origem": row["fluxo_origem"],
                        "nome_projeto": row["nome_projeto"],
                        "criado_em": row["criado_em"],
                        "criado_por": row["criado_por"],
                        "arquivos": [],
                    }
                    ordem.append(gid)
                grupos[gid]["arquivos"].append({
                    "id": row["id"],
                    "tipo": row["tipo"],
                    "nome_arquivo": row["nome_arquivo"],
                    "tamanho_bytes": row["tamanho_bytes"],
                })
            return [grupos[gid] for gid in ordem]

    def buscar_conteudo(self, documento_id: str) -> bytes:
        with self._client() as client:
            result = client.execute("SELECT conteudo FROM documentos WHERE id = ?", [documento_id])
            if not result:
                raise DocumentStoreError("Documento não encontrado (pode ter sido excluído).")
            return result[0]["conteudo"]

    def excluir_grupo(self, grupo_id: str) -> None:
        with self._client() as client:
            client.execute("DELETE FROM documentos WHERE grupo_id = ?", [grupo_id])
