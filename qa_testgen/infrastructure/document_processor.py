import fitz
from docx import Document

class DocumentProcessor:
    @staticmethod
    def extract_plain_text(uploaded_file) -> str:
        ext = uploaded_file.name.split('.')[-1].lower()
        text = ""
        if ext == "pdf":
            data = uploaded_file.read()
            doc = fitz.open(stream=data, filetype="pdf")
            for page in doc:
                text += page.get_text() + "\n"
            doc.close()
        elif ext == "docx":
            doc = Document(uploaded_file)
            for p in doc.paragraphs:
                text += p.text + "\n"
        elif ext == "txt":
            text = uploaded_file.getvalue().decode("utf-8")
        return text.strip()

    @staticmethod
    def extract_plain_text_multi(uploaded_files: list) -> str:
        """
        Extrai e concatena o texto de múltiplos arquivos, mantendo marcadores
        claros de onde cada documento começa/termina — isso ajuda a IA a não
        misturar contexto entre documentos diferentes (ex.: dois anexos que
        descrevem módulos distintos do mesmo sistema).
        """
        parts = []
        for uploaded_file in uploaded_files or []:
            text = DocumentProcessor.extract_plain_text(uploaded_file)
            if not text:
                continue
            parts.append(
                f"===== INÍCIO DO DOCUMENTO: {uploaded_file.name} =====\n"
                f"{text}\n"
                f"===== FIM DO DOCUMENTO: {uploaded_file.name} ====="
            )
        return "\n\n".join(parts).strip()
