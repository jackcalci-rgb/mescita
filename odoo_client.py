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
        self.demo = not self.url
        self._uid = None
        self._demo_stock = {
            1: {"id": 1, "name": "Franciacorta Brut", "qty": 6, "categ": "spumante"},
            2: {"id": 2, "name": "Vermentino di Gallura", "qty": 10, "categ": "bianco_leggero"},
            3: {"id": 3, "name": "Chardonnay barrique", "qty": 4, "categ": "bianco_corposo"},
            4: {"id": 4, "name": "Pinot Nero Alto Adige", "qty": 5, "categ": "rosso_leggero"},
            5: {"id": 5, "name": "Barolo DOCG", "qty": 3, "categ": "rosso_corposo"},
            6: {"id": 6, "name": "Passito di Pantelleria", "qty": 2, "categ": "dolce"},
        }
        # ordini POS finti per la demo (prodotto 105 = "Calice Barolo DOCG")
        self._demo_pos = [
            {"ordine": "Ristorante/0001", "tavolo": "T3", "stato": "paid", "product_id": 105, "qty": 2, "data": "2000-01-01 00:00:00"},
            {"ordine": "Ristorante/0002", "tavolo": "T5", "stato": "draft", "product_id": 105, "qty": 3, "data": "2000-01-01 00:00:00"},
            {"ordine": "Ristorante/0002", "tavolo": "T5", "stato": "draft", "product_id": 101, "qty": 1, "data": "2000-01-01 00:00:00"},
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
        if self.categ_name:
            domain.append(("product_id.categ_id.name", "ilike", self.categ_name))
        quants = self._call("stock.quant", "search_read", domain,
                            fields=["product_id", "quantity"])
        tot = {}
        for q in quants:
            pid, pname = q["product_id"]
            tot.setdefault(pid, {"id": pid, "name": pname, "qty": 0, "categ": None})
            tot[pid]["qty"] += q["quantity"]
        return sorted(tot.values(), key=lambda v: v["name"])

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
            return [{"id": 100 + v["id"], "name": f"Calice {v['name']}"} for v in self._demo_stock.values()]
        return self._call("product.product", "search_read", [("available_in_pos", "=", True)],
                          fields=["id", "name"], order="name")

    def righe_pos(self, product_ids, dal_utc: str):
        """Righe d'ordine POS (anche ordini non ancora pagati) per i prodotti dati.

        dal_utc: 'YYYY-MM-DD HH:MM:SS' in UTC (come le date di Odoo).
        Ritorna [{ordine, tavolo, stato, product_id, qty, data}].
        Nota: un ordine compare qui appena la cassa lo sincronizza con Odoo
        (in modalità ristorante: a ogni invio in cucina / cambio tavolo / pagamento).
        """
        if not product_ids:
            return []
        if self.demo:
            return [dict(r) for r in self._demo_pos if r["product_id"] in product_ids]
        righe = self._call("pos.order.line", "search_read",
                           [("product_id", "in", list(product_ids)), ("create_date", ">=", dal_utc),
                            ("order_id.state", "!=", "cancel")],
                           fields=["order_id", "product_id", "qty", "create_date"])
        ordini = {}
        ids = list({r["order_id"][0] for r in righe})
        if ids:
            campi = ["id", "name", "state"]
            try:
                info = self._call("pos.order", "read", ids, fields=campi + ["table_id"])
            except Exception:  # POS senza modulo ristorante: niente tavoli
                info = self._call("pos.order", "read", ids, fields=campi)
            ordini = {o["id"]: o for o in info}
        out = []
        for r in righe:
            o = ordini.get(r["order_id"][0], {})
            tav = o.get("table_id")
            out.append({"ordine": o.get("name", r["order_id"][1]),
                        "tavolo": tav[1] if tav else None,
                        "stato": o.get("state", "draft"),
                        "product_id": r["product_id"][0],
                        "qty": r["qty"], "data": r["create_date"]})
        return out
