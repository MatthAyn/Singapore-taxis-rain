from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # génère des images sans ouvrir de fenêtre
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

PROC = Path("processed")
FIG = Path("figures")
FIG.mkdir(exist_ok=True)
MIN_EXPECTED = 3                 # on ignore les cas où l'on attend moins de 3 taxis
WEEKS_WINDOW = 4                 # attendu local : 4 semaines avant et 4 semaines après
MIN_NEIGHBORS = 4                # au moins 4 valeurs sèches pour calculer l'attendu
OFFSETS = list(range(-4, 13))    # de 1 h avant à 3 h après, par pas de 15 min
PLACEBO_SHIFT = 7 * 96           # une semaine en tranches de 15 min
N_BOOTSTRAP = 500
rng = np.random.default_rng(42)  # graine fixe : résultats reproductibles

df = pd.read_parquet(PROC / "zone_15min.parquet")
df["n_taxis"] = df["n_taxis"].astype("float")
df["tod"] = df["slot"].dt.hour * 4 + df["slot"].dt.minute // 15  # quart d'heure du jour (0-95)

# --- 1. Taxis libres dans toute la ville au fil du temps ---
city = df[df["taxi_data"]].groupby("slot")["n_taxis"].sum()
daily = city.resample("D").mean()

fig, ax = plt.subplots(figsize=(12, 4))
ax.plot(city.index, city.values, linewidth=0.3, alpha=0.5, label="Tranches de 15 min")
ax.plot(daily.index, daily.values, linewidth=2, label="Moyenne journalière")
ax.set_title("Taxis libres à Singapour (toute la ville)")
ax.set_ylabel("Nombre de taxis libres")
ax.legend()
fig.tight_layout()
fig.savefig(FIG / "1_taxis_ville.png", dpi=120)
plt.close(fig)

print("--- Taxis libres en moyenne par mois (toute la ville) ---")
for month, value in city.groupby(city.index.month).mean().items():
    print(f"  mois {month} : {value:,.0f}")

# --- 2. Profil journalier : semaine contre week-end ---
prof = city.to_frame("n").reset_index()
prof["hour"] = prof["slot"].dt.hour
prof["weekend"] = prof["slot"].dt.dayofweek >= 5
profile = prof.groupby(["weekend", "hour"])["n"].mean().unstack(0)

fig, ax = plt.subplots(figsize=(10, 4))
ax.plot(profile.index, profile[False], marker="o", label="Semaine")
ax.plot(profile.index, profile[True], marker="o", label="Week-end")
ax.set_title("Taxis libres selon l'heure (moyenne toute la ville)")
ax.set_xlabel("Heure (Singapour)")
ax.set_ylabel("Nombre de taxis libres")
ax.set_xticks(range(0, 24))
ax.legend()
fig.tight_layout()
fig.savefig(FIG / "2_profil_journalier.png", dpi=120)
plt.close(fig)

print("\n--- Heures extrêmes (semaine) ---")
print(f"  le plus de taxis libres : {profile[False].idxmax()} h ({profile[False].max():,.0f})")
print(f"  le moins de taxis libres : {profile[False].idxmin()} h ({profile[False].min():,.0f})")

# --- 3. Attendu local : même zone, même quart d'heure, même jour de la semaine,
#        moyenne des valeurs sèches des 4 semaines avant et après (jour lui-même exclu) ---
df["dry_val"] = df["n_taxis"].where((df["rain_mm_15min"] == 0) & df["taxi_data"])
keys = ["h3_cell", "tod", "day_of_week"]
df = df.sort_values(keys + ["slot"]).reset_index(drop=True)

window = 2 * WEEKS_WINDOW + 1
rolling = df.groupby(keys)["dry_val"].rolling(window, center=True, min_periods=1)
roll_sum = rolling.sum().reset_index(level=keys, drop=True)
roll_cnt = rolling.count().reset_index(level=keys, drop=True)

center_valid = df["dry_val"].notna()
sum_excl = roll_sum - df["dry_val"].fillna(0)   # on retire le jour lui-même
cnt_excl = roll_cnt - center_valid.astype(int)
df["expected"] = (sum_excl / cnt_excl).where(cnt_excl >= MIN_NEIGHBORS)

df = df.sort_values(["h3_cell", "slot"]).reset_index(drop=True)
print(f"\nLignes avec un attendu local : {df['expected'].notna().mean():.1%}")

# Contrôle global : sur les tranches sèches, le ratio doit être proche de 1
dry_check = df[df["dry_val"].notna() & (df["expected"] >= MIN_EXPECTED)]
print(f"Contrôle tranches sèches : ratio {dry_check['n_taxis'].sum() / dry_check['expected'].sum():.3f}")

# --- 4. Effet selon l'intensité de la pluie ---
valid = df[df["taxi_data"] & df["rain_mm_15min"].notna() & (df["expected"] >= MIN_EXPECTED)]
valid = valid.assign(rain_cat=pd.cut(
    valid["rain_mm_15min"], bins=[-0.01, 0, 1, 5, np.inf],
    labels=["Sec", "0 à 1 mm", "1 à 5 mm", "Plus de 5 mm"],
))
by_cat = valid.groupby("rain_cat", observed=True).agg(
    n_taxis=("n_taxis", "sum"), expected=("expected", "sum"), n_lignes=("n_taxis", "size"))
by_cat["ratio"] = by_cat["n_taxis"] / by_cat["expected"]

fig, ax = plt.subplots(figsize=(8, 4.5))
labels = [f"{cat}\n({int(n):,} tranches)" for cat, n in zip(by_cat.index, by_cat["n_lignes"])]
bars = ax.bar(labels, by_cat["ratio"])
for bar, ratio in zip(bars, by_cat["ratio"]):
    text = "référence" if abs(ratio - 1) < 0.005 else f"{(ratio - 1) * 100:+.0f} %"
    ax.text(bar.get_x() + bar.get_width() / 2, ratio + 0.02, text,
            ha="center", fontsize=11, fontweight="bold")
ax.axhline(1, color="grey", linestyle="--", linewidth=1)
ax.set_ylim(0, 1.15)
ax.set_title("Taxis libres selon l'intensité de la pluie (réel / attendu local)")
ax.set_ylabel("Ratio réel / attendu")
fig.tight_layout()
fig.savefig(FIG / "3_pluie_intensite.png", dpi=120)
plt.close(fig)

print("\n--- Effet de l'intensité de la pluie ---")
for cat, row in by_cat.iterrows():
    print(f"  {cat:<13} : ratio {row['ratio']:.2f} ({int(row['n_lignes']):,} tranches)")

# --- 5. Avant, pendant et après le début d'une averse ---
# Début d'averse : au moins 2 mm sur 15 min, alors qu'il ne pleuvait pas juste avant
g = df.groupby("h3_cell")
starts = (df["rain_mm_15min"] >= 2) & (g["rain_mm_15min"].shift(1) == 0)
n_events = int(starts.sum())
print(f"\nDébuts d'averse détectés : {n_events:,}")


def event_matrix(shift_base=0):
    """Matrices averses x décalages des taxis réels (N) et attendus (E)."""
    n_mat = np.full((n_events, len(OFFSETS)), np.nan)
    e_mat = np.full((n_events, len(OFFSETS)), np.nan)
    for j, k in enumerate(OFFSETS):
        n_vals = g["n_taxis"].shift(-(k + shift_base))[starts].values
        e_vals = g["expected"].shift(-(k + shift_base))[starts].values
        ok = ~np.isnan(n_vals) & ~np.isnan(e_vals) & (e_vals >= MIN_EXPECTED)
        n_mat[ok, j] = n_vals[ok]
        e_mat[ok, j] = e_vals[ok]
    return n_mat, e_mat


def ratio_of_sums(n_mat, e_mat):
    return np.nansum(n_mat, axis=0) / np.nansum(e_mat, axis=0)


def bootstrap_band(n_mat, e_mat):
    """Intervalle de confiance à 95 % par rééchantillonnage des averses."""
    idx = rng.integers(0, len(n_mat), size=(N_BOOTSTRAP, len(n_mat)))
    ratios = np.nansum(n_mat[idx], axis=1) / np.nansum(e_mat[idx], axis=1)
    return np.percentile(ratios, [2.5, 97.5], axis=0)


minutes = np.array(OFFSETS) * 15

# Vraies averses
n_real, e_real = event_matrix(0)
real = ratio_of_sums(n_real, e_real)
real_low, real_high = bootstrap_band(n_real, e_real)

# Placebo : même zone, même heure, une semaine plus tôt
n_plac, e_plac = event_matrix(-PLACEBO_SHIFT)
placebo = ratio_of_sums(n_plac, e_plac)
plac_low, plac_high = bootstrap_band(n_plac, e_plac)

fig, ax = plt.subplots(figsize=(11, 4.5))
ax.fill_between(minutes, real_low, real_high, alpha=0.25, label="Intervalle de confiance 95 %")
ax.plot(minutes, real, marker="o", label="Averses réelles")
ax.fill_between(minutes, plac_low, plac_high, color="grey", alpha=0.15)
ax.plot(minutes, placebo, marker=".", color="grey", linestyle=":",
        label="Placebo : même zone et même heure, 7 jours avant")
ax.axvline(0, color="red", linestyle="--", linewidth=1, label="Début de l'averse")
ax.axhline(1, color="black", linestyle="--", linewidth=0.8)
ax.set_xticks(range(-60, 181, 30))
ax.set_title("Taxis libres autour du début d'une averse (réel / attendu local)")
ax.set_xlabel("Minutes par rapport au début de l'averse")
ax.set_ylabel("Ratio réel / attendu")
ax.legend(loc="lower right", fontsize=9)
fig.tight_layout()
fig.savefig(FIG / "4_pluie_evenement.png", dpi=120)
plt.close(fig)

print("\n--- Autour du début d'une averse (ratio [intervalle 95 %] | placebo) ---")
for m, r, lo, hi, p in zip(minutes, real, real_low, real_high, placebo):
    print(f"  {m:>4} min : {r:.2f} [{lo:.2f} - {hi:.2f}] | placebo {p:.2f}")

pre = minutes < 0
print(f"\nAvant l'averse (moyenne de -60 à -15 min) : réel {real[pre].mean():.2f}"
      f" | placebo {placebo[pre].mean():.2f}")
print(f"Placebo sur toute la fenêtre : {placebo.mean():.2f}")
low = int(np.argmin(real))
print(f"Creux : {real[low]:.2f} [{real_low[low]:.2f} - {real_high[low]:.2f}]"
      f" à {minutes[low]} min après le début de l'averse")
print(f"Baisse par rapport à l'heure précédente : {(real[low] / real[pre].mean() - 1) * 100:+.0f} %")

print(f"\nGraphiques enregistrés dans le dossier {FIG}/")
