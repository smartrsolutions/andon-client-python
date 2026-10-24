"""
The view's values from the environment: the names the .env from the Andon
configurator uses, so that file can be handed to a process unedited:

  ANDON_URL  ANDON_VIEW  ANDON_WRITE_SECRET  ANDON_CONTENT_KEY  ANDON_KEY_VERSION

A prefix replaces the leading ANDON_ of each: with ANDON_CLOUD_, the view ID is
read from ANDON_CLOUD_VIEW. That is what lets several views share one
environment - one prefix per view.
"""

from __future__ import annotations

import os
import re
from typing import Dict, Mapping, Optional

from ._js import trim

# Upper case, ending in an underscore, so that ANDON_CLOUD_ + VIEW reads like a
# variable name.
PREFIX = re.compile(r'[A-Z][A-Z0-9_]*_')

SUFFIXES = ('URL', 'VIEW', 'WRITE_SECRET', 'CONTENT_KEY', 'KEY_VERSION')


def name(prefix: Optional[str], suffix: str) -> str:
    """
    name is the variable a suffix is read from. Every message about a missing value
    has to go through here, otherwise it names a variable nobody set.
    """
    return (prefix or 'ANDON_') + suffix


def pick(prefix: Optional[str], suffix: str, env: Optional[Mapping[str, str]] = None) -> str:
    """pick reads one value, trimmed; the empty string counts as unset."""
    return trim((env if env is not None else os.environ).get(name(prefix, suffix)) or '')


def check_prefix(prefix: Optional[str]) -> str:
    """
    check_prefix raises on a prefix that is not one. An empty prefix is not an
    error: it means the default, ANDON_.
    """
    if not prefix:
        return ''
    if not PREFIX.fullmatch(prefix):
        raise ValueError('environment prefix "' + prefix + '" has to be upper case and end in an '
                         'underscore, for example ANDON_ or ANDON_CLOUD_')
    return prefix


def read_file(path: 'os.PathLike[str] | str') -> Dict[str, str]:
    """
    read_file reads a .env as the configurator writes it: NAME=value lines, # for a
    comment, a value in quotes taken without them, a leading `export ` allowed.
    """
    out: Dict[str, str] = {}
    with open(path, encoding='utf-8-sig') as f:
        for line in f:
            line = trim(line)
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = trim(line[len('export '):])
            name, eq, value = line.partition('=')
            if not eq or not trim(name):
                continue
            value = trim(value)
            if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
                value = value[1:-1]
            out[trim(name)] = value
    return out
