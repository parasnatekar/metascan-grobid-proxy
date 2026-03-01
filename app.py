from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse
import requests
from bs4 import BeautifulSoup
import re

app = FastAPI()

# Public GROBID endpoint (ScienceMiner)
GROBID_URL = "https://cloud.science-miner.com/grobid/api/processFulltextDocument"


@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {
        "service": "metascan-grobid-proxy",
        "status": "running",
        "endpoints": ["/healthz", "/extract", "/docs"],
    }


@app.get("/healthz")
def healthz():
    return {"ok": True}


def _clean_text(x: str | None) -> str | None:
    if not x:
        return None
    x = re.sub(r"\s+", " ", x).strip()
    return x or None


def _extract_year(soup: BeautifulSoup) -> str | None:
    # Prefer a date in sourceDesc/biblStruct if present
    # GROBID TEI often includes: <date when="2020"> or <date type="published" when="2020-...">
    for date_tag in soup.find_all("date"):
        when = date_tag.get("when")
        if when and len(when) >= 4 and when[:4].isdigit():
            return when[:4]

        txt = _clean_text(date_tag.get_text())
        if txt:
            m = re.search(r"(19|20)\d{2}", txt)
            if m:
                return m.group(0)

    return None


def _extract_title(soup: BeautifulSoup) -> str | None:
    # Prefer analytic title if present (paper title)
    # TEI: <titleStmt><title> or inside <analytic><title level="a">
    title = None

    # Try most specific patterns first
    candidates = soup.find_all("title")
    for t in candidates:
        # Ignore journal/book titles (level="j" etc) if possible
        level = t.get("level")
        typ = t.get("type")
        txt = _clean_text(t.get_text())
        if not txt:
            continue

        # Best case: analytic article title
        if level == "a":
            return txt

        # Some TEI uses type="main"
        if typ == "main":
            title = txt

    return title or (_clean_text(candidates[0].get_text()) if candidates else None)


def _extract_abstract(soup: BeautifulSoup) -> str | None:
    abs_tag = soup.find("abstract")
    if not abs_tag:
        return None
    return _clean_text(abs_tag.get_text())


def _extract_doi(soup: BeautifulSoup) -> str | None:
    # TEI: <idno type="DOI">10....</idno>
    doi_tag = soup.find("idno", {"type": "DOI"})
    if doi_tag:
        doi = _clean_text(doi_tag.get_text())
        return doi

    # fallback: search anywhere for DOI pattern
    text = soup.get_text(" ", strip=True)
    m = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", text, re.IGNORECASE)
    return m.group(0) if m else None


def _extract_authors(soup: BeautifulSoup) -> list[str]:
    authors = []

    # Prefer authors in sourceDesc/biblStruct/analytic if present
    # TEI: <analytic><author>...</author></analytic>
    analytic = soup.find("analytic")
    author_nodes = analytic.find_all("author") if analytic else soup.find_all("author")

    for author in author_nodes:
        pers = author.find("persName")
        if not pers:
            continue

        # Sometimes there are multiple forenames
        forenames = [f.get_text(strip=True) for f in pers.find_all("forename") if f.get_text(strip=True)]
        surname = pers.find("surname")
        surname_txt = surname.get_text(strip=True) if surname and surname.get_text(strip=True) else ""

        name_parts = []
        if forenames:
            name_parts.append(" ".join(forenames))
        if surname_txt:
            name_parts.append(surname_txt)

        full = _clean_text(" ".join(name_parts))
        if full and full not in authors:
            authors.append(full)

    return authors


@app.post("/extract")
async def extract_metadata(file: UploadFile = File(...)):
    try:
        pdf_bytes = await file.read()
        if not pdf_bytes:
            return JSONResponse({"error": "Empty file uploaded"}, status_code=400)

        # Forward PDF to GROBID
        # NOTE: ScienceMiner uses field name "input"
        resp = requests.post(
            GROBID_URL,
            files={"input": (file.filename or "paper.pdf", pdf_bytes, "application/pdf")},
            timeout=(10, 120),  # connect, read
        )

        if resp.status_code != 200:
            return JSONResponse(
                {
                    "error": "GROBID extraction failed",
                    "status_code": resp.status_code,
                    "details": resp.text[:500],  # keep short
                },
                status_code=502,
            )

        tei_xml = resp.text
        soup = BeautifulSoup(tei_xml, "xml")

        title = _extract_title(soup)
        abstract = _extract_abstract(soup)
        doi = _extract_doi(soup)
        year = _extract_year(soup)
        authors = _extract_authors(soup)

        return {
            "title": title,
            "authors": authors,
            "year": year,
            "doi": doi,
            "abstract": abstract,
        }

    except requests.Timeout:
        return JSONResponse(
            {"error": "Timeout while calling GROBID (try again)"}, status_code=504
        )
    except Exception as e:
        return JSONResponse({"error": "Server error", "details": str(e)}, status_code=500)
