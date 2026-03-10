from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
import re
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI()

FULLTEXT_URL = "https://cloud.science-miner.com/grobid/api/processFulltextDocument"
HEADER_URL = "https://cloud.science-miner.com/grobid/api/processHeaderDocument"


# ---------- requests session with retries ----------
session = requests.Session()

retry_strategy = Retry(
    total=3,
    connect=3,
    read=3,
    backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=frozenset(["POST", "GET", "HEAD"])
)

adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)
session.mount("http://", adapter)


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


def _clean_text(x):
    if not x:
        return None
    x = re.sub(r"\s+", " ", x).strip()
    return x or None


def _extract_year(soup):
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


def _extract_title(soup):
    title = None
    candidates = soup.find_all("title")

    for t in candidates:
        level = t.get("level")
        typ = t.get("type")
        txt = _clean_text(t.get_text())
        if not txt:
            continue

        if level == "a":
            return txt

        if typ == "main":
            title = txt

    return title or (_clean_text(candidates[0].get_text()) if candidates else None)


def _extract_abstract(soup):
    abs_tag = soup.find("abstract")
    if not abs_tag:
        return None
    return _clean_text(abs_tag.get_text())


def _extract_doi(soup):
    doi_tag = soup.find("idno", {"type": "DOI"})
    if doi_tag:
        return _clean_text(doi_tag.get_text())

    text = soup.get_text(" ", strip=True)
    m = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", text, re.IGNORECASE)
    return m.group(0) if m else None


def _extract_authors(soup):
    authors = []
    analytic = soup.find("analytic")
    author_nodes = analytic.find_all("author") if analytic else soup.find_all("author")

    for author in author_nodes:
        pers = author.find("persName")
        if not pers:
            continue

        forenames = [
            f.get_text(strip=True)
            for f in pers.find_all("forename")
            if f.get_text(strip=True)
        ]
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


def _parse_tei(tei_xml):
    soup = BeautifulSoup(tei_xml, "xml")

    return {
        "title": _extract_title(soup),
        "authors": _extract_authors(soup),
        "year": _extract_year(soup),
        "doi": _extract_doi(soup),
        "abstract": _extract_abstract(soup),
    }


def _call_grobid(url, pdf_bytes, filename):
    return session.post(
        url,
        files={"input": (filename or "paper.pdf", pdf_bytes, "application/pdf")},
        timeout=(15, 180),
        verify=False,
        headers={
            "Connection": "close",
            "User-Agent": "MetaScan-Grobid-Proxy/1.0"
        }
    )


@app.post("/extract")
async def extract_metadata(file: UploadFile = File(...)):
    try:
        pdf_bytes = await file.read()

        if not pdf_bytes:
            return JSONResponse({"error": "Empty file uploaded"}, status_code=400)

        # ---------- Try FULLTEXT first ----------
        try:
            resp = _call_grobid(FULLTEXT_URL, pdf_bytes, file.filename)

            if resp.status_code == 200 and resp.text.strip():
                data = _parse_tei(resp.text)
                data["source"] = "grobid_fulltext"
                return data

        except Exception:
            pass

        # ---------- Fallback to HEADER ----------
        try:
            resp = _call_grobid(HEADER_URL, pdf_bytes, file.filename)

            if resp.status_code == 200 and resp.text.strip():
                data = _parse_tei(resp.text)
                data["source"] = "grobid_header_fallback"
                return data

        except Exception:
            pass

        return JSONResponse(
            {
                "error": "Both GROBID fulltext and header extraction failed",
                "details": "Public GROBID service is likely unstable or unavailable right now."
            },
            status_code=502
        )

    except requests.Timeout:
        return JSONResponse(
            {"error": "Timeout while calling GROBID (try again)"},
            status_code=504
        )
    except Exception as e:
        return JSONResponse(
            {"error": "Server error", "details": str(e)},
            status_code=500
        )
