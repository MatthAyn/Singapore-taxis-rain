from pathlib import Path

import joblib
import matplotlib
matplotlib.use("Agg")  # génère des images sans ouvrir de fenêtre
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.ensemble import HistGradientBoostingRegressor  # noqa: E402
from sklearn.inspection import permutation_importance  # noqa: E402
from sklearn.metrics import mean_absolute_error, mean_squared_error  # noqa: E402

PROC = Path("processed")
FIG = Path("figures")
FIG.mkdir(exist_ok=True)
HORIZON = 2  # prédiction à 2 tranches, soit 30 minutes
SPLIT = pd.Timestamp("2026-09-24 00:00", tz="Asia/Singapore")  # début du test
RAIN_THRESHOLD = 1.0  # tranche "pluvieuse" pour l'évaluation : plus de 1 mm / 15 min

df = pd.read_parquet(PROC / "zone_15min.parquet")
df["n_taxis"] = df["n_taxis"].astype("float")
df = df.sort_values(["h3_cell", "slot"]).reset_index(drop=True)
g = df.groupby("h3_cell")

# --- 1. Cible : taxis libres dans la zone 30 minutes plus tard ---
df["target"] = g["n_taxis"].shift(-HORIZON)
df["target_rain"] = g["rain_mm_15min"].shift(-HORIZON)  # pour l'évaluation uniquement

# --- 2. Variables explicatives (toutes connues au moment de la prédiction) ---
df["zone_code"] = df["h3_cell"].astype("category").cat.codes
df["tod"] = df["slot"].dt.hour * 4 + df["slot"].dt.minute // 15
df["lag_0"] = df["n_taxis"]
df["lag_15min"] = g["n_taxis"].shift(1)
df["lag_30min"] = g["n_taxis"].shift(2)
df["lag_1h"] = g["n_taxis"].shift(4)
# Même heure que la cible, la veille et la semaine précédente
df["same_time_yesterday"] = g["n_taxis"].shift(96 - HORIZON)
df["same_time_last_week"] = g["n_taxis"].shift(672 - HORIZON)

df["rain_now"] = df["rain_mm_15min"]
df["rain_prev"] = g["rain_mm_15min"].shift(1)
rain_lags = pd.concat([g["rain_mm_15min"].shift(k) for k in range(4)], axis=1)
df["rain_1h"] = rain_lags.sum(axis=1, min_count=1)
df["city_rain_share"] = (df["rain_mm_15min"] > 0).groupby(df["slot"]).transform("mean")

BASE_FEATURES = [
    "zone_code", "tod", "day_of_week",
    "lag_0", "lag_15min", "lag_30min", "lag_1h",
    "same_time_yesterday", "same_time_last_week",
]
RAIN_FEATURES = ["rain_now", "rain_prev", "rain_1h", "city_rain_share", "rain_station_km"]
ALL_FEATURES = BASE_FEATURES + RAIN_FEATURES

# --- 3. Découpage temporel ---
data = df[df["target"].notna() & df["lag_0"].notna()]
train = data[data["slot"] < SPLIT]
test = data[(data["slot"] >= SPLIT) & data["same_time_last_week"].notna()]
print(f"Entraînement : {len(train):,} lignes (jusqu'au {SPLIT:%Y-%m-%d})")
print(f"Test : {len(test):,} lignes (à partir du {SPLIT:%Y-%m-%d})")
rainy = test["target_rain"] > RAIN_THRESHOLD
print(f"Dont tranches pluvieuses (> {RAIN_THRESHOLD} mm) : {rainy.sum():,}")


def train_model(features):
    model = HistGradientBoostingRegressor(
        loss="poisson",
        max_iter=300,
        learning_rate=0.1,
        categorical_features=[features.index("zone_code")],
        early_stopping=False,
        random_state=42,
    )
    model.fit(train[features], train["target"])
    return model


print("\nEntraînement du modèle sans pluie...")
model_base = train_model(BASE_FEATURES)
print("Entraînement du modèle avec pluie...")
model_full = train_model(ALL_FEATURES)

# --- 4. Évaluation ---
predictions = {
    "Persistance (comme maintenant)": test["lag_0"].values,
    "Semaine dernière, même heure": test["same_time_last_week"].values,
    "Modèle sans pluie": model_base.predict(test[BASE_FEATURES]),
    "Modèle avec pluie": model_full.predict(test[ALL_FEATURES]),
}

y = test["target"].values
print("\n--- Erreur moyenne absolue (MAE, en taxis par zone) ---")
print(f"{'Méthode':<32} {'Toutes':>8} {'Pluie':>8} {'RMSE':>8}")
results = {}
for name, pred in predictions.items():
    mae_all = mean_absolute_error(y, pred)
    mae_rain = mean_absolute_error(y[rainy.values], pred[rainy.values])
    rmse = np.sqrt(mean_squared_error(y, pred))
    results[name] = (mae_all, mae_rain)
    print(f"{name:<32} {mae_all:>8.2f} {mae_rain:>8.2f} {rmse:>8.2f}")

ref = results["Persistance (comme maintenant)"]
best = results["Modèle avec pluie"]
no_rain = results["Modèle sans pluie"]
print(f"\nGain du modèle sur la persistance : {(1 - best[0] / ref[0]) * 100:.1f} %"
      f" (pendant la pluie : {(1 - best[1] / ref[1]) * 100:.1f} %)")
print(f"Apport de la pluie (modèle avec vs sans) : {(1 - best[0] / no_rain[0]) * 100:.1f} %"
      f" (pendant la pluie : {(1 - best[1] / no_rain[1]) * 100:.1f} %)")

# --- 5. Importance des variables (par permutation, sur un échantillon du test) ---
sample = test.sample(30_000, random_state=42)
imp = permutation_importance(
    model_full, sample[ALL_FEATURES], sample["target"],
    scoring="neg_mean_absolute_error", n_repeats=3, random_state=42,
)
importance = pd.Series(imp.importances_mean, index=ALL_FEATURES).sort_values()

fig, ax = plt.subplots(figsize=(8, 5))
ax.barh(importance.index, importance.values)
ax.set_title("Importance des variables (hausse de l'erreur si on les mélange)")
ax.set_xlabel("Hausse de la MAE (taxis)")
fig.tight_layout()
fig.savefig(FIG / "5_importance_variables.png", dpi=120)
plt.close(fig)

print("\n--- Variables les plus importantes ---")
for feat, value in importance.sort_values(ascending=False).head(8).items():
    print(f"  {feat:<22} : +{value:.2f} taxis d'erreur si on la retire")

# --- 6. Exemple visuel : la zone la plus fournie, sur 3 jours de test ---
top_zone = test.groupby("h3_cell")["target"].mean().idxmax()
zone = test[test["h3_cell"] == top_zone].copy()
zone["pred_full"] = model_full.predict(zone[ALL_FEATURES])
zone["target_time"] = zone["slot"] + pd.Timedelta(minutes=15 * HORIZON)
window = zone[zone["target_time"] < zone["target_time"].min() + pd.Timedelta(days=3)]

fig, ax = plt.subplots(figsize=(12, 4))
ax.plot(window["target_time"], window["target"], label="Réel", linewidth=1.5)
ax.plot(window["target_time"], window["pred_full"], label="Modèle avec pluie", linewidth=1.2)
ax.plot(window["target_time"], window["lag_0"], label="Persistance", linewidth=0.8, alpha=0.6)
ax.set_title(f"Prédiction à 30 min, zone la plus fournie ({top_zone}), 3 jours de test")
ax.set_ylabel("Taxis libres")
ax.legend()
fig.tight_layout()
fig.savefig(FIG / "6_prediction_exemple.png", dpi=120)
plt.close(fig)

joblib.dump(model_full, PROC / "model_hgb.joblib")
print(f"\nModèle enregistré : {PROC / 'model_hgb.joblib'}")
print(f"Graphiques enregistrés dans le dossier {FIG}/")
