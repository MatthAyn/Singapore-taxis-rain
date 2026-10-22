import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# --- Paramètres ---
STEP_MINUTES = 15    # un relevé toutes les 15 minutes
PAUSE = 1            # secondes d'attente entre deux appels

SGT = timezone(timedelta(hours=8))  # heure de Singapour
BASE = Path("raw/backfill")

URL_TAXIS = "https://api.data.gov.sg/v1/transport/taxi-availability"
URL_PARKINGS = "https://api.data.gov.sg/v1/transport/carpark-availability"
URL_PLUIE = "https://api-open.data.gov.sg/v2/real-time/api/rainfall"


def get_json(url, params):
    """Appelle l'API avec jusqu'à 3 tentatives. Renvoie None en cas d'échec."""
    for attempt in range(1, 4):
        try:
            response = requests.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            print(f"  connexion échouée ({e}), tentative {attempt}/3")
            time.sleep(5 * attempt)
            continue

        if response.status_code == 200:
            try:
                return response.json()
            except ValueError:
                print("  réponse JSON invalide")
                return None

        if response.status_code == 429:  # trop de requêtes
            print(f"  limite d'appels atteinte, pause de {30 * attempt} s")
            time.sleep(30 * attempt)
            continue

        print(f"  erreur HTTP {response.status_code}")
        return None
    return None


def save(data, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def backfill_snapshots(name, url, start, end):
    """Taxis et parkings : un appel par tranche de 15 minutes."""
    t = start
    while t <= end:
        # Nom du fichier : heure demandée, convertie en UTC
        file_name = t.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json"
        path = BASE / name / file_name

        if not path.exists():  # reprise : on saute ce qui est déjà téléchargé
            params = {"date_time": t.strftime("%Y-%m-%dT%H:%M:%S")}
            data = get_json(url, params)
            if data is not None:
                save(data, path)
                print(f"[{name}] OK {t:%Y-%m-%d %H:%M} SGT")
            time.sleep(PAUSE)

        t += timedelta(minutes=STEP_MINUTES)


def backfill_pluie(start_day, end_day):
    """Pluie : une journée entière par appel, en suivant la pagination."""
    day = start_day
    while day <= end_day:
        day_str = day.strftime("%Y-%m-%d")
        page = 1
        token = None

        while True:
            path = BASE / "pluie" / f"{day_str}_p{page:02d}.json"

            if path.exists():
                # Page déjà téléchargée : on la relit pour récupérer le jeton suivant
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            else:
                params = {"date": day_str}
                if token:
                    params["paginationToken"] = token
                data = get_json(URL_PLUIE, params)
                if data is None:
                    print(f"[pluie] échec {day_str} page {page}")
                    break
                save(data, path)
                time.sleep(PAUSE)

            token = data.get("data", {}).get("paginationToken")
            if not token:  # plus de page suivante
                break
            page += 1

        print(f"[pluie] {day_str} terminé ({page} pages)")
        day += timedelta(days=1)


# --- Période à récupérer : 90 jours complets ---
start = datetime(2026, 7, 16, 0, 0, tzinfo=SGT)
end = datetime(2026, 10, 14, 23, 45, tzinfo=SGT)

print(f"Backfill du {start:%Y-%m-%d %H:%M} au {end:%Y-%m-%d %H:%M} (heure de Singapour)")

backfill_snapshots("taxis", URL_TAXIS, start, end)
backfill_snapshots("parkings", URL_PARKINGS, start, end)
backfill_pluie(start.date(), end.date())  # du 16 juillet au 14 octobre inclus

print("Terminé.")
