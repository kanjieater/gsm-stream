# Server-managed browser settings

`profiles.yml` → `defaults.ui` is authoritative for explicitly listed UI settings.
On page load, `/bridge-sync.js` updates those localStorage settings and the
corresponding fields in saved presets before the upstream frontend initializes.
Boolean storage uses upstream's `1`/`0` format; preset values remain JSON booleans.

Changing a configured setting in the browser is temporary: the server's value
returns on reload or when selecting a reconciled preset. To leave a setting
browser-managed, omit it from `defaults.ui`. Removing it does not reset the last
saved browser value. Already-open tabs do not receive server changes until reload.

Only UI setting keys belong in `defaults.ui`, not data keys such as `lineData`,
`userNotes`, `timeValue`, `settingPresets` or `actionHistory`. The bridge never
clears the storage namespace on a defaults change. Text, notes, timer, unknown
keys, preset metadata and unconfigured settings must be preserved.

Regression tests execute the real bridge template with existing storage,
including false-to-true and true-to-false changes and stale saved presets.
A Chromium rehearsal against the actual GSM 2026.9.2 frontend also confirmed
that an existing disabled 'Remove all Whitespace' checkbox becomes checked
on reload while saved text remains rendered and notes/timer survive. The test
uses intercepted network traffic; it does not inject fixture text into GSM.

Changing a host's profiles file does not update another host automatically.
For isolated instances, explicitly update both files if lockstep is desired.
The primary container remains stopped during the instance-2 upgrade test.
