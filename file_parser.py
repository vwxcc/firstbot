import os
import csv
import chardet
import tempfile

import PyPDF2
import docx
import openpyxl

from pptx import Presentation
from PIL import Image


class ParseError(Exception):
    pass


def parse_pdf(path):
    try:
        reader = PyPDF2.PdfReader(path)

        pages = []

        for page in reader.pages:
            text = page.extract_text() or ""

            if text.strip():
                pages.append(text)

        return "\n\n".join(pages).strip()

    except Exception as e:
        raise ParseError(
            f"PDF: {e}"
        )


def parse_docx(path):
    try:
        doc = docx.Document(path)

        result = []

        for paragraph in doc.paragraphs:
            if paragraph.text.strip():
                result.append(paragraph.text)

        for table in doc.tables:
            for row in table.rows:
                result.append(
                    " | ".join(
                        cell.text.strip()
                        for cell in row.cells
                    )
                )

        return "\n".join(result).strip()

    except Exception as e:
        raise ParseError(
            f"DOCX: {e}"
        )


def parse_xlsx(path):
    try:
        workbook = openpyxl.load_workbook(
            path,
            data_only=True,
            read_only=True
        )

        result = []

        for sheet in workbook.worksheets:
            result.append(
                f"=== Лист: {sheet.title} ==="
            )

            for row in sheet.iter_rows(
                values_only=True
            ):
                values = []

                for value in row:
                    if value is None:
                        values.append("")
                    else:
                        values.append(str(value))

                if any(values):
                    result.append(
                        " | ".join(values)
                    )

        workbook.close()

        return "\n".join(result).strip()

    except Exception as e:
        raise ParseError(
            f"XLSX: {e}"
        )


def parse_pptx(path):
    try:
        presentation = Presentation(path)

        result = []

        for index, slide in enumerate(
            presentation.slides,
            start=1
        ):
            result.append(
                f"=== Слайд {index} ==="
            )

            for shape in slide.shapes:
                if hasattr(shape, "text"):
                    text = shape.text.strip()

                    if text:
                        result.append(text)

        return "\n".join(result).strip()

    except Exception as e:
        raise ParseError(
            f"PPTX: {e}"
        )


def parse_csv(path):
    try:
        with open(
            path,
            "rb"
        ) as f:
            raw = f.read()

        encoding = (
            chardet.detect(raw).get("encoding")
            or "utf-8"
        )

        text = raw.decode(
            encoding,
            errors="replace"
        )

        rows = csv.reader(
            text.splitlines()
        )

        result = []

        for row in rows:
            result.append(
                " | ".join(row)
            )

        return "\n".join(result).strip()

    except Exception as e:
        raise ParseError(
            f"CSV: {e}"
        )


def parse_txt(path):
    try:
        with open(
            path,
            "rb"
        ) as f:
            raw = f.read()

        encoding = (
            chardet.detect(raw).get("encoding")
            or "utf-8"
        )

        return raw.decode(
            encoding,
            errors="replace"
        ).strip()

    except Exception as e:
        raise ParseError(
            f"TXT: {e}"
        )


def parse_image(path):
    """
    Для обычных изображений возвращаем специальный объект.
    Само изображение будет отправляться Claude отдельно.
    """

    try:
        image = Image.open(path)

        return {
            "type": "image",
            "path": path,
            "name": os.path.basename(path),
            "text": (
                f"Изображение {image.width}x{image.height}"
            )
        }

    except Exception as e:
        raise ParseError(
            f"IMAGE: {e}"
        )


def parse_heic(path):
    """
    HEIC/HEIF → JPEG.
    Требует Pillow с поддержкой HEIF либо pyheif.
    """

    try:
        import pyheif

        heif = pyheif.read(path)

        image = Image.frombytes(
            heif.mode,
            heif.size,
            heif.data,
            "raw",
            heif.mode,
            heif.stride
        )

        fd, output = tempfile.mkstemp(
            suffix=".jpg"
        )

        os.close(fd)

        image.save(
            output,
            "JPEG",
            quality=90
        )

        return {
            "type": "image",
            "path": output,
            "name": os.path.basename(path),
            "text": "HEIC изображение"
        }

    except Exception as e:
        raise ParseError(
            f"HEIC: {e}"
        )


def parse_file(path, extension):
    ext = extension.lower().lstrip(".")

    if ext == "pdf":
        return parse_pdf(path)

    if ext == "docx":
        return parse_docx(path)

    if ext == "xlsx":
        return parse_xlsx(path)

    if ext == "pptx":
        return parse_pptx(path)

    if ext == "csv":
        return parse_csv(path)

    if ext in {
        "txt",
        "md",
        "log",
        "json",
        "xml",
        "html",
        "css",
        "js",
        "py",
        "java",
        "cpp",
        "c",
        "h",
        "sql"
    }:
        return parse_txt(path)

    if ext in {
        "jpg",
        "jpeg",
        "png",
        "webp"
    }:
        return parse_image(path)

    if ext in {
        "heic",
        "heif"
    }:
        return parse_heic(path)

    if ext in {
        "mp4",
        "mov",
        "m4v",
        "avi",
        "mkv",
        "webm"
    }:
        return {
            "type": "video",
            "path": path,
            "name": os.path.basename(path),
            "text": (
                "Видеофайл. Для полноценного анализа "
                "нужна поддержка video input."
            )
        }

    raise ParseError(
        f"Неподдерживаемый формат: .{ext}"
    )
