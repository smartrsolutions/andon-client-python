# Andon client for Python

Send values from Python to [Andon](https://andon.app), the app for iPhone and iPad
that shows your data as tiles and widgets. Everything is end-to-end encrypted: the
content key stays in your process, and only the devices you paired can read what
you send.

**Get the app:** [Andon on the App Store](https://apps.apple.com/app/id6819064723).

## Getting started

1. **Design** your tiles in the [Andon configurator](https://andon.app/en/configurator)
   and create the view there. Download the `.env` and the document, `view.json`:
   it is the template your values fill.
2. **Pair** your iPhone or iPad: scan the configurator's QR code with the
   [Andon app](https://apps.apple.com/app/id6819064723).
3. **Install** this package: `pip install andon-client`.
4. **Send values**, by tile ID:

```python
from datetime import datetime, timezone
from pathlib import Path

from andon_client import AndonView

# The .env from the configurator, as it is: ANDON_VIEW, ANDON_WRITE_SECRET,
# ANDON_CONTENT_KEY, ANDON_KEY_VERSION. Variables set in the environment win.
view = AndonView.from_env(env_file='.env', template=Path('view.json'))

view.set('t_oee', 79.3)                                    # one tile
view.set({'t_oee': 79.3, 't_shift': 580})                  # several at once
view.set('t_takt', 31.5, at=datetime.now(timezone.utc))    # appended to a series

view.close()                                               # sends what is still pending
```

The package collects, checks, encrypts and uploads, in a thread of its own. A
script that sets its values and ends still sends them: Python waits for that
thread until they are out, at the end of the upload interval if one was sent just
before. When the relay cannot be reached then, that try is the last one: the script
ends, and `on_error` (or the log) says what was not sent. `close()` sends at once;
a long-running process calls it when it stops, or uses the view in a `with` block.

The same package exists for Node.js,
[`@smartrsolutions/andon-client`](https://www.npmjs.com/package/@smartrsolutions/andon-client),
and for Node-RED, [`@smartrsolutions/node-red-andon`](https://www.npmjs.com/package/@smartrsolutions/node-red-andon),
with the same rules.

## Values

`set(tile_id, value, at=None, series=None, lane=None)` gives one tile a value.
What the value does depends on the tile:

| Tile | Value | Effect |
|---|---|---|
| label | number or text | replaces the value |
| gauge, progress, donut | number | replaces the value |
| line, bar over time | number | appended to the series, now or at `at`; `series` picks the series |
| line, bar over time | list of `[timestamp, value]` pairs | replaces the series; `series` picks which |
| line, bar over time | `{'series': [{'name', 'samples'}]}` | replaces every series |
| line, bar over time | `{'series': [{'name', 'start', 'stepSec', 'points'}]}` | replaces every series: a point every `stepSec` from `start` |
| bar with categories, pie, donut | list of numbers, one per category | replaces the values; `series` picks the series |
| bar with categories, pie, donut | `{'categories', 'series': [{'name', 'values'}]}` | replaces categories and values together |
| bar with categories, one series | `{'series': [{'name', 'values', 'statuses'}]}` | a verdict per bar: `ok`, `warning`, `critical` or `None` |
| table | list of rows, a row a list of cells | replaces the rows |
| table | list of `{'cells', 'status'}` | replaces the rows, with a status per row |
| timeline | state ID | starts a new segment, now or at `at`; `lane` picks the lane |
| timeline | `{'from', 'to'}` | moves the time axis; what lies before `from` is trimmed |
| any tile | dict, e.g. `{'value': 79.3, 'status': 'warning'}` | sets the named fields; `None` removes one |

`at` is a `datetime` (a naive one is local time, as Python takes it), seconds since
the epoch as `time.time()` gives them, or ISO 8601 text. `series` and `lane` are
names. Numbers may arrive as text (`"79.3"`), as MQTT and most PLCs deliver them.
Timestamps inside values are ISO 8601 in UTC with a trailing `Z`, such as
`2026-09-22T18:30:05Z`.

`set({tile_id: value, ...}, at=..., series=..., lane=...)` sets several tiles at
once; each value takes the same forms, and the call is taken or refused as a whole.
Values set together go out in the same upload.

`replace(document)` swaps in a whole view document, template included: for a source
that builds its tiles itself rather than filling those of a template.
`generatedAt` and `staleAfterSec` are set on upload.

A value that does not fit its tile raises `InputError`, one that would leave the
view invalid raises `ValidationError`, both a `ValueError`; the message says what
the tile takes, and the view stays as it was. The first real value of a series or
timeline lane replaces the points the template came with: those are the
configurator's start values.

## When it uploads

- A change goes out at most once per **interval** (`interval_sec`, 30 by default,
  10 at least). The first change after a quiet spell goes out at once.
- While values keep arriving, even unchanged ones, a heartbeat goes out after half
  the **stale** time (`stale_after_sec`, three intervals by default).
- When no value arrives, nothing is sent: the app then shows the view as stale,
  which is what it should show when your source has stopped.
- Devices are woken only when a tile's **status** changes (`notify='auto'`;
  `'always'` or `'never'`). Every wake-up costs battery on every paired device.

`flush()` sends a pending change now and returns the relay's answer; `close()`
sends what is pending and stops the thread (`close(flush=False)` drops it).

## Status

The status of a tile (`ok`, `warning`, `critical`) is your judgement: the app does
not compute it from the value. Send it with the value, or name the tile in
`derive_status` to have it derived from its bands.

## Options

```python
AndonView(
    view,                  # vw_..., from the .env
    write_secret,          # ws_..., from the .env
    content_key,           # base64, from the .env
    key_version=1,         # from the .env
    template=None,         # the template: a dict, its JSON text, or a path to view.json
    interval_sec=30,
    stale_after_sec=None,  # three intervals
    notify='auto',
    window=None,           # {'points': 60, 'age_sec': 0}: how much of a series is kept
    derive_status=(),      # tile IDs whose status follows from their bands
    on_sent=None,          # f(report), after every stored upload
    on_error=None,         # f(failure): message, status, retry_in_sec; logged without it
)
```

`AndonView.from_env(prefix=None, env=None, env_file=None, **options)` reads the
keys from the environment, and from a `.env` file if you name one. A prefix such as
`ANDON_CLOUD_` replaces the leading `ANDON_` of every name, so several views can
share one environment.

After an upload failed, `on_error` says when the next try is (`retry_in_sec`). A
rejected write secret or an unknown view stops the uploads until the next value:
trying again changes nothing until the `.env` is current. Without `on_error`, the
message goes to the `andon_client` logger as a warning.

`on_sent` and `on_error` run in the thread that uploads: the package's own, or
yours during `flush()` and `close()`. Keep them short.

## Security

- The content key never leaves your process. The write secret is sent only in the
  `Authorization` header of the upload, and neither appears in a report or an
  error message.
- The `.env` from the configurator also holds the invite secret. This package does
  not read it: a data source does not pair devices.

## Requirements

Python 3.9 or newer. The package depends on
[`cryptography`](https://pypi.org/project/cryptography/) for the encryption and
[`jsonschema`](https://pypi.org/project/jsonschema/) for the checks; HTTP goes
through the standard library, and honours the usual proxy variables.

## Contributing

The package is developed in a private repository next to the Andon relay and its
specification; its GitHub repository is a public mirror that receives every release
as one commit. Issues are welcome there, or by mail to <support@andon.app>. Pull
requests cannot be merged in the mirror, because the next release would overwrite
them: a change we take arrives with the next release. Security problems: please
write to <support@andon.app> instead of opening a public issue.

## License

Copyright 2026 SMARTR.solutions. MIT License; the text is in the file `LICENSE` of
this package.
