import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# Dossier du projet, retrouvé à partir de l'emplacement de ce script
BASE_DIR = Path(__file__).resolve().parent.parent
RAW = BASE_DIR / "raw" / "live"
LOGS = BASE_DIR / "logs"
LOGS.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOGS / "ingest.log", encoding="utf-8"),
        logging.StreamHandler(),  # affiche aussi dans le terminal si on le lance à la main
    ],
)
log = logging.getLogger("ingest")

SGT = timezone(timedelta(hours=8))
URL_TAXIS = "https://api.data.gov.sg/v1/transport/taxi-availability"
URL_PARKINGS = "https://api.data.gov.sg/v1/transport/carpark-availability"
URL_PLUIE = "https://api-open.data.gov.sg/v2/real-time/api/rainfall"


def fetch(name, url, params=None):
    """Appelle l'API avec 3 tentatives. Renvoie None en cas d'échec, sans planter."""
    for attempt in range(1, 4):
        try:
            response = requests.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            log.warning(f"[{name}] connexion échouée ({e}), tentative {attempt}/3")
            time.sleep(5 * attempt)
            continue

        if response.status_code == 200:
            try:
                return response.json()
            except ValueError:
                log.error(f"[{name}] réponse JSON invalide")
                return None

        if response.status_code == 429:
            log.warning(f"[{name}] limite d'appels atteinte, pause de {20 * attempt} s")
            time.sleep(20 * attempt)
            continue

        log.error(f"[{name}] erreur HTTP {response.status_code}")
        return None

    log.error(f"[{name}] échec après 3 tentatives")
    return None


def save(data, name, file_name):
    folder = RAW / name
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / file_name
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


collected_at = datetime.now(timezone.utc)
stamp = collected_at.strftime("%Y%m%dT%H%M%SZ")
now_sgt = collected_at.astimezone(SGT)
ok, failed = 0, 0

# Taxis et parkings : état actuel
for name, url in [("taxis", URL_TAXIS), ("parkings", URL_PARKINGS)]:
    data = fetch(name, url)
    if data is not None:
        save(data, name, f"{stamp}.json")
        ok += 1
    else:
        failed += 1

# Pluie : première page du jour = les 25 derniers relevés (environ 2 h)
days = [now_sgt.date()]
if now_sgt.hour == 0:  # juste après minuit : on récupère aussi la fin de la veille
    days.append(now_sgt.date() - timedelta(days=1))
for day in days:
    day_str = day.strftime("%Y-%m-%d")
    data = fetch("pluie", URL_PLUIE, {"date": day_str})
    if data is not None:
        save(data, "pluie", f"{stamp}_{day_str}.json")
        ok += 1
    else:
        failed += 1

log.info(f"Collecte {now_sgt:%Y-%m-%d %H:%M} SGT terminée : {ok} réussies, {failed} échouées")
