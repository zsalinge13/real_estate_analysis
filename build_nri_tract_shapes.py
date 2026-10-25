"""Join National Risk Index tracts to Census tract polygons.

Reads nri_census_tracts_ca.csv and writes one GeoJSON feature per tract that
appears in that file. Geometry comes from the newest Census cartographic
boundary shapefile cb_*_06_tract_500k.zip, reprojected to EPSG:4326. Census
GEOID is matched to TRACTFIPS. Tracts that are not in the CSV are left out.
A National Risk Index tract with no polygon is reported and omitted.
"""

from __future__ import annotations

import csv
import json
import re
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import shapefile
from pyproj import CRS, Transformer

CSV_PATH = Path(__file__).resolve().parent / "nri_census_tracts_ca.csv"
OUTPUT = Path(__file__).resolve().parent / "nri_census_tract_shapes.geojson"
USER_AGENT = "real-estate-analysis"
ID_FIELDS = ("TRACTFIPS", "COUNTY", "STATEABBRV", "STCOFIPS")
NUMBER_RE = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?\Z")
STATE_FIPS = "06"
# California, with room for the Channel Islands and cartographic simplification.
LON_RANGE = (-125.0, -114.0)
LAT_RANGE = (32.0, 42.5)
OLDEST_YEAR = 2010
MAX_ZIP_BYTES = 200_000_000


def tract_fips(value: object) -> str:
    text = "" if value is None else str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if not text.isdigit() or not 1 <= len(text) <= 11:
        raise SystemExit(f"TRACTFIPS is not an 11-digit census id: {text!r}.")
    return text.zfill(11)


def read_csv(path: Path) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    if not path.is_file():
        raise SystemExit(f"{path.name} was not found.")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = tuple(reader.fieldnames or ())
        if "TRACTFIPS" not in columns:
            raise SystemExit(f"{path.name} has no TRACTFIPS column.")
        if any(not name for name in columns) or len(set(columns)) != len(columns):
            raise SystemExit(f"{path.name} has a blank or duplicate column name.")
        rows: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw in reader:
            if None in raw:
                raise SystemExit(f"{path.name} has a row with extra columns.")
            item = {name: "" if raw.get(name) is None else str(raw[name]) for name in columns}
            tract = tract_fips(item["TRACTFIPS"])
            if tract in seen:
                raise SystemExit(f"Duplicate TRACTFIPS {tract}.")
            seen.add(tract)
            item["TRACTFIPS"] = tract
            rows.append(item)
    if not rows:
        raise SystemExit(f"{path.name} has no tracts.")
    return columns, rows


def property_token(column: str, value: str, tract: str) -> str:
    if column in ID_FIELDS:
        return json.dumps(value)
    text = value.strip()
    if text == "":
        return "null"
    if NUMBER_RE.fullmatch(text) is None:
        raise SystemExit(f"{column} for {tract} is not a number.")
    return text


def census_url(year: int) -> str:
    return (
        "https://www2.census.gov/geo/tiger/"
        f"GENZ{year}/shp/cb_{year}_{STATE_FIPS}_tract_500k.zip"
    )


def url_exists(url: str) -> bool:
    request = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            kind = response.headers.get("Content-Type", "")
            return response.status == 200 and "html" not in kind.lower()
    except urllib.error.HTTPError as exc:
        if exc.code in {404, 403, 405}:
            return False
        raise SystemExit(f"Census boundary lookup failed with HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise SystemExit("Census boundary lookup failed.") from exc


def newest_census_year() -> tuple[int, str]:
    this_year = datetime.now(timezone.utc).year
    for year in range(this_year + 1, OLDEST_YEAR - 1, -1):
        url = census_url(year)
        if url_exists(url):
            return year, url
    raise SystemExit("No California census tract cartographic boundary file was found.")


def download(url: str, dest: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            if response.status != 200:
                raise SystemExit(f"Census download failed with HTTP {response.status}.")
            with dest.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Census download failed with HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise SystemExit("Census download failed.") from exc


def extract_shapefile(archive: Path, dest: Path) -> tuple[Path, Path]:
    with zipfile.ZipFile(archive) as bundle:
        total = 0
        for info in bundle.infolist():
            total += info.file_size
            name = Path(info.filename)
            if name.is_absolute() or ".." in name.parts:
                raise SystemExit("Census shapefile archive has an unexpected path.")
        if total > MAX_ZIP_BYTES:
            raise SystemExit("Census shapefile archive is larger than expected.")
        bundle.extractall(dest)
    shapefiles = sorted(dest.rglob("cb_*_06_tract_500k.shp"))
    projections = sorted(dest.rglob("cb_*_06_tract_500k.prj"))
    if len(shapefiles) != 1 or len(projections) != 1:
        raise SystemExit("Census tract archive did not contain one tract shapefile.")
    return shapefiles[0], projections[0]


def transformer_to_wgs84(prj_path: Path) -> Transformer:
    source = CRS.from_user_input(prj_path.read_text(encoding="utf-8"))
    return Transformer.from_crs(source, CRS.from_epsg(4326), always_xy=True)


def _is_position(node: object) -> bool:
    return (
        isinstance(node, (list, tuple))
        and len(node) >= 2
        and isinstance(node[0], (int, float))
        and isinstance(node[1], (int, float))
    )


def reproject_geometry(geom: dict, transformer: Transformer, tract: str) -> dict | None:
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype not in {"Polygon", "MultiPolygon"} or not coords:
        return None
    positions: list[tuple[float, float]] = []

    def collect(node: object) -> None:
        if _is_position(node):
            positions.append((float(node[0]), float(node[1])))
            return
        if isinstance(node, (list, tuple)):
            for child in node:
                collect(child)

    collect(coords)
    if not positions:
        return None
    xs, ys = transformer.transform(
        [point[0] for point in positions],
        [point[1] for point in positions],
    )
    mapped = iter(
        _rounded_position(float(lon), float(lat), tract) for lon, lat in zip(xs, ys)
    )

    def rebuild(node: object) -> list:
        if _is_position(node):
            return next(mapped)
        return [rebuild(child) for child in node]

    geometry = {"type": gtype, "coordinates": rebuild(coords)}
    if not _rings_closed(geometry):
        raise SystemExit(f"Tract {tract} polygon has an empty ring.")
    return geometry


def _rounded_position(lon: float, lat: float, tract: str) -> list[float]:
    lon = round(lon, 6)
    lat = round(lat, 6)
    if not (LON_RANGE[0] <= lon <= LON_RANGE[1] and LAT_RANGE[0] <= lat <= LAT_RANGE[1]):
        raise SystemExit(
            f"Tract {tract} has a coordinate outside California after reprojection."
        )
    return [lon, lat]


def _rings_closed(geom: dict) -> bool:
    polygons = (
        geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    )
    for polygon in polygons:
        if not polygon:
            return False
        for ring in polygon:
            if len(ring) < 4:
                return False
            ring[-1] = [ring[0][0], ring[0][1]]
    return True


def census_polygons(
    shp_path: Path, transformer: Transformer, wanted: set[str]
) -> dict[str, dict]:
    polygons: dict[str, dict] = {}
    reader = shapefile.Reader(str(shp_path), encoding="utf-8")
    try:
        fields = [field[0] for field in reader.fields[1:]]
        if "GEOID" not in fields:
            raise SystemExit("Census tract shapefile has no GEOID field.")
        for record in reader.iterShapeRecords():
            geoid = tract_fips(record.record["GEOID"])
            if geoid not in wanted:
                continue
            if geoid in polygons:
                raise SystemExit(f"Census shapefile has more than one feature for {geoid}.")
            geometry = reproject_geometry(
                record.shape.__geo_interface__, transformer, geoid
            )
            if geometry is None:
                continue
            polygons[geoid] = geometry
    finally:
        reader.close()
    extra = set(polygons) - wanted
    if extra:
        raise SystemExit("Refusing to write census tracts that are not in the CSV.")
    return polygons


def write_geojson(
    path: Path,
    columns: tuple[str, ...],
    rows: list[dict[str, str]],
    polygons: dict[str, dict],
) -> tuple[int, list[str]]:
    missing = [row["TRACTFIPS"] for row in rows if row["TRACTFIPS"] not in polygons]
    tmp = path.with_name(path.name + ".tmp")
    written = 0
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write('{"type":"FeatureCollection","features":[')
            first = True
            for row in rows:
                tract = row["TRACTFIPS"]
                geometry = polygons.get(tract)
                if geometry is None:
                    continue
                if not first:
                    handle.write(",")
                first = False
                properties = ",".join(
                    json.dumps(column)
                    + ":"
                    + property_token(column, row.get(column, ""), tract)
                    for column in columns
                )
                handle.write(
                    '{"type":"Feature","properties":{'
                    + properties
                    + '},"geometry":'
                )
                json.dump(geometry, handle, separators=(",", ":"))
                handle.write("}")
                written += 1
            handle.write("]}\n")
        if written == 0:
            raise SystemExit("No National Risk Index tracts matched a census tract polygon.")
        if written + len(missing) != len(rows):
            raise SystemExit("Tract count does not add up.")
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return written, missing


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) > 2:
        print(
            "Usage: python build_nri_tract_shapes.py [csv] [geojson]",
            file=sys.stderr,
        )
        return 1
    csv_path = Path(args[0]) if args else CSV_PATH
    output = Path(args[1]) if len(args) == 2 else OUTPUT
    columns, rows = read_csv(csv_path)
    wanted = {row["TRACTFIPS"] for row in rows}
    year, url = newest_census_year()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        archive = root / f"cb_{year}_{STATE_FIPS}_tract_500k.zip"
        download(url, archive)
        shp_path, prj_path = extract_shapefile(archive, root / "shp")
        transformer = transformer_to_wgs84(prj_path)
        polygons = census_polygons(shp_path, transformer, wanted)
    written, missing = write_geojson(output, columns, rows, polygons)
    print(f"Using Census cartographic boundary cb_{year}_{STATE_FIPS}_tract_500k.")
    print(f"Wrote {written} tracts to {output.name}.")
    print(f"Unmatched TRACTFIPS count: {len(missing)}.")
    if missing:
        print("Unmatched TRACTFIPS: " + ", ".join(missing))
    print(f"File size: {output.stat().st_size} bytes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
