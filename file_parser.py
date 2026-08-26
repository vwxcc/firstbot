# file_parser.py
# Улучшенный модуль извлечения текста из разных типов файлов.
import os
import tempfile
from typing import Optional
import PyPDF2
import docx
import openpyxl
from pptx import Presentation
import csv
import io
import pyheif
from PIL import Image
import chardet

class ParseError(Exception):
    """Исключение, выбрасываемое при ошибке парсинга."""
    pass

def parse_pdf(file_path: str) -> str:
    """Извлекает текст из PDF-файла."""
    try:
        text_parts = []
        with open(file_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        return "\n".join(text_parts).strip()
    except Exception as e:
        raise ParseError(f"Ошибка при чтении PDF: {e}")

def parse_docx(file_path: str) -> str:
    """Извлекает текст из DOCX-файла."""
    try:
        doc = docx.Document(file_path)
        text = "\n".join([para.text for para in doc.paragraphs])
        return text.strip()
    except Exception as e:
        raise ParseError(f"Ошибка при чтении DOCX: {e}")

def parse_pptx(file_path: str) -> str:
    """Извлекает текст из PPTX-файла."""
    try:
        prs = Presentation(file_path)
        texts = []
        for slide in prs.slides:
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text:
                    texts.append(shape.text)
        return "\n".join(texts).strip()
    except Exception as e:
        raise ParseError(f"Ошибка при чтении PPTX: {e}")

def parse_xlsx(file_path: str) -> str:
    """Извлекает текст из XLSX-файла (все ячейки)."""
    try:
        wb = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
        rows = []
        for sheet in wb.worksheets:
            for row in sheet.iter_rows(values_only=True):
                row_text = [str(cell) if cell is not None else "" for cell in row]
                rows.append(" ".join(row_text))
        return "\n".join(rows).strip()
    except Exception as e:
        raise ParseError(f"Ошибка при чтении XLSX: {e}")

def _read_text_file_with_encoding_fallback(path: str) -> str:
    """Пытается определить кодировку и прочитать файл."""
    with open(path, "rb") as bf:
        raw = bf.read()
    # Определяем кодировку
    enc = chardet.detect(raw).get("encoding") or "utf-8"
    try:
        return raw.decode(enc).strip()
    except Exception:
        # Попытка с utf-8 и latin1
        for enc_try in ("utf-8", "cp1251", "latin1"):
            try:
                return raw.decode(enc_try).strip()
            except Exception:
                continue
    # Если всё упало — поднимаем ошибку
    raise ParseError("Не удалось определить кодировку текстового файла.")

def parse_csv(file_path: str) -> str:
    """Извлекает текст из CSV-файла с попыткой определения кодировки."""
    try:
        text = _read_text_file_with_encoding_fallback(file_path)
        # Если файл прочитан целиком, разбиваем через csv.reader
        buf = io.StringIO(text)
        reader = csv.reader(buf)
        lines = []
        for row in reader:
            lines.append(" ".join(row))
        return "\n".join(lines).strip()
    except ParseError:
        raise
    except Exception as e:
        raise ParseError(f"Ошибка при чтении CSV: {e}")

def parse_txt(file_path: str) -> str:
    """Читает обычный текстовый файл с автоопределением кодировки."""
    try:
        return _read_text_file_with_encoding_fallback(file_path)
    except ParseError:
        raise
    except Exception as e:
        raise ParseError(f"Ошибка при чтении TXT: {e}")

def parse_heic(file_path: str, keep_temp: bool = False) -> str:
    """
    Конвертирует HEIC в JPEG, возвращает путь к созданному JPEG.
    Если возникла ошибка — бросает ParseError.
    Caller обязан удалить временный файл после использования.
    """
    try:
        heif_file = pyheif.read(file_path)
        # Попытка создать изображение через PIL
        try:
            image = Image.frombytes(
                heif_file.mode,
                heif_file.size,
                heif_file.data,
                "raw",
                heif_file.mode
            )
        except Exception:
            # fallback: попробовать собрать через Image.frombuffer
            try:
                image = Image.frombuffer(
                    heif_file.mode,
                    heif_file.size,
                    heif_file.data,
                    "raw",
                    heif_file.mode,
                    0,
                    1
                )
            except Exception as e:
                raise ParseError(f"Не удалось создать изображение из HEIC: {e}")

        # Сохраняем как временный JPEG
        temp_jpeg = tempfile.NamedTemporaryFile(delete=False, suffix='.jpg')
        try:
            image.save(temp_jpeg.name, format="JPEG", quality=85)
            return temp_jpeg.name
        finally:
            temp_jpeg.close()
    except ParseError:
        raise
    except Exception as e:
        raise ParseError(f"Ошибка при конвертации HEIC: {e}")

def parse_file(file_path: str, file_extension: Optional[str]) -> str:
    """
    Универсальная функция: вызывает соответствующий парсер и возвращает текст.
    В случае ошибки бросает ParseError.
    Для HEIC возвращает путь к JPEG (caller должен удалить файл).
    """
    if file_extension:
        ext = file_extension.lower()
    else:
        ext = os.path.splitext(file_path)[1].lstrip('.').lower()

    if ext == 'pdf':
        return parse_pdf(file_path)
    elif ext == 'docx':
        return parse_docx(file_path)
    elif ext == 'pptx':
        return parse_pptx(file_path)
    elif ext == 'xlsx' or ext == 'xls':
        return parse_xlsx(file_path)
    elif ext == 'csv':
        return parse_csv(file_path)
    elif ext == 'txt':
        return parse_txt(file_path)
    elif ext == 'heic':
        # Возвращаем путь к jpeg
        return parse_heic(file_path)
    else:
        raise ParseError(f"Файл с расширением {ext} не поддерживается для извлечения текста.")
