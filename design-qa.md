# Neo Control native interface QA

final result: passed

## Scope and visual target

User request: make the existing native Linux control panel more like PredatorSense, while preserving working hardware controls and adding the previously requested 45°C target. This is a visual adaptation, not an exact replica or a web prototype. Acer branding, robot art, game shortcuts and unsupported controls are not reproduced.

Source: official Acer PHN16-71 manual, pages 46 and 54. Local source visual truth: `../../work/predatorsense-reference-home.png` and `../../work/predatorsense-reference-fans.png`, each 1058×1500. Relevant application crops: home (200,779)-(915,1173), fans (200,269)-(915,667). The manual embeds smaller screenshots; these are layout/color references, not pixel-perfect specifications.

Implementation: `evidence/gui-overview.png`, `evidence/gui-cooling.png`, `evidence/gui-lighting.png`, `evidence/gui-battery.png`; app-content captures 1160×759 from a native 1160×820 window (window decoration excluded), scale 1. No CSS/browser viewport applies to GTK.

Combined full-view comparison inputs: `../../work/comparison-overview.png` and `../../work/comparison-cooling.png`. Each source crop and implementation was scaled to 900 pixels wide without changing aspect ratio and viewed together. States: live overview and fan-control screen. The reference fan screen shows Auto; the implementation cooling capture deliberately shows the new 45°C target and real Maximum RPM. Telemetry values and supported controls differ intentionally. Focused region comparison was not needed after full-view checks at readable resolution.

## Findings and history

- Fixed P2: first dashboard capture had its heading scrolled out of view by initial widget focus. Navigation now explicitly receives focus; a fresh capture shows the full heading and main controls.
- Fixed P2: oversized gauge cards pushed the overview shortcut below the initial viewport. Reduced gauge height and replaced the large shortcut card with a compact row. Updated overview shows all primary controls.
- Fixed P2: missing fan telemetry could leave old RPM gauge values visible. Missing driver data now clears RPM gauges and labels them unavailable.
- Current comparison: no unresolved P0/P1/P2 issues for the requested adaptation. Dark cyan theme, top tabs, circular live readings, selected mode tiles and persistent monitoring column are present. Long Lighting/Custom content scrolls within the content area while navigation and status remain visible.

## Verification

Native GTK smoke check emitted clicks on each navigation tab and checked the selected page; Custom panel opened and closed through its actual button callback. All four pages rendered without GTK errors. Active target capture shows its selected tile, hottest temperature, Maximum status, and RPM readings. Target-enabled screenshot exit restored firmware Auto, confirmed from sysfs. Hardware testing and 25 unit tests passed; no claim of Windows feature parity or guaranteed temperature cap.

## Follow-up polish

P3: animated backgrounds and Acer's decorative graphics are absent; typography follows installed native fonts. Resizing at very small desktop sizes was not exhaustively tested. A sustained gaming thermal benchmark has not been run.
