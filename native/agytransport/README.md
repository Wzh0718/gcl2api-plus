# Antigravity native transport

Optional Go helper for Antigravity HTTP traffic. Python connects through a local
Unix socket; the helper sends requests through the account's selected proxy.
The socket is mode `0600`, target hosts are allowlisted, and redirects are returned
without following them. Helper failures do not silently fall back to HTTPX.

The OAuth profile retains Go's HTTP/2 negotiation. The CloudCode profile uses
HTTP/1.1 without ALPN. The sanitized capture metadata in
`tests/fixtures/agy_transport_2.28.2.json` records the reference client observed
during development. Matching a capture does not establish why an account was
restricted or guarantee compatibility with future client releases.

## Enable

Docker builds compile the helper automatically. Set
`ANTIGRAVITY_NATIVE_TRANSPORT_ENABLED=true` in the Compose `.env` file, then
recreate the service using an image built from this change. The default is false.
Existing OAuth client selection/fallback behavior is retained.

For a source checkout, build from this directory:

```sh
go build -trimpath -o agytransport .
go test -count=1 ./...
```

Start the Python service from the repository root with the same environment
switch. The service starts and stops the helper. The default socket is
`${CREDENTIALS_DIR:-./creds}/agytransport.sock`; use distinct socket paths for
separate service instances. A writable credentials directory is required.

Use `ANTIGRAVITY_NATIVE_TRANSPORT_BINARY` to override the executable path.
Custom CA files and extra allowed hosts are documented in `.env.example`.
Changing the host allowlist requires restarting the helper.
