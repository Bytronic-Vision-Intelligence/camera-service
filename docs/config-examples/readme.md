# Camera configuration examples

One file per camera type, in the shape `app/main.py` and the drivers read
today. They are reference material, not deployment config: nothing loads them.
service-orchestrator writes the real `config.yaml` from its own configuration
and launches the binary pointed at it.

`../../config.example.yaml` is the annotated shape and the copy that ships in a
release. These are the deployments that have actually run, kept for the vendor
detail in them — FLIR's opto-isolated line, the LJ-S program settings, which
cameras honour a capture timeout.

They were previously `app/dependencies/config_*.yaml` and were flat: `ip`,
`port` and `trigger_topic` at the top level. A few things about them had drifted
out of agreement with the code that read them, and are corrected here:

* `service.trigger` now requires both `trigger_type` (`software` | `hardware`)
  and `capture_type` (`single` | `continuous`). The old single-field values map
  as: `external` → hardware+single, `internal` → software+single, and
  `continuous` (as `trigger_type`) → software+continuous (or hardware+continuous
  when free-run was intended).
* MQTT topics may use `{project}` / `{camera_id}` placeholders, filled at
  startup.
* Prefer the `images:` multi-output list (each entry can set `topic_end`,
  `image_format`, `archive`) over a single `image:` block.
* `capture_timout` was a typo, and `10` meant ten *milliseconds* — enough for a
  real Pylon or FLIR grab to time out on every trigger. It is `capture_timeout`
  here, at a workable 5000.
* `is_archived: "true"` is a string, and so is `"false"` — both are truthy, so
  archiving could not be turned off. They are booleans here.
