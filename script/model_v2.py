from pathlib import Path

import h3
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.inspection import permutation_importance
from sklearn.metrics import mean_absolute_error

PROC = Path("processed")
HORIZON = 2          # prédiction à 2 tranches, soit 30 minutes
SPLIT = pd.Timestamp("2026-09-24 00:00", tz="Asia/Singapore")  # début du test
RAIN_THRESHOLD = 1.0  # tranche "pluvieuse" pour l'évaluation : plus de 1 mm / 15 min
RAIN_WEIGHT = 10.0    # poids des moments pluvieux à l'entraînement

df = pd.read_parquet(PROC / "zone_15min.parquet")
df["n_taxis"] = df["n_taxis"].astype("float")

# --- 1. Pluie dans les zones voisines (anneaux H3) ---
rain_pivot = df.pivot(index="slot", columns="h3_cell", values="rain_mm_15min")
cells = list(rain_pivot.columns)
empty = pd.Series(np.nan, index=rain_pivot.index)
ring1_mean, ring2_max = {}, {}
for c in cells:
    disk1 = set(h3.grid_disk(c, 1))
    ring1 = [n for n in disk1 if n != c and n in rain_pivot.columns]
    ring2 = [n for n in h3.grid_disk(c, 2) if n not in disk1 and n in rain_pivot.columns]
    ring1_mean[c] = rain_pivot[ring1].mean(axis=1) if ring1 else empty
    ring2_max[c] = rain_pivot[ring2].max(axis=1) if ring2 else empty


def to_long(wide, name):
    out = pd.DataFrame(wide)
    out.index.name = "slot"
    return out.reset_index().melt(id_vars="slot", var_name="h3_cell", value_name=name)


df = df.merge(to_long(ring1_mean, "rain_ring1_mean"), on=["slot", "h3_cell"], how="left")
df = df.merge(to_long(ring2_max, "rain_ring2_max"), on=["slot", "h3_cell"], how="left")
df = df.sort_values(["h3_cell", "slot"]).reset_index(drop=True)
g = df.groupby("h3_cell")

# --- 2. Cible : variation relative des taxis libres dans 30 minutes ---
df["target"] = g["n_taxis"].shift(-HORIZON)
df["target_rain"] = g["rain_mm_15min"].shift(-HORIZON)  # évaluation et poids uniquement
df["target_log_change"] = np.log1p(df["target"]) - np.log1p(df["n_taxis"])

# --- 3. Variables explicatives (toutes connues au moment de la prédiction) ---
df["zone_code"] = df["h3_cell"].astype("category").cat.codes
df["tod"] = df["slot"].dt.hour * 4 + df["slot"].dt.minute // 15
df["lag_0"] = df["n_taxis"]
df["lag_15min"] = g["n_taxis"].shift(1)
df["lag_30min"] = g["n_taxis"].shift(2)
df["lag_1h"] = g["n_taxis"].shift(4)
df["trend_15min"] = df["lag_0"] - df["lag_15min"]
df["trend_1h"] = df["lag_0"] - df["lag_1h"]
df["same_time_yesterday"] = g["n_taxis"].shift(96 - HORIZON)
df["same_time_last_week"] = g["n_taxis"].shift(672 - HORIZON)

df["rain_now"] = df["rain_mm_15min"]
df["rain_prev"] = g["rain_mm_15min"].shift(1)
rain_lags = pd.concat([g["rain_mm_15min"].shift(k) for k in range(4)], axis=1)
df["rain_1h"] = rain_lags.sum(axis=1, min_count=1)
df["city_rain_share"] = (df["rain_mm_15min"] > 0).groupby(df["slot"]).transform("mean")
df["rain_ring1_prev"] = g["rain_ring1_mean"].shift(1)
df["rain_ring1_trend"] = df["rain_ring1_mean"] - df["rain_ring1_prev"]

# Minutes écoulées depuis le début de l'averse en cours (-15 s'il ne pleut pas)
raining = df["rain_now"] > 0
start = raining & ~(g["rain_mm_15min"].shift(1) > 0)
run_id = start.astype(int).groupby(df["h3_cell"]).cumsum()
position = df.groupby([df["h3_cell"], run_id]).cumcount()
df["minutes_since_rain_start"] = np.where(raining, position * 15, -15)

BASE_FEATURES = [
    "zone_code", "tod", "day_of_week",
    "lag_0", "lag_15min", "lag_30min", "lag_1h", "trend_15min", "trend_1h",
    "same_time_yesterday", "same_time_last_week",
]
RAIN_FEATURES = [
    "rain_now", "rain_prev", "rain_1h", "city_rain_share", "rain_station_km",
    "rain_ring1_mean", "rain_ring1_prev", "rain_ring1_trend", "rain_ring2_max",
    "minutes_since_rain_start",
]
ALL_FEATURES = BASE_FEATURES + RAIN_FEATURES
V1_FEATURES = [  # variables de la v1, pour la recharger et la comparer
    "zone_code", "tod", "day_of_week", "lag_0", "lag_15min", "lag_30min", "lag_1h",
    "same_time_yesterday", "same_time_last_week",
    "rain_now", "rain_prev", "rain_1h", "city_rain_share", "rain_station_km",
]

# --- 4. Découpage temporel et poids ---
data = df[df["target"].notna() & df["lag_0"].notna()]
train = data[data["slot"] < SPLIT]
test = data[(data["slot"] >= SPLIT) & data["same_time_last_week"].notna()]

rainy_context = ((train["target_rain"] > 0) | (train["rain_1h"] > 0)
                 | (train["rain_ring1_mean"] > 0))
weights = np.where(rainy_context, RAIN_WEIGHT, 1.0)
print(f"Entraînement : {len(train):,} lignes, dont {rainy_context.sum():,} en contexte pluvieux")
print(f"Test : {len(test):,} lignes")

rainy = (test["target_rain"] > RAIN_THRESHOLD).values
onset = ((test["target_rain"] > RAIN_THRESHOLD) & (test["rain_now"] == 0)).values
print(f"Tranches pluvieuses en test : {rainy.sum():,} | dont débuts d'averse : {onset.sum():,}")


def train_model(features):
    model = HistGradientBoostingRegressor(
        loss="squared_error",
        max_iter=300,
        learning_rate=0.1,
        categorical_features=[features.index("zone_code")],
        early_stopping=False,
        random_state=42,
    )
    model.fit(train[features], train["target_log_change"], sample_weight=weights)
    return model


def to_level(model, features, rows):
    """Reconvertit la variation prédite en nombre de taxis."""
    log_change = model.predict(rows[features])
    return np.clip(np.expm1(np.log1p(rows["lag_0"].values) + log_change), 0, None)


print("\nEntraînement de la v2 sans pluie...")
v2_base = train_model(BASE_FEATURES)
print("Entraînement de la v2 avec pluie...")
v2_full = train_model(ALL_FEATURES)
v1 = joblib.load(PROC / "model_hgb.joblib")

# --- 5. Évaluation ---
predictions = {
    "Persistance (comme maintenant)": test["lag_0"].values,
    "Semaine dernière, même heure": test["same_time_last_week"].values,
    "v1 avec pluie": v1.predict(test[V1_FEATURES]),
    "v2 sans pluie": to_level(v2_base, BASE_FEATURES, test),
    "v2 avec pluie": to_level(v2_full, ALL_FEATURES, test),
}

y = test["target"].values
print("\n--- Erreur moyenne absolue (MAE, en taxis par zone) ---")
print(f"{'Méthode':<32} {'Toutes':>8} {'Pluie':>8} {'Début':>8}")
results = {}
for name, pred in predictions.items():
    maes = (mean_absolute_error(y, pred),
            mean_absolute_error(y[rainy], pred[rainy]),
            mean_absolute_error(y[onset], pred[onset]))
    results[name] = maes
    print(f"{name:<32} {maes[0]:>8.2f} {maes[1]:>8.2f} {maes[2]:>8.2f}")


def gain(a, b, i):
    return (1 - results[a][i] / results[b][i]) * 100


print("\nGain de la v2 avec pluie...")
for ref in ["Persistance (comme maintenant)", "v1 avec pluie", "v2 sans pluie"]:
    print(f"  par rapport à {ref:<32}: toutes {gain('v2 avec pluie', ref, 0):+.1f} %"
          f" | pluie {gain('v2 avec pluie', ref, 1):+.1f} %"
          f" | début d'averse {gain('v2 avec pluie', ref, 2):+.1f} %")

# --- 6. Importance des variables de pluie (sur un échantillon riche en pluie) ---
sample = pd.concat([test[rainy], test[~rainy].sample(20_000, random_state=42)])
imp = permutation_importance(
    v2_full, sample[ALL_FEATURES], sample["target_log_change"],
    scoring="neg_mean_absolute_error", n_repeats=3, random_state=42,
)
importance = pd.Series(imp.importances_mean, index=ALL_FEATURES).sort_values(ascending=False)

print("\n--- Importance des variables (échantillon enrichi en pluie, échelle relative) ---")
for feat, value in importance.head(10).items():
    tag = "  <- pluie" if feat in RAIN_FEATURES else ""
    print(f"  {feat:<26} : {value * 100:.2f}{tag}")

joblib.dump(v2_full, PROC / "model_v2.joblib")
print(f"\nModèle enregistré : {PROC / 'model_v2.joblib'}")

# --- 7. Prédictions de la période de test, pour l'application ---
out = test[["slot", "h3_cell", "n_taxis", "target", "rain_now", "rain_ring1_mean"]].copy()
out["pred_30min"] = predictions["v2 avec pluie"].round(1)
out["pred_persistence"] = test["lag_0"].values
out.to_parquet(PROC / "predictions_v2.parquet", index=False)
print(f"Prédictions enregistrées : {PROC / 'predictions_v2.parquet'} ({len(out):,} lignes)")
