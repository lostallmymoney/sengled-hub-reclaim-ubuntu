# Payload provenance & verification

This directory vendors every on-hub binary the reclaim project needs. The
project is fully standalone: no external checkout is required. Binaries that
have a buildable source live under `source/`; the rest are imported binaries.

## Files

| file                          | SHA-256                                                            | origin |
|-------------------------------|--------------------------------------------------------------------|--------|
| bank2-safe-flash-v2-block     | 1e2620d71f8649df40b12275a658c61a8d22a94abe5a6060b7a3eb896d53f192   | built from source/bank2-safe-flash-v2-block.c |
| em357-flash-v641-public-v1    | 6afbf7f43b57bc7474729bf13dc9bf00d69a7e1c639e90afa9ef50f40127345e   | built from source/em357-flash-v641-public-v1.c |
| em357-v641-live-probe-v1      | 91db79830c99483a4114621f201ed1159f6bdef0a5e85c8e1aff715f53f1b78f   | built from source/em357-v641-live-probe-v1.c |
| ezsp_gateway-v3               | 999b3ad630a466257cfc9a8d5894e526402aaf1f9a5a17ca5462a3a52adde4bf   | built from source/ezsp-listen-bridge-v3.c |
| ezsp_start.sh                 | bebd95504a3455165552c8fc2cec32462a618e288ea813af6371c20d9085bad8   | script (checked in) |
| hub-chmodx-v1                 | 043ac28a5530f477d6fbe8ee5705b64294ffd6cf8240cb6ba50d4d74c024c26a   | built from source/hub-chmodx-v1.S |
| mirror-flash-bank1-safe-v1    | 713c9638720867c1ed03cbcc92ca841b40d0904d9fdc74fd99ad248c3285bc54   | built from source/mirror-flash-bank1-safe-v1.c |

## Verify

To confirm the vendored binaries are intact (run from the repo root):

```bash
sha256sum -c <(cat <<'EOF'
1e2620d71f8649df40b12275a658c61a8d22a94abe5a6060b7a3eb896d53f192  payload/bank2-safe-flash-v2-block
6afbf7f43b57bc7474729bf13dc9bf00d69a7e1c639e90afa9ef50f40127345e  payload/em357-flash-v641-public-v1
91db79830c99483a4114621f201ed1159f6bdef0a5e85c8e1aff715f53f1b78f  payload/em357-v641-live-probe-v1
999b3ad630a466257cfc9a8d5894e526402aaf1f9a5a17ca5462a3a52adde4bf  payload/ezsp_gateway-v3
bebd95504a3455165552c8fc2cec32462a618e288ea813af6371c20d9085bad8  payload/ezsp_start.sh
043ac28a5530f477d6fbe8ee5705b64294ffd6cf8240cb6ba50d4d74c024c26a  payload/hub-chmodx-v1
713c9638720867c1ed03cbcc92ca841b40d0904d9fdc74fd99ad248c3285bc54  payload/mirror-flash-bank1-safe-v1
EOF
)
```

## Notes

- Every on-hub binary is tracked by git. They are required for the tool to
  function; they are not regenerated at build/run time.
- The MIPS C/assembly sources that produce these binaries are in `source/`
  (see `docs/BUILD-NOTES.md` for the clang MIPS cross-compile recipe).
- `ezsp_start.sh` is a shell script payload (not compiled).