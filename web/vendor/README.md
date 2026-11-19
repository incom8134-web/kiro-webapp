# Vendored libraries (served by the robot, so the page works offline on the hotspot)

| Library | Version | From | Licence |
|---|---|---|---|
| Remix Icon | 4.9.1 | https://cdnjs.cloudflare.com/ajax/libs/remixicon/4.9.1/ (`remixicon.min.css`, `remixicon.woff2`) | Apache-2.0 |
| Inter (variable, Latin) | 4.1.1 | https://cdnjs.cloudflare.com/ajax/libs/inter-ui/4.1.1/variable-latin/InterVariable-subset.woff2 (cdnjs lists no SRI for fonts) | OFL-1.1 |
| Geist Mono (variable, Latin) | 5.3.0 | npm package `@fontsource-variable/geist-mono` 5.3.0, file `files/geist-mono-latin-wght-normal.woff2`, stored as `geist-mono/GeistMono-latin-wght.woff2` with its licence `OFL.txt`. SHA-256 `684ad5b531f81d43c1e8c7038262d5db7cdc1f68006e04d6c7769efa8d33c8cc` | OFL-1.1 |
| Chart.js | 4.6.1 | https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.6.1/chart.umd.min.js | MIT |

Downloaded 19 Nov 2026; SHA-512 matched the cdnjs SRI hashes. `remixicon.min.css` was changed in one place:
its `@font-face` now loads only `remixicon.woff2`.

Geist Mono (the number font of both pages) was added on 19 Nov 2026 from the npm registry, because cdnjs does not carry it.
