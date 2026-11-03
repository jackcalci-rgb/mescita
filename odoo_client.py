"""Collegamento a Odoo via XML-RPC (funziona con Odoo 14-18, Community ed Enterprise).

Se ODOO_URL non è impostato, usa un magazzino finto (modalità demo).
"""
import os
import xmlrpc.client


class OdooClient:
    def __init__(self):
        self.url = os.getenv("ODOO_URL", "").rstrip("/")
        self.db = os.getenv("ODOO_DB", "")
        self.user = os.getenv("ODOO_USER", "")
        self.password = os.getenv("ODOO_API_KEY", "")
        # ID della posizione di magazzino (es. "WH/Stock" del ristorante)
        self.location_id = int(os.getenv("ODOO_LOCATION_ID", "0") or 0)
        # Categoria prodotto Odoo che contiene i vini (opzionale)
        self.categ_name = os.getenv("ODOO_WINE_CATEGORY", "")
        # Categoria prodotto Odoo dei calici venduti al POS (es. "Mescita")
        self.calice_categ = os.getenv("ODOO_CALICE_CATEGORY", "")
        # Attributo del calice che indica il produttore (vale anche per
        # "Produttore Classico" e "Produttore Charmat")
        self.attr_produttore = os.getenv("ODOO_ATTRIBUTO_PRODUTTORE", "Produttore")
        self.demo = not self.url
        self._uid = None
        self._demo_stock = {
            1: {"id": 1, "prezzo": 28, "name": "Franciacorta Brut", "qty": 6, "categ": "spumante"},
            2: {"id": 2, "prezzo": 14, "name": "Vermentino di Gallura", "qty": 10, "categ": "bianco_leggero"},
            3: {"id": 3, "prezzo": 32, "name": "Chardonnay barrique", "qty": 4, "categ": "bianco_corposo"},
            4: {"id": 4, "prezzo": 24, "name": "Pinot Nero Alto Adige", "qty": 5, "categ": "rosso_leggero"},
            5: {"id": 5, "prezzo": 55, "name": "Barolo DOCG", "qty": 3, "categ": "rosso_corposo"},
            6: {"id": 6, "prezzo": 48, "name": "Passito di Pantelleria", "qty": 2, "categ": "dolce"},
        }
        # ordini POS finti per la demo (calici con attributo produttore)
        self._demo_pos = [
            {"ordine": "Ristorante/0001", "tavolo": "T3", "stato": "paid", "product_id": 900, "prodotto": "[CR] Nebbiolo 100ML (Barolo)", "produttore": "Barolo", "qty": 2, "data": "2000-01-01 00:00:00"},
            {"ordine": "Ristorante/0002", "tavolo": "T5", "stato": "draft", "product_id": 900, "prodotto": "[CR] Nebbiolo 100ML (Barolo)", "produttore": "Barolo", "qty": 3, "data": "2000-01-01 00:00:00"},
            {"ordine": "Ristorante/0002", "tavolo": "T5", "stato": "draft", "product_id": 901, "prodotto": "Calice Bollicine", "produttore": "Franciacorta", "qty": 1, "data": "2000-01-01 00:00:00"},
        ]

    # --- connessione -------------------------------------------------------
    def _uid_get(self):
        if self._uid is None:
            common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
            self._uid = common.authenticate(self.db, self.user, self.password, {})
            if not self._uid:
                raise RuntimeError("Login Odoo fallito: controlla utente e API key")
        return self._uid

    def _call(self, model, method, *args, **kw):
        models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", allow_none=True)
        return models.execute_kw(self.db, self._uid_get(), self.password, model, method, list(args), kw)

    # --- filtro categorie -----------------------------------------------
    def _campo_pos_categ(self):
        """Nome del campo categoria POS sul prodotto (cambia tra versioni di Odoo)."""
        if not hasattr(self, "_pos_field"):
            campi = self._call("product.product", "fields_get", attributes=["type"])
            self._pos_field = next((f for f in ("pos_categ_ids", "pos_categ_id") if f in campi), None)
        return self._pos_field

    def _filtro_categorie(self, testo: str, prefisso: str = ""):
        """Dominio Odoo per una o più categorie separate da virgola.

        Ogni nome viene cercato sia tra le categorie prodotto (sottocategorie incluse)
        sia tra le categorie del POS (es. "Vini Bianchi", "Mescita").
        """
        nomi = [n.strip() for n in (testo or "").split(",") if n.strip()]
        if not nomi:
            return []
        pos = self._campo_pos_categ()
        termini = []
        for n in nomi:
            t = [(f"{prefisso}categ_id.complete_name", "ilike", n)]
            if pos:
                t.append((f"{prefisso}{pos}.name", "ilike", n))
            termini.append(t)
        dom = []
        tutti = [x for t in termini for x in t]
        dom += ["|"] * (len(tutti) - 1)
        return dom + tutti

    # --- lettura stock ----------------------------------------------------
    def vini_in_stock(self):
        """Ritorna [{id, name, qty, categ}] dei vini con giacenza > 0."""
        if self.demo:
            return [dict(v) for v in self._demo_stock.values() if v["qty"] > 0]
        domain = [("quantity", ">", 0), ("product_id.type", "in", ["product", "consu"])]
        if self.location_id:
            domain.append(("location_id", "child_of", self.location_id))
        else:
            domain.append(("location_id.usage", "=", "internal"))
        domain += self._filtro_categorie(self.categ_name, "product_id.")
        quants = self._call("stock.quant", "search_read", domain,
                            fields=["product_id", "quantity"])
        tot = {}
        for q in quants:
            pid, pname = q["product_id"]
            tot.setdefault(pid, {"id": pid, "name": pname, "qty": 0, "categ": None})
            tot[pid]["qty"] += q["quantity"]
        prezzi = self.prezzi(list(tot))
        for pid, v in tot.items():
            v["prezzo"] = prezzi.get(pid)
        return sorted(tot.values(), key=lambda v: v["name"])

    def prezzi(self, product_ids):
        """{product_id: costo} dal campo costo del prodotto (standard_price) in Odoo."""
        if self.demo:
            return {i: self._demo_stock[i]["prezzo"] for i in product_ids if i in self._demo_stock}
        if not product_ids:
            return {}
        campo = os.getenv("ODOO_CAMPO_PREZZO", "standard_price")
        return {p["id"]: p.get(campo) for p in
                self._call("product.product", "read", list(product_ids), fields=[campo])}

    # --- scarico a fine sessione -----------------------------------------
    def scarica_bottiglia(self, product_id: int, quante: int = 1):
        """Toglie `quante` bottiglie dall'inventario con una rettifica di inventario."""
        if self.demo:
            self._demo_stock[product_id]["qty"] -= quante
            return True
        domain = [("product_id", "=", product_id), ("quantity", ">", 0)]
        if self.location_id:
            domain.append(("location_id", "child_of", self.location_id))
        else:
            domain.append(("location_id.usage", "=", "internal"))
        quants = self._call("stock.quant", "search_read", domain,
                            fields=["id", "quantity"], limit=1, order="quantity desc")
        if not quants:
            raise RuntimeError(f"Nessuna giacenza trovata per il prodotto {product_id}")
        q = quants[0]
        self._call("stock.quant", "write", [q["id"]], {"inventory_quantity": q["quantity"] - quante})
        self._call("stock.quant", "action_apply_inventory", [q["id"]])
        return True

    # --- POS: calici venduti in tempo reale --------------------------------
    def prodotti_pos(self):
        """Prodotti vendibili al POS (per abbinare 'Calice X' alla bottiglia X)."""
        if self.demo:
            return [{"id": 900, "name": "Calice Rosso"}, {"id": 901, "name": "Calice Bollicine"}]
        domain = [("available_in_pos", "=", True)]
        domain += self._filtro_categorie(self.calice_categ)
        return self._call("product.product", "search_read", domain,
                          fields=["id", "name"], order="name")

    def _produttori_di(self, ptav_ids):
        """{ptav_id: nome valore} solo per gli attributi 'Produttore...'."""
        if not ptav_ids:
            return {}
        vals = self._call("product.template.attribute.value", "read", list(ptav_ids),
                          fields=["name", "attribute_id"])
        return {v["id"]: v["name"] for v in vals
                if v.get("attribute_id") and self.attr_produttore.lower() in v["attribute_id"][1].lower()}

    def righe_pos(self, product_ids, dal_utc: str):
        """Righe d'ordine POS (anche ordini non ancora pagati) per i prodotti dati.

        dal_utc: 'YYYY-MM-DD HH:MM:SS' in UTC (come le date di Odoo).
        Ritorna [{ordine, tavolo, stato, product_id, prodotto, produttore, qty, data}].
        Il produttore arriva dall'attributo "Produttore" / "Produttore Classico" /
        "Produttore Charmat" del calice, sia come variante sia come attributo senza variante.
        Nota: un ordine compare qui appena la cassa lo sincronizza con Odoo
        (in modalità ristorante: a ogni invio in cucina / cambio tavolo / pagamento).
        """
        if not product_ids:
            return []
        if self.demo:
            from datetime import datetime, timezone
            ora = max(dal_utc, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
            return [{**r, "data": ora} for r in self._demo_pos if r["product_id"] in product_ids]
        dom = [("product_id", "in", list(product_ids)), ("create_date", ">=", dal_utc),
               ("order_id.state", "!=", "cancel")]
        campi = ["order_id", "product_id", "qty", "create_date"]
        try:
            righe = self._call("pos.order.line", "search_read", dom, fields=campi + ["attribute_value_ids"])
        except Exception:  # versioni senza attributi sulle righe POS
            righe = self._call("pos.order.line", "search_read", dom, fields=campi)
        # attributi delle varianti (se il produttore crea varianti di prodotto)
        pids = list({r["product_id"][0] for r in righe})
        var_ptav = {}
        if pids:
            for p in self._call("product.product", "read", pids, fields=["product_template_attribute_value_ids"]):
                var_ptav[p["id"]] = p.get("product_template_attribute_value_ids") or []
        tutti = {x for r in righe for x in (r.get("attribute_value_ids") or [])}
        tutti |= {x for v in var_ptav.values() for x in v}
        produttori = self._produttori_di(tutti)

        ordini = {}
        ids = list({r["order_id"][0] for r in righe})
        if ids:
            campi_o = ["id", "name", "state"]
            try:
                info = self._call("pos.order", "read", ids, fields=campi_o + ["table_id"])
            except Exception:  # POS senza modulo ristorante: niente tavoli
                info = self._call("pos.order", "read", ids, fields=campi_o)
            ordini = {o["id"]: o for o in info}
        out = []
        for r in righe:
            o = ordini.get(r["order_id"][0], {})
            tav = o.get("table_id")
            pid = r["product_id"][0]
            prod = next((produttori[x] for x in (r.get("attribute_value_ids") or []) + var_ptav.get(pid, [])
                         if x in produttori), "")
            out.append({"ordine": o.get("name", r["order_id"][1]),
                        "tavolo": tav[1] if tav else None,
                        "stato": o.get("state", "draft"),
                        "product_id": pid, "prodotto": r["product_id"][1],
                        "produttore": prod,
                        "qty": r["qty"], "data": r["create_date"]})
        return out
