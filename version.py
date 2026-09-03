"""FilePicker version information.

``VERSION`` is the semantic version of the app. The CI workflow (see
``.github/workflows/build.yml``) reads it to tag releases, and the updater
(``updater.py``) uses it to decide whether a newer binary exists.
"""

APP_NAME = "FilePicker"
VERSION = "0.6.9"