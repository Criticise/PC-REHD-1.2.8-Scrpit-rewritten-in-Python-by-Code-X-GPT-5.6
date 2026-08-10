Drop newer approved local `orjson` wheels here.

Examples:

- `orjson-3.12.2-cp312-cp312-win_amd64.whl`
- `orjson-3.12.2-cp314-cp314-win_amd64.whl`

Bootstrap order:

1. `_vendor_upgrade/orjson_re6_v4`
2. `_vendor_fixed/orjson_re6_v4`

Rollback order:

1. `_vendor_fixed/orjson_re6_v4`
