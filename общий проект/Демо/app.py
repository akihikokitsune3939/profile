from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import List
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup
from docx import Document
from flask import Flask, render_template, request
from pypdf import PdfReader

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024


@dataclass
class SearchResult:
    title: str
    snippet: str
    link: str
    source: str = ""


def extract_text_from_pdf(file_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(file_bytes))
    return "\n".join((p.extract_text() or "") for p in reader.pages).strip()


def extract_text_from_docx(file_bytes: bytes) -> str:
    doc = Document(io.BytesIO(file_bytes))
    return "\n".join(p.text for p in doc.paragraphs if p.text).strip()


def fetch_url_text(url: str) -> tuple[str, list[str]]:
    warnings: list[str] = []
    if not url:
        return "", warnings
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if not r.ok:
            warnings.append("Не удалось прочитать ссылку.")
            return "", warnings
        soup = BeautifulSoup(r.text, "html.parser")
        return " ".join(soup.stripped_strings)[:12000], warnings
    except Exception:
        warnings.append("Ссылка недоступна.")
        return "", warnings


def extract_input_text(raw_text: str, source_url: str, uploaded_file) -> tuple[str, list[str]]:
    warnings = []
    text = (raw_text or "").strip()

    if uploaded_file and uploaded_file.filename:
        file_bytes = uploaded_file.read()
        name = uploaded_file.filename.lower()
        try:
            if name.endswith(".pdf"):
                text = extract_text_from_pdf(file_bytes)
            elif name.endswith(".docx"):
                text = extract_text_from_docx(file_bytes)
            else:
                warnings.append("Поддерживаются PDF и DOCX.")
        except Exception:
            warnings.append("Не удалось прочитать файл.")

    if not text and source_url:
        text, ws = fetch_url_text(source_url)
        warnings.extend(ws)

    return text, warnings


def normalize_result_link(link: str) -> str:
    if not link:
        return ""
    p = urlparse(link)
    if "duckduckgo.com" in p.netloc and p.path.startswith("/l/"):
        t = parse_qs(p.query).get("uddg", [""])[0]
        return unquote(t) if t else link
    return link


def ddg_search(query: str, limit: int = 8) -> List[SearchResult]:
    if not query:
        return []
    try:
        r = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=12,
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        out: List[SearchResult] = []
        for n in soup.select("div.result")[:limit]:
            a = n.select_one("a.result__a")
            s = n.select_one("a.result__snippet") or n.select_one("div.result__snippet")
            if not a:
                continue
            link = normalize_result_link(a.get("href", ""))
            host = (urlparse(link).netloc or "").lower().replace("www.", "")
            out.append(SearchResult(a.get_text(" ", strip=True), s.get_text(" ", strip=True) if s else "", link, host))
        return out
    except Exception:
        return []


def detect_name(text: str) -> str:
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    for ln in lines[:8]:
        if 2 <= len(ln.split()) <= 4:
            return ln
    m = re.search(r"([A-ZА-Я][a-zа-я]+\s+[A-ZА-Я][a-zа-я]+)", text)
    return m.group(1) if m else ""


def detect_company(text: str) -> str:
    m = re.search(r"(?:ООО|АО|ИП|LLC|Inc|Ltd)\s+[\w\-\"'«»]{2,}", text, flags=re.IGNORECASE)
    return m.group(0) if m else ""


def base_score(text: str, results_count: int, has_entity: bool) -> tuple[int, list[str], list[str]]:
    risk = 55
    good, bad = [], []

    if len(text) > 500:
        risk -= 10
        good.append("Текст достаточно подробный.")
    else:
        risk += 15
        bad.append("Текст короткий.")

    if results_count > 0:
        risk -= min(20, results_count * 2)
        good.append(f"Найдены внешние источники: {results_count}.")
    else:
        risk += 20
        bad.append("Внешние источники не найдены.")

    if has_entity:
        risk -= 8
        good.append("Выделены сущности для проверки.")
    else:
        risk += 8
        bad.append("Сущности не выделены.")

    credibility = max(1, min(100, 101 - max(1, min(100, risk))))
    return credibility, good, bad


def analyze_resume(text: str):
    name = detect_name(text)
    company = detect_company(text)
    q = " ".join([x for x in [name, company, "резюме"] if x]) or text[:120]
    results = ddg_search(q, limit=8)
    score, good, bad = base_score(text, len(results), bool(name or company))
    entities = [x for x in [name, company] if x]
    return score, good, bad, results, entities


def analyze_vacancy(text: str):
    company = detect_company(text)
    q = f"{company} отзывы сотрудников" if company else f"{text[:120]} отзывы сотрудников"
    results = ddg_search(q, limit=10)

    filtered = []
    for r in results:
        t = f"{r.title} {r.snippet}".lower()
        if any(x in t for x in ["значение слова", "википедия", "словарь"]):
            continue
        if "отзывы" in t or "работодатель" in t or "employee" in t:
            filtered.append(r)

    score, good, bad = base_score(text, len(filtered), bool(company))
    entities = [company] if company else []
    if company and filtered:
        good.append("Найдены отзывы сотрудников по компании.")
    elif company:
        bad.append("Отзывы сотрудников по компании найдены слабо.")
    return score, good, bad, filtered, entities


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/resume", methods=["GET", "POST"])
def resume_page():
    context = {"score": None, "truths": [], "doubts": [], "results": [], "warnings": [], "entities": []}
    if request.method == "POST":
        text, warnings = extract_input_text(request.form.get("raw_text", ""), request.form.get("resume_url", ""), request.files.get("resume_file"))
        context["warnings"] = warnings
        if text:
            score, truths, doubts, results, entities = analyze_resume(text)
            context.update({"score": score, "truths": truths, "doubts": doubts, "results": results, "entities": entities})
        else:
            context["warnings"].append("Добавьте текст, файл или ссылку.")
    return render_template("resume.html", **context)


@app.route("/vacancy", methods=["GET", "POST"])
def vacancy_page():
    context = {"score": None, "truths": [], "doubts": [], "results": [], "warnings": [], "entities": []}
    if request.method == "POST":
        text, warnings = extract_input_text(request.form.get("raw_text", ""), request.form.get("vacancy_url", ""), request.files.get("vacancy_file"))
        context["warnings"] = warnings
        if text:
            score, truths, doubts, results, entities = analyze_vacancy(text)
            context.update({"score": score, "truths": truths, "doubts": doubts, "results": results, "entities": entities})
        else:
            context["warnings"].append("Добавьте текст, файл или ссылку.")
    return render_template("vacancy.html", **context)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5051, debug=True)
