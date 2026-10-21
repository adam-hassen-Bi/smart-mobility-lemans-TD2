import csv, json, os, time
from collections import defaultdict
import requests
import pandas as pd
from google.transit import gtfs_realtime_pb2

TRIP_UPDATE_URL = "https://proxy.transport.data.gouv.fr/resource/setram-lemans-gtfs-rt-trip-update"
ALERT_URL = "https://proxy.transport.data.gouv.fr/resource/setram-lemans-gtfs-rt-service-alert"

def fetch_feed(url, retries=4, backoff=5):
    """Same retry pattern as the EU capitals project's fetch_weather_batch, for
    the same reason: a single slow TLS handshake on a shared runner shouldn't
    fail an otherwise-healthy scheduled run."""
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=(15, 30))
            resp.raise_for_status()
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(resp.content)
            return feed
        except Exception as e:
            last_error = e
            print(f"  fetch attempt {attempt}/{retries} failed for {url}: {e}")
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise RuntimeError(f"Unreachable after {retries} attempts: {url}") from last_error

def load_route_names():
    path = "data/gtfs_static/routes.csv"
    if not os.path.exists(path):
        return {}
    df = pd.read_csv(path, dtype=str)
    return {
        row["route_id"]: (row["route_short_name"] or row["route_id"])
        for _, row in df.iterrows()
    }

def compute_route_aggregates(trip_update_feed):
    per_route = defaultdict(lambda: {"delays": [], "n_skipped": 0, "n_trips": 0})
    for entity in trip_update_feed.entity:
        tu = entity.trip_update
        route_id = tu.trip.route_id or "UNKNOWN"
        per_route[route_id]["n_trips"] += 1
        for stu in tu.stop_time_update:
            if stu.schedule_relationship == 1:  # SKIPPED
                per_route[route_id]["n_skipped"] += 1
            delay = None
            if stu.HasField("arrival"):
                delay = stu.arrival.delay
            elif stu.HasField("departure"):
                delay = stu.departure.delay
            if delay is not None:
                per_route[route_id]["delays"].append(delay)
    rows = []
    for route_id, d in per_route.items():
        delays = d["delays"]
        rows.append({
            "route_id": route_id,
            "n_trips_observed": d["n_trips"],
            "n_stop_updates": len(delays),
            "n_skipped": d["n_skipped"],
            "avg_delay_s": round(sum(delays) / len(delays), 1) if delays else 0.0,
            "max_delay_s": max(delays) if delays else 0,
        })
    return rows

def extract_alerts(alert_feed):
    alerts = []
    for e in alert_feed.entity:
        a = e.alert
        routes_affected = [ie.route_id for ie in a.informed_entity if ie.route_id]
        header = a.header_text.translation[0].text if a.header_text.translation else ""
        alerts.append({"alert_id": e.id, "routes": ",".join(routes_affected), "header": header})
    return alerts

def append_csv(path, rows, fieldnames):
    file_exists = os.path.exists(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)

LATE_THRESHOLD_S = 180   # a route averaging more than 3 minutes late counts as "late"
HISTORY_WINDOW_DAYS = 7

def build_history_summary(route_names, now_ts):
    """Compact view of the last HISTORY_WINDOW_DAYS for the dashboard.

    The raw history stays in data/history/*.csv (one row per route per
    snapshot); this file only holds what the page needs: one point per
    snapshot, one row per route and one row per hour of the day.
    """
    import glob
    files = sorted(glob.glob("data/history/route_delays_*.csv"))[-2:]  # current + previous month
    if not files:
        return None
    h = pd.concat((pd.read_csv(f, dtype={"route_id": str}) for f in files), ignore_index=True)
    h = h[h["snapshot_ts"] >= now_ts - HISTORY_WINDOW_DAYS * 86400]
    h = h[h["n_stop_updates"] > 0]
    if h.empty:
        return None
    h["weighted"] = h["avg_delay_s"] * h["n_stop_updates"]
    h["late"] = h["avg_delay_s"] > LATE_THRESHOLD_S

    snap = h.groupby("snapshot_ts").agg(
        weighted=("weighted", "sum"), updates=("n_stop_updates", "sum"),
        n_routes=("route_id", "count"), n_late=("late", "sum"),
    ).reset_index()
    snapshots = [{
        "ts": int(r.snapshot_ts),
        "avg_delay_s": round(r.weighted / r.updates, 1),
        "n_routes": int(r.n_routes),
        "n_routes_late": int(r.n_late),
    } for r in snap.itertuples()]

    rt = h.groupby("route_id").agg(
        weighted=("weighted", "sum"), updates=("n_stop_updates", "sum"),
        n_snapshots=("snapshot_ts", "nunique"), n_late=("late", "sum"),
        worst_delay_s=("max_delay_s", "max"),
    ).reset_index()
    by_route = sorted(({
        "route_id": r.route_id,
        "route_name": route_names.get(r.route_id, r.route_id),
        "avg_delay_s": round(r.weighted / r.updates, 1),
        "n_snapshots": int(r.n_snapshots),
        "pct_snapshots_late": round(100 * r.n_late / r.n_snapshots, 1),
        "worst_delay_s": int(r.worst_delay_s),
    } for r in rt.itertuples()), key=lambda r: r["avg_delay_s"], reverse=True)

    # hour of day in local time: that is the time a planner reasons in
    h["hour"] = pd.to_datetime(h["snapshot_ts"], unit="s", utc=True).dt.tz_convert("Europe/Paris").dt.hour
    hr = h.groupby("hour").agg(weighted=("weighted", "sum"), updates=("n_stop_updates", "sum"),
                               n_snapshots=("snapshot_ts", "nunique")).reset_index()
    by_hour = [{"hour": int(r.hour), "avg_delay_s": round(r.weighted / r.updates, 1),
                "n_snapshots": int(r.n_snapshots)} for r in hr.itertuples()]

    return {
        "window_days": HISTORY_WINDOW_DAYS,
        "late_threshold_s": LATE_THRESHOLD_S,
        "n_snapshots": len(snapshots),
        "first_ts": snapshots[0]["ts"],
        "snapshots": snapshots,
        "by_route": by_route,
        "by_hour": by_hour,
    }

def main():
    route_names = load_route_names()

    print("Fetching TripUpdate feed...")
    tu_feed = fetch_feed(TRIP_UPDATE_URL)
    snapshot_ts = tu_feed.header.timestamp
    route_rows = compute_route_aggregates(tu_feed)
    for r in route_rows:
        r["snapshot_ts"] = snapshot_ts
    print(f"Computed aggregates for {len(route_rows)} routes")

    print("Fetching Alert feed...")
    alert_feed = fetch_feed(ALERT_URL)
    alerts = extract_alerts(alert_feed)
    for a in alerts:
        a["snapshot_ts"] = snapshot_ts
    print(f"Found {len(alerts)} active alerts")

    month_key = time.strftime("%Y-%m", time.gmtime(snapshot_ts))
    append_csv(
        f"data/history/route_delays_{month_key}.csv", route_rows,
        ["snapshot_ts", "route_id", "n_trips_observed", "n_stop_updates", "n_skipped", "avg_delay_s", "max_delay_s"],
    )
    if alerts:
        append_csv(
            "data/history/alerts_log.csv", alerts,
            ["snapshot_ts", "alert_id", "routes", "header"],
        )

    # current_state.json for the live dashboard
    for r in route_rows:
        r["route_name"] = route_names.get(r["route_id"], r["route_id"])
    route_rows.sort(key=lambda r: r["avg_delay_s"], reverse=True)
    all_delays = [r["avg_delay_s"] for r in route_rows]
    network_avg_delay = round(sum(all_delays) / len(all_delays), 1) if all_delays else 0.0

    current_state = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(snapshot_ts)),
        "network_avg_delay_s": network_avg_delay,
        "n_routes_reporting": len(route_rows),
        "routes": route_rows,
        "alerts": alerts,
    }
    os.makedirs("docs/data", exist_ok=True)
    with open("docs/data/current_state.json", "w") as f:
        json.dump(current_state, f, indent=2)
    print(f"Wrote docs/data/current_state.json (network avg delay = {network_avg_delay}s)")

    summary = build_history_summary(route_names, snapshot_ts)
    if summary:
        with open("docs/data/history.json", "w") as f:
            json.dump(summary, f)
        print(f"Wrote docs/data/history.json ({summary['n_snapshots']} snapshots over the last {HISTORY_WINDOW_DAYS} days)")

if __name__ == "__main__":
    main()
