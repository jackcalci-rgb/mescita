"""Categorie di vino e durata massima dopo l'apertura (in giorni)."""

CATEGORIE = {
    "spumante":       {"nome": "Spumanti e Champagne",               "min": 1, "max": 2},
    "bianco_leggero": {"nome": "Bianchi leggeri e rosati",           "min": 2, "max": 3},
    "bianco_corposo": {"nome": "Bianchi corposi / affinati in legno", "min": 2, "max": 3},
    "rosso_leggero":  {"nome": "Rossi leggeri (es. Pinot Nero)",     "min": 1, "max": 3},
    "rosso_corposo":  {"nome": "Rossi corposi e tannici",            "min": 4, "max": 5},
    "dolce":          {"nome": "Dolci, liquorosi, passiti",          "min": 7, "max": 28},
}

CALICI_PER_BOTTIGLIA = 7


def stato_durata(categoria: str, giorni_aperta: int) -> str:
    """ok = entro il minimo, attenzione = tra min e max, scaduta = oltre il max."""
    c = CATEGORIE.get(categoria)
    if not c:
        return "sconosciuta"
    if giorni_aperta > c["max"]:
        return "scaduta"
    if giorni_aperta >= c["min"]:
        return "attenzione"
    return "ok"
