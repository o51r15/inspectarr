# Vendored front-end assets

Served by Inspectarr itself so the UI makes no third-party requests and works
on a LAN with no internet. Update by replacing the file and this table.

| File | Version | Source | SHA-256 | Licence |
|---|---|---|---|---|
| `chart.umd.min.js` | Chart.js 4.4.1 | https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js | `81ffafe13c37e1b25793b020d446f4d9739b949dadb7f9f79d709a0cad781c2f` | MIT |
| `chartjs-adapter-date-fns.bundle.min.js` | chartjs-adapter-date-fns 3.0.0 | https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js | `ea7ab30d26c38dcf1f2d26bb43e73a94537b58f1906f55e1a546dd09321b5615` | MIT |

Fonts (`../fonts/`): IBM Plex Sans 400/500/600/700 and IBM Plex Mono 400/500,
latin subset woff2 from `@fontsource/ibm-plex-sans@5.2.5` and
`@fontsource/ibm-plex-mono@5.2.5`. SIL Open Font License 1.1, see `../fonts/OFL.txt`.

These are the versions that were already in use from the CDNs (the ROADMAP's
"Chart.js 4.4.8" note was wrong; 4.4.1 is what the page loaded).
