import json
from collections import Counter
from pathlib import Path

import duckdb

RAW = Path("raw/backfill/parkings")
P = "processed/parkings_availability.parquet"

# --- 1. Fichiers illisibles : que contiennent-ils, et quand ? ---
bad = []
for file in sorted(RAW.glob("*.json")):
    with open(file, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            bad.append((file.name, "JSON invalide"))
            continue
    items = data.get("items")
    if not items or "carpark_data" not in items[0]:
        bad.append((file.name, str(data)[:150]))

print(f"=== {len(bad)} fichiers problématiques ===")
for name, extract in bad[:3]:
    print(f"{name} -> {extract}")

# Répartition par jour (date UTC tirée du nom de fichier)
per_day = Counter(name[:8] for name, _ in bad)
print("\nRépartition par jour (UTC) :")
for day, n in sorted(per_day.items()):
    print(f"  {day} : {n}")

con = duckdb.connect()

# --- 2. Places libres > places totales : quels parkings ? ---
print("\n=== Places libres > places totales ===")
con.sql(f"""
    SELECT carpark_number, lot_type, COUNT(*) AS n_lignes,
           MAX(total_lots) AS total_lots, MAX(lots_available) AS max_libres
    FROM '{P}'
    WHERE lots_available > total_lots
    GROUP BY carpark_number, lot_type
    ORDER BY n_lignes DESC
    LIMIT 10
""").show()

# --- 3. Données non mises à jour depuis plus d'1 h : concentrées ou dispersées ? ---
print("\n=== Données périmées (> 1 h) ===")
con.sql(f"""
    SELECT COUNT(DISTINCT carpark_number) AS n_parkings_concernes,
           COUNT(*) AS n_lignes
    FROM '{P}'
    WHERE timestamp - update_datetime > INTERVAL 1 HOUR
""").show()

con.sql(f"""
    SELECT carpark_number, COUNT(*) AS n_lignes_perimees
    FROM '{P}'
    WHERE timestamp - update_datetime > INTERVAL 1 HOUR
    GROUP BY carpark_number
    ORDER BY n_lignes_perimees DESC
    LIMIT 10
""").show()
