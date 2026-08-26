import os
import csv
import chardet
import PyPDF2
import docx
import openpyxl

from pptx import Presentation


class ParseError(Exception):
    pass


def parse_pdf(file_path: str) -> str:

    try:

        with open(file_path, "rb") as f:

            reader = PyPDF2.PdfReader(f)

            pages = []

            for page in reader.pages:

                text = page.extract_text()

                if text:
                    pages.append(text)

            return "\n\n".join(pages).strip()

    except Exception as e:

        raise ParseError(
            f"PDF: {e}"
        )


def parse_docx(file_path: str) -> str:

    try:

        document = docx.Document(file_path)

        parts = []

        # Обычный текст.
        for paragraph in document.paragraphs:

            text = paragraph.text.strip()

            if text:
                parts.append(text)

        # Таблицы.
        for table in document.tables:

            for row in table.rows:

                cells = [
                    cell.text.strip()
                    for cell in row.cells
                ]

                parts.append(
                    " | ".join(cells)
                )

        return "\n".join(parts).strip()

    except Exception as e:

        raise ParseError(
            f"DOCX: {e}"
        )


def parse_pptx(file_path: str) -> str:

    try:

        presentation = Presentation(file_path)

        slides = []

        for slide_number, slide in enumerate(
            presentation.slides,
            start=1
        ):

            parts = [
                f"[Слайд {slide_number}]"
            ]

            for shape in slide.shapes:

                if hasattr(shape, "text"):

                    text = shape.text.strip()

                    if text:
                        parts.append(text)

            slides.append(
                "\n".join(parts)
            )

        return "\n\n".join(slides).strip()

    except Exception as e:

        raise ParseError(
            f"PPTX: {e}"
        )


def parse_xlsx(file_path: str) -> str:

    try:

        workbook = openpyxl.load_workbook(
            file_path,
            data_only=True,
            read_only=True
        )

        sheets = []

        for sheet in workbook.worksheets:

            rows = [
                f"[Лист: {sheet.title}]"
            ]

            for row in sheet.iter_rows(
                values_only=True
            ):

                values = []

                for value in row:

                    if value is None:
                        values.append("")
                    else:
                        values.append(str(value))

                line = " | ".join(values).strip()

                if line:
                    rows.append(line)

            sheets.append(
                "\n".join(rows)
            )

        workbook.close()

        return "\n\n".join(sheets).strip()

    except Exception as e:

        raise ParseError(
            f"XLSX: {e}"
        )


def detect_encoding(file_path: str) -> str:

    try:

        with open(
            file_path,
            "rb"
        ) as f:

            raw = f.read(100000)

        result = chardet.detect(raw)

        encoding = result.get("encoding")

        if encoding:
            return encoding

    except Exception:
        pass

    return "utf-8"


def parse_text(file_path: str) -> str:

    try:

        encoding = detect_encoding(
            file_path
        )

        with open(
            file_path,
            "r",
            encoding=encoding,
            errors="replace"
        ) as f:

            return f.read().strip()

    except Exception as e:

        raise ParseError(
            f"TXT/CSV: {e}"
        )


def parse_csv(file_path: str) -> str:

    try:

        encoding = detect_encoding(
            file_path
        )

        rows = []

        with open(
            file_path,
            "r",
            encoding=encoding,
            errors="replace",
            newline=""
        ) as f:

            reader = csv.reader(f)

            for row in reader:

                rows.append(
                    " | ".join(row)
                )

        return "\n".join(rows).strip()

    except Exception as e:

        raise ParseError(
            f"CSV: {e}"
        )


def parse_file(file_path: str, original_name: str = "") -> str:

    extension = (
        os.path.splitext(
            original_name or file_path
        )[1]
        .lower()
    )

    if not extension:

        extension = (
            os.path.splitext(file_path)[1]
            .lower()
        )

    if extension == ".pdf":
        return parse_pdf(file_path)

    if extension == ".docx":
        return parse_docx(file_path)

    if extension == ".pptx":
        return parse_pptx(file_path)

    if extension in (
        ".xlsx",
        ".xlsm"
    ):
        return parse_xlsx(file_path)

    if extension == ".csv":
        return parse_csv(file_path)

    if extension in (
        ".txt",
        ".md",
        ".py",
        ".json",
        ".xml",
        ".html",
        ".css",
        ".js",
        ".log"
    ):
        return parse_text(file_path)

    raise ParseError(
        f"Неподдерживаемый тип файла: {extension}"
    )
