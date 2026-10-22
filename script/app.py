from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pydeck as pdk
import streamlit as st

PROC = Path("processed")
VIEWS = ["Taxis libres maintenant", "Prédiction dans 30 min", "Variation prévue", "Pluie actuelle"]
TIMES = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 15, 30, 45)]
PLACEHOLDER = "Choisir..."

st.set_page_config(page_title="Taxis libres à Singapour", layout="wide")


@st.cache_data
def load_predictions():
    df = pd.read_parquet(PROC / "predictions_v2.parquet")
    df["slot_local"] = df["slot"].dt.tz_localize(None)  # heure de Singapour, sans fuseau
    return df


@st.cache_data
def rainy_moments(df, n=15, min_gap_hours=2):
    """Les moments où le plus de zones ont plus de 1 mm, un seul par averse."""
    by_slot = pd.DataFrame({
        "zones": (df["rain_now"] > 1).groupby(df["slot_local"]).sum(),
        "total": df.groupby("slot_local")["rain_now"].sum(),
    })
    by_slot = by_slot[by_slot["zones"] > 0].sort_values(["zones", "total"], ascending=False)
    chosen = []
    for moment, row in by_slot.iterrows():
        if all(abs(moment - c) >= pd.Timedelta(hours=min_gap_hours) for c, _ in chosen):
            chosen.append((moment, int(row["zones"])))
        if len(chosen) == n:
            break
    return {f"{m:%d/%m à %H:%M} ({z} zones sous la pluie)": m for m, z in chosen}


def rgba(x, low, high):
    """Interpole entre deux couleurs ; l'opacité augmente avec la valeur (0 à 1)."""
    x = np.clip(x, 0, 1)
    rgb = np.array(low) + (np.array(high) - np.array(low)) * x[:, None]
    alpha = 25 + 205 * x
    return np.column_stack([rgb, alpha]).astype(int)


def sequential_colors(values, cap, high=(0, 200, 255)):
    x = values.fillna(0).to_numpy(float) / cap
    return rgba(x, low=(60, 90, 140), high=high)


def diverging_colors(values, cap):
    x = np.clip(values.fillna(0).to_numpy(float) / cap, -1, 1)
    out = rgba(np.abs(x), low=(200, 200, 200), high=(255, 70, 70))       # baisse : rouge
    rise = rgba(np.abs(x), low=(200, 200, 200), high=(60, 220, 120))     # hausse : vert
    out[x > 0] = rise[x > 0]
    return out


def go_to_rainy_moment():
    choice = st.session_state["rainy_choice"]
    if choice != PLACEHOLDER:
        moment = rainy[choice]
        st.session_state["day"] = moment.date()
        st.session_state["time"] = f"{moment:%H:%M}"
        st.session_state["view"] = "Pluie actuelle"


df = load_predictions()
rainy = rainy_moments(df)
days = sorted(df["slot_local"].dt.date.unique())

# Valeurs par défaut au premier chargement
st.session_state.setdefault("day", days[0])
st.session_state.setdefault("time", "18:00")
st.session_state.setdefault("view", VIEWS[0])

# --- Barre latérale ---
st.sidebar.title("Paramètres")
st.sidebar.selectbox(
    "Aller à un moment pluvieux", [PLACEHOLDER] + list(rainy.keys()),
    key="rainy_choice", on_change=go_to_rainy_moment,
)
day = st.sidebar.date_input("Jour", min_value=days[0], max_value=days[-1], key="day")
time_str = st.sidebar.select_slider("Heure (Singapour)", options=TIMES, key="time")
view = st.sidebar.radio("Afficher sur la carte", VIEWS, key="view")
st.sidebar.caption("Période de test du modèle : du 24 septembre au 14 octobre 2026.")

moment = pd.Timestamp(f"{day} {time_str}")
frame = df[df["slot_local"] == moment].copy()

st.title("Taxis libres à Singapour")
st.caption(f"{moment:%d/%m/%Y à %H:%M} (heure de Singapour) — prédiction à 30 minutes")

if frame.empty:
    st.warning("Aucune donnée pour ce moment (trou dans la source). Choisis une autre heure.")
    st.stop()

# --- Indicateurs clés ---
col1, col2, col3, col4 = st.columns(4)
col1.metric("Taxis libres maintenant", f"{frame['n_taxis'].sum():,.0f}")
col2.metric("Prévus dans 30 min", f"{frame['pred_30min'].sum():,.0f}")
col3.metric("Réels dans 30 min", f"{frame['target'].sum():,.0f}")
col4.metric("Zones sous la pluie", f"{(frame['rain_now'] > 0).sum()} / {len(frame)}")

# --- Couleurs selon l'affichage choisi ---
frame["variation"] = frame["pred_30min"] - frame["n_taxis"]
if view == "Taxis libres maintenant":
    colors = sequential_colors(frame["n_taxis"], cap=60)
elif view == "Prédiction dans 30 min":
    colors = sequential_colors(frame["pred_30min"], cap=60)
elif view == "Variation prévue":
    colors = diverging_colors(frame["variation"], cap=10)
else:
    colors = sequential_colors(frame["rain_now"], cap=5, high=(150, 120, 255))
frame[["r", "g", "b", "a"]] = colors

# Textes de l'infobulle (déjà formatés)
frame["t_now"] = frame["n_taxis"].map("{:.0f}".format)
frame["t_pred"] = frame["pred_30min"].map("{:.1f}".format)
frame["t_real"] = frame["target"].map("{:.0f}".format)
frame["t_rain"] = frame["rain_now"].map(lambda v: "inconnue" if pd.isna(v) else f"{v:.1f} mm")

layer = pdk.Layer(
    "H3HexagonLayer",
    data=frame[["h3_cell", "r", "g", "b", "a", "t_now", "t_pred", "t_real", "t_rain"]],
    get_hexagon="h3_cell",
    get_fill_color="[r, g, b, a]",
    get_line_color=[255, 255, 255, 60],
    line_width_min_pixels=1,
    pickable=True,
    stroked=True,
    extruded=False,
)
tooltip = {
    "text": "Zone {h3_cell}\nTaxis libres maintenant : {t_now}\n"
            "Prédiction à 30 min : {t_pred}\nRéel à 30 min : {t_real}\nPluie : {t_rain}"
}
st.pydeck_chart(pdk.Deck(
    layers=[layer],
    initial_view_state=pdk.ViewState(latitude=1.35, longitude=103.82, zoom=10.2),
    tooltip=tooltip,
))

legend = {
    "Taxis libres maintenant": "Transparent : peu de taxis libres. Bleu vif : 60 ou plus.",
    "Prédiction dans 30 min": "Transparent : peu de taxis libres prévus. Bleu vif : 60 ou plus.",
    "Variation prévue": "Rouge : baisse prévue. Vert : hausse prévue. Transparent : stable.",
    "Pluie actuelle": "Transparent : sec. Violet vif : 5 mm ou plus sur 15 min.",
}
st.caption(legend[view])

# --- Courbe de la journée : réel contre prédiction (toute la ville) ---
st.subheader("La journée dans toute la ville")
day_df = df[df["slot_local"].dt.date == day].copy()
day_df["target_time"] = day_df["slot_local"] + pd.Timedelta(minutes=30)
curve = day_df.groupby("target_time").agg(
    reel=("target", "sum"), prediction=("pred_30min", "sum"), persistance=("pred_persistence", "sum")
)
curve.columns = ["Réel", "Prédiction du modèle", "Persistance"]
st.line_chart(curve)

mae = (frame["pred_30min"] - frame["target"]).abs().mean()
mae_p = (frame["pred_persistence"] - frame["target"]).abs().mean()
st.caption(f"À ce moment : erreur moyenne par zone de {mae:.2f} taxis pour le modèle, "
           f"{mae_p:.2f} pour la persistance.")
