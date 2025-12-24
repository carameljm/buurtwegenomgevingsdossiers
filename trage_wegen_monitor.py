# INSTALLATIE (Voer dit uit in een aparte cel):
# !pip install geopandas requests openpyxl fiona pyproj rtree

import sys
import os
import time
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from io import BytesIO

try:
    import geopandas as gpd
    import pandas as pd
    import requests
    from shapely.geometry import box
except ImportError:
    print("FOUT: Bibliotheken ontbreken. Voer !pip install uit.")
    sys.exit()

# --- CONFIGURATIE ---
def vind_pad(bestandsnaam):
    if os.path.exists(bestandsnaam):
        return bestandsnaam
    specifiek_pad = os.path.join('/kaggle/input/geojson', bestandsnaam)
    if os.path.exists(specifiek_pad):
        return specifiek_pad
    base_input = '/kaggle/input'
    if os.path.exists(base_input):
        for root, dirs, files in os.walk(base_input):
            if bestandsnaam in files:
                return os.path.join(root, bestandsnaam)
    return bestandsnaam

PAD_BUURTWEGEN = vind_pad('buurtwegenmaarkedallocal (1).geojson')
PAD_WIJZIGINGEN = vind_pad('wijzigingenmaarkedallocal (1).geojson')

OMGEVING_WFS = "https://www.mercator.vlaanderen.be/raadpleegdienstenmercatorpubliek/wfs"
LAGEN_OMGEVING = ["lu:lu_omv_gd_v2", "lu:lu_omv_vk_v2"]

# Regio filter: Maarkedal/Oudenaarde regio
REGIO_FILTER_COORDS = (90000, 160000, 110000, 180000)

def haal_data(url, params, beschrijving):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/xml, application/gml+xml, */*'
    }
    for poging in range(3):
        try:
            with requests.Session() as s:
                response = s.get(url, params=params, headers=headers, timeout=120)
                if response.status_code != 200 or b"Exception" in response.content:
                    if b"Illegal property name" in response.content:
                        return "COLUMN_ERROR"
                    print(f"Poging {poging+1}: Serverfout voor {beschrijving}. Status: {response.status_code}")
                    time.sleep(10)
                    continue
                return gpd.read_file(BytesIO(response.content))
        except Exception as e:
            print(f"Fout bij poging {poging+1} voor {beschrijving}: {e}")
            time.sleep(15)
    return None

def laad_lokale_wegen():
    """Laadt de lokale GeoJSON bestanden en behoudt ALLE velden."""
    print(f"Laden van lokale trage wegen bestanden...")
    try:
        if not os.path.exists(PAD_BUURTWEGEN) or not os.path.exists(PAD_WIJZIGINGEN):
            print("FOUT: Bestanden niet gevonden in Kaggle input.")
            return None

        gdf_buurt = gpd.read_file(PAD_BUURTWEGEN)
        gdf_wijzig = gpd.read_file(PAD_WIJZIGINGEN)
        
        if gdf_buurt.crs is None: gdf_buurt.set_crs("EPSG:31370", inplace=True)
        if gdf_wijzig.crs is None: gdf_wijzig.set_crs("EPSG:31370", inplace=True)
        
        # Voeg bronvermelding toe aan de bestaande velden
        gdf_buurt['bron_laag'] = 'Atlas_Buurtwegen'
        gdf_wijzig['bron_laag'] = 'Wijzigingen_Trage_Wegen'
        
        # Combineren zonder kolommen te filteren
        gecombineerd = pd.concat([gdf_buurt, gdf_wijzig], ignore_index=True)
        print(f"Succesvol geladen: {len(gdf_buurt)} buurtwegen en {len(gdf_wijzig)} wijzigingen met alle velden.")
        return gecombineerd
    except Exception as e:
        print(f"FOUT bij laden lokale bestanden: {e}")
        return None

def run_monitor():
    start_tijd = datetime.now()
    print(f"--- Start Controle (Alle Dossiers - 14 dagen): {start_tijd.strftime('%d-%m-%Y %H:%M')} ---")
    
    # We kijken 14 dagen terug om alle dossiers te vangen
    cutoff_date = (start_tijd - timedelta(days=14)).strftime('%Y-%m-%d')
    recente_dossiers = []
    regio_box = box(*REGIO_FILTER_COORDS)

    datum_opties = ["datum_indiening", "tijdstip_registratie", "registratiedatum"]

    for laag in LAGEN_OMGEVING:
        gdf_laag = None
        for datum_veld in datum_opties:
            params_omv = {
                'service': 'WFS',
                'version': '1.1.0',
                'request': 'GetFeature',
                'typeName': laag,
                'outputFormat': 'application/json',
                'srsName': 'EPSG:31370',
                'propertyName': '*', 
                'CQL_FILTER': f"{datum_veld} >= {cutoff_date}T00:00:00Z"
            }
            res = haal_data(OMGEVING_WFS, params_omv, f"Omgevingsloket ({laag})")
            if isinstance(res, str) and res == "COLUMN_ERROR": continue
            if res is not None and not isinstance(res, str):
                gdf_laag = res
                break
        
        if gdf_laag is not None and not gdf_laag.empty:
            if gdf_laag.crs is None: gdf_laag.set_crs("EPSG:31370", inplace=True)
            gdf_gefilterd = gdf_laag[gdf_laag.geometry.intersects(regio_box)]
            if not gdf_gefilterd.empty:
                print(f"Gevonden: {len(gdf_gefilterd)} dossiers voor {laag}.")
                recente_dossiers.append(gdf_gefilterd)

    if not recente_dossiers:
        print("Geen nieuwe dossiers gevonden. Controle beëindigd.")
        return

    totaal_omv = pd.concat(recente_dossiers, ignore_index=True)
    
    # Genereer een directe link naar het publieke loket
    def maak_loket_link(row):
        d_id = str(row.get('dossier_id', ''))
        if d_id:
            return f"https://omgevingsloketinzage.vlaanderen.be/raadpleegen-dossier/_/dossier/{d_id}"
        return ""

    totaal_omv['Link_Loket'] = totaal_omv.apply(maak_loket_link, axis=1)

    gdf_trage_wegen = laad_lokale_wegen()

    if gdf_trage_wegen is None or gdf_trage_wegen.empty:
        return

    if totaal_omv.crs != gdf_trage_wegen.crs:
        totaal_omv = totaal_omv.to_crs(gdf_trage_wegen.crs)
        
    print("Starten ruimtelijke analyse (alle velden behouden)...")
    match = gpd.sjoin(totaal_omv, gdf_trage_wegen, how='inner', predicate='intersects')
    
    if not match.empty:
        print(f"ALARM: {len(match)} kruisingen gevonden!")
        if not os.path.exists('rapporten'): os.makedirs('rapporten')
        bestandsnaam = f"rapporten/kruisingen_rapport_{start_tijd.strftime('%Y%m%d_%H%M')}.xlsx"
        
        # Uitgebreide prioriteit kolommen voor het rapport inclusief alle toestand-velden
        prioriteit = [
            'dossier_id', 
            'Link_Loket', 
            'projectnaam', 
            'datum_indiening', 
            'fase', 
            'status', 
            'gepubliceerd', 
            'datum_beslissing', 
            'uiterste_beslisdatum', 
            'startdatum_oo', 
            'einddatum_oo', 
            'bron_laag', 
            'naam', 
            'nummer'
        ]
        
        alle_kolommen = [c for c in match.columns if c not in ['geometry', 'index_right']]
        
        # Sorteer kolommen: eerst de prioriteit, dan de rest van de velden
        finale_kolommen = [c for c in prioriteit if c in alle_kolommen]
        rest_kolommen = [c for c in alle_kolommen if c not in finale_kolommen]
        finale_kolommen.extend(rest_kolommen)
        
        rapport_df = match[finale_kolommen].drop_duplicates()
        
        # Gebruiksvriendelijke namen voor de belangrijkste velden
        hernoem_dict = {
            'naam': 'Trage_Weg_Naam',
            'nummer': 'Trage_Weg_Nummer',
            'dossier_id': 'Dossiernummer',
            'startdatum_oo': 'Start_Openbaar_Onderzoek',
            'einddatum_oo': 'Eind_Openbaar_Onderzoek'
        }
        rapport_df = rapport_df.rename(columns={k: v for k, v in hernoem_dict.items() if k in rapport_df.columns})
        
        rapport_df.to_excel(bestandsnaam, index=False)
        print(f"Gedetailleerd rapport gegenereerd: {bestandsnaam}")
    else:
        print("Geen kruisingen gevonden.")

if __name__ == "__main__":
    run_monitor()
