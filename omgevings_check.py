import hassapi as hass
import geopandas as gpd
import pandas as pd
import requests
import json
from io import StringIO
from datetime import datetime, timedelta
from shapely.geometry import box

class OmgevingsCheck(hass.Hass):

    def initialize(self):
        # TEST: Draai 1 seconde na opstarten #
        self.run_in(self.run_monitor, 1) 
        
        # Normale planning: elke dag om 01:00
        self.run_daily(self.run_monitor, "01:00:00")
        self.log("OmgevingsCheck App geïnitialiseerd. Volgende check om 01:00.")

    def run_monitor(self, kwargs=None):
        self.log("Start controle op omgevingsdossiers...")
        
        URL_BUURTWEGEN = "https://raw.githubusercontent.com/carameljm/buurtwegenomgevingsdossiers/main/buurtwegenoostvlaanderen.geojson"
        URL_WIJZIGINGEN = "https://raw.githubusercontent.com/carameljm/buurtwegenomgevingsdossiers/main/wijzigingenoostvlaanderen.geojson"
        N8N_WEBHOOK_URL = "http://homeassistant:8081/webhook/7b7ea0c7-9f50-4712-b922-f9e67cd657fa"

        try:
            # 1. BBOX Definitie (Lambert 72)
            minx, miny, maxx, maxy = 77144, 158145, 127271, 200742
            regio_bbox = box(minx, miny, maxx, maxy)
            
            # 2. Wegen laden en transformeren
            def load_filtered(url):
                try:
                    gdf = gpd.read_file(url, bbox=regio_bbox)
                    if gdf is None or gdf.empty: return None
                    if gdf.crs is None: gdf.set_crs("EPSG:4326", inplace=True)
                    return gdf.to_crs("EPSG:31370")
                except Exception as e:
                    self.log(f"Fout bij laden {url}: {e}")
                    return None

            gdf_buurt = load_filtered(URL_BUURTWEGEN)
            gdf_wijzig = load_filtered(URL_WIJZIGINGEN)
            
            if gdf_buurt is None and gdf_wijzig is None:
                self.log("Geen wegenbestanden kunnen laden.")
                return

            wegen_lagen = []
            if gdf_buurt is not None:
                gdf_buurt['bron_laag'] = 'Atlas_Buurtwegen'
                wegen_lagen.append(gdf_buurt)
            if gdf_wijzig is not None:
                gdf_wijzig['bron_laag'] = 'Wijzigingen_Trage_Wegen'
                wegen_lagen.append(gdf_wijzig)
                
            gdf_trage_wegen = pd.concat(wegen_lagen, ignore_index=True)

            # 3. WFS Omgevingsdossiers ophalen
            WFS_URL = "https://www.mercator.vlaanderen.be/raadpleegdienstenmercatorpubliek/wfs"
            LAGEN_OMGEVING = ["lu:lu_omv_gd_v2", "lu:lu_omv_vk_v2"]
            cutoff_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
            
            recente_dossiers = []
            bbox_str = f"{minx}, {miny}, {maxx}, {maxy}"

            for laag in LAGEN_OMGEVING:
                # Filter op BBOX en datum_indiening
                cql = f"BBOX(geom, {bbox_str}) AND datum_indiening >= {cutoff_date}T00:00:00Z"
                params = {
                    'service': 'WFS', 'version': '1.1.0', 'request': 'GetFeature',
                    'typeName': laag, 'outputFormat': 'application/json',
                    'srsName': 'EPSG:31370', 'CQL_FILTER': cql
                }
                
                try:
                    r = requests.get(WFS_URL, params=params, timeout=30)
                    if r.status_code == 200 and "ExceptionReport" not in r.text:
                        temp_gdf = gpd.read_file(StringIO(r.text))
                        if not temp_gdf.empty:
                            recente_dossiers.append(temp_gdf)
                except Exception as e:
                    self.log(f"Fout bij ophalen WFS laag {laag}: {e}")

            if not recente_dossiers:
                self.log("Geen recente dossiers gevonden in de regio.")
                return

            totaal_omv = pd.concat(recente_dossiers, ignore_index=True)
            # Krimp buffer om valse rand-matches te voorkomen
            totaal_omv['geometry'] = totaal_omv.geometry.buffer(-1.0)
            totaal_omv = totaal_omv[~totaal_omv.geometry.is_empty]

            # 4. Ruimtelijke Join (Kruisen van dossiers met wegen)
            match = gpd.sjoin(totaal_omv, gdf_trage_wegen, how='inner', predicate='intersects')

            if not match.empty:
                vondsten = []
                self.log(f"MATCH GEVONDEN: {len(match)} dossiers gekoppeld aan trage wegen.")
                
                # Voorbereiden data voor JSON export
                match_data = match.copy()
                
                # Voeg berekende coördinaten toe
                match_data['centroid_x'] = match_data.geometry.centroid.x.round(2)
                match_data['centroid_y'] = match_data.geometry.centroid.y.round(2)
                
                # Verwijder niet-serialiseerbare kolommen
                cols_to_drop = ['geometry', 'index_right']
                for col in cols_to_drop:
                    if col in match_data.columns:
                        match_data = match_data.drop(columns=[col])

                for idx, row in match_data.iterrows():
                    # FIX: Vervang NaN door None (wordt null in JSON)
                    dossier_dict = row.where(pd.notnull(row), None).to_dict()
                    
                    # Converteer overige objecten naar JSON-vriendelijke types
                    for k, v in dossier_dict.items():
                        # Datums naar ISO string
                        if isinstance(v, (datetime, pd.Timestamp)):
                            dossier_dict[k] = v.isoformat()
                        # Extra check voor zwevende NaN waarden
                        elif isinstance(v, float) and (v != v):
                            dossier_dict[k] = None
                    
                    # Genereer de directe link naar het Omgevingsloket
                    d_id = (dossier_dict.get('dossier_id') or 
                            dossier_dict.get('dossierid') or 
                            dossier_dict.get('projectnummer') or 
                            f"ID-{idx}")
                    
                    dossier_dict['omgevingsloket_link'] = f"https://omgevingsloketinzage.vlaanderen.be/raadpleegen-dossier/_/dossier/{d_id}"
                    
                    vondsten.append(dossier_dict)

                # 5. Bundel versturen naar n8n
                self.log(f"Bundel van {len(vondsten)} matches versturen naar n8n...")
                try:
                    r_post = requests.post(N8N_WEBHOOK_URL, json={"matches": vondsten}, timeout=30)
                    r_post.raise_for_status()
                    self.log("Data succesvol verzonden naar n8n.")
                except Exception as e:
                    self.log(f"Fout bij versturen naar n8n: {e}")
            else:
                self.log("Geen overlap gevonden tussen trage wegen en nieuwe dossiers.")

        except Exception as e:
            self.log(f"CRITICAL ERROR in OmgevingsCheck: {e}", level="ERROR")