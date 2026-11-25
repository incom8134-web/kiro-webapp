# Vendored libraries (served by the robot, so the page works offline on the hotspot)

| Library | Version | From | Licence |
|---|---|---|---|
| Remix Icon | 4.9.1 | https://cdnjs.cloudflare.com/ajax/libs/remixicon/4.9.1/ (`remixicon.min.css`, `remixicon.woff2`) | Apache-2.0 |
| Inter (variable, Latin) | 4.1.1 | https://cdnjs.cloudflare.com/ajax/libs/inter-ui/4.1.1/variable-latin/InterVariable-subset.woff2 (cdnjs lists no SRI for fonts) | OFL-1.1 |
| Noto Sans KR (variable, Hangul subset) | 2.004 | https://github.com/google/fonts/tree/main/ofl/notosanskr (`NotoSansKR[wght].ttf`, subset to woff2) | OFL-1.1 (`noto/OFL.txt`) |
| Chart.js | 4.6.1 | https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.6.1/chart.umd.min.js | MIT |

Downloaded 19 Nov 2026; SHA-512 matched the cdnjs SRI hashes. `remixicon.min.css` was changed in one place:
its `@font-face` now loads only `remixicon.woff2`.

`noto/NotoSansKR-subset.woff2` is 434 KB (444,140 bytes). It is the variable font
(weights 100–900, so 400/500/600/700 are covered) subset to the 2,350 KS X 1001 Hangul
syllables, Hangul jamo (U+1100–U+11FF), compatibility jamo (U+3130–U+318F), and Korean
punctuation. Every Hangul character in this app is in that set. `unicode-range` on the
`@font-face` keeps Latin on Inter.
