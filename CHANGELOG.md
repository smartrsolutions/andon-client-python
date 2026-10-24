# Changelog

Versions follow [semantic versioning](https://semver.org): a major version may
break existing code, and its entry says what to change.

## 1.0.0

The first version.

- **AndonView**: `set(tile_id, value, at=, series=, lane=)`, `set({tile_id: value})`,
  `replace(document)`, `flush()`, `close()`, `with`, `AndonView.from_env()` with
  `env_file` for the configurator's `.env`, `on_sent` and `on_error`.
- **The rules of `@smartrsolutions/andon-client` 1.0.1**, the package for Node.js:
  what a value does to its tile, the checks and the words of every refusal, the
  sealing, and when an upload goes out. Its answers to some 2,900 documents and
  values are part of this package's tests.
- Uploads run in a thread of the package's own, which lives while an upload is
  due; a script that sets its values and ends still sends them.
