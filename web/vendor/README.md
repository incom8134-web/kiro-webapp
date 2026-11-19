# Vendored libraries (served by the robot, so the page works offline on the hotspot)

| Library | Version | From | Licence |
|---|---|---|---|
| Remix Icon | 4.9.1 | https://cdnjs.cloudflare.com/ajax/libs/remixicon/4.9.1/ (`remixicon.min.css`, `remixicon.woff2`) | Apache-2.0 |
| Inter (variable, Latin) | 4.1.1 | https://cdnjs.cloudflare.com/ajax/libs/inter-ui/4.1.1/variable-latin/InterVariable-subset.woff2 (cdnjs lists no SRI for fonts) | OFL-1.1 |
| Chart.js | 4.6.1 | https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.6.1/chart.umd.min.js | MIT |

Downloaded 19 Nov 2026; SHA-512 matched the cdnjs SRI hashes. `remixicon.min.css` was changed in one place:
its `@font-face` now loads only `remixicon.woff2`.
