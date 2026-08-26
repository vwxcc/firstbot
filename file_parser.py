import os
import tempfile
import PyPDF2
import docx
import openpyxl
from pptx import Presentation
import csv
import chardet
import pyheif
from PIL import Image

class ParseError(Exception):
    pass

def parse_pdf(file_path: str) -> str:
    try:
        with open(file_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            text = []
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text.append(page_text)
            return "\n".join(text).strip()
    except Exception as e:
        raise ParseError(f"PDF: {e}")

def parse_docx(file_path: str) -> str:
    try:
        doc = docx.Document(file_path)
        text = [para.text for para in doc.paragraphs if para.text.strip()]
        return "\n".join(text).strip()
    except Exception as e:
        raise ParseError(f"DOCX: {e}")

def parse_pptx(file_path: str) -> str:
    try:
        prs = Presentation(file_path)
        text = []
        for slide in prs.slides:
            for shape in slide.shapes:
                if hasattr(shape, "text"):
                    text.append(shape.text)
        return "\n".join(text).strip()
    except Exception as e:
        raise ParseError(f"PPTX: {e}")

def parse_xlsx(file_path: str) -> str:
    try:
        wb = openpyxl.load_workbook(file_path, data_only=True)
        text = []
        for sheet in wb.worksheets:
            for row in sheet.iter_rows(values_only=True):
                row_text = [str(cell) if cell is not None else "" for cell in row]
                text.append(" ".join(row_text))
        return "\n".join(text).strip()
    except Exception as e:
        raise ParseError(f"XLSX: {e}")

def parse_csv(file_path: str) -> str:
    try:
        with open(file_path, 'rb') as f:
            raw = f.read()
            enc = chardet.detect(raw)['encoding'] or 'utf-8'
        lines = []
        with open(file_path, 'r', encoding=enc) as f:
            reader = csv.reader(f)
            for row in reader:
                lines.append(" ".join(row))
        return "\n".join(lines).strip()
    except Exception as e:
        raise ParseError(f"CSV: {e}")

def parse_txt(file_path: str) -> str:
    try:
        with open(file_path, 'rb') as f:
            raw = f.read()
            enc = chardet.detect(raw)['encoding'] or 'utf-8'
        with open(file_path, 'r', encoding=enc) as f:
            return f.read().strip()
    except Exception as e:
        raise ParseError(f"TXT: {e}")

def parse_heic(file_path: str) -> str:
    """Конвертирует HEIC в JPEG и возвращает путь к JPEG."""
    try:
        heif_file = pyheif.read(file_path)
        image = Image.frombytes(
            heif_file.mode,
            heif_file.size,
            heif_file.data,
            "raw",
            heif_file.mode,
            heif_file.stride,
        )
        temp_jpeg = tempfile.NamedTemporaryFile(delete=False, suffix='.jpg')
        image.save(temp_jpeg.name, "JPEG")
        return temp_jpeg.name
    except Exception as e:
        raise ParseError(f"HEIC: {e}")

def parse_file(file_path: str, file_extension: str) -> str:
    ext = file_extension.lower()
    if ext == 'pdf':
        return parse_pdf(file_path)
    elif ext == 'docx':
        return parse_docx(file_path)
    elif ext == 'pptx':
        return parse_pptx(file_path)
    elif ext == 'xlsx':
        return parse_xlsx(file_path)
    elif ext == 'csv':
        return parse_csv(file_path)
    elif ext == 'txt':
        return parse_txt(file_path)
    elif ext == 'heic':
        return parse_heic(file_path)
    else:
        raise ParseError(f"Неподдерживаемое расширение: {ext}")
