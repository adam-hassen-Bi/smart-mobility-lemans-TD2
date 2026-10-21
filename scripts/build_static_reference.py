import csv, io, json, math, os, time, zipfile
import requests
import pandas as pd

GTFS_URL = "https://www.data.gouv.fr/api/1/datasets/r/5339d96c-6d20-4a01-939a-40f7b56d6cc1"

def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))

def fetch_gtfs_zip(retries=4, backoff=5):
    import time
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(GTFS_URL, timeout=(15, 60))
            resp.raise_for_status()
            return zipfile.ZipFile(io.BytesIO(resp.content))
        except requests.exceptions.RequestException as e:
            last_error = e
            print(f"  GTFS download attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise RuntimeError(f"Could not download GTFS after {retries} attempts") from last_error

def nearest_commune(lat, lon, communes):
    best_name, best_dist = None, None
    for c in communes:
        d = haversine_km(lat, lon, c["lat"], c["lon"])
        if best_dist is None or d < best_dist:
            best_dist, best_name = d, c["commune"]
    return best_name

def compute_supply_vs_population(freq, communes_df):
    """Supply-vs-demand view, one row per commune.

    Metric: scheduled weekday stop-visits per 1,000 residents (a bus or tram
    stopping once at one stop = one stop-visit). Population is only a PROXY for
    demand: no open ridership data exists.

    Computed here, in the monthly static job, rather than in the 15-minute
    snapshot: both inputs (the schedule and the INSEE population) change at most
    a few times a year, so recomputing it every 15 minutes would add nothing.
    """
    per_commune = (
        freq.groupby("nearest_commune")
        .agg(
            stop_visits=("daily_trip_count", "sum"),
            n_stops=("stop_id", "count"),
            top_stop_visits=("daily_trip_count", "max"),
        )
        .reset_index()
        .rename(columns={"nearest_commune": "commune"})
    )
    top_stops = (
        freq.sort_values("daily_trip_count", ascending=False)
        .drop_duplicates("nearest_commune")[["nearest_commune", "stop_name"]]
        .rename(columns={"nearest_commune": "commune", "stop_name": "top_stop_name"})
    )
    out = communes_df.merge(per_commune, on="commune", how="left").merge(top_stops, on="commune", how="left")
    out[["stop_visits", "n_stops", "top_stop_visits"]] = out[["stop_visits", "n_stops", "top_stop_visits"]].fillna(0).astype(int)
    out["top_stop_name"] = out["top_stop_name"].fillna("")
    out["visits_per_1000"] = (out["stop_visits"] / out["population"] * 1000).round(1)
    out["stops_per_1000"] = (out["n_stops"] / out["population"] * 1000).round(2)
    # share of the commune's supply carried by its single busiest stop: a high
    # value means one hub/terminus drives the whole number
    out["top_stop_share"] = (out["top_stop_visits"] / out["stop_visits"].where(out["stop_visits"] > 0)).fillna(0).round(3)
    out = out.sort_values("visits_per_1000", ascending=False).reset_index(drop=True)
    out["rank"] = out.index + 1
    return out

def main():
    communes_df = pd.read_csv("data/reference/communes.csv")
    communes = communes_df.to_dict("records")

    print("Downloading SETRAM static GTFS feed...")
    zf = fetch_gtfs_zip()

    # dtype=str throughout: stop_id/route_id/trip_id mix numeric-looking and
    # non-numeric values (e.g. "FLEX-381") across GTFS files -- letting pandas
    # infer dtypes per-file causes silent int64-vs-object merge failures.
    routes = pd.read_csv(zf.open("routes.txt"), dtype=str)
    stops = pd.read_csv(zf.open("stops.txt"), dtype=str)
    trips = pd.read_csv(zf.open("trips.txt"), dtype=str)
    calendar = pd.read_csv(zf.open("calendar.txt"), dtype=str)
    stop_times = pd.read_csv(zf.open("stop_times.txt"), usecols=["trip_id", "stop_id"], dtype=str)
    stops["stop_lat"] = stops["stop_lat"].astype(float)
    stops["stop_lon"] = stops["stop_lon"].astype(float)
    print(f"Loaded {len(routes)} routes, {len(stops)} stops, {len(trips)} trips, {len(stop_times)} stop_times")

    # Pick a representative weekday: the Tuesday service pattern with the
    # longest validity window (avoids picking a one-off exception date).
    calendar["date_span"] = (
        pd.to_datetime(calendar["end_date"], format="%Y%m%d")
        - pd.to_datetime(calendar["start_date"], format="%Y%m%d")
    )
    tue_services = calendar[calendar["tuesday"] == "1"].sort_values("date_span", ascending=False)
    if tue_services.empty:
        raise RuntimeError("No Tuesday service found in calendar.txt -- feed structure may have changed")
    service_id = tue_services.iloc[0]["service_id"]
    print(f"Using service_id={service_id} as the representative weekday")

    weekday_trip_ids = set(trips[trips["service_id"] == service_id]["trip_id"])
    weekday_stop_times = stop_times[stop_times["trip_id"].isin(weekday_trip_ids)]

    freq = weekday_stop_times.groupby("stop_id").size().reset_index(name="daily_trip_count")
    freq = freq.merge(
        stops[["stop_id", "stop_name", "stop_lat", "stop_lon", "wheelchair_boarding"]],
        on="stop_id", how="left",
    ).dropna(subset=["stop_lat"])

    print(f"Assigning {len(freq)} stops to nearest of {len(communes)} communes...")
    freq["nearest_commune"] = freq.apply(
        lambda row: nearest_commune(row["stop_lat"], row["stop_lon"], communes), axis=1
    )

    freq = freq.sort_values("daily_trip_count", ascending=False)
    os.makedirs("data/gtfs_static", exist_ok=True)
    freq.to_csv("data/gtfs_static/stops_frequency.csv", index=False)
    routes[["route_id", "route_short_name", "route_long_name", "route_type"]].to_csv(
        "data/gtfs_static/routes.csv", index=False
    )
    supply = compute_supply_vs_population(freq, communes_df)
    supply.to_csv("data/gtfs_static/commune_supply.csv", index=False)
    network = {
        "population": int(supply["population"].sum()),
        "stop_visits": int(supply["stop_visits"].sum()),
        "n_stops": int(supply["n_stops"].sum()),
    }
    network["visits_per_1000"] = round(network["stop_visits"] / network["population"] * 1000, 1)
    os.makedirs("docs/data", exist_ok=True)
    with open("docs/data/supply_demand.json", "w") as f:
        json.dump({
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "service_id": service_id,
            "metric": "Scheduled weekday stop-visits per 1,000 residents",
            "network": network,
            "communes": supply.to_dict("records"),
            "stops": freq[["stop_name", "stop_lat", "stop_lon", "daily_trip_count", "nearest_commune"]]
                     .rename(columns={"stop_lat": "lat", "stop_lon": "lon", "daily_trip_count": "visits", "nearest_commune": "commune"})
                     .round({"lat": 5, "lon": 5}).to_dict("records"),
        }, f, ensure_ascii=False)
    print(f"Wrote data/gtfs_static/stops_frequency.csv ({len(freq)} rows)")
    print(f"Wrote data/gtfs_static/commune_supply.csv and docs/data/supply_demand.json ({len(supply)} communes)")
    print(f"Wrote data/gtfs_static/routes.csv ({len(routes)} rows)")
    print("Top 5 busiest stops:")
    print(freq[["stop_name", "daily_trip_count", "nearest_commune"]].head(5).to_string(index=False))
    print("Supply per 1,000 residents (top 5):")
    print(supply[["commune", "visits_per_1000", "top_stop_name", "top_stop_share"]].head(5).to_string(index=False))

if __name__ == "__main__":
    main()
