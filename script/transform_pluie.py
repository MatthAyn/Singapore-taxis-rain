import json
from pathlib import Path

import pandas as pd

RAW = Path("raw/backfill/pluie")
OUT = Path("processed")
OUT.mkdir(exist_ok=True)

# Période officielle du backfill (heure de Singapour)
START = pd.Timestamp("2026-07-16 00:00", tz="Asia/Singapore")
END = pd.Timestamp("2026-10-15 00:00", tz="Asia/Singapore")

stations = {}       # id -> infos de la station
readings = []       # une entrée par station et par horodatage
types_units = set() # pour vérifier que l'unité et le type de mesure sont constants
errors = 0

files = sorted(RAW.glob("*.json"))
print(f"{len(files)} fichiers à traiter")

for file in files:
    try:
        with open(file, encoding="utf-8") as f:
            data = json.load(f)
        block = data["data"]
    except (json.JSONDecodeError, KeyError) as e:
        print(f"Fichier ignoré {file.name} : {e}")
        errors += 1
        continue

    types_units.add((block.get("readingType"), block.get("readingUnit")))

    # Référentiel des stations (on garde la dernière version vue)
    for st in block.get("stations", []):
        stations[st["id"]] = {
            "station_id": st["id"],
            "name": st.get("name"),
            "latitude": st.get("location", {}).get("latitude"),
            "longitude": st.get("location", {}).get("longitude"),
        }

    # Relevés : un bloc par horodatage, une valeur par station
    for bloc in block.get("readings", []):
        ts = bloc["timestamp"]
        for r in bloc.get("data", []):
            readings.append({
                "timestamp": ts,
                "station_id": r.get("stationId"),
                "rain_mm": r.get("value"),
            })

# --- Mise en forme ---
df = pd.DataFrame(readings)
df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_convert("Asia/Singapore")
df["rain_mm"] = pd.to_numeric(df["rain_mm"], errors="coerce")

before = len(df)
df = df[(df["timestamp"] >= START) & (df["timestamp"] < END)]
out_of_period = before - len(df)

before = len(df)
df = df.drop_duplicates(["timestamp", "station_id"])
duplicates = before - len(df)

df = df.sort_values(["timestamp", "station_id"]).reset_index(drop=True)
df.to_parquet(OUT / "pluie_releves.parquet", index=False)

st = pd.DataFrame(stations.values()).sort_values("station_id")
st.to_parquet(OUT / "pluie_stations.parquet", index=False)

# --- Contrôles qualité ---
expected_ts = pd.date_range(START, END, freq="5min", inclusive="left")
present_ts = df["timestamp"].unique()
missing_ts = expected_ts.difference(present_ts)

per_ts = df.groupby("timestamp")["station_id"].nunique()
expected_rows = len(present_ts) * df["station_id"].nunique()

per_station = df.groupby("station_id")["timestamp"].nunique().sort_values()
rainy = (df["rain_mm"] > 0).mean()

print("\n--- Résumé ---")
print(f"Fichiers lus : {len(files)} | illisibles : {errors}")
print(f"Types et unités de mesure rencontrés : {types_units}")
print(f"Hors période : {out_of_period:,} | doublons écartés : {duplicates:,}")
print(f"Relevés conservés : {len(df):,}")
print(f"Stations : {len(st)} dans le référentiel | {df['station_id'].nunique()} avec des relevés")
print(f"Tranches de 5 min : {len(present_ts):,} présentes sur {len(expected_ts):,} attendues")
print(f"Tranches manquantes : {len(missing_ts)}")
if len(missing_ts) > 0:
    print(f"  exemples : {[str(t) for t in missing_ts[:5]]}")
print(f"Stations par tranche : min {per_ts.min()} | max {per_ts.max()}")
print(f"Couples station x tranche absents : {expected_rows - len(df):,}")
print("Stations les moins présentes :")
for sid, n in per_station.head(5).items():
    print(f"  {sid} : {n:,} tranches sur {len(present_ts):,}")
print(f"Valeurs illisibles : {df['rain_mm'].isna().sum()} | négatives : {(df['rain_mm'] < 0).sum()}")
print(f"Part des relevés avec pluie (> 0 mm) : {rainy:.1%}")
print(f"Maximum sur 5 min : {df['rain_mm'].max()} mm")
print(f"Période : {df['timestamp'].min()} -> {df['timestamp'].max()}")
