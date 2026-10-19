# Statistics calculations

All `/stats` calculations are scoped to the authenticated uploader. Detection
statistics use READY uploads so partial and failed processing results do not
inflate counts. The `days` parameter on the summary and PDF affects only trends;
the remaining KPIs use all retained uploads.

| Metric | Definition and checks |
| --- | --- |
| Trash composition | Count image and video detection rows by label, then divide each count by the total. Percentages are rounded independently to two decimals. |
| Environmental footprint | Sum recorded `area_sqm` values across the same detection rows; unknown areas contribute no area, but their detections still count. Verified against composition and manifest totals. |
| Fleet efficiency | Per assigned worker, READY / (READY + FAILED). Pending and in-progress uploads are excluded from the denominator. Configured idle workers and historical workers are retained. An empty denominator preserves the existing 1.0 default. |
| Tasks processed today | Successful uploads whose first recorded READY transition is today in UTC. The stats endpoint uses the current user's uploads; the operational dashboard uses all users. Worker state counters are not used for personal statistics. |
| Temporal trends | Completed uploads containing trash, grouped by upload date in UTC. Exactly `days` calendar dates ending today are returned, including zero days. Future timestamps are excluded. |
| Processing time | First processing READY timestamp minus upload timestamp, averaged once per successful upload. Later notes and Lab validation logs are excluded. Missing or invalid completion timings are omitted. Worker breakdowns attribute that same elapsed time to the assigned worker; the overall average also includes unassigned uploads. |
| Hotspot density | Distinct PostGIS grid cells containing located detections with confidence >= 0.8. Images contribute their location once; videos contribute qualifying detection locations. The separate media count deduplicates video IDs. |
| Fun facts | Area and label facts use the same READY detections. Worker activity counts terminal tasks, and success rate excludes unfinished uploads. Northernmost finds include video detections and latitude zero. |
| Map grid | READY images and located video detections are counted once per map report. Confidence is expressed as a percentage, using maximum detection confidence per image. Dominant labels use detection counts with alphabetical tie-breaking. Density is reports per square degree (`count / resolution²`). |
| Dashboard CPU / memory | CPU usage is normalized by available cores and cgroup quota. Host usage is measured when cgroups are unavailable. Memory uses the effective memory limit with host fallback. |
| Dashboard engagement | Active users expire after five minutes, including on reads. UTC daily activity includes today and fills elapsed inactive days. Request durations use a monotonic performance clock. These metrics remain process-local. |
| Dashboard uptime | Process age is reported separately. The availability percentage is `null` because no persisted uptime/downtime history exists. Process age divided by 30 days does not measure availability. |
| Reports | Excel preserves one row per detection and converts fractional confidences to percentages. Both PDF languages include all six KPIs and every requested trend date. |

Area is the sum of measured detection areas, not a deduplicated estimate of unique
land coverage. Video detections can show the same physical object in successive
frames. Hotspots use an angular grid of 0.001 degrees; they are an approximation
of spatial concentration, not clusters at a fixed distance in meters.

SQL queries on the request's AsyncSession execute sequentially. Summary responses
retain the existing five-minute cache and map grids the existing one-minute cache.

## Verification

`tests/test_stats_calculations.py` executes the real PostgreSQL/PostGIS queries
against mixed images, video detections, failed and unfinished uploads, missing
areas/logs, historical workers, and another user's data. Other regression tests
cover the endpoints, dashboard time boundaries and CPU quotas, and rendered PDFs.

Run unit tests with `pytest`. For integration tests, explicitly set
`TEST_DATABASE_URL` to a disposable PostgreSQL/PostGIS database whose name ends in
`_test`. Each run creates and removes a unique schema; tests never use the
application's `DATABASE_URL` or drop application tables.
