from pathlib import Path

import h3
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

PROC = Path("processed")
RES = 7            # résolution H3 : hexagones d'environ 5 km²
SLOT = "15min"     # taille des tranches de temps
N_STATIONS = 3     # nombre de stations de pluie candidates par hexagone

START = pd.Timestamp("2026-07-16 00:00", tz="Asia/Singapore")
END = pd.Timestamp("2026-10-15 00:00", tz="Asia/Singapore")
slots = pd.date_range(START, END, freq=SLOT)  # grille de temps complète


def haversine_km(lat1, lon1, lat2, lon2):
    """Distance en km entre deux points (ou tableaux de points) GPS."""
    lat1, lon1, lat2, lon2 = map(np.radians, [lat1, lon1, lat2, lon2])
    a = (np.sin((lat2 - lat1) / 2) ** 2
         + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2)
    return 6371 * 2 * np.arcsin(np.sqrt(a))


# --- 1. Un seul relevé de taxis par tranche : le plus proche de l'heure de la tranche ---
snap = pd.read_parquet(PROC / "taxis_snapshots.parquet", columns=["timestamp"])
snap["slot"] = snap["timestamp"].dt.round(SLOT)
snap["gap_s"] = (snap["timestamp"] - snap["slot"]).abs().dt.total_seconds()
snap = snap.sort_values("gap_s").drop_duplicates("slot").sort_values("slot")
snap = snap[snap["slot"].isin(slots)]
print(f"Tranches avec un relevé de taxis : {len(snap):,} sur {len(slots):,}")

# --- 2. Comptage des taxis par hexagone et par tranche ---
# Chaque relevé a été écrit comme un bloc (row group) dans le Parquet :
# on lit par paquets de blocs pour ne jamais couper un relevé en deux.
pf = pq.ParquetFile(PROC / "taxis_points.parquet")
n_groups = pf.num_row_groups
counts = []
for first in range(0, n_groups, 500):
    groups = list(range(first, min(first + 500, n_groups)))
    df = pf.read_row_groups(groups).to_pandas()
    df = df.merge(snap[["timestamp", "slot"]], on="timestamp", how="inner")
    df = df.drop_duplicates(["timestamp", "longitude", "latitude"])  # retrait des doublons
    df["h3_cell"] = [
        h3.latlng_to_cell(lat, lon, RES) for lat, lon in zip(df["latitude"], df["longitude"])
    ]
    counts.append(df.groupby(["slot", "h3_cell"]).size().rename("n_taxis").reset_index())
    print(f"  {groups[-1] + 1:,}/{n_groups:,} relevés traités")

taxi = pd.concat(counts).groupby(["slot", "h3_cell"], as_index=False)["n_taxis"].sum()

# --- 3. Référentiel des zones (centre de chaque hexagone) ---
zones = pd.DataFrame({"h3_cell": sorted(taxi["h3_cell"].unique())})
centers = [h3.cell_to_latlng(c) for c in zones["h3_cell"]]
zones["lat"] = [c[0] for c in centers]
zones["lon"] = [c[1] for c in centers]
zones.to_parquet(PROC / "zones.parquet", index=False)
print(f"Zones (hexagones) : {len(zones)}")

# --- 4. Grille complète zones x tranches ---
grid = pd.MultiIndex.from_product([slots, zones["h3_cell"]], names=["slot", "h3_cell"])
table = pd.DataFrame(index=grid).reset_index()
table = table.merge(taxi, on=["slot", "h3_cell"], how="left")
table["taxi_data"] = table["slot"].isin(snap["slot"])
# Relevé présent mais aucun taxi dans la zone : 0. Pas de relevé du tout : vide.
table.loc[table["taxi_data"] & table["n_taxis"].isna(), "n_taxis"] = 0
table["n_taxis"] = table["n_taxis"].astype("Int64")

# --- 5. Pluie par station et par tranche (cumul des 3 relevés de 5 min) ---
rain = pd.read_parquet(PROC / "pluie_releves.parquet")
rain["slot"] = rain["timestamp"].dt.ceil(SLOT)  # 09:05, 09:10, 09:15 -> tranche 09:15
agg = rain.groupby(["slot", "station_id"])["rain_mm"].agg(["sum", "count"]).reset_index()
agg.loc[agg["count"] < 3, "sum"] = np.nan  # tranche incomplète : inutilisable
station_rain = agg.pivot(index="slot", columns="station_id", values="sum").reindex(slots)

# --- 6. Pluie par zone : station la plus proche disponible ---
stations = pd.read_parquet(PROC / "pluie_stations.parquet")
rain_parts = []
for _, z in zones.iterrows():
    dist = haversine_km(z["lat"], z["lon"], stations["latitude"].values,
                        stations["longitude"].values)
    nearest = np.argsort(dist)[:N_STATIONS]

    values = pd.Series(np.nan, index=slots)
    used = pd.Series(None, index=slots, dtype="object")
    used_dist = pd.Series(np.nan, index=slots)
    for k in nearest:
        sid = stations["station_id"].iloc[k]
        if sid not in station_rain.columns:
            continue
        s = station_rain[sid]
        fill = values.isna() & s.notna()
        values[fill] = s[fill]
        used[fill] = sid
        used_dist[fill] = dist[k]

    rain_parts.append(pd.DataFrame({
        "slot": slots,
        "h3_cell": z["h3_cell"],
        "rain_mm_15min": values.values,
        "rain_station": used.values,
        "rain_station_km": used_dist.values.round(2),
    }))

table = table.merge(pd.concat(rain_parts), on=["slot", "h3_cell"], how="left")

# --- 7. Variables de calendrier et sauvegarde ---
table["hour"] = table["slot"].dt.hour
table["day_of_week"] = table["slot"].dt.dayofweek  # 0 = lundi
table = table.sort_values(["slot", "h3_cell"]).reset_index(drop=True)
table.to_parquet(PROC / "zone_15min.parquet", index=False)

# --- Contrôles qualité ---
print("\n--- Résumé ---")
print(f"Lignes : {len(table):,} ({len(slots):,} tranches x {len(zones)} zones)")
print(f"Lignes avec données taxis : {table['taxi_data'].mean():.1%}")
print(f"Lignes avec pluie connue : {table['rain_mm_15min'].notna().mean():.1%}")
print(f"Lignes avec pluie > 0 : {(table['rain_mm_15min'] > 0).mean():.1%}")
print(f"Distance moyenne à la station utilisée : {table['rain_station_km'].mean():.2f} km")
print(f"Distance maximale : {table['rain_station_km'].max():.2f} km")
top = table.groupby("h3_cell")["n_taxis"].mean().sort_values(ascending=False).head(5)
print("Zones avec le plus de taxis libres en moyenne :")
for cell, n in top.items():
    lat, lon = h3.cell_to_latlng(cell)
    print(f"  {cell} ({lat:.4f}, {lon:.4f}) : {n:.1f} taxis")
