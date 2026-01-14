const { execSync } = require('child_process');
const fs = require('fs');

const URL_BUURTWEGEN = "https://raw.githubusercontent.com/carameljm/buurtwegenomgevingsdossiers/main/buurtwegenoostvlaanderen.geojson";
const URL_WIJZIGINGEN = "https://raw.githubusercontent.com/carameljm/buurtwegenomgevingsdossiers/main/wijzigingenoostvlaanderen.geojson";

const pythonScript = `
import geopandas as gpd
import pandas as pd
import requests
import json
from io import StringIO
from datetime import datetime, timedelta
from shapely.geometry import box

def run_monitor():
    try:
        # 1. BBOX Maarkedal Regio (Lambert 72)
        minx, miny, maxx, maxy = 77144, 158145, 127271, 200742
        regio_bbox = box(minx, miny, maxx, maxy)
        
        def load_filtered(url):
            try:
                gdf = gpd.read_file(url, bbox=regio_bbox)
                if gdf is None or gdf.empty: return None
                if gdf.crs is None: gdf.set_crs("EPSG:4326", inplace=True)
                return gdf.to_crs("EPSG:31370")
            except: return None

        gdf_buurt = load_filtered("${URL_BUURTWEGEN}")
        gdf_wijzig = load_filtered("${URL_WIJZIGINGEN}")
        
        wegen_lagen = [l for l in [gdf_buurt, gdf_wijzig] if l is not None]
        if not wegen_lagen: 
            return {"status": "error", "message": "Geen wegen geladen"}
            
        gdf_trage_wegen = pd.concat(wegen_lagen, ignore_index=True)

        # 2. Omgevingsloket WFS (Check laatste 7 dagen)
        WFS_URL = "https://www.mercator.vlaanderen.be/raadpleegdienstenmercatorpubliek/wfs"
        LAGEN_OMGEVING = ["lu:lu_omv_gd_v2", "lu:lu_omv_vk_v2"]
        cutoff_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        
        recente_dossiers = []
        for laag in LAGEN_OMGEVING:
            cql = f"BBOX(geom, {minx}, {miny}, {maxx}, {maxy}) AND datum_indiening >= {cutoff_date}T00:00:00Z"
            params = {'service': 'WFS', 'version': '1.1.0', 'request': 'GetFeature', 'typeName': laag, 'outputFormat': 'application/json', 'srsName': 'EPSG:31370', 'CQL_FILTER': cql}
            try:
                r = requests.get(WFS_URL, params=params, timeout=30)
                if "Illegal property name: geom" in r.text:
                    params['CQL_FILTER'] = params['CQL_FILTER'].replace('geom', 'geometry')
                    r = requests.get(WFS_URL, params=params, timeout=30)
                if r.status_code == 200:
                    temp_gdf = gpd.read_file(StringIO(r.text))
                    if not temp_gdf.empty: recente_dossiers.append(temp_gdf)
            except: continue

        # 3. Export data voor de kaart (Regio Wegen)
        # We filteren de enorme lijst naar enkel wat in de BBOX ligt + buffer
        regio_wegen_gdf = gdf_trage_wegen[gdf_trage_wegen.intersects(regio_bbox.buffer(500))].copy()
        # Vereenvoudig geometrie (simplify) om bestand nog kleiner te maken (10cm tolerantie)
        regio_wegen_gdf['geometry'] = regio_wegen_gdf.geometry.simplify(0.1)

        result_dict = {
            "status": "oke",
            "wegen_regio": json.loads(regio_wegen_gdf.to_crs("EPSG:4326").to_json()),
            "matches": {"type": "FeatureCollection", "features": []}
        }

        # 4. Zoek Matches (met 1m krimp)
        if recente_dossiers:
            totaal_omv = pd.concat(recente_dossiers, ignore_index=True)
            totaal_omv['orig_geom'] = totaal_omv.geometry
            totaal_omv['geometry'] = totaal_omv.geometry.buffer(-1.0)
            totaal_omv = totaal_omv[~totaal_omv.geometry.is_empty]

            match = gpd.sjoin(totaal_omv, gdf_trage_wegen, how='inner', predicate='intersects')

            if not match.empty:
                export_gdf = totaal_omv.loc[match.index.unique()].copy()
                export_gdf['geometry'] = export_gdf['orig_geom']
                
                def make_link(row):
                    d_id = row.get('dossierid') or row.get('dossier_id')
                    return f"https://omgevingsloketinzage.vlaanderen.be/raadpleegen-dossier/_/dossier/{d_id}"
                
                export_gdf['Link'] = export_gdf.apply(make_link, axis=1)
                for col in export_gdf.select_dtypes(include=['datetime64', 'datetimetz']).columns:
                    export_gdf[col] = export_gdf[col].dt.strftime('%Y-%m-%d')
                
                result_dict["status"] = "MATCH"
                result_dict["matches"] = json.loads(export_gdf.to_crs("EPSG:4326").to_json())

        return result_dict

    except Exception as e:
        return {"status": "error", "message": str(e)}

print(json.dumps(run_monitor()))
`;

try {
    const encoded = Buffer.from(pythonScript).toString('base64');
    const output = execSync(`echo "${encoded}" | base64 -d | python3`, { timeout: 90000 }).toString();
    const result = JSON.parse(output);

    if (result.status !== "error") {
        // Sla het kleine regio-wegen bestand op
        fs.writeFileSync('wegen_regio.geojson', JSON.stringify(result.wegen_regio));
        // Sla de matches op (of een lege collection)
        fs.writeFileSync('matches.geojson', JSON.stringify(result.matches));
        console.log(`Success: ${result.status}. Files written.`);
    } else {
        console.error("Python Error:", result.message);
    }
} catch (err) {
    console.error("Critical Node Error:", err.message);
}
