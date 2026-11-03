"""Mescita a calice: controllo bottiglie aperte vs calici venduti, collegato a Odoo."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from categorie import CALICI_PER_BOTTIGLIA, CATEGORIE, stato_durata
from odoo_client import OdooClient

DB_PATH = os.getenv("MESCITA_DB", os.path.join(os.path.dirname(__file__), "mescita.db"))
odoo = OdooClient()
app = FastAPI(title="Mescita")

# Protezione semplice: se MESCITA_PASSWORD è impostata, il browser chiede utente/password.
_PWD = os.getenv("MESCITA_PASSWORD", "")


@app.middleware("http")
async def basic_auth(request, call_next):
    if _PWD:
        import base64
        from fastapi.responses import Response
        h = request.headers.get("authorization", "")
        ok = False
        if h.startswith("Basic "):
            try:
                ok = base64.b64decode(h[6:]).decode().split(":", 1)[1] == _PWD
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Mescita"'})
    return await call_next(request)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessioni (
  id INTEGER PRIMARY KEY, data TEXT NOT NULL, aperta_il TEXT NOT NULL, chiusa_il TEXT);
CREATE TABLE IF NOT EXISTS bottiglie (
  id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, nome TEXT NOT NULL,
  categoria TEXT NOT NULL, aperta_il TEXT NOT NULL, calici INTEGER NOT NULL DEFAULT 0,
  stato TEXT NOT NULL DEFAULT 'aperta',          -- aperta | finita | chiusa
  chiusa_il TEXT, giorni_aperta INTEGER, faccina TEXT, scaricata_odoo INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS vendite (
  id INTEGER PRIMARY KEY, bottiglia_id INTEGER NOT NULL, sessione_id INTEGER NOT NULL,
  ts TEXT NOT NULL, calici INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS categoria_prodotto (
  product_id INTEGER PRIMARY KEY, categoria TEXT NOT NULL);
-- quale prodotto POS "calice" corrisponde a quale vino in bottiglia
CREATE TABLE IF NOT EXISTS abbinamenti (
  calice_product_id INTEGER PRIMARY KEY, calice_nome TEXT NOT NULL, vino_product_id INTEGER NOT NULL);
"""


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


with db() as c:
    c.executescript(SCHEMA)
    if "aperta_ts" not in [r["name"] for r in c.execute("PRAGMA table_info(bottiglie)")]:
        c.execute("ALTER TABLE bottiglie ADD COLUMN aperta_ts TEXT")  # ora di apertura, UTC


def adesso_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def oggi():
    return os.getenv("MESCITA_OGGI") or date.today().isoformat()  # override per test


def giorni_da(d):
    return (date.fromisoformat(oggi()) - date.fromisoformat(d)).days


def faccina(calici):
    return "sorridente" if calici == CALICI_PER_BOTTIGLIA else "arrabbiata"


def sessione_aperta(c):
    return c.execute("SELECT * FROM sessioni WHERE chiusa_il IS NULL ORDER BY id DESC LIMIT 1").fetchone()


def serializza(b):
    d = dict(b)
    g = giorni_da(b["aperta_il"]) if b["stato"] != "chiusa" else b["giorni_aperta"]
    d["giorni"] = g
    d["durata"] = stato_durata(b["categoria"], g)
    d["max_giorni"] = CATEGORIE.get(b["categoria"], {}).get("max")
    d["categoria_nome"] = CATEGORIE.get(b["categoria"], {}).get("nome", b["categoria"])
    d["faccina_prevista"] = faccina(b["calici"])
    return d


# ---------------------------------------------------------------- API
@app.get("/api/info")
def info():
    return {"demo": odoo.demo, "calici_per_bottiglia": CALICI_PER_BOTTIGLIA,
            "categorie": CATEGORIE, "oggi": oggi()}


@app.get("/api/stock")
def stock():
    vini = odoo.vini_in_stock()
    with db() as c:
        mapp = {r["product_id"]: r["categoria"] for r in c.execute("SELECT * FROM categoria_prodotto")}
    for v in vini:
        v["categoria"] = mapp.get(v["id"]) or v.get("categ")
    return vini


@app.get("/api/sessione")
def get_sessione():
    with db() as c:
        s = sessione_aperta(c)
        return dict(s) if s else None


@app.post("/api/sessione/apri")
def apri_sessione():
    with db() as c:
        if sessione_aperta(c):
            raise HTTPException(400, "C'è già una sessione aperta")
        c.execute("INSERT INTO sessioni (data, aperta_il) VALUES (?, ?)",
                  (oggi(), datetime.now().isoformat(timespec="seconds")))
        return dict(sessione_aperta(c))


@app.get("/api/bottiglie")
def bottiglie_aperte():
    with db() as c:
        rows = c.execute("SELECT * FROM bottiglie WHERE stato != 'chiusa' ORDER BY aperta_il, id").fetchall()
        return [serializza(r) for r in rows]


class NuovaBottiglia(BaseModel):
    product_id: int
    nome: str
    categoria: str


@app.post("/api/bottiglie")
def apri_bottiglia(b: NuovaBottiglia):
    if b.categoria not in CATEGORIE:
        raise HTTPException(400, "Categoria non valida")
    with db() as c:
        if not sessione_aperta(c):
            raise HTTPException(400, "Apri prima la sessione del giorno")
        c.execute("INSERT OR REPLACE INTO categoria_prodotto VALUES (?, ?)", (b.product_id, b.categoria))
        cur = c.execute("INSERT INTO bottiglie (product_id, nome, categoria, aperta_il, aperta_ts) VALUES (?,?,?,?,?)",
                        (b.product_id, b.nome, b.categoria, oggi(), adesso_utc()))
        return serializza(c.execute("SELECT * FROM bottiglie WHERE id=?", (cur.lastrowid,)).fetchone())


class Delta(BaseModel):
    delta: int = 1


@app.post("/api/bottiglie/{bid}/calice")
def vendi_calice(bid: int, d: Delta):
    with db() as c:
        s = sessione_aperta(c)
        if not s:
            raise HTTPException(400, "Nessuna sessione aperta")
        b = c.execute("SELECT * FROM bottiglie WHERE id=?", (bid,)).fetchone()
        if not b or b["stato"] == "chiusa":
            raise HTTPException(404, "Bottiglia non trovata")
        nuovi = b["calici"] + d.delta
        if nuovi < 0:
            raise HTTPException(400, "I calici non possono essere negativi")
        c.execute("UPDATE bottiglie SET calici=? WHERE id=?", (nuovi, bid))
        c.execute("INSERT INTO vendite (bottiglia_id, sessione_id, ts, calici) VALUES (?,?,?,?)",
                  (bid, s["id"], datetime.now().isoformat(timespec="seconds"), d.delta))
        return serializza(c.execute("SELECT * FROM bottiglie WHERE id=?", (bid,)).fetchone())


class Finita(BaseModel):
    finita: bool = True


@app.post("/api/bottiglie/{bid}/finita")
def segna_finita(bid: int, f: Finita):
    """Segna la bottiglia come vuota. Lo scarico da Odoo avviene solo con 'Chiudi sessione'."""
    with db() as c:
        b = c.execute("SELECT * FROM bottiglie WHERE id=?", (bid,)).fetchone()
        if not b or b["stato"] == "chiusa":
            raise HTTPException(404, "Bottiglia non trovata")
        c.execute("UPDATE bottiglie SET stato=? WHERE id=?", ("finita" if f.finita else "aperta", bid))
        return serializza(c.execute("SELECT * FROM bottiglie WHERE id=?", (bid,)).fetchone())


@app.post("/api/sessione/chiudi")
def chiudi_sessione():
    with db() as c:
        s = sessione_aperta(c)
        if not s:
            raise HTTPException(400, "Nessuna sessione aperta")
        riepilogo = {"finite": [], "restano_aperte": [], "errori": []}
        for b in c.execute("SELECT * FROM bottiglie WHERE stato='finita'").fetchall():
            try:
                odoo.scarica_bottiglia(b["product_id"], 1)
                scaricata = 1
            except Exception as e:  # la bottiglia si chiude comunque, ma segnaliamo l'errore
                scaricata = 0
                riepilogo["errori"].append(f"{b['nome']}: {e}")
            g = giorni_da(b["aperta_il"])
            c.execute("""UPDATE bottiglie SET stato='chiusa', chiusa_il=?, giorni_aperta=?,
                         faccina=?, scaricata_odoo=? WHERE id=?""",
                      (oggi(), g, faccina(b["calici"]), scaricata, b["id"]))
            riepilogo["finite"].append(serializza(c.execute("SELECT * FROM bottiglie WHERE id=?", (b["id"],)).fetchone()))
        for b in c.execute("SELECT * FROM bottiglie WHERE stato='aperta'").fetchall():
            riepilogo["restano_aperte"].append(serializza(b))
        c.execute("UPDATE sessioni SET chiusa_il=? WHERE id=?",
                  (datetime.now().isoformat(timespec="seconds"), s["id"]))
        riepilogo["calici_sessione"] = c.execute(
            "SELECT COALESCE(SUM(calici),0) FROM vendite WHERE sessione_id=?", (s["id"],)).fetchone()[0]
        return riepilogo


@app.get("/api/storico")
def storico(limit: int = 100):
    with db() as c:
        rows = c.execute("SELECT * FROM bottiglie WHERE stato='chiusa' ORDER BY chiusa_il DESC, id DESC LIMIT ?",
                         (limit,)).fetchall()
        return [serializza(r) for r in rows]


# ---------------------------------------------------------------- POS in tempo reale
@app.get("/api/pos/prodotti")
def pos_prodotti():
    return odoo.prodotti_pos()


@app.get("/api/abbinamenti")
def get_abbinamenti():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM abbinamenti ORDER BY calice_nome")]


class Abbinamento(BaseModel):
    calice_product_id: int
    calice_nome: str
    vino_product_id: int


@app.post("/api/abbinamenti")
def set_abbinamento(a: Abbinamento):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO abbinamenti VALUES (?,?,?)",
                  (a.calice_product_id, a.calice_nome, a.vino_product_id))
    return get_abbinamenti()


@app.delete("/api/abbinamenti/{calice_product_id}")
def del_abbinamento(calice_product_id: int):
    with db() as c:
        c.execute("DELETE FROM abbinamenti WHERE calice_product_id=?", (calice_product_id,))
    return get_abbinamenti()


@app.get("/api/pos/calici")
def pos_calici():
    """Calici battuti in cassa (anche su conti ancora aperti) per i vini con bottiglia aperta,
    confrontati con i calici segnati nell'app."""
    with db() as c:
        bott = c.execute("SELECT * FROM bottiglie WHERE stato != 'chiusa'").fetchall()
        abb = c.execute("SELECT * FROM abbinamenti").fetchall()
    calice_di = {}  # vino -> [calice ids]
    nome_calice = {}
    for a in abb:
        calice_di.setdefault(a["vino_product_id"], []).append(a["calice_product_id"])
        nome_calice[a["calice_product_id"]] = a["calice_nome"]
    per_vino, tutte = [], []
    vini = {}
    for b in bott:
        v = vini.setdefault(b["product_id"], {"vino_product_id": b["product_id"], "nome": b["nome"],
                                              "calici_app": 0, "dal": None, "bottiglie": 0})
        v["calici_app"] += b["calici"]
        v["bottiglie"] += 1
        dal = b["aperta_ts"] or f"{b['aperta_il']} 00:00:00"
        v["dal"] = min(v["dal"] or dal, dal)
    for v in vini.values():
        ids = calice_di.get(v["vino_product_id"], [])
        righe = odoo.righe_pos(ids, v["dal"]) if ids else []
        for r in righe:
            r["calice"] = nome_calice.get(r["product_id"], "")
            r["vino"] = v["nome"]
        tutte += righe
        cassa = sum(r["qty"] for r in righe)
        per_vino.append({**v, "abbinato": bool(ids), "calici_cassa": cassa,
                         "calici_aperti": sum(r["qty"] for r in righe if r["stato"] == "draft"),
                         "differenza": cassa - v["calici_app"]})
    ordini = {}
    for r in tutte:
        o = ordini.setdefault(r["ordine"], {"ordine": r["ordine"], "tavolo": r["tavolo"],
                                           "stato": r["stato"], "righe": []})
        o["righe"].append({"calice": r["calice"], "vino": r["vino"], "qty": r["qty"]})
    return {"per_vino": per_vino, "ordini": sorted(ordini.values(), key=lambda o: o["ordine"])}


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


@app.get("/")
def home():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))
