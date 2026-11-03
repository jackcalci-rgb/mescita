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
