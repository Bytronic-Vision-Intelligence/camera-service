# Tools

Reserved for operational scripts specific to a service. Run them from the
repository root so imports and relative paths resolve consistently.

Intentionally empty in the template. Release tooling lives in `scripts/`
(`package.sh`) and local CI in `docker-local/`.

The service takes no arguments beyond `--help`: it reads `config.yaml` from the
directory it runs from.

```bash
python app/main.py
```
