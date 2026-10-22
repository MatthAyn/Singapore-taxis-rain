# Singapore Taxis

Projet de pipeline de données sur la disponibilité des taxis à Singapour, croisée avec la pluie et les parkings.

## Sources de données

### Taxis
- URL : https://api.data.gov.sg/v1/transport/taxi-availability
- Mise à jour toutes les 30 secondes (source : LTA Datamall)
- Format : GeoJSON
- Paramètre `date_time` pour une date passée, format YYYY-MM-DDTHH:mm:ss (heure de Singapour)
- Clé API optionnelle (pour des limites d'appels plus élevées)
- Structure : FeatureCollection > features > geometry (MultiPoint) + properties
- Coordonnées en WGS84 (EPSG:4326), ordre [longitude, latitude]
- properties : timestamp (avec fuseau +08:00), taxi_count, api_info.status
- Points en double observés dans les coordonnées, à vérifier au nettoyage
- Historique : date_time renvoie le dernier relevé juste avant l'heure demandée
- Ordre des points non significatif (parfois trié par longitude)
- Doublons fréquents, précision GPS variable selon les points
- Forte concentration de taxis libres à l'aéroport de Changi
- Historique disponible au moins 3 mois en arrière (testé au 2026-07-14)

### Pluie
- URL : https://api-open.data.gov.sg/v2/real-time/api/rainfall
- Fréquence de mise à jour : non indiquée dans la doc, à vérifier sur les données
- Unité : millimètres (mm)
- Paramètre `date` : YYYY-MM-DD (journée entière) ou YYYY-MM-DDTHH:mm:ss (instant précis), heure de Singapour
- Sans paramètre, renvoie le dernier relevé
- Clé API optionnelle
- readings : liste de blocs { timestamp, data: [stationId, value] }
- Horodatage arrondi à 5 minutes, un seul par bloc pour toutes les stations
- Historique disponible via le paramètre date
- Résultats paginés : 25 blocs de 5 min par page, suivre paginationToken jusqu'à ce qu'il disparaisse
- Ordre des relevés : du plus récent au plus ancien
- Stations parfois absentes de certains blocs (ex : S305 le 2026-10-07 entre 20h50 et 21h30) : ne pas confondre absence de donnée et 0 mm

### Parkings (disponibilité)
- URL : https://api.data.gov.sg/v1/transport/carpark-availability
- Mise à jour toutes les minutes, appel recommandé une fois par minute
- Paramètre `date_time` : YYYY-MM-DDTHH:mm:ss (heure de Singapour)
- Types de places : C (voitures), H (poids lourds), S (motos avec side-car), Y (motos)
- Clé API optionnelle
- Structure : items > timestamp + carpark_data > [carpark_number, update_datetime, carpark_info]
- carpark_info : liste par type de place (lot_type, lots_available, total_lots)
- Jointure : carpark_number = Car Park No (CSV de référence)
- Pièges : nombres stockés en texte (à convertir), update_datetime sans fuseau horaire (SGT implicite)
- Historique disponible via date_time (testé au 2026-10-07)

### Parkings (référence)
- Source : https://data.gov.sg/datasets/d_23f946fa557947f93a8043bbef41dd09/view
- Fichier CSV d'environ 2 300 parkings, mis à jour chaque mois
- Colonnes clés : Car Park No, Address, X Coord, Y Coord
- `Car Park No` sert de clé de jointure avec l'API de disponibilité
- Coordonnées en SVY21 (système de Singapour), à convertir en latitude/longitude

## Points d'attention
- Deux générations d'API : v1 (taxis, parkings) avec le paramètre `date_time`, v2 (pluie) avec le paramètre `date`
- Toutes les heures sont en heure de Singapour (SGT)
- Horodatages différents selon les sources (taxis à la seconde, pluie toutes les 5 min) : alignement nécessaire

## Backfill
- Script : script/backfill.py
- Période : du 2026-07-16 au 2026-10-14 (90 jours), heure de Singapour
- Taxis et parkings : un relevé toutes les 15 minutes
- Pluie : journées complètes, 12 pages de 25 relevés de 5 minutes par jour
- Reprise automatique : les fichiers déjà présents sont sautés
- Gestion des limites d'appels (HTTP 429) : pause puis nouvelle tentative
- Volume : environ 3,3 Go de JSON brut

## Qualité des données (constats)

### Taxis
- taxi_count inclut les doublons : surestimation d'environ 4 % du nombre réel de taxis
- 71 relevés identiques pour deux heures demandées différentes : absences de publication de plus de 15 min

### Parkings
- Aucune donnée pour les demandes à minuit pile (SGT) : un relevé manquant par jour, compensé par celui de 00h15
- A88, places motos (Y) : places libres supérieures au total en permanence (erreur de la source)
- Capteurs figés sur toute la période : WGL, NHC, W527, HE17, HG55, PL57, JM33 ; quasi figé : PL40. À exclure
- Retards ponctuels de mise à jour (> 1 h) sur 1 524 parkings, environ 1 % des lignes : à signaler, pas à supprimer

### Pluie
- 90 stations, unité et type de mesure constants (cumul sur 5 min, en mm)
- 20 tranches de 5 min sans aucun relevé sur 26 208 (ex : 2026-08-14 de 16h25 à 16h40)
- 6,7 % des couples station x tranche absents : absence de donnée, à ne pas confondre avec 0 mm
- Stations peu fiables : S203 (47 % de présence), S305, S224, S303, S307 (environ 60 %)
- Pluie dans 1 % des relevés ; maximum de 19,7 mm en 5 min (à confirmer par les stations voisines)

## Table d'analyse : processed/zone_15min.parquet
- Grain : une ligne par zone H3 (résolution 7, environ 5 km²) et par tranche de 15 minutes
- 138 zones x 8 737 tranches = 1 205 706 lignes
- n_taxis : taxis libres uniques (relevé le plus proche de la tranche) ; vide si aucun relevé (0,9 % des tranches)
- rain_mm_15min : cumul des 3 relevés de 5 min de la station la plus proche disponible (3 candidates)
- rain_station, rain_station_km : station utilisée et distance (moyenne 1,7 km, max 8,8 km)
- Zone la plus fournie : aéroport de Changi (186 taxis libres en moyenne)

## Résultats

### Effet de la pluie sur les taxis libres
Méthode : nombre réel de taxis libres comparé au nombre attendu un jour sec,
dans la même zone, à la même heure et le même jour de la semaine.

- Pluie faible (0 à 1 mm / 15 min) : -10 %
- Pluie modérée (1 à 5 mm) : -16 %
- Forte pluie (plus de 5 mm) : -24 %
- Creux 15 à 30 min après le début d'une averse (-15 %), retour à la normale en environ 2 h 15
- 1 173 débuts d'averse analysés sur 90 jours

### Autres constats
- Moyenne stable d'un mois à l'autre (1 885 à 1 992 taxis libres)
- Maximum à 20 h, minimum à 3 h en semaine

### Limites
- Seuls les taxis libres sont observés : ni la flotte en service, ni la demande
- Corrélation, pas causalité démontrée
- Ratio supérieur à 1 dans l'heure précédant une averse : à investiguer
- Peu de données de forte pluie (1 566 lignes)

### Effet de la pluie sur les taxis libres
Méthode : nombre réel de taxis libres comparé à un attendu local (même zone,
même quart d'heure, même jour de la semaine, jours secs des 4 semaines avant et après).

- Baisse de 21 % des taxis libres entre l'heure qui précède une averse et son creux,
  15 à 30 min après le début (intervalle de confiance à 95 % par bootstrap)
- Retour au niveau normal en environ 1 h 45
- Effet croissant avec l'intensité : -9 % (0 à 1 mm / 15 min), -15 % (1 à 5 mm), -22 % (plus de 5 mm)
- Résultats stables avec deux méthodes de calcul de l'attendu (globale et locale)
- Test placebo (même zone, même heure, 7 jours avant) : aucune chute en V,
  ce qui confirme le lien avec la pluie

### Question ouverte
- Taxis libres au-dessus de l'attendu dans l'heure qui précède une averse (+9 %),
  et placebo décalé (0,91 au lieu de 1). Piste : poids de l'aéroport de Changi
  dans les sommes. À vérifier en l'excluant ou en utilisant des médianes.

## Modèle de prédiction (taxis libres par zone, à 30 minutes)
- HistGradientBoostingRegressor (scikit-learn), découpage temporel :
  entraînement jusqu'au 2026-09-23, test du 2026-09-24 au 2026-10-14
- MAE en taxis par zone (toutes tranches | pluie > 1 mm | début d'averse) :
  - Persistance : 4,86 | 3,79 | 4,03
  - v1 (niveau, perte de Poisson) : 3,83 | 3,95 | 3,91
  - v2 (variation relative, poids x10 sur la pluie, tendances) : 3,89 | 3,53 | 3,56
  - v2 sans variables de pluie : 3,89 | 3,45 | 3,45
- La v2 bat la persistance de 20 % en général et de 12 % sur les débuts d'averse

## Conclusion
- La pluie fait baisser les taxis libres d'environ 21 % (effet mesuré sur 1 173 averses)
- À 30 minutes, cette information est déjà portée par la dynamique récente des taxis :
  les variables météo n'améliorent pas la prévision (ablation avec et sans pluie)
- Perspective : intégrer une prévision de pluie à très court terme (radar)
  pour anticiper les chutes avant le début de l'averse

## Application interactive
- Lancement : streamlit run script/app.py
- Carte des 138 zones H3 sur la période de test (24 septembre - 14 octobre 2026)
- Affichages : taxis libres, prédiction à 30 min, variation prévue, pluie
- Sélecteur des 15 moments les plus pluvieux, courbe journalière réel / modèle / persistance

Exemples de limites observées :
- 27/09 à 01h30 (averse au nord et à l'ouest) : le modèle exagère la chute nocturne
  (-31 % prévu, -18 % réel)
- 27/09 à 12h30 (pluie sur toute l'île) : chute déjà passée, le modèle prévoit
  la remontée mais l'exagère (+11 % prévu, +4 % réel)

  ## Collecte continue
  - script/ingest.py : collecte de l'état actuel des trois sources, conçue pour être planifiée
    toutes les 15 minutes (journal dans logs/ingest.log, reprise sur erreur)
  - Non déployée en continu : l'analyse repose sur les 90 jours du backfill