import json
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

RAW = Path("raw/backfill/parkings")
OUT = Path("processed")
OUT.mkdir(exist_ok=True)

# Période officielle du backfill (heure de Singapour)
START = pd.Timestamp("2026-07-16 00:00", tz="Asia/Singapore")
END = pd.Timestamp("2026-10-15 00:00", tz="Asia/Singapore")

out_path = OUT / "parkings_availability.parquet"
writer = None      # écriture progressive du Parquet, fichier par fichier
snapshots = []
errors = 0
out_of_period = 0
duplicates = 0
seen = set()       # horodatages déjà traités

files = sorted(RAW.glob("*.json"))
print(f"{len(files)} fichiers à traiter")

for i, file in enumerate(files, 1):
    try:
        with open(file, encoding="utf-8") as f:
            data = json.load(f)
        item = data["items"][0]
        carparks = item["carpark_data"]
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"Fichier ignoré {file.name} : {e}")
        errors += 1
        continue

    ts = pd.Timestamp(item["timestamp"]).tz_convert("Asia/Singapore")

    if not (START <= ts < END):
        out_of_period += 1
        continue
    if ts in seen:
        duplicates += 1
        continue
    seen.add(ts)

    # Aplatissement : une ligne par parking ET par type de place
    rows = []
    for cp in carparks:
        for info in cp.get("carpark_info", []):
            rows.append({
                "carpark_number": cp.get("carpark_number"),
                "update_datetime": cp.get("update_datetime"),
                "lot_type": info.get("lot_type"),
                "total_lots": info.get("total_lots"),
                "lots_available": info.get("lots_available"),
            })

    df = pd.DataFrame(rows)
    df.insert(0, "timestamp", ts)

    # Les nombres arrivent en texte : conversion en entiers (valeur vide si illisible)
    for col in ["total_lots", "lots_available"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")

    # update_datetime n'a pas de fuseau : on précise que c'est l'heure de Singapour
    df["update_datetime"] = (
        pd.to_datetime(df["update_datetime"], errors="coerce")
        .dt.tz_localize("Asia/Singapore")
    )

    table = pa.Table.from_pandas(df, preserve_index=False)
    if writer is None:
        writer = pq.ParquetWriter(out_path, table.schema)  # écrase l'ancien fichier
    writer.write_table(table)

    # Indicateurs de qualité par relevé
    age_min = (ts - df["update_datetime"]).dt.total_seconds() / 60
    snapshots.append({
        "timestamp": ts,
        "source_file": file.name,
        "n_carparks": df["carpark_number"].nunique(),
        "n_rows": len(df),
        "n_unreadable_numbers": int(df[["total_lots", "lots_available"]].isna().sum().sum()),
        "n_available_gt_total": int((df["lots_available"] > df["total_lots"]).sum()),
        "n_stale_over_60min": int((age_min > 60).sum()),
    })

    if i % 500 == 0:
        print(f"{i}/{len(files)} fichiers traités")

if writer is not None:
    writer.close()

snap = pd.DataFrame(snapshots).sort_values("timestamp")
snap.to_parquet(OUT / "parkings_snapshots.parquet", index=False)

# --- Contrôles qualité ---
print("\n--- Résumé ---")
print(f"Fichiers lus : {len(files)} | illisibles : {errors}")
print(f"Hors période : {out_of_period} | relevés en double écartés : {duplicates}")
print(f"Relevés conservés : {len(snap)}")
print(f"Lignes au total : {snap['n_rows'].sum():,}")
print(f"Parkings par relevé : min {snap['n_carparks'].min()} | max {snap['n_carparks'].max()}")
print(f"Nombres illisibles : {snap['n_unreadable_numbers'].sum():,}")
print(f"Lignes avec places libres > places totales : {snap['n_available_gt_total'].sum():,}")
print(f"Lignes non mises à jour depuis plus d'1 h : {snap['n_stale_over_60min'].sum():,}")
print(f"Période : {snap['timestamp'].min()} -> {snap['timestamp'].max()}")
