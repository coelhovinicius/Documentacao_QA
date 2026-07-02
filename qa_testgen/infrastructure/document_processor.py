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
