Expected local Python runtime bundle layout:

- `python-3.14.6-embed-amd64.zip`

Bootstrap reports this local baseline bundle for system-level recovery. The
BAT/PS1 first-stage detector installs the signed full Python 3.14.6 EXE when no
working interpreter exists; Python A/B management owns later upgrades and
rollbacks.

Bundle reporting now validates archive structure instead of only checking
whether a file exists. A broken or truncated embed zip is reported as invalid
and does not count as an available recovery runtime.

Bootstrap itself cannot execute before Python exists. That first-stage decision
therefore belongs only to the BAT/PS1 detector, not Launcher or 3ds Max.
