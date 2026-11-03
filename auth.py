"""Accesso multi-utente: dipendenti con nome + PIN, cookie di sessione firmato."""
import hashlib
import hmac
import os
import secrets
import time

COOKIE = "mescita_sess"
DURATA = 14 * 24 * 3600  # il login resta valido 14 giorni

SCHEMA = """
CREATE TABLE IF NOT EXISTS utenti (
  id INTEGER PRIMARY KEY, nome TEXT NOT NULL UNIQUE COLLATE NOCASE,
  pin_hash TEXT NOT NULL, salt TEXT NOT NULL,
  ruolo TEXT NOT NULL DEFAULT 'dipendente',      -- dipendente | admin
  attivo INTEGER NOT NULL DEFAULT 1, creato_il TEXT);
CREATE TABLE IF NOT EXISTS impostazioni (chiave TEXT PRIMARY KEY, valore TEXT);
"""


def hash_pin(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 200_000).hex()


def nuovo_utente(c, nome, pin, ruolo="dipendente"):
    salt = secrets.token_hex(16)
    c.execute("INSERT INTO utenti (nome, pin_hash, salt, ruolo, creato_il) VALUES (?,?,?,?,datetime('now'))",
              (nome.strip(), hash_pin(pin, salt), salt, ruolo))


def imposta_pin(c, uid, pin):
    salt = secrets.token_hex(16)
    c.execute("UPDATE utenti SET pin_hash=?, salt=? WHERE id=?", (hash_pin(pin, salt), salt, uid))


def verifica(c, nome, pin):
    u = c.execute("SELECT * FROM utenti WHERE nome=? AND attivo=1", (nome.strip(),)).fetchone()
    if u and hmac.compare_digest(u["pin_hash"], hash_pin(pin, u["salt"])):
        return u
    return None


def init(c):
    c.executescript(SCHEMA)
    if not c.execute("SELECT 1 FROM impostazioni WHERE chiave='segreto'").fetchone():
        c.execute("INSERT INTO impostazioni VALUES ('segreto', ?)", (secrets.token_hex(32),))
    # primo avvio: crea l'amministratore. Nome "admin", PIN = MESCITA_PASSWORD (o "0000").
    if not c.execute("SELECT 1 FROM utenti").fetchone():
        nuovo_utente(c, os.getenv("MESCITA_ADMIN", "admin"), os.getenv("MESCITA_PASSWORD") or "0000", "admin")


def _segreto(c):
    return c.execute("SELECT valore FROM impostazioni WHERE chiave='segreto'").fetchone()[0].encode()


def crea_token(c, uid):
    scad = int(time.time()) + DURATA
    msg = f"{uid}.{scad}"
    firma = hmac.new(_segreto(c), msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}.{firma}"


def utente_da_token(c, token):
    try:
        uid, scad, firma = (token or "").split(".")
        atteso = hmac.new(_segreto(c), f"{uid}.{scad}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(firma, atteso) or int(scad) < time.time():
            return None
        return c.execute("SELECT id, nome, ruolo FROM utenti WHERE id=? AND attivo=1", (int(uid),)).fetchone()
    except Exception:
        return None
