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
`port` and `trigger_topic` at the top level. Three things about them had drifted
out of agreement with the code that read them, and all three are corrected here:

* `trigger_type` sat under `trigger:`, which the drivers read, while `main.py`
  looked for it under `camera:`. It now lives under `trigger:` only.
* `archive_parameters` was never read: `main.py` requires `archive_params`.
* `is_archived: "true"` is a string, and so is `"false"` — both are truthy, so
  archiving could not be turned off. They are booleans here.
