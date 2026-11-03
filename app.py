"""Mescita a calice: controllo bottiglie aperte vs calici venduti, collegato a Odoo."""
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import auth
from categorie import CALICI_PER_BOTTIGLIA, CATEGORIE, stato_durata
from odoo_client import OdooClient

DB_PATH = os.getenv("MESCITA_DB", os.path.join(os.path.dirname(__file__), "mescita.db"))
odoo = OdooClient()
app = FastAPI(title="Mescita")

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
-- (vecchio) quale prodotto POS "calice" corrisponde a quale vino in bottiglia
CREATE TABLE IF NOT EXISTS abbinamenti (
  calice_product_id INTEGER PRIMARY KEY, calice_nome TEXT NOT NULL, vino_product_id INTEGER NOT NULL);
-- abbinamenti manuali produttore -> bottiglia, usati quando quello automatico
-- (nome produttore contenuto nel nome della bottiglia) non basta.
-- calice_product_id = 0 vale per qualsiasi calice; produttore = '' per qualsiasi produttore.
CREATE TABLE IF NOT EXISTS abbinamenti_produttore (
  calice_product_id INTEGER NOT NULL DEFAULT 0, produttore TEXT NOT NULL DEFAULT '',
  descrizione TEXT NOT NULL, vino_product_id INTEGER NOT NULL,
  PRIMARY KEY (calice_product_id, produttore));
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
    auth.init(c)
    for col, tipo in (("aperta_da", "TEXT"), ("chiusa_da", "TEXT"), ("prezzo", "REAL")):
        if col not in [r["name"] for r in c.execute("PRAGMA table_info(bottiglie)")]:
            c.execute(f"ALTER TABLE bottiglie ADD COLUMN {col} {tipo}")
    if "utente" not in [r["name"] for r in c.execute("PRAGMA table_info(vendite)")]:
        c.execute("ALTER TABLE vendite ADD COLUMN utente TEXT")
    if "scaricata_ts" not in [r["name"] for r in c.execute("PRAGMA table_info(bottiglie)")]:
        c.execute("ALTER TABLE bottiglie ADD COLUMN scaricata_ts TEXT")  # quando Odoo ha confermato lo scarico
    c.execute("""INSERT OR IGNORE INTO abbinamenti_produttore
                 SELECT calice_product_id, '', calice_nome, vino_product_id FROM abbinamenti""")


# ---------------------------------------------------------------- accesso
PUBBLICI = ("/login", "/api/login", "/static/")
SOGLIA_PREZZO = float(os.getenv("MESCITA_SOGLIA_PREZZO", "45"))


@app.middleware("http")
async def richiedi_login(request: Request, call_next):
    path = request.url.path
    if not path.startswith(PUBBLICI):
        with db() as c:
            u = auth.utente_da_token(c, request.cookies.get(auth.COOKIE))
        if not u:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Accesso richiesto"}, status_code=401)
            return RedirectResponse("/login")
        request.state.utente = dict(u)
    return await call_next(request)


def chi(request: Request):
    return request.state.utente["nome"]


def solo_admin(request: Request):
    if request.state.utente["ruolo"] != "admin":
        raise HTTPException(403, "Solo l'amministratore può farlo")


class Login(BaseModel):
    nome: str
    pin: str


@app.post("/api/login")
def login(l: Login, request: Request):
    with db() as c:
        u = auth.verifica(c, l.nome, l.pin)
        if not u:
            raise HTTPException(401, "Nome o PIN sbagliati")
        r = JSONResponse({"nome": u["nome"], "ruolo": u["ruolo"]})
        r.set_cookie(auth.COOKIE, auth.crea_token(c, u["id"]), max_age=auth.DURATA,
                     httponly=True, samesite="lax", secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https")
        return r


@app.post("/api/logout")
def logout():
    r = JSONResponse({"ok": True})
    r.delete_cookie(auth.COOKIE)
    return r


@app.get("/api/me")
def me(request: Request):
    return request.state.utente


@app.get("/api/utenti")
def lista_utenti(request: Request):
    solo_admin(request)
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, nome, ruolo, attivo, creato_il FROM utenti ORDER BY attivo DESC, nome")]


class NuovoUtente(BaseModel):
    nome: str
    pin: str
    ruolo: str = "dipendente"


def _controlla_pin(pin):
    if not re.fullmatch(r"\d{4,8}", pin or ""):
        raise HTTPException(400, "Il PIN deve avere da 4 a 8 cifre")


@app.post("/api/utenti")
def aggiungi_utente(u: NuovoUtente, request: Request):
    solo_admin(request)
    _controlla_pin(u.pin)
    if not u.nome.strip() or u.ruolo not in ("dipendente", "admin"):
        raise HTTPException(400, "Dati non validi")
    with db() as c:
        if c.execute("SELECT 1 FROM utenti WHERE nome=?", (u.nome.strip(),)).fetchone():
            raise HTTPException(400, "Esiste già un utente con questo nome")
        auth.nuovo_utente(c, u.nome, u.pin, u.ruolo)
    return lista_utenti(request)


class ModificaUtente(BaseModel):
    pin: str | None = None
    ruolo: str | None = None
    attivo: bool | None = None


@app.post("/api/utenti/{uid}")
def modifica_utente(uid: int, m: ModificaUtente, request: Request):
    solo_admin(request)
    with db() as c:
        if uid == request.state.utente["id"] and (m.attivo is False or m.ruolo == "dipendente"):
            raise HTTPException(400, "Non puoi disattivare o declassare te stesso")
        if m.pin is not None:
            _controlla_pin(m.pin)
            auth.imposta_pin(c, uid, m.pin)
        if m.ruolo in ("dipendente", "admin"):
            c.execute("UPDATE utenti SET ruolo=? WHERE id=?", (m.ruolo, uid))
        if m.attivo is not None:
            c.execute("UPDATE utenti SET attivo=? WHERE id=?", (int(m.attivo), uid))
    return lista_utenti(request)


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


def sessione_corrente(c):
    """La sessione si apre da sola alla prima operazione della giornata."""
    s = sessione_aperta(c)
    if not s:
        c.execute("INSERT INTO sessioni (data, aperta_il) VALUES (?, ?)",
                  (oggi(), datetime.now().isoformat(timespec="seconds")))
        s = sessione_aperta(c)
    return s


def serializza(b):
    d = dict(b)
    g = giorni_da(b["aperta_il"]) if b["stato"] != "chiusa" else b["giorni_aperta"]
    d["giorni"] = g
    d["durata"] = stato_durata(b["categoria"], g)
    d["max_giorni"] = CATEGORIE.get(b["categoria"], {}).get("max")
    d["categoria_nome"] = CATEGORIE.get(b["categoria"], {}).get("nome", b["categoria"])
    d["faccina_prevista"] = faccina(b["calici"])
    d["costosa"] = bool(d.get("prezzo") and d["prezzo"] > SOGLIA_PREZZO)
    return d


# ---------------------------------------------------------------- API
@app.get("/api/info")
def info():
    return {"demo": odoo.demo, "calici_per_bottiglia": CALICI_PER_BOTTIGLIA, "soglia_prezzo": SOGLIA_PREZZO,
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
    conferma_prezzo: bool = False


@app.post("/api/bottiglie")
def apri_bottiglia(b: NuovaBottiglia, request: Request):
    if b.categoria not in CATEGORIE:
        raise HTTPException(400, "Categoria non valida")
    try:
        prezzo = odoo.prezzi([b.product_id]).get(b.product_id)
    except Exception:
        prezzo = None
    if prezzo and prezzo > SOGLIA_PREZZO and not b.conferma_prezzo:
        # il browser mostra l'avviso e richiama con conferma_prezzo=true
        return JSONResponse({"detail": "conferma_prezzo", "prezzo": prezzo, "soglia": SOGLIA_PREZZO},
                            status_code=409)
    with db() as c:
        sessione_corrente(c)
        c.execute("INSERT OR REPLACE INTO categoria_prodotto VALUES (?, ?)", (b.product_id, b.categoria))
        cur = c.execute("""INSERT INTO bottiglie (product_id, nome, categoria, aperta_il, aperta_ts, aperta_da, prezzo)
                           VALUES (?,?,?,?,?,?,?)""",
                        (b.product_id, b.nome, b.categoria, oggi(), adesso_utc(), chi(request), prezzo))
        return serializza(c.execute("SELECT * FROM bottiglie WHERE id=?", (cur.lastrowid,)).fetchone())


class Delta(BaseModel):
    delta: int = 1


@app.post("/api/bottiglie/{bid}/calice")
def vendi_calice(bid: int, d: Delta, request: Request):
    with db() as c:
        s = sessione_corrente(c)
        b = c.execute("SELECT * FROM bottiglie WHERE id=?", (bid,)).fetchone()
        if not b or b["stato"] == "chiusa":
            raise HTTPException(404, "Bottiglia non trovata")
        nuovi = b["calici"] + d.delta
        if nuovi < 0:
            raise HTTPException(400, "I calici non possono essere negativi")
        c.execute("UPDATE bottiglie SET calici=? WHERE id=?", (nuovi, bid))
        c.execute("INSERT INTO vendite (bottiglia_id, sessione_id, ts, calici, utente) VALUES (?,?,?,?,?)",
                  (bid, s["id"], datetime.now().isoformat(timespec="seconds"), d.delta, chi(request)))
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


@app.post("/api/bottiglie/{bid}/chiudi")
def chiudi_bottiglia(bid: int, request: Request):
    """Bottiglia finita, confermata da una persona: la scarica subito da Odoo e la chiude.
    Se lo scarico su Odoo non riesce, la bottiglia resta aperta e si può riprovare."""
    with db() as c:
        b = c.execute("SELECT * FROM bottiglie WHERE id=?", (bid,)).fetchone()
        if not b or b["stato"] == "chiusa":
            raise HTTPException(404, "Bottiglia non trovata")
        try:
            odoo.scarica_bottiglia(b["product_id"], 1)
        except Exception as e:
            raise HTTPException(502, f"Odoo non ha scaricato la bottiglia, riprova: {e}")
        c.execute("""UPDATE bottiglie SET stato='chiusa', chiusa_il=?, giorni_aperta=?,
                     faccina=?, scaricata_odoo=1, scaricata_ts=?, chiusa_da=? WHERE id=?""",
                  (oggi(), giorni_da(b["aperta_il"]), faccina(b["calici"]),
                   datetime.now().isoformat(timespec="minutes"), chi(request), bid))
        return serializza(c.execute("SELECT * FROM bottiglie WHERE id=?", (bid,)).fetchone())


@app.post("/api/bottiglie/{bid}/sincronizza")
def risincronizza(bid: int):
    """Riprova lo scarico su Odoo di una bottiglia chiusa ma non ancora scaricata."""
    with db() as c:
        b = c.execute("SELECT * FROM bottiglie WHERE id=?", (bid,)).fetchone()
        if not b or b["stato"] != "chiusa" or b["scaricata_odoo"]:
            raise HTTPException(400, "Niente da sincronizzare")
        try:
            odoo.scarica_bottiglia(b["product_id"], 1)
        except Exception as e:
            raise HTTPException(502, f"Odoo non ha scaricato la bottiglia, riprova: {e}")
        c.execute("UPDATE bottiglie SET scaricata_odoo=1, scaricata_ts=? WHERE id=?",
                  (datetime.now().isoformat(timespec="minutes"), bid))
        return serializza(c.execute("SELECT * FROM bottiglie WHERE id=?", (bid,)).fetchone())


@app.post("/api/sessione/chiudi")
def chiudi_sessione():
    with db() as c:
        s = sessione_corrente(c)
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
            if scaricata:
                c.execute("UPDATE bottiglie SET scaricata_ts=? WHERE id=?",
                          (datetime.now().isoformat(timespec="minutes"), b["id"]))
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


def norm(t):
    return " ".join(re.sub(r"[^\w]+", " ", (t or "").lower()).split())


@app.get("/api/abbinamenti")
def get_abbinamenti():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM abbinamenti_produttore ORDER BY descrizione")]


class Abbinamento(BaseModel):
    calice_product_id: int = 0
    produttore: str = ""
    descrizione: str
    vino_product_id: int


@app.post("/api/abbinamenti")
def set_abbinamento(a: Abbinamento):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO abbinamenti_produttore VALUES (?,?,?,?)",
                  (a.calice_product_id, norm(a.produttore), a.descrizione, a.vino_product_id))
    return get_abbinamenti()


@app.delete("/api/abbinamenti")
def del_abbinamento(calice_product_id: int = 0, produttore: str = ""):
    with db() as c:
        c.execute("DELETE FROM abbinamenti_produttore WHERE calice_product_id=? AND produttore=?",
                  (calice_product_id, norm(produttore)))
    return get_abbinamenti()


@app.get("/api/pos/calici")
def pos_calici():
    """Calici battuti in cassa (anche su conti ancora aperti), assegnati alle bottiglie aperte
    tramite l'attributo Produttore del calice, e confrontati con i calici segnati nell'app."""
    with db() as c:
        bott = c.execute("SELECT * FROM bottiglie WHERE stato != 'chiusa'").fetchall()
        abb = {(a["calice_product_id"], a["produttore"]): a["vino_product_id"]
               for a in c.execute("SELECT * FROM abbinamenti_produttore")}
    vini = {}
    for b in bott:
        v = vini.setdefault(b["product_id"], {"vino_product_id": b["product_id"], "nome": b["nome"],
                                              "calici_app": 0, "dal": None, "bottiglie": 0,
                                              "calici_cassa": 0, "calici_aperti": 0})
        v["calici_app"] += b["calici"]
        v["bottiglie"] += 1
        dal = b["aperta_ts"] or f"{b['aperta_il']} 00:00:00"
        v["dal"] = min(v["dal"] or dal, dal)
    righe = []
    if vini:
        calici_ids = {p["id"] for p in odoo.prodotti_pos()} | {k[0] for k in abb if k[0]}
        righe = odoo.righe_pos(calici_ids, min(v["dal"] for v in vini.values()))

    def assegna(r):
        """Ritorna (vino_product_id | None, esito)."""
        pid, prod = r["product_id"], norm(r.get("produttore"))
        for k in ((pid, prod), (0, prod), (pid, "")):
            if k in abb and (k[1] or k[0]):
                return (abb[k], "manuale") if abb[k] in vini else (None, "bottiglia non aperta")
        if not prod:
            return None, "senza produttore"
        cand = [v for v in vini.values() if prod in norm(v["nome"])]
        if len(cand) == 1:
            return cand[0]["vino_product_id"], "automatico"
        return None, "più bottiglie aperte" if cand else "nessuna bottiglia aperta"

    ordini, da_abbinare = {}, {}
    for r in righe:
        vid, esito = assegna(r)
        v = vini.get(vid)
        if v and r["data"] < v["dal"]:
            v, esito = None, "venduto prima dell'apertura"
        if v:
            v["calici_cassa"] += r["qty"]
            if r["stato"] == "draft":
                v["calici_aperti"] += r["qty"]
        else:
            k = (r["product_id"], norm(r.get("produttore")))
            d = da_abbinare.setdefault(k, {"calice_product_id": r["product_id"], "prodotto": r["prodotto"],
                                           "produttore": r.get("produttore") or "", "qty": 0, "esito": esito})
            d["qty"] += r["qty"]
        o = ordini.setdefault(r["ordine"], {"ordine": r["ordine"], "tavolo": r["tavolo"],
                                           "stato": r["stato"], "righe": []})
        o["righe"].append({"calice": r["prodotto"], "produttore": r.get("produttore") or "",
                           "vino": v["nome"] if v else None, "esito": esito, "qty": r["qty"]})
    per_vino = [{**v, "differenza": v["calici_cassa"] - v["calici_app"]} for v in vini.values()]
    return {"per_vino": per_vino, "ordini": sorted(ordini.values(), key=lambda o: o["ordine"]),
            "da_abbinare": list(da_abbinare.values())}


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


@app.get("/login")
def pagina_login():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "login.html"))


@app.get("/")
def home():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))
