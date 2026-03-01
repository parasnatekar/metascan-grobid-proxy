from fastapi import FastAPI, UploadFile, File
import requests
from bs4 import BeautifulSoup

app = FastAPI()

GROBID_URL = "https://cloud.science-miner.com/grobid/api/processFulltextDocument"

@app.get("/")
def root():
    return {"status": "Grobid Proxy Running"}

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.post("/extract")
async def extract_metadata(file: UploadFile = File(...)):

    # forward PDF to public Grobid
    resp = requests.post(
        GROBID_URL,
        files={"input": (file.filename, await file.read(), "application/pdf")},
        timeout=120
    )

    if resp.status_code != 200:
        return {"error": "Grobid extraction failed", "status_code": resp.status_code}

    tei_xml = resp.text
    soup = BeautifulSoup(tei_xml, "xml")

    # title
    title_tag = soup.find("title")
    title = title_tag.text.strip() if title_tag else None

    # abstract
    abstract_tag = soup.find("abstract")
    abstract = abstract_tag.text.strip() if abstract_tag else None

    # doi
    doi_tag = soup.find("idno", {"type": "DOI"})
    doi = doi_tag.text.strip() if doi_tag else None

    # year
    year = None
    date_tag = soup.find("date")
    if date_tag:
        when = date_tag.get("when")
        if when and len(when) >= 4:
            year = when[:4]
        else:
            text = (date_tag.text or "").strip()
            if len(text) >= 4:
                year = text[:4]

    # authors
    authors = []
    for author in soup.find_all("author"):
        pers = author.find("persName")
        if not pers:
            continue
        forename = pers.find("forename")
        surname = pers.find("surname")
        name = ""
        if forename and forename.text:
            name += forename.text.strip() + " "
        if surname and surname.text:
            name += surname.text.strip()
        name = name.strip()
        if name:
            authors.append(name)

    return {
        "title": title,
        "authors": authors,
        "year": year,
        "doi": doi,
        "abstract": abstract
    }
