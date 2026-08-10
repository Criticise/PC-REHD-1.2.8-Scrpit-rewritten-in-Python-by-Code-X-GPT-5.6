# Codex V4 NumPy Fixed Floor

This directory is the ABI-specific offline repair floor for the patched `ufbx`
runtime dependency. Bootstrap must prefer these wheels before the packaged
`_vendor_py/cpXXX` lane and before any network repair.

## Approved artifacts

- `numpy-2.5.1-cp314-cp314-win_amd64.whl`
  - SHA256: `24d0eb82c0541d3415a33425db64ae439dffccd7b4dbcb30e7c35120205c506a`

## Maintenance rules

1. Never load a wheel whose CPython ABI does not match the active runtime.
2. Install or unpack into a staging lane, then run a fresh-child `numpy` and
   patched `ufbx` health probe before committing the lane atomically.
3. A network candidate is an upgrade candidate, not a replacement for this
   fixed floor. Record its version and artifact SHA256 before trying it.
4. Block only the exact failed version plus artifact fingerprint. A newer
   version or rebuilt artifact must be eligible for a new staged attempt.
5. Network failure must not invalidate a healthy active or last-known-good
   runtime lane.
