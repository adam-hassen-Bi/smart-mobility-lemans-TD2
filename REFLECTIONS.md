# Reflections — TD2 Smart Mobility Le Mans

## 1. Before building — easy vs. hard sub-questions

The central question has three parts: *where* is the network weak, *when* is it weak,
and *where is supply not matching demand*. I expected "where" to be the easiest,
because the timetable (GTFS) is published, so scheduled supply per stop is just a
count. "When" looked harder, because it needs delays observed over many days, and
the supply-vs-demand part looked the hardest, because there is no ridership data at
all, only a proxy.

## 2. Prediction — why the static feed does not refresh every 15 minutes

The timetable only changes a few times a year, when SETRAM publishes a new GTFS
file. Downloading 2.4 MB and recomputing 200,000 stop-times every 15 minutes would
produce the same result almost every time, waste runner time and fill the Git
history with thousands of identical commits.

## 3. Prediction — auto-typed ID columns joined together

If `stop_id` becomes an integer in one file (all values look numeric) and stays a
string in another (because of a value like `FLEX-381`), then `1001` and `"1001"` are
different values: the join either fails with a type error or, worse, runs and
silently matches nothing, leaving empty or NaN rows. Reading every GTFS file with
`dtype=str` keeps IDs as labels, which is what they are — nobody does arithmetic on
a stop ID.

## 4. Storage — per-stop vs. per-route logging over a semester

A 12-week semester is 84 days × 96 snapshots a day ≈ 8,000 snapshots. With about
5,000 stop-time updates per snapshot, logging every update gives roughly **40 million
rows** (several GB of CSV — not something to keep in Git). One row per route (≈ 26–30
routes) gives about **230,000 rows**, a few MB. The per-route version loses *where on
the route* delay builds up: a planner asking "which junction or stop should get a
bus lane or a priority traffic light?" cannot answer it, because the delay of a
whole route is averaged into one number.

## 5. Alternative to the early-good / late-bad colour rule

I would use a passenger rule: colour by how far a route is **off schedule in either
direction**, with an on-time band (between 1 min early and 3 min late) in neutral
grey, and both early and late beyond it in the warning colour. I built this as the
"Off schedule" toggle on the dashboard. On the snapshot of 21 October at 11:17, the
original rule puts route 5 (+1 min 29) at the top and shows route 34 (−2 min) as the
"best" route; under my rule route 34 and route 15 (both running well ahead) jump to
the top. A planner would then also act on early-running routes — adjusting
timetables or holding buses at timing points — instead of only chasing late ones.

## 6. Scheduling mismatch — both halves

- **Static every 15 minutes is wasteful**: the input changes a few times a year, so
  96 runs a day recompute the same reference, use runner time and add 2,880 identical
  commits a month for no new information.
- **Real-time once a month is useless**: delays are perishable and GTFS-RT keeps no
  archive. One snapshot a month is 12 random moments a year — it cannot show rush
  hour versus off-peak, a disruption that lasts two hours, or whether a route is
  chronically late. A snapshot that is not taken is lost forever.

The schedule of each pipeline should match how fast its source changes.

## 7. Sanity check of one delayed route

Route 5 is the most delayed route in all three snapshots taken on 21 October between
11:08 and 11:17 (average +1 min 06 to +1 min 29, worst single trip +11 min 39), and
in every snapshot it also reports **60 to 64 skipped stops** — far more than any other
route except 28 (51 to 59). A stable number of skipped stops across snapshots looks like a
planned diversion (roadworks or a closed street) rather than random traffic, and a
diversion would also explain the extra minutes. The other extreme is an artefact:
route 34 shows about −2 min, but it is based on only 1 or 2 trips and 16 updates,
with a maximum delay of 0 to 8 seconds — so it means "two buses predicted early", not "the best route in
the network". Small samples need to be read with their sample size next to them,
which is why the dashboard shows the number of trips and updates in every tooltip.

## 8. When population misleads as a demand proxy

The area around the 24 Heures circuit, the MMArena stadium and the exhibition centre
in the south of the agglomeration has almost no residents, but tens of thousands of
visitors on event days, so real demand is far above what population suggests. The
reverse also exists: a residential commune where most people own a car and work
nearby can have a large population but low transit demand.

## 9. Supply metric — and a commune where it misleads

**Metric:** scheduled weekday stop-visits per 1,000 residents (one bus or tram
stopping once at one stop = one stop-visit). The network average is 202.

**Where it misleads:** in my output **La Chapelle-Saint-Aubin** is ranked first with
1,010 stop-visits per 1,000 residents — five times the network average — ahead of
Coulaines (762) and Rouillon (715), while **Le Mans itself is only 10th (154)**.
That is clearly wrong: Le Mans is where the trams and almost all lines run. The cause
is the stop-to-commune rule: each stop is assigned to the commune whose **centre** is
nearest, not to the commune it is actually in. La Chapelle-Saint-Aubin (2,319
residents) receives 53 stops — almost as many as Allonnes (69 stops, 10,846
residents) — because stops in Le Mans' outer neighbourhoods are closer to its centre
than to the centre of Le Mans. Small communes next to Le Mans are inflated and Le Mans
is deflated. At the other end, Fay, Chaufour-Notre-Dame and Fatines have only 2–3
stops, and a single stop carries 50–66 % of their supply, so their figure depends on
one stop. The dashboard flags all of these. The fix would be to assign stops with
the real commune boundaries (a point-in-polygon join with the official contours).

## 10. Aggregating stop-level delays without blowing up the file

Instead of appending raw events, I would keep a **fixed-size summary table that is
updated in place**: one row per stop × hour of day × day type (weekday / Saturday /
Sunday), holding running totals — number of observations, sum of delays, number of
observations more than 3 minutes late, and the maximum. That is about 800 stops ×
24 × 3 ≈ 58,000 rows whatever the length of the semester, and the average delay or
share of late arrivals is computed from the totals. If a dated trace is needed, a
daily roll-up per stop (800 rows a day, ≈ 67,000 rows for the semester) stays small
enough for Git.

## 11. Back to the first predictions

- **Answerable today:** current delays by route, active alerts, and scheduled supply
  per stop and per commune — as I expected for "where".
- **Needed judgment calls:** more than I expected. The network delay KPI is a
  construct: the TD's unweighted average of route averages gives **+2.7 s** on the
  11:17 snapshot, while weighting every stop-time update equally gives **+14 s**,
  because small routes running early pull the unweighted figure down. The on-time
  band, the colour rule, the nearest-centre assignment and the population proxy are
  all choices that change the conclusion.
- **Needs weeks of history:** the "when" (the hour-of-day chart only has one hour so
  far), which routes are *chronically* late rather than late once, and any
  prediction. My first guess was right that "when" is the hard part; I
  underestimated how much the supply-vs-demand answer depends on modelling choices
  rather than on the data itself.
