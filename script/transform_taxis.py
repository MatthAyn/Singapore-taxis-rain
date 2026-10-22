import json
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

RAW = Path("raw/backfill/taxis")
OUT = Path("processed")
OUT.mkdir(exist_ok=True)

# Période officielle du backfill (heure de Singapour)
START = pd.Timestamp("2026-07-16 00:00", tz="Asia/Singapore")
END = pd.Timestamp("2026-10-15 00:00", tz="Asia/Singapore")

points_path = OUT / "taxis_points.parquet"
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
        feature = data["features"][0]
        props = feature["properties"]
        coords = feature["geometry"]["coordinates"]
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"Fichier ignoré {file.name} : {e}")
        errors += 1
        continue

    # Horodatage du relevé, converti explicitement en heure de Singapour
    ts = pd.Timestamp(props["timestamp"]).tz_convert("Asia/Singapore")

    # On ne garde que la période du backfill
    if not (START <= ts < END):
        out_of_period += 1
        continue

    # Chaque relevé une seule fois (l'API renvoie parfois le même pour deux heures demandées)
    if ts in seen:
        duplicates += 1
        continue
    seen.add(ts)

    # Une ligne par taxi. Attention : GeoJSON = [longitude, latitude]
    points = pd.DataFrame(coords, columns=["longitude", "latitude"], dtype="float64")
    points.insert(0, "timestamp", ts)

    table = pa.Table.from_pandas(points, preserve_index=False)
    if writer is None:
        writer = pq.ParquetWriter(points_path, table.schema)  # écrase l'ancien fichier
    writer.write_table(table)

    snapshots.append({
        "timestamp": ts,
        "source_file": file.name,
        "taxi_count": props["taxi_count"],
        "n_points": len(points),
        "n_unique_points": len(points.drop_duplicates(["longitude", "latitude"])),
        "api_status": props.get("api_info", {}).get("status"),
    })

    if i % 500 == 0:
        print(f"{i}/{len(files)} fichiers traités")

if writer is not None:
    writer.close()

snap = pd.DataFrame(snapshots).sort_values("timestamp")
snap.to_parquet(OUT / "taxis_snapshots.parquet", index=False)

# --- Contrôles qualité ---
total_points = snap["n_points"].sum()
mismatch = (snap["taxi_count"] != snap["n_points"]).sum()
dup_share = 1 - snap["n_unique_points"].sum() / total_points

print("\n--- Résumé ---")
print(f"Fichiers lus : {len(files)} | illisibles : {errors}")
print(f"Hors période : {out_of_period} | relevés en double écartés : {duplicates}")
print(f"Relevés conservés : {len(snap)}")
print(f"Points au total : {total_points:,}")
print(f"Relevés où taxi_count != nombre de points : {mismatch}")
print(f"Part moyenne de doublons : {dup_share:.1%}")
print(f"Période : {snap['timestamp'].min()} -> {snap['timestamp'].max()}")
