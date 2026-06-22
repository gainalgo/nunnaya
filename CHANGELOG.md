# Changelog

All notable changes to this project are documented in this file.
The format is loosely based on [Keep a Changelog](https://keepachangelog.com/);
entries are grouped by date.

## [Unreleased]

### Fixed

- **Config save silently dropped every setting past position 80 in large
  groups.** The dashboard saves settings by chunking them into multiple POST
  requests to keep each URL within length limits. The loop advanced its cursor
  by **250** per iteration but sliced only **80** fields per request, so any
  field beyond index 80 within a group was never included in any request.
  Groups with roughly 200 fields (e.g. the Regime group) lost more than half
  their settings on every save — toggles appeared to "revert" after a reload
  while smaller groups saved fine, which made the symptom intermittent and hard
  to trace. The backend was never at fault; the values simply never reached it.
  Fixed by aligning the loop stride with the slice size (`250 → 80`), so all
  fields are sent across consecutive chunks. Applies to the three spot
  dashboards. The futures dashboard already used a matching stride and slice and
  was not affected.
