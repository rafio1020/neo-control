# PredatorSense 4.1.635 — local reverse-engineering findings

Analyzed the supplied Windows package using official REA 6.7.0 and Ghidra 12.1.4. No Windows executable or extracted JavaScript was executed. These are static observations; the separate Linux hardware tests determine whether a control actually works on this PHN16-71.

## Performance and fans

The AppX contains an Electron application, with `app/resources/app.asar`. Its bundled `main.js` explicitly defines these operating-mode values:

| Windows application name | Value | Linux-facing label |
|---|---:|---|
| QUIET | 0 | Quiet |
| DEFAULT | 1 | Balanced |
| EXTREME | 4 | Performance |
| TURBO | 5 | Turbo |
| ECO | 6 | Eco |

The application serializes operating-mode requests as an object containing `mode`. Its fan enum is **Auto=0, Max=1, Custom=2**. These application values must not be confused with the WMI fan-behavior values **1, 2, 3** used by the Linux protocol implementation. The bundled scenario defaults contain separate CPU/GPU custom settings, with automatic control enabled and a stored custom speed of 30.

Evidence: REA static application record `ev_6a346f6bb181a36749e13c0fb0c2383dc89397ddc334a372a4750919c423c2f0`; selected sources `package.json`, `main.js`, `main.js.map`. Original minified main.js character offsets: operating enum 24530; fan enum 24826; custom default 30381; request-packing routine 64858. Source-map names are present, but original source contents are not embedded. Analysis is partial: 2,873 other inventoried files were outside this REA source selection.

Native REA/Ghidra inspection of AcerHardwareService.exe found the named `AcerGamingFunction` fan-behavior, fan-speed, fan-table and miscellaneous-setting interfaces. That string evidence identifies interfaces, not complete payload semantics or runtime success. Record: `ev_c26e6f4ff7a0140cc14ee7c4111cb4e102615ed272d51fc1ed73d89456405b92`.

## Keyboard effects

REA/Ghidra decompilation of the supplied `AcerECKeyboardController.dll` establishes the firmware-facing effect IDs, distinct from the broader Electron UI's `SupportedEffect` enum:

| ID | Effect | User color | Direction |
|---:|---|---|---|
| 0 | Static | Yes | Not advertised |
| 1 | Breathing | Yes | Not advertised |
| 2 | Neon | No | Not advertised |
| 3 | Wave | No | Left/right; generic metadata also advertises up/down and horizontal/vertical |
| 4 | Shifting | Yes | Left/right |
| 5 | Zoom | Yes | Not advertised |
| 6 | Meteor | Yes | Not advertised |
| 7 | Twinkling | Yes | Not advertised |

The mode constructor at `0x180007b20` records speed bounds **1–9** and brightness **0–100**. Metadata flags are 0x51 for modes 0,1,5,6,7; 0x11 for Neon; 0x1f for Wave; 0x53 for Shifting. Interpretation of these flags is corroborated by the official [OpenRGB 0.7 controller header](https://github.com/CalcProgrammer1/OpenRGB/blob/release_0.7/RGBController/RGBController.h), whose mode layout and flags match this library. Static-mode speed being advertised does not prove a visible effect.

The native SetMode routine at `0x180001aa0` constructs this **16-byte** buffer:

```
mode, speed, brightness, 0, direction_argument + 1, R, G, B,
3, 1, 0, 0, 0, 0, 0, 0
```

The direction increment occurs inside the native library. A Linux implementation that already uses firmware direction values must not increment them twice. The packet always contains RGB bytes, even for modes whose metadata does not advertise custom color; therefore ignored fields must not cause false readback failures.

Evidence: modes `ev_40a1dd0bdd68db332c0ba8793c33dcc928f712c36faa560e05ecdb1eb72c03df`; SetMode `ev_9b632072c32456a48c6ad7401826614d30822981a788dbadd71da4b1394b572a`. Decompiled output is recovered pseudocode, not Acer's original source. Ghidra did not load a matching PDB. Some short effect strings are undefined as strings in Ghidra; their original PE bytes and constructor references were cross-checked directly.

## GPU clocks

The supplied `Predator PHN16-71.ini` contains five PCI device sections. Each declares Turbo GPU offset **100** and RAM offset **200**, with zero for Quiet/Default/Extreme. The file declares Count=3 while listing five device entries, so configuration alone does not establish how every entry is selected. These are Windows configuration observations, not evidence that a Linux NVIDIA interface supports equivalent offset writes. Do not silently equate selecting the firmware Turbo profile with applying these GPU clock offsets.

## Battery limit: unresolved in this package

A bounded literal search of the Electron main bundle and main renderer bundle found BATTERY_BOOST, but no matches for the tested battery-limiter, charge-limit or calibration names. BATTERY_BOOST is not evidence of an 80% charging limit. This does not prove the entire package lacks a battery-limit path. The Linux driver's battery-limit protocol must be attributed to its separately documented firmware interface and hardware readback tests, not to these static Windows findings.

## Reproducibility

Published SHA-256 identities and evidence IDs are indexed in `evidence/rea-index.json`. Paths marked `<local-analysis>` or `<local-package>` refer to private local analysis artifacts, not files shipped in this repository. Full analysis artifacts remain in the original workspace, including ASAR inventory/extraction, selected JavaScript analysis, native string evidence and native keyboard decompilation. Acer's proprietary binaries and bundled application code have not been copied into the user-facing outputs.

REA was used through its CLI without changing Codex MCP configuration. Ghidra and a locally assembled Fedora JDK are under work; the system Java installation was not modified. No claim of complete PredatorSense parity follows from this analysis.


## Follow-up: Windows fan/profile interaction

Inspection of the actual renderer bundle resolves an important distinction. The active V2023 operating-mode screen (`app-mode-nitro`) requests Automatic when selecting Quiet; its Turbo selection does not explicitly request Maximum. The bundled legacy screen (`app-mode`) does pair Turbo with Maximum, but the system-page selector uses V2023 because `IsV2023` returns true in this build. The separate `TurboButton` lock path explicitly chooses Maximum; configurable mode-switch-key paths differ. These are static application observations, not a complete trace of native Windows service side effects.

Windows Custom sliders send their changes immediately, and the payload includes independent automatic/manual flags for CPU and GPU fans. Neo Control currently requires Apply for sliders, changes both fans' control modes together, keeps cooling active when the window closes, and restores Auto on explicit Quit or lease expiry. The PredatorSense key opens the app; the physical Turbo key is not handled. It is therefore not full Windows behavior parity.

A confirmed Neo Control mismatch has been corrected: ordinary profile changes now retain the selected Maximum/manual setting instead of always leaving Auto. Quiet and Eco select Auto. Hardware transition evidence is in `evidence/profile-fan-retention.json`; renderer identity and offsets are in `evidence/windows-fan-behavior.json`.
